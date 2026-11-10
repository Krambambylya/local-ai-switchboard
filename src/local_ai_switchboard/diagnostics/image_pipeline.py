"""File and report helpers for the image smoke test.

These functions do not start a gateway, a scheduler, or ComfyUI.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from local_ai_switchboard.diagnostics.comfy_graph import validate_graph_support
from local_ai_switchboard.diagnostics.flux2_klein import (
    DIFFUSION_MODEL,
    TEXT_ENCODER,
    VAE_MODEL,
    WORKFLOW_ID,
)
from local_ai_switchboard.providers.workflows import WorkflowError, load_manifest
from local_ai_switchboard.scheduler.model import SchedulerState

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
GPU_CSV_HEADER = (
    "timestamp,memory_used_mib,memory_total_mib,utilization_gpu,temperature_c,power_w"
)
_COMMIT_FLAGS = (
    "models_verified",
    "workflow_validated",
    "text_before_image",
    "scheduler_idle_before_image",
    "comfyui_checked",
    "image_generated",
    "dimensions_verified",
    "image_scheduler_succeeded",
    "scheduler_idle_after_image",
    "text_after_image",
    "scheduler_idle_after_text",
    "resources_released",
)
_PARTIAL_BLOCKS = {"active_jobs", "scheduler_unreadable"}
_SCHEDULER_STATES = {state.value for state in SchedulerState}
_REJECTED_TEXT_ENCODER = "qwen_3_4b.safetensors"
_DETAIL_STRINGS = (
    "comfyui_version",
    "comfyui_version_source",
    "gpu",
    "driver",
)
_DETAIL_INTS = ("vram_mib", "width", "height", "seed", "steps", "batch_size")


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


def commit_ready(report: dict[str, Any]) -> bool:
    """True only when every generation stage passed and nothing failed."""

    if report.get("vram_fit_claimed") is not False:
        return False
    if str(report.get("failure_stage") or "").strip():
        return False
    if str(report.get("blocked_reason") or "").strip():
        return False
    if report.get("transition") != "passed":
        return False
    return all(report.get(name) is True for name in _COMMIT_FLAGS)


def pipeline_verdict(report: dict[str, Any]) -> str:
    """PASS, FAIL, or PARTIAL. A VRAM-fit claim cannot pass."""

    if report.get("vram_fit_claimed") is not False:
        return "FAIL"
    blocked = str(report.get("blocked_reason") or "").strip()
    if blocked in _PARTIAL_BLOCKS:
        return "PARTIAL"
    if (
        commit_ready(report)
        and report.get("workflow_enabled") is True
        and report.get("doctor_verified") is True
    ):
        return "PASS"
    if str(report.get("failure_stage") or "").strip() or blocked:
        return "FAIL"
    if any(report.get(name) is True for name in _COMMIT_FLAGS):
        return "PARTIAL"
    return "FAIL"


def image_failure_stage(error: str) -> str:
    """Map a generation error to stage C or D. This does not choose a retry."""

    text = error.lower()
    if (
        "cannot run this workflow" in text
        or "comfyui is not reachable" in text
        or "did not become healthy" in text
        or "exited before it became healthy" in text
        or "comfyui portable installation was not found" in text
    ):
        return "C"
    return "D"


def klein_configuration_problems(graph: dict[str, Any]) -> list[str]:
    """Return mismatches against the adapted FLUX.2 Klein graph. An empty list matches."""

    problems: list[str] = []
    if not isinstance(graph, dict) or not graph:
        return ["workflow graph is empty"]

    def _input(node_id: str, key: str) -> Any:
        node = graph.get(node_id)
        if not isinstance(node, dict):
            problems.append(f"node {node_id} is missing")
            return None
        inputs = node.get("inputs")
        if not isinstance(inputs, dict) or key not in inputs:
            problems.append(f"node {node_id} has no input {key}")
            return None
        return inputs[key]

    unet = _input("75:70", "unet_name")
    clip = _input("75:71", "clip_name")
    vae = _input("75:72", "vae_name")
    sampler = _input("75:61", "sampler_name")
    steps = _input("75:62", "steps")
    cfg = _input("75:63", "cfg")
    batch = _input("75:66", "batch_size")
    if unet != DIFFUSION_MODEL:
        problems.append(f"diffusion model is {unet!r}, expected {DIFFUSION_MODEL}")
    if clip == _REJECTED_TEXT_ENCODER:
        problems.append(
            "text encoder is qwen_3_4b.safetensors. The FP4 file is qwen_3_4b_fp4_flux2.safetensors"
        )
    elif clip != TEXT_ENCODER:
        problems.append(f"text encoder is {clip!r}, expected {TEXT_ENCODER}")
    if vae != VAE_MODEL:
        problems.append(f"VAE is {vae!r}, expected {VAE_MODEL}")
    if sampler != "euler":
        problems.append(f"sampler is {sampler!r}, expected euler")
    if steps != 4:
        problems.append(f"steps are {steps!r}, expected 4")
    if cfg != 1 and cfg != 1.0:
        problems.append(f"cfg is {cfg!r}, expected 1.0")
    if batch != 1:
        problems.append(f"batch size is {batch!r}, expected 1")
    save = graph.get("9")
    if not isinstance(save, dict) or save.get("class_type") != "SaveImage":
        problems.append("SaveImage node 9 is missing")
    return problems


def manifest_configuration_problems(path: Path) -> list[str]:
    """Validate a candidate manifest id, enabled state, and adapted Klein settings."""

    try:
        manifest = load_manifest(path)
    except (OSError, WorkflowError, json.JSONDecodeError, ValueError) as exc:
        return [str(exc)]
    problems: list[str] = []
    if manifest.id != WORKFLOW_ID:
        problems.append(f"workflow id is {manifest.id}, expected {WORKFLOW_ID}")
    if not manifest.enabled or manifest.workflow is None:
        problems.append("candidate manifest is not an enabled graph")
        return problems
    problems.extend(klein_configuration_problems(manifest.workflow))
    return problems


def open_image_job_count(path: Path) -> int:
    """Count queued and running image jobs. A missing database has none.

    A database that cannot be read raises ValueError. The caller must not
    stop the gateway in that case.
    """

    if not path.is_file():
        return 0
    header = path.read_bytes()[:16]
    if not header.startswith(b"SQLite format 3"):
        raise ValueError("switchboard.db is not a SQLite database")
    try:
        connection = sqlite3.connect(path, timeout=5.0)
    except sqlite3.Error as exc:
        raise ValueError(f"switchboard.db could not be read: {exc}") from exc
    try:
        connection.execute("PRAGMA query_only = ON")
        try:
            row = connection.execute(
                "SELECT COUNT(*) FROM image_jobs WHERE status IN ('queued', 'running')"
            ).fetchone()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "no such table" in message:
                return 0
            raise ValueError(f"switchboard.db could not be read: {exc}") from exc
        if row is None:
            return 0
        return int(row[0])
    finally:
        connection.close()


def inspect_image_jobs(path: Path) -> dict[str, object]:
    """Return a JSON-ready view of open image jobs."""

    try:
        count = open_image_job_count(path)
    except ValueError as exc:
        return {"ok": False, "count": None, "detail": str(exc)}
    return {"ok": True, "count": count, "detail": ""}


def latest_scheduler_state(text: str) -> str | None:
    """Return the last scheduler state recorded in a gateway log excerpt."""

    last: str | None = None
    marker = "scheduler state "
    for line in text.splitlines():
        message = line
        stripped = line.strip()
        if stripped.startswith("{"):
            try:
                payload = json.loads(stripped)
            except json.JSONDecodeError:
                payload = None
            if isinstance(payload, dict) and isinstance(payload.get("message"), str):
                message = payload["message"]
        index = message.rfind(marker)
        if index == -1:
            continue
        token = message[index + len(marker) :].strip().split(" ", 1)[0].strip(".,")
        if token in _SCHEDULER_STATES:
            last = token
    return last


def describe_offload(stats: dict[str, Any] | None, log_text: str = "") -> dict[str, object]:
    """Record ComfyUI memory counters and offload lines. Missing data stays missing."""

    result: dict[str, object] = {
        "observed": False,
        "detail": "ComfyUI did not report an offload state.",
        "claimed_fully_resident": False,
    }
    if isinstance(stats, dict):
        devices = stats.get("devices")
        if isinstance(devices, list) and devices and isinstance(devices[0], dict):
            device = devices[0]
            kept: dict[str, object] = {}
            for key in ("name", "type", "vram_total", "vram_free", "torch_vram_total", "torch_vram_free"):
                value = device.get(key)
                if key in device and not isinstance(value, bool):
                    kept[key] = value
            if kept:
                result["device"] = kept
                result["detail"] = (
                    "ComfyUI reported device memory counters. "
                    "They do not show whether every weight is resident."
                )
            total = device.get("vram_total")
            torch_total = device.get("torch_vram_total")
            if (
                isinstance(total, (int, float))
                and not isinstance(total, bool)
                and isinstance(torch_total, (int, float))
                and not isinstance(torch_total, bool)
                and torch_total < total
            ):
                result["observed"] = True
                result["detail"] = (
                    "torch_vram_total is below vram_total. "
                    "ComfyUI is not holding the full device memory in Torch."
                )
        version = _comfyui_version(stats)
        if version:
            result["comfyui_version"] = version
    lines: list[str] = []
    for line in log_text.splitlines():
        lower = line.lower()
        if "offload" in lower or "lowvram" in lower or "reserve-vram" in lower:
            lines.append(line.strip()[:400])
    if lines:
        result["observed"] = True
        result["log_lines"] = lines[:20]
        if str(result["detail"]).startswith("ComfyUI did not"):
            result["detail"] = "The gateway or ComfyUI log mentioned offload or reserve VRAM."
    return result


def release_observation(
    queue: dict[str, Any] | None,
    stats: dict[str, Any] | None,
    log_text: str = "",
    *,
    threshold_mb: int = 1024,
) -> dict[str, object]:
    """Describe whether managed ComfyUI resources look released. Unknown stays unknown."""

    queue_idle: bool | None = None
    if isinstance(queue, dict):
        running = queue.get("queue_running")
        pending = queue.get("queue_pending")
        if isinstance(running, list) and isinstance(pending, list):
            queue_idle = not running and not pending
    memory_free: bool | None = None
    if isinstance(stats, dict):
        devices = stats.get("devices")
        if isinstance(devices, list) and devices and isinstance(devices[0], dict):
            value = devices[0].get("vram_free")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                memory_free = None
            else:
                memory_free = value >= threshold_mb * 1024 * 1024
    log_released = (
        "provider image released GPU resources" in log_text or "comfyui free requested" in log_text
    )
    confirmed = bool(log_released or (queue_idle is True and memory_free is True))
    return {
        "queue_idle": queue_idle,
        "memory_free": memory_free,
        "log_released": log_released,
        "confirmed": confirmed,
    }


def comfyui_log_text(gateway_text: str, extra_text: str = "") -> dict[str, object]:
    """Build the ComfyUI log excerpt. A missing source is stated and not invented."""

    extra = extra_text.strip()
    if extra:
        return {"collected": True, "text": extra_text if extra_text.endswith("\n") else extra_text + "\n"}
    lines = [line for line in gateway_text.splitlines() if "comfy" in line.lower()]
    if lines:
        return {"collected": True, "text": "\n".join(lines) + "\n"}
    return {
        "collected": False,
        "text": (
            "ComfyUI log was not available as a separate file.\n"
            "The portable launcher does not redirect ComfyUI stdout to its own log.\n"
            "No gateway log line from this run mentioned ComfyUI.\n"
        ),
    }


def _comfyui_version(stats: dict[str, Any]) -> str:
    system = stats.get("system")
    if not isinstance(system, dict):
        return ""
    for key in ("comfyui_version", "version"):
        value = system.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _clean_details(details: dict[str, Any]) -> dict[str, object]:
    cleaned: dict[str, object] = {}
    for key in _DETAIL_STRINGS:
        if key not in details or details[key] is None:
            continue
        value = details[key]
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"verification detail {key} is invalid")
        cleaned[key] = value.strip()
    for key in _DETAIL_INTS:
        if key not in details or details[key] is None:
            continue
        value = details[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"verification detail {key} is invalid")
        cleaned[key] = value
    if "cfg" in details and details["cfg"] is not None:
        cfg = details["cfg"]
        if isinstance(cfg, bool) or not isinstance(cfg, (int, float)) or float(cfg) != 1.0:
            raise ValueError("verification detail cfg is invalid")
        cleaned["cfg"] = 1.0
    if "sampler" in details and details["sampler"] is not None:
        if details["sampler"] != "euler":
            raise ValueError("verification detail sampler is invalid")
        cleaned["sampler"] = "euler"
    models = details.get("models")
    if models is not None:
        if not isinstance(models, list) or not models:
            raise ValueError("verification detail models is invalid")
        cleaned_models: list[dict[str, str]] = []
        for item in models:
            if not isinstance(item, dict):
                raise ValueError("verification detail models is invalid")
            filename = item.get("filename")
            digest = item.get("sha256")
            folder = item.get("folder")
            if not isinstance(filename, str) or not filename.strip():
                raise ValueError("verification detail models is invalid")
            if not isinstance(digest, str):
                raise ValueError("verification detail models is invalid")
            digest = digest.strip().lower()
            if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
                raise ValueError("verification detail models is invalid")
            record = {"filename": filename.strip(), "sha256": digest}
            if folder is not None:
                if not isinstance(folder, str) or not folder.strip():
                    raise ValueError("verification detail models is invalid")
                record["folder"] = folder.strip()
            revision = item.get("revision")
            if isinstance(revision, str) and revision.strip():
                record["revision"] = revision.strip()
            cleaned_models.append(record)
        cleaned["models"] = cleaned_models
    parameters = details.get("workflow_parameters")
    if parameters is not None:
        if not isinstance(parameters, dict):
            raise ValueError("verification detail workflow_parameters is invalid")
        cleaned["workflow_parameters"] = parameters
    return cleaned


def commit_verification(
    *,
    passed: bool,
    manifest_sha256: str,
    launch_profile: str,
    report_dir: str,
    details: dict[str, Any] | None = None,
) -> dict[str, object]:
    """Build the pass record. A failed run cannot produce one."""

    if not passed:
        raise ValueError("verification is recorded only after every stage passes")
    digest = manifest_sha256.strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("verification record needs the manifest SHA256")
    if not launch_profile.strip() or not report_dir.strip():
        raise ValueError("verification record is incomplete")
    document: dict[str, object] = {
        "workflow_id": WORKFLOW_ID,
        "status": "passed",
        "manifest_sha256": digest,
        "launch_profile": launch_profile,
        "report_dir": report_dir,
        "vram_fit_claimed": False,
        "verified_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    if details:
        document.update(_clean_details(details))
    return document


def summarize_gpu_csv(text: str) -> dict[str, object]:
    """Reduce nvidia-smi samples. An empty or unreadable file reports zero samples."""

    used: list[int] = []
    totals: list[int] = []
    utilizations: list[int] = []
    temperatures: list[float] = []
    powers: list[float] = []
    timestamps: list[str] = []
    for line in text.splitlines()[1:]:
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 6:
            continue
        try:
            memory_used = int(float(parts[1]))
        except ValueError:
            continue
        used.append(memory_used)
        timestamps.append(parts[0])
        try:
            totals.append(int(float(parts[2])))
        except ValueError:
            pass
        try:
            utilizations.append(int(float(parts[3])))
        except ValueError:
            pass
        try:
            temperatures.append(float(parts[4]))
        except ValueError:
            pass
        try:
            powers.append(float(parts[5]))
        except ValueError:
            pass
    if not used:
        return {
            "samples": 0,
            "initial_vram_mib": None,
            "peak_vram_mib": None,
            "final_vram_mib": None,
            "memory_total_mib": None,
            "peak_temperature_c": None,
            "peak_utilization_gpu": None,
            "peak_power_w": None,
            "started_at": None,
            "finished_at": None,
        }
    return {
        "samples": len(used),
        "initial_vram_mib": used[0],
        "peak_vram_mib": max(used),
        "final_vram_mib": used[-1],
        "memory_total_mib": totals[0] if totals else None,
        "peak_temperature_c": max(temperatures) if temperatures else None,
        "peak_utilization_gpu": max(utilizations) if utilizations else None,
        "peak_power_w": max(powers) if powers else None,
        "started_at": timestamps[0],
        "finished_at": timestamps[-1],
    }


def recommendation_for(report: dict[str, Any]) -> str:
    """One next step for the operator. A pass has no repair step."""

    blocked = str(report.get("blocked_reason") or "").strip()
    if blocked == "active_jobs":
        return (
            "Wait until the current GPU scheduler jobs finish, then run this check again. "
            "No process was stopped."
        )
    if blocked == "scheduler_unreadable":
        return (
            "The scheduler state could not be read. No process was stopped. "
            "Inspect logs\\switchboard.log and data\\switchboard.db, then run this check again."
        )
    stage = str(report.get("failure_stage") or "").strip()
    messages = {
        "A": "Fix the preflight error above. This check does not download model files.",
        "B": (
            "Ollama and the gateway text request must succeed before an image job starts. "
            "The text request goes through the gateway, not directly to Ollama."
        ),
        "C": (
            "ComfyUI must serve the adapted graph. The pinned ComfyUI version was not changed "
            "and a second ComfyUI process was not started."
        ),
        "D": (
            "The image job did not produce a valid PNG. A CUDA out-of-memory error is recorded "
            "with the GPU samples and is not retried unless -LowVramReserveMb was set, and then only once."
        ),
        "E": "GPU samples are incomplete. nvidia-smi could not supply every counter, and missing counters were not invented.",
        "F": "The image job finished, but the following gateway chat did not return the scheduler to Ollama and IDLE.",
        "record": (
            "The run finished, but the workflow was not left in the verified registry. "
            "The previous image-pipeline.json was kept."
        ),
    }
    if stage in messages:
        return messages[stage]
    if pipeline_verdict(report) == "PASS":
        return "No further action is required on this PC before a Mac client calls the LAN Gateway."
    return "See the failure detail. The verified workflow record was not replaced."


def _conclusion(result: str) -> str:
    mac = (
        " This Windows check does not show that the Gateway is reachable from the Mac. "
        "After a PASS, send a request from the Mac to the Windows LAN address."
    )
    if result == "PASS":
        return (
            "The local FLUX.2 Klein pipeline is ready to use from a Mac through the LAN Gateway."
            + mac
        )
    if result == "PARTIAL":
        return (
            "The pipeline is not confirmed. Do not use it from a Mac until a later run returns PASS."
            + mac
        )
    return "The pipeline is not ready to use from a Mac through the LAN Gateway." + mac


def _file_label(files: dict[str, Any], name: str) -> str:
    value = files.get(name)
    if value is True:
        return "written"
    if value == "note":
        return "not collected; the report file explains why"
    return "not available"


def render_report_markdown(report: dict[str, Any]) -> str:
    """Render the operator summary. A VRAM-fit claim is rejected."""

    if report.get("vram_fit_claimed") is not False:
        raise ValueError("the report must not claim that the model fits fully in VRAM")

    def yes_no(value: object) -> str:
        return "yes" if value is True else "no"

    result = pipeline_verdict(report)
    peak = report.get("peak_vram_mib")
    temperature = report.get("gpu_temperature_c")
    duration = report.get("generation_seconds")
    utilization = report.get("peak_utilization_gpu")
    power = report.get("peak_power_w")
    started = report.get("generation_started_at") or report.get("gpu_started_at")
    finished = report.get("generation_finished_at") or report.get("gpu_finished_at")
    files = report.get("files") if isinstance(report.get("files"), dict) else {}
    lines = [
        result,
        "",
        "# Image pipeline verification",
        "",
        f"Result: {result}",
        "",
        f"- Models verified: {yes_no(report.get('models_verified'))}",
        f"- Workflow validated: {yes_no(report.get('workflow_validated'))}",
        f"- Text before image: {yes_no(report.get('text_before_image'))}",
        f"- ComfyUI checked: {yes_no(report.get('comfyui_checked'))}",
        f"- ComfyUI generated image: {yes_no(report.get('image_generated'))}",
        f"- Image dimensions verified: {yes_no(report.get('dimensions_verified'))}",
        f"- Peak VRAM: {peak if peak is not None else 'not recorded'} MiB",
        f"- GPU utilization peak: {utilization if utilization is not None else 'not recorded'} percent",
        f"- GPU power peak: {power if power is not None else 'not recorded'} W",
        f"- GPU temperature: {temperature if temperature is not None else 'not recorded'} C",
        f"- Image generation started: {started or 'not recorded'}",
        f"- Image generation finished: {finished or 'not recorded'}",
        f"- Image generation duration: {duration if duration is not None else 'not recorded'} seconds",
        f"- ComfyUI to Ollama transition: {report.get('transition') or 'not run'}",
        f"- Ollama response before image: {report.get('ollama_response_before') or ''}",
        f"- Ollama response after transition: {report.get('ollama_response') or ''}",
        f"- Workflow enabled: {'enabled' if report.get('workflow_enabled') is True else 'left disabled'}",
        "",
        "Peak VRAM is a measurement. This report does not claim that the model fits fully in VRAM.",
        "",
        f"Launch profile: {report.get('launch_profile') or 'default'}",
        "",
        "Files:",
        f"- report.md: {_file_label(files, 'report_md')}",
        f"- report.json: {_file_label(files, 'report_json')}",
        f"- gpu-metrics.csv: {_file_label(files, 'gpu_metrics_csv')}",
        f"- gateway.log: {_file_label(files, 'gateway_log')}",
        f"- comfyui.log: {_file_label(files, 'comfyui_log')}",
        f"- generated-image.png: {_file_label(files, 'generated_image')}",
        f"- environment.json: {_file_label(files, 'environment_json')}",
    ]
    offload = report.get("offload")
    if isinstance(offload, dict) and offload.get("detail"):
        lines.extend(["", f"Offload: {offload['detail']}"])
    stage = report.get("failure_stage") or ""
    if stage:
        lines.extend(["", f"Failure stage: {stage}"])
        detail = report.get("failure_detail") or ""
        if detail:
            lines.extend(["", str(detail)])
    recommendation = report.get("recommendation") or recommendation_for(report)
    lines.extend(["", "Recommendation:", "", recommendation, "", "Conclusion:", "", _conclusion(result)])
    return "\n".join(lines) + "\n"


def write_pipeline_report(
    directory: Path,
    report: dict[str, Any],
    *,
    csv_text: str,
    error_log: str = "",
    image: bytes | None = None,
) -> str:
    """Write the report files and return PASS, FAIL, or PARTIAL."""

    if report.get("vram_fit_claimed") is not False:
        raise ValueError("the report must not claim that the model fits fully in VRAM")
    result = pipeline_verdict(report)
    stored = dict(report)
    stored["result"] = result
    if not stored.get("recommendation"):
        stored["recommendation"] = recommendation_for(stored)
    stored["mac_lan_checked"] = False
    directory.mkdir(parents=True, exist_ok=True)
    environment = stored.get("environment")
    if not isinstance(environment, dict):
        environment = {
            "available": False,
            "detail": "environment was not collected",
        }
        stored["environment"] = environment
    (directory / "environment.json").write_text(
        json.dumps(environment, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (directory / "report.json").write_text(
        json.dumps(stored, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (directory / "report.md").write_text(render_report_markdown(stored), encoding="utf-8")
    (directory / "gpu-metrics.csv").write_text(csv_text, encoding="utf-8")
    if error_log:
        (directory / "errors.log").write_text(error_log, encoding="utf-8")
    if image is not None:
        (directory / "generated-image.png").write_bytes(image)
    return result
