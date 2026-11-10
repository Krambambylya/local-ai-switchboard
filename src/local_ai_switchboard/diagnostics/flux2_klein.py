"""Adapt the official FLUX.2 Klein API graph for the local FP8 installation.

The node ids come from the downloaded upstream file. This module refuses to
continue when those ids or class types differ. It does not create nodes.
"""

from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

from local_ai_switchboard.providers.workflows import WorkflowError, load_manifest

WORKFLOW_ID = "flux2-klein-t2i"
DIFFUSION_MODEL = "flux-2-klein-4b-fp8.safetensors"
TEXT_ENCODER = "qwen_3_4b_fp4_flux2.safetensors"
VAE_MODEL = "flux2-vae.safetensors"
DIAGNOSTIC_PROMPT = (
    "Professional editorial photograph of a single traditional kayak on a calm "
    "winding forest river, early morning mist, dense pine forest, natural "
    "realistic lighting, sophisticated composition, no text, no logos."
)
SOURCE_PAGE = "https://comfy.org/hub/workflows/flux-2-klein-4b-text-to-image-814fd547d86e/"
REQUESTED_SOURCE_URL = (
    "https://comfy.com/catalog-media/70fa47f1-1d84-4d4b-9a4f-f8b64790a7d1/"
    "d25b4bdb-1e64-44c3-8c86-26443f52318c"
)
SOURCE_URL = (
    "https://comfy.org/catalog-media/70fa47f1-1d84-4d4b-9a4f-f8b64790a7d1/"
    "d25b4bdb-1e64-44c3-8c86-26443f52318c"
)

EXPECTED_CLASSES = {
    "9": "SaveImage",
    "76": "PrimitiveStringMultiline",
    "75:61": "KSamplerSelect",
    "75:62": "Flux2Scheduler",
    "75:63": "CFGGuider",
    "75:64": "SamplerCustomAdvanced",
    "75:65": "VAEDecode",
    "75:66": "EmptyFlux2LatentImage",
    "75:67": "CLIPTextEncode",
    "75:68": "PrimitiveInt",
    "75:69": "PrimitiveInt",
    "75:70": "UNETLoader",
    "75:71": "CLIPLoader",
    "75:72": "VAELoader",
    "75:73": "RandomNoise",
    "75:74": "CLIPTextEncode",
}

# Scalar values the downloaded API graph must still have before adaptation.
EXPECTED_UPSTREAM_VALUES: dict[tuple[str, str], object] = {
    ("75:68", "value"): 1024,
    ("75:69", "value"): 1024,
    ("75:62", "steps"): 20,
    ("75:63", "cfg"): 5,
    ("75:70", "unet_name"): "flux-2-klein-base-4b.safetensors",
    ("75:71", "clip_name"): "qwen_3_4b.safetensors",
    ("75:72", "vae_name"): VAE_MODEL,
    ("75:61", "sampler_name"): "euler",
    ("75:66", "batch_size"): 1,
    ("75:73", "noise_seed"): 0,
}

ADAPTED_VALUES: dict[tuple[str, str], object] = {
    ("75:68", "value"): 768,
    ("75:69", "value"): 768,
    ("75:62", "steps"): 4,
    ("75:63", "cfg"): 1.0,
    ("75:70", "unet_name"): DIFFUSION_MODEL,
    ("75:71", "clip_name"): TEXT_ENCODER,
}


class WorkflowSourceError(ValueError):
    """The upstream API graph does not match the recorded node map."""


def assert_upstream_graph(graph: dict[str, Any]) -> None:
    """Raise when node ids, class types, or the recorded scalars differ."""

    if set(graph) != set(EXPECTED_CLASSES):
        missing = sorted(set(EXPECTED_CLASSES) - set(graph))
        extra = sorted(set(graph) - set(EXPECTED_CLASSES))
        raise WorkflowSourceError(
            f"upstream node ids differ. missing={missing} extra={extra}"
        )
    for node_id, class_type in EXPECTED_CLASSES.items():
        node = graph[node_id]
        if not isinstance(node, dict) or node.get("class_type") != class_type:
            actual = node.get("class_type") if isinstance(node, dict) else type(node).__name__
            raise WorkflowSourceError(
                f"upstream node {node_id} class_type is {actual}, expected {class_type}"
            )
    for (node_id, key), expected in EXPECTED_UPSTREAM_VALUES.items():
        actual = graph[node_id]["inputs"].get(key)
        if actual != expected:
            raise WorkflowSourceError(
                f"upstream node {node_id} input {key} is {actual!r}, expected {expected!r}"
            )
    positive = graph["75:74"]["inputs"].get("text")
    if positive != ["76", 0]:
        raise WorkflowSourceError("upstream positive prompt is not linked to node 76")
    saved = graph["9"]["inputs"].get("images")
    if saved != ["75:65", 0]:
        raise WorkflowSourceError("upstream SaveImage is not linked to node 75:65")


def adapt_upstream_graph(graph: dict[str, Any]) -> dict[str, Any]:
    """Copy the official graph and change only the local model and sampler settings."""

    assert_upstream_graph(graph)
    adapted: dict[str, Any] = deepcopy(graph)
    for (node_id, key), value in ADAPTED_VALUES.items():
        adapted[node_id]["inputs"][key] = value
    return adapted


def build_manifest(graph: dict[str, Any]) -> dict[str, Any]:
    """Return a gateway manifest for an already adapted graph."""

    if graph.get("9", {}).get("class_type") != "SaveImage":
        raise WorkflowSourceError("adapted graph is missing SaveImage node 9")
    if graph["75:70"]["inputs"].get("unet_name") != DIFFUSION_MODEL:
        raise WorkflowSourceError("adapted graph does not name the FP8 diffusion model")
    if graph["75:71"]["inputs"].get("clip_name") != TEXT_ENCODER:
        raise WorkflowSourceError("adapted graph does not name the FP4 text encoder")
    if graph["75:72"]["inputs"].get("vae_name") != VAE_MODEL:
        raise WorkflowSourceError("adapted graph does not name the FLUX.2 VAE")
    return {
        "id": WORKFLOW_ID,
        "title": "FLUX.2 Klein 4B text to image",
        "enabled": True,
        "disabled_reason": None,
        "workflow": graph,
        "bindings": {
            "prompt": {"node": "76", "input": "value"},
            "width": {"node": "75:68", "input": "value"},
            "height": {"node": "75:69", "input": "value"},
            "seed": {"node": "75:73", "input": "noise_seed"},
            "steps": {"node": "75:62", "input": "steps"},
        },
        "required": ["prompt"],
        "defaults": {"width": 768, "height": 768, "steps": 4},
    }


def class_types_in_manifest(path: Path) -> list[str]:
    """Return the sorted class types used by one manifest."""

    manifest = load_manifest(path)
    if manifest.workflow is None:
        raise WorkflowError("workflow graph is missing")
    found: list[str] = []
    for node in manifest.workflow.values():
        class_type = node.get("class_type")
        if isinstance(class_type, str) and class_type not in found:
            found.append(class_type)
    return sorted(found)


def linked_inputs(graph: dict[str, Any]) -> list[tuple[str, str, list[Any]]]:
    """Return every connection in stable node order."""

    links: list[tuple[str, str, list[Any]]] = []
    for node_id in graph:
        node = graph[node_id]
        inputs = node.get("inputs", {})
        if not isinstance(inputs, dict):
            continue
        for key, value in inputs.items():
            if isinstance(value, list):
                links.append((node_id, key, value))
    return links


def provenance_document(*, sha256: str, download_date: str) -> dict[str, Any]:
    """Describe the downloaded file and the local edits. This is not the graph."""

    return {
        "source_page": SOURCE_PAGE,
        "requested_url": REQUESTED_SOURCE_URL,
        "requested_url_result": "HTTP 404 from comfy.com. The same path on comfy.org returned the API graph.",
        "source_url": SOURCE_URL,
        "download_date": download_date,
        "sha256": sha256,
        "original_model_references": {
            "unet_name": "flux-2-klein-base-4b.safetensors",
            "clip_name": "qwen_3_4b.safetensors",
            "vae_name": VAE_MODEL,
        },
        "modifications": [
            {
                "node": "75:68",
                "input": "value",
                "from": 1024,
                "to": 768,
                "reason": "Local width. The downloaded API graph uses 1024. The hub page preview showed 1344.",
            },
            {
                "node": "75:69",
                "input": "value",
                "from": 1024,
                "to": 768,
                "reason": "Local height. The downloaded API graph uses 1024. The hub page preview showed 768.",
            },
            {
                "node": "75:62",
                "input": "steps",
                "from": 20,
                "to": 4,
                "reason": "The FP8 file is the distilled 4B variant and uses 4 steps.",
            },
            {
                "node": "75:63",
                "input": "cfg",
                "from": 5,
                "to": 1.0,
                "reason": "The distilled 4-step mode uses CFG 1.0.",
            },
            {
                "node": "75:70",
                "input": "unet_name",
                "from": "flux-2-klein-base-4b.safetensors",
                "to": DIFFUSION_MODEL,
                "reason": "Local FP8 diffusion weights.",
            },
            {
                "node": "75:71",
                "input": "clip_name",
                "from": "qwen_3_4b.safetensors",
                "to": TEXT_ENCODER,
                "reason": "Local FP4 text encoder. qwen_3_4b.safetensors is a different file.",
            },
        ],
        "unchanged": [
            "node ids and connections",
            "75:61 sampler_name euler",
            "75:72 vae_name flux2-vae.safetensors",
            "75:66 batch_size 1",
            "75:73 noise_seed",
            "76 prompt input",
            "9 SaveImage",
        ],
    }


def dump_json(payload: object) -> str:
    """Serialize workflow JSON with a trailing newline."""

    return json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
