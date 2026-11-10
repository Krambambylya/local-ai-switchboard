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
    comfyui_log_text,
    commit_ready,
    commit_verification,
    describe_offload,
    image_failure_stage,
    inspect_image_jobs,
    klein_configuration_problems,
    latest_scheduler_state,
    manifest_configuration_problems,
    open_image_job_count,
    pipeline_verdict,
    png_dimensions,
    release_observation,
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


def _passed_report() -> dict[str, object]:
    return {
        "models_verified": True,
        "workflow_validated": True,
        "text_before_image": True,
        "scheduler_idle_before_image": True,
        "comfyui_checked": True,
        "image_generated": True,
        "dimensions_verified": True,
        "image_scheduler_succeeded": True,
        "scheduler_idle_after_image": True,
        "text_after_image": True,
        "scheduler_idle_after_text": True,
        "resources_released": True,
        "doctor_verified": True,
        "workflow_enabled": True,
        "peak_vram_mib": 7000,
        "gpu_temperature_c": 72,
        "generation_seconds": 12,
        "transition": "passed",
        "ollama_response": "ready",
        "ollama_response_before": "ready",
        "launch_profile": "default",
        "vram_fit_claimed": False,
        "failure_stage": "",
        "blocked_reason": "",
        "files": {
            "report_md": True,
            "report_json": True,
            "gpu_metrics_csv": True,
            "gateway_log": True,
            "comfyui_log": "note",
            "generated_image": True,
            "environment_json": True,
        },
    }


def test_report_states_the_result_without_claiming_a_vram_fit(tmp_path: Path) -> None:
    report = _passed_report()
    text = render_report_markdown(report)
    assert text.startswith("PASS\n")
    assert "ready to use from a Mac through the LAN Gateway" in text
    assert "does not show that the Gateway is reachable from the Mac" in text
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
    assert failed.startswith("FAIL\n")
    assert "Workflow enabled: left disabled" in failed
    assert "Failure stage: E" in failed
    assert "not ready to use from a Mac" in failed
    write_pipeline_report(tmp_path / "latest", disabled, csv_text="timestamp\n", error_log="CUDA out of memory")
    assert (tmp_path / "latest" / "report.json").is_file()
    assert (tmp_path / "latest" / "report.md").read_text(encoding="utf-8").startswith("FAIL\n")
    assert (tmp_path / "latest" / "gpu-metrics.csv").is_file()
    assert (tmp_path / "latest" / "environment.json").is_file()
    assert "CUDA out of memory" in (tmp_path / "latest" / "errors.log").read_text(encoding="utf-8")
    assert not (tmp_path / "latest" / "generated-image.png").exists()


def test_partial_and_failed_runs_do_not_commit(tmp_path: Path) -> None:
    active = {
        "models_verified": True,
        "workflow_validated": True,
        "blocked_reason": "active_jobs",
        "failure_stage": "A",
        "vram_fit_claimed": False,
    }
    assert pipeline_verdict(active) == "PARTIAL"
    assert commit_ready(active) is False
    assert render_report_markdown(active).startswith("PARTIAL\n")
    unread = {
        "blocked_reason": "scheduler_unreadable",
        "failure_stage": "A",
        "vram_fit_claimed": False,
    }
    assert pipeline_verdict(unread) == "PARTIAL"
    incomplete = {**_passed_report(), "doctor_verified": False, "workflow_enabled": False}
    assert commit_ready(incomplete) is True
    assert pipeline_verdict(incomplete) == "PARTIAL"
    oom = {**_passed_report(), "failure_stage": "D", "failure_detail": "CUDA out of memory", "workflow_enabled": False}
    assert pipeline_verdict(oom) == "FAIL"
    assert commit_ready(oom) is False
    assert image_failure_stage("ComfyUI is not reachable") == "C"
    assert image_failure_stage("ComfyUI cannot run this workflow: UNETLoader") == "C"
    assert image_failure_stage("CUDA out of memory") == "D"
    assert image_failure_stage("The image job did not finish before the verifier timeout.") == "D"
    assert image_failure_stage("output is not a PNG") == "D"
    with pytest.raises(ValueError, match="only after every stage"):
        commit_verification(
            passed=False,
            manifest_sha256="ab" * 32,
            launch_profile="default",
            report_dir=str(tmp_path),
        )
    assert pipeline_verdict(_passed_report()) == "PASS"
    assert commit_ready(_passed_report()) is True


def test_klein_graph_rejects_the_full_size_text_encoder() -> None:
    assert manifest_configuration_problems(MANIFEST) == []
    graph = json.loads(API.read_text(encoding="utf-8"))
    assert klein_configuration_problems(graph) == []
    graph["75:71"]["inputs"]["clip_name"] = "qwen_3_4b.safetensors"
    problems = klein_configuration_problems(graph)
    assert any("qwen_3_4b.safetensors" in problem for problem in problems)
    graph["75:71"]["inputs"]["clip_name"] = TEXT_ENCODER
    graph["75:63"]["inputs"]["cfg"] = 5
    assert any("cfg" in problem for problem in klein_configuration_problems(graph))
    graph["75:63"]["inputs"]["cfg"] = 1.0
    graph["75:61"]["inputs"]["sampler_name"] = "dpmpp_2m"
    assert any("sampler" in problem for problem in klein_configuration_problems(graph))


def test_open_image_jobs_block_a_restart(tmp_path: Path) -> None:
    missing = tmp_path / "missing.db"
    assert open_image_job_count(missing) == 0
    assert inspect_image_jobs(missing)["count"] == 0
    database = tmp_path / "switchboard.db"
    database.write_bytes(b"not a database")
    with pytest.raises(ValueError, match="not a SQLite"):
        open_image_job_count(database)
    blocked = inspect_image_jobs(database)
    assert blocked["ok"] is False
    connection = __import__("sqlite3").connect(tmp_path / "jobs.db")
    connection.execute(
        """
        CREATE TABLE image_jobs (
            id TEXT PRIMARY KEY,
            status TEXT NOT NULL
        )
        """
    )
    connection.execute("INSERT INTO image_jobs (id, status) VALUES ('job-1', 'running')")
    connection.execute("INSERT INTO image_jobs (id, status) VALUES ('job-2', 'succeeded')")
    connection.commit()
    connection.close()
    assert open_image_job_count(tmp_path / "jobs.db") == 1


def test_scheduler_log_gpu_summary_and_offload_do_not_invent_values() -> None:
    text = "\n".join(
        [
            '{"message": "scheduler state LLM_ACTIVE"}',
            "scheduler state IDLE",
            '{"message": "provider image released GPU resources: free"}',
        ]
    )
    assert latest_scheduler_state(text) == "IDLE"
    assert latest_scheduler_state("") is None
    assert latest_scheduler_state("scheduler state NOWHERE") is None
    summary = summarize_gpu_csv(
        "timestamp,memory_used_mib,memory_total_mib,utilization_gpu,temperature_c,power_w\n"
        "2026-11-10T10:00:00, 1000, 8188, 10, 40, [N/A]\n"
        "2026-11-10T10:00:01, 6400, 8188, 95, 70, 110\n"
    )
    assert summary["samples"] == 2
    assert summary["peak_vram_mib"] == 6400
    assert summary["memory_total_mib"] == 8188
    assert summary["peak_utilization_gpu"] == 95
    assert summary["peak_power_w"] == 110
    assert summary["started_at"] == "2026-11-10T10:00:00"
    assert summary["finished_at"] == "2026-11-10T10:00:01"
    empty = summarize_gpu_csv("timestamp,memory_used_mib,memory_total_mib,utilization_gpu,temperature_c,power_w\n")
    assert empty["samples"] == 0
    assert empty["peak_vram_mib"] is None
    assert empty["peak_power_w"] is None
    offload = describe_offload(
        {
            "system": {"comfyui_version": "0.41.0"},
            "devices": [{"vram_total": 8000, "torch_vram_total": 5000, "name": "cuda:0"}],
        },
        "lowvram mode\n",
    )
    assert offload["observed"] is True
    assert offload["claimed_fully_resident"] is False
    assert offload["comfyui_version"] == "0.41.0"
    unknown = describe_offload(None, "")
    assert unknown["observed"] is False
    assert unknown["claimed_fully_resident"] is False
    assert "device" not in unknown
    released = release_observation(
        {"queue_running": [], "queue_pending": []},
        {"devices": [{"vram_free": 7_000_000_000, "torch_vram_free": 7_000_000_000}]},
        text,
    )
    assert released["confirmed"] is True
    assert released["queue_idle"] is True
    missing_memory = release_observation({"queue_running": [], "queue_pending": []}, None, "")
    assert missing_memory["confirmed"] is False
    assert missing_memory["memory_free"] is None
    note = comfyui_log_text("gateway started\n", "")
    assert note["collected"] is False
    assert "not available" in str(note["text"])
    copied = comfyui_log_text("", "Started ComfyUI\n")
    assert copied["collected"] is True


def test_verification_record_keeps_model_checksums_and_a_second_pass(tmp_path: Path) -> None:
    details = {
        "comfyui_version": "0.41.0",
        "comfyui_version_source": "pin",
        "gpu": "NVIDIA GeForce RTX 4060",
        "driver": "560.00",
        "vram_mib": 8188,
        "width": 768,
        "height": 768,
        "seed": 42,
        "steps": 4,
        "cfg": 1.0,
        "sampler": "euler",
        "batch_size": 1,
        "models": [
            {
                "filename": "flux-2-klein-4b-fp8.safetensors",
                "folder": "diffusion_models",
                "sha256": "97ed34fe0567e436200f2faee3939b88f2b5d99f8af2a4dc16532c4245c0ccb6",
            },
            {
                "filename": "qwen_3_4b_fp4_flux2.safetensors",
                "folder": "text_encoders",
                "sha256": "3eab03a77adb0ee5304a4e677d5c10ac22f9049c1d7c894adca4f8bb39206ca8",
                "revision": "8556e4d870cda7c53c7942b190bfeea5be9bd411",
            },
        ],
        "workflow_parameters": {"workflow_id": "flux2-klein-t2i", "cfg": 1.0, "sampler": "euler"},
    }
    first = commit_verification(
        passed=True,
        manifest_sha256="ab" * 32,
        launch_profile="default",
        report_dir=str(tmp_path),
        details=details,
    )
    second = commit_verification(
        passed=True,
        manifest_sha256="AB" * 32,
        launch_profile="default",
        report_dir=str(tmp_path),
        details=details,
    )
    assert first["status"] == second["status"] == "passed"
    assert first["manifest_sha256"] == second["manifest_sha256"]
    assert first["vram_fit_claimed"] is False
    assert first["models"] == second["models"]
    assert first["cfg"] == 1.0
    assert first["sampler"] == "euler"
    assert "verified_at" in first
    with pytest.raises(ValueError, match="cfg"):
        commit_verification(
            passed=True,
            manifest_sha256="ab" * 32,
            launch_profile="default",
            report_dir=str(tmp_path),
            details={"cfg": 5},
        )
    live = tmp_path / "workflows"
    live.mkdir()
    original = '{"id":"flux2-klein-4b","enabled":false}\n'
    (live / "flux2-klein-4b.json").write_text(original, encoding="utf-8")
    backup = tmp_path / "backup"
    save_workflow_snapshot(live, backup)
    stage_live_manifest(live, MANIFEST)
    stage_live_manifest(live, MANIFEST)
    restore_workflow_snapshot(live, backup)
    assert not (live / "flux2-klein-t2i.json").exists()
    assert (live / "flux2-klein-4b.json").read_text(encoding="utf-8") == original


def test_report_writes_the_generated_png_name(tmp_path: Path) -> None:
    png = b"\x89PNG\r\n\x1a\n" + b"rest"
    result = write_pipeline_report(
        tmp_path / "run",
        _passed_report(),
        csv_text="timestamp\n",
        image=png,
    )
    assert result == "PASS"
    assert (tmp_path / "run" / "generated-image.png").read_bytes() == png
    stored = json.loads((tmp_path / "run" / "report.json").read_text(encoding="utf-8"))
    assert stored["result"] == "PASS"
    assert stored["mac_lan_checked"] is False
    assert "environment was not collected" in (tmp_path / "run" / "environment.json").read_text(encoding="utf-8")
