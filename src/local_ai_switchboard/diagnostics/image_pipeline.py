"""File and report helpers for the image smoke test.

These functions do not start a gateway, a scheduler, or ComfyUI.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from local_ai_switchboard.diagnostics.comfy_graph import validate_graph_support
from local_ai_switchboard.diagnostics.flux2_klein import WORKFLOW_ID
from local_ai_switchboard.providers.workflows import WorkflowError, load_manifest

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
GPU_CSV_HEADER = (
    "timestamp,memory_used_mib,memory_total_mib,utilization_gpu,temperature_c,power_w"
)


def png_dimensions(payload: bytes) -> tuple[int, int]:
    """Read width and height from a PNG IHDR. No image library is required."""

    if len(payload) < 24 or not payload.startswith(_PNG_SIGNATURE):
        raise ValueError("output is not a PNG")
    if payload[12:16] != b"IHDR":
        raise ValueError("PNG is missing an IHDR chunk")
    width = int.from_bytes(payload[16:20], "big")
    height = int.from_bytes(payload[20:24], "big")
    if width <= 0 or height <= 0:
        raise ValueError("PNG dimensions are invalid")
    return width, height


def is_out_of_memory(error: str) -> bool:
    """True when a job error reports a CUDA allocation failure."""

    text = error.lower()
    return "out of memory" in text or "outofmemory" in text or "cuda oom" in text


def retry_reserve_vram_mb(*, failures: int, error: str, reserve_vram_mb: int | None) -> int | None:
    """Return the one allowed reserve-vram retry, or None.

    failures counts image attempts that have already failed. A second failure
    does not retry. A missing reserve value does not invent one.
    """

    if failures != 1 or reserve_vram_mb is None or reserve_vram_mb < 0:
        return None
    if not is_out_of_memory(error):
        return None
    return reserve_vram_mb


def launch_profile_name(reserve_vram_mb: int | None) -> str:
    """Name the ComfyUI launch profile that produced a result."""

    if reserve_vram_mb is None:
        return "default"
    return f"reserve-vram:{reserve_vram_mb}"


def save_workflow_snapshot(directory: Path, backup: Path) -> None:
    """Copy the live top-level workflow files. Subdirectories stay untouched."""

    backup.mkdir(parents=True, exist_ok=True)
    for child in backup.iterdir():
        if child.is_file():
            child.unlink()
    names: list[str] = []
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            if path.is_file():
                (backup / path.name).write_bytes(path.read_bytes())
                names.append(path.name)
    (backup / "snapshot.json").write_text(
        json.dumps({"files": names}, indent=2) + "\n",
        encoding="utf-8",
    )


def restore_workflow_snapshot(directory: Path, backup: Path) -> None:
    """Put the live workflow directory back to the snapshot."""

    payload = json.loads((backup / "snapshot.json").read_text(encoding="utf-8"))
    names = payload.get("files")
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError("workflow snapshot is invalid")
    directory.mkdir(parents=True, exist_ok=True)
    allowed = set(names)
    for path in directory.glob("*.json"):
        if path.is_file() and path.name not in allowed:
            path.unlink()
    for name in names:
        (directory / name).write_bytes((backup / name).read_bytes())


def check_object_info_directory(manifest_path: Path, directory: Path) -> list[str]:
    """Validate a manifest against object_info JSON files saved from ComfyUI."""

    manifest = load_manifest(manifest_path)
    if manifest.workflow is None:
        return ["workflow graph is missing"]
    merged: dict[str, Any] = {}
    if directory.is_dir():
        for path in sorted(directory.glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                return [f"{path.name} is not valid object info"]
            if isinstance(payload, dict):
                for key, value in payload.items():
                    if isinstance(value, dict):
                        merged[str(key)] = value
    return validate_graph_support(manifest.workflow, merged)


def stage_live_manifest(directory: Path, manifest_path: Path) -> Path:
    """Validate the candidate and copy it into the live registry directory."""

    manifest = load_manifest(manifest_path)
    if manifest.id != WORKFLOW_ID or not manifest.enabled or manifest.workflow is None:
        raise WorkflowError("the live registry only receives the enabled flux2-klein-t2i manifest")
    directory.mkdir(parents=True, exist_ok=True)
    destination = directory / f"{manifest.id}.json"
    destination.write_bytes(manifest_path.read_bytes())
    loaded = load_manifest(destination)
    if loaded.id != WORKFLOW_ID or not loaded.enabled:
        raise WorkflowError("the staged manifest failed validate_manifest")
    return destination


def commit_verification(
    *,
    passed: bool,
    manifest_sha256: str,
    launch_profile: str,
    report_dir: str,
) -> dict[str, object]:
    """Build the pass record. A failed run cannot produce one."""

    if not passed:
        raise ValueError("verification is recorded only after every stage passes")
    digest = manifest_sha256.strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("verification record needs the manifest SHA256")
    if not launch_profile.strip() or not report_dir.strip():
        raise ValueError("verification record is incomplete")
    return {
        "workflow_id": WORKFLOW_ID,
        "status": "passed",
        "manifest_sha256": digest,
        "launch_profile": launch_profile,
        "report_dir": report_dir,
        "vram_fit_claimed": False,
    }


def summarize_gpu_csv(text: str) -> dict[str, object]:
    """Reduce nvidia-smi samples. An empty file reports zero samples."""

    used: list[int] = []
    temperatures: list[float] = []
    for line in text.splitlines()[1:]:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            used.append(int(float(parts[1])))
        except ValueError:
            continue
        try:
            temperatures.append(float(parts[4]))
        except ValueError:
            continue
    if not used:
        return {"samples": 0, "initial_vram_mib": None, "peak_vram_mib": None, "final_vram_mib": None, "peak_temperature_c": None}
    return {
        "samples": len(used),
        "initial_vram_mib": used[0],
        "peak_vram_mib": max(used),
        "final_vram_mib": used[-1],
        "peak_temperature_c": max(temperatures) if temperatures else None,
    }


def render_report_markdown(report: dict[str, Any]) -> str:
    """Render the operator summary. A VRAM-fit claim is rejected."""

    if report.get("vram_fit_claimed") is not False:
        raise ValueError("the report must not claim that the model fits fully in VRAM")

    def yes_no(value: object) -> str:
        return "yes" if value is True else "no"

    peak = report.get("peak_vram_mib")
    temperature = report.get("gpu_temperature_c")
    duration = report.get("generation_seconds")
    lines = [
        "# Image pipeline verification",
        "",
        f"- Models verified: {yes_no(report.get('models_verified'))}",
        f"- Workflow validated: {yes_no(report.get('workflow_validated'))}",
        f"- ComfyUI generated image: {yes_no(report.get('image_generated'))}",
        f"- Image dimensions verified: {yes_no(report.get('dimensions_verified'))}",
        f"- Peak VRAM: {peak if peak is not None else 'not recorded'} MiB",
        f"- GPU temperature: {temperature if temperature is not None else 'not recorded'} C",
        f"- Image generation duration: {duration if duration is not None else 'not recorded'} seconds",
        f"- ComfyUI to Ollama transition: {report.get('transition') or 'not run'}",
        f"- Ollama response after transition: {report.get('ollama_response') or ''}",
        f"- Workflow enabled: {'enabled' if report.get('workflow_enabled') is True else 'left disabled'}",
        "",
        "Peak VRAM is a measurement. This report does not claim that the model fits fully in VRAM.",
        "",
        f"Launch profile: {report.get('launch_profile') or 'default'}",
    ]
    stage = report.get("failure_stage") or ""
    if stage:
        lines.extend(["", f"Failure stage: {stage}"])
        detail = report.get("failure_detail") or ""
        if detail:
            lines.extend(["", str(detail)])
    return "\n".join(lines) + "\n"


def write_pipeline_report(
    directory: Path,
    report: dict[str, Any],
    *,
    csv_text: str,
    error_log: str = "",
    image: bytes | None = None,
) -> None:
    """Write the report files. image.png is written only when bytes are supplied."""

    directory.mkdir(parents=True, exist_ok=True)
    (directory / "report.json").write_text(
        json.dumps(report, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (directory / "report.md").write_text(render_report_markdown(report), encoding="utf-8")
    (directory / "gpu-metrics.csv").write_text(csv_text, encoding="utf-8")
    if error_log:
        (directory / "errors.log").write_text(error_log, encoding="utf-8")
    if image is not None:
        (directory / "image.png").write_bytes(image)
