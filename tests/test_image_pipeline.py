"""FLUX.2 Klein workflow adaptation and smoke-test decisions. No GPU is used."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from local_ai_switchboard.diagnostics.comfy_graph import validate_graph_support
from local_ai_switchboard.diagnostics.flux2_klein import (
    ADAPTED_VALUES,
    DIAGNOSTIC_PROMPT,
    DIFFUSION_MODEL,
    TEXT_ENCODER,
    VAE_MODEL,
    WORKFLOW_ID,
    adapt_upstream_graph,
    build_manifest,
    linked_inputs,
)
from local_ai_switchboard.diagnostics.image_pipeline import (
    check_object_info_directory,
    commit_verification,
    png_dimensions,
    render_report_markdown,
    restore_workflow_snapshot,
    retry_reserve_vram_mb,
    save_workflow_snapshot,
    stage_live_manifest,
    summarize_gpu_csv,
    write_pipeline_report,
)
from local_ai_switchboard.providers.workflows import WorkflowError, apply_bindings, validate_manifest
from local_ai_switchboard.workflows import packaged_workflows_dir

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / "workflows" / "sources" / "flux2-klein-t2i-upstream.json"
META = ROOT / "workflows" / "sources" / "flux2-klein-t2i-upstream.meta.json"
API = ROOT / "workflows" / "flux2-klein-t2i" / "workflow_api.json"
MANIFEST = ROOT / "workflows" / "flux2-klein-t2i" / "manifest.json"


def _catalog(graph: dict[str, object]) -> dict[str, dict[str, object]]:
    classes: dict[str, dict[str, object]] = {}
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        class_type = node.get("class_type")
        inputs = node.get("inputs")
        if not isinstance(class_type, str) or not isinstance(inputs, dict):
            continue
        spec = classes.setdefault(class_type, {"input": {"required": {}}, "output": ["OUTPUT"]})
        required = spec["input"]["required"]
        assert isinstance(required, dict)
        for name, value in inputs.items():
            if isinstance(value, str) and name in {
                "unet_name",
                "clip_name",
                "vae_name",
                "sampler_name",
                "weight_dtype",
                "type",
                "device",
            }:
                current = required.get(name)
                if not isinstance(current, list) or not current or not isinstance(current[0], list):
                    required[name] = [[value]]
                elif value not in current[0]:
                    current[0].append(value)
            elif isinstance(value, str):
                required.setdefault(name, ["STRING", {}])
            elif isinstance(value, float) and not isinstance(value, bool):
                required.setdefault(name, ["FLOAT", {}])
            elif isinstance(value, int) and not isinstance(value, bool):
                required.setdefault(name, ["INT", {}])
            elif isinstance(value, list):
                required.setdefault(name, ["*"])
    return classes


def test_upstream_snapshot_matches_its_provenance() -> None:
    raw = UPSTREAM.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    meta = json.loads(META.read_text(encoding="utf-8"))
    assert meta["sha256"] == digest
    assert meta["download_date"] == "2026-11-10"
    assert meta["source_url"].startswith("https://comfy.org/catalog-media/")
    assert "HTTP 404" in meta["requested_url_result"]
    graph = json.loads(raw)
    assert graph["75:70"]["inputs"]["unet_name"] == "flux-2-klein-base-4b.safetensors"
    assert graph["75:71"]["inputs"]["clip_name"] == "qwen_3_4b.safetensors"
    assert graph["75:72"]["inputs"]["vae_name"] == VAE_MODEL


def test_adaptation_changes_only_the_local_settings() -> None:
    upstream = json.loads(UPSTREAM.read_text(encoding="utf-8"))
    adapted = adapt_upstream_graph(upstream)
    assert linked_inputs(upstream) == linked_inputs(adapted)
    assert adapted["75:61"]["inputs"]["sampler_name"] == "euler"
    assert adapted["75:66"]["inputs"]["batch_size"] == 1
    assert adapted["75:73"]["inputs"]["noise_seed"] == 0
    assert adapted["9"]["class_type"] == "SaveImage"
    assert adapted["75:74"]["inputs"]["text"] == ["76", 0]
    for (node_id, key), value in ADAPTED_VALUES.items():
        assert adapted[node_id]["inputs"][key] == value
        assert upstream[node_id]["inputs"][key] != value
    assert json.loads(API.read_text(encoding="utf-8")) == adapted


def test_manifest_bindings_and_disabled_slot_stay_separate() -> None:
    manifest = validate_manifest(json.loads(MANIFEST.read_text(encoding="utf-8")))
    assert manifest.id == WORKFLOW_ID
    assert manifest.enabled is True
    assert manifest.workflow == json.loads(API.read_text(encoding="utf-8"))
    assert manifest.workflow == build_manifest(manifest.workflow)["workflow"]
    assert set(manifest.bindings) == {"prompt", "width", "height", "seed", "steps"}
    assert manifest.bindings["prompt"].node == "76"
    assert manifest.bindings["prompt"].input == "value"
    assert manifest.bindings["seed"].input == "noise_seed"
    assert manifest.required == ["prompt"]
    assert manifest.defaults == {"width": 768, "height": 768, "steps": 4}
    graph = apply_bindings(
        manifest,
        {"prompt": DIAGNOSTIC_PROMPT, "width": 768, "height": 768, "seed": 42, "steps": 4},
    )
    assert graph["76"]["inputs"]["value"] == DIAGNOSTIC_PROMPT
    assert graph["75:70"]["inputs"]["unet_name"] == DIFFUSION_MODEL
    assert graph["75:71"]["inputs"]["clip_name"] == TEXT_ENCODER
    assert graph["75:72"]["inputs"]["vae_name"] == VAE_MODEL
    assert graph["9"]["inputs"]["images"] == ["75:65", 0]
    with pytest.raises(WorkflowError, match="between 64 and 2048"):
        apply_bindings(manifest, {"prompt": DIAGNOSTIC_PROMPT, "width": 32})
    packaged = json.loads((packaged_workflows_dir() / "flux2-klein-4b.json").read_text(encoding="utf-8"))
    assert packaged["enabled"] is False
    assert packaged["workflow"] is None
    assert "class_type" not in json.dumps(packaged)


def test_object_info_reports_missing_class_model_and_link() -> None:
    graph = json.loads(API.read_text(encoding="utf-8"))
    assert validate_graph_support(graph, _catalog(graph)) == []
    missing_class = _catalog(graph)
    del missing_class["UNETLoader"]
    assert any("UNETLoader" in problem for problem in validate_graph_support(graph, missing_class))
    missing_model = _catalog(graph)
    missing_model["UNETLoader"]["input"]["required"]["unet_name"] = [["other.safetensors"]]
    assert any("not installed" in problem for problem in validate_graph_support(graph, missing_model))
    broken = json.loads(json.dumps(graph))
    del broken["76"]
    assert any("missing node 76" in problem for problem in validate_graph_support(broken, _catalog(graph)))


def test_png_dimensions_and_gpu_summary() -> None:
    payload = (
        b"\x89PNG\r\n\x1a\n"
        + (13).to_bytes(4, "big")
        + b"IHDR"
        + (768).to_bytes(4, "big")
        + (768).to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00\x00\x00\x00\x00"
    )
    assert png_dimensions(payload) == (768, 768)
    with pytest.raises(ValueError, match="not a PNG"):
        png_dimensions(b"not-a-png")
    summary = summarize_gpu_csv(
        "timestamp,memory_used_mib,memory_total_mib,utilization_gpu,temperature_c,power_w\n"
        "t0, 1000, 8188, 1, 40, 20\n"
        "t1, 7000, 8188, 90, 72, 110\n"
        "t2, 2500, 8188, 5, 55, 30\n"
    )
    assert summary["initial_vram_mib"] == 1000
    assert summary["peak_vram_mib"] == 7000
    assert summary["final_vram_mib"] == 2500
    assert summary["peak_temperature_c"] == 72


def test_low_vram_retry_happens_once_and_only_when_configured() -> None:
    oom = "ComfyUI execution failed: CUDA out of memory"
    assert retry_reserve_vram_mb(failures=1, error=oom, reserve_vram_mb=2048) == 2048
    assert retry_reserve_vram_mb(failures=2, error=oom, reserve_vram_mb=2048) is None
    assert retry_reserve_vram_mb(failures=1, error=oom, reserve_vram_mb=None) is None
    assert retry_reserve_vram_mb(failures=1, error="ComfyUI rejected the prompt", reserve_vram_mb=2048) is None


def test_object_info_directory_names_a_missing_class(tmp_path: Path) -> None:
    info = tmp_path / "object-info"
    info.mkdir()
    (info / "SaveImage.json").write_text("{}", encoding="utf-8")
    problems = check_object_info_directory(MANIFEST, info)
    assert any("UNETLoader" in problem for problem in problems)


def test_verification_is_recorded_only_after_success(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="only after every stage"):
        commit_verification(
            passed=False,
            manifest_sha256="a" * 64,
            launch_profile="default",
            report_dir="report",
        )
    document = commit_verification(
        passed=True,
        manifest_sha256="AB" * 32,
        launch_profile="default",
        report_dir=str(tmp_path),
    )
    assert document["status"] == "passed"
    assert document["workflow_id"] == WORKFLOW_ID
    assert document["vram_fit_claimed"] is False
    assert document["manifest_sha256"] == "ab" * 32


def test_failed_verification_restores_the_previous_registry(tmp_path: Path) -> None:
    live = tmp_path / "workflows"
    live.mkdir()
    original = '{"id":"flux2-klein-4b","enabled":false}\n'
    (live / "flux2-klein-4b.json").write_text(original, encoding="utf-8")
    backup = tmp_path / "backup"
    save_workflow_snapshot(live, backup)
    stage_live_manifest(live, MANIFEST)
    assert (live / "flux2-klein-t2i.json").is_file()
    (live / "flux2-klein-4b.json").write_text("changed\n", encoding="utf-8")
    restore_workflow_snapshot(live, backup)
    assert not (live / "flux2-klein-t2i.json").exists()
    assert (live / "flux2-klein-4b.json").read_text(encoding="utf-8") == original
    with pytest.raises(WorkflowError):
        stage_live_manifest(live, packaged_workflows_dir() / "flux2-klein-4b.json")


def test_report_states_the_result_without_claiming_a_vram_fit(tmp_path: Path) -> None:
    report = {
        "models_verified": True,
        "workflow_validated": True,
        "image_generated": True,
        "dimensions_verified": True,
        "peak_vram_mib": 7000,
        "gpu_temperature_c": 72,
        "generation_seconds": 12,
        "transition": "passed",
        "ollama_response": "ready",
        "workflow_enabled": True,
        "launch_profile": "default",
        "vram_fit_claimed": False,
        "failure_stage": "",
    }
    text = render_report_markdown(report)
    assert "Models verified: yes" in text
    assert "Workflow validated: yes" in text
    assert "ComfyUI generated image: yes" in text
    assert "Image dimensions verified: yes" in text
    assert "Peak VRAM: 7000 MiB" in text
    assert "GPU temperature: 72 C" in text
    assert "Image generation duration: 12 seconds" in text
    assert "ComfyUI to Ollama transition: passed" in text
    assert "Ollama response after transition: ready" in text
    assert "Workflow enabled: enabled" in text
    assert "does not claim that the model fits fully in VRAM" in text
    with pytest.raises(ValueError, match="must not claim"):
        render_report_markdown({**report, "vram_fit_claimed": True})
    disabled = {**report, "workflow_enabled": False, "failure_stage": "E", "failure_detail": "CUDA out of memory"}
    failed = render_report_markdown(disabled)
    assert "Workflow enabled: left disabled" in failed
    assert "Failure stage: E" in failed
    write_pipeline_report(tmp_path / "latest", disabled, csv_text="timestamp\n", error_log="CUDA out of memory")
    assert (tmp_path / "latest" / "report.json").is_file()
    assert (tmp_path / "latest" / "report.md").is_file()
    assert (tmp_path / "latest" / "gpu-metrics.csv").is_file()
    assert "CUDA out of memory" in (tmp_path / "latest" / "errors.log").read_text(encoding="utf-8")
