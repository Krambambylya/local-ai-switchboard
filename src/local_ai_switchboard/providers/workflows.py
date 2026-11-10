"""Load API-format ComfyUI workflows and apply explicit parameter bindings.

An enabled manifest must already contain the node and input a binding writes.
This module does not create nodes.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_PROMPT_LIMIT = 4000


class WorkflowError(ValueError):
    """A manifest or a generation request does not match the registry rules."""


class Binding(BaseModel):
    """One request field mapped onto an existing workflow input."""

    model_config = ConfigDict(extra="forbid")

    node: str = Field(min_length=1, max_length=128)
    input: str = Field(min_length=1, max_length=128)


class WorkflowManifest(BaseModel):
    """One registered workflow. Disabled entries have no graph."""

    model_config = ConfigDict(extra="forbid")

    id: str
    title: str = Field(min_length=1, max_length=200)
    enabled: bool
    disabled_reason: str | None = None
    workflow: dict[str, Any] | None = None
    bindings: dict[str, Binding] = Field(default_factory=dict)
    required: list[str] = Field(default_factory=list)
    defaults: dict[str, Any] = Field(default_factory=dict)

    @field_validator("id")
    @classmethod
    def id_is_a_slug(cls, value: str) -> str:
        if _ID.fullmatch(value) is None:
            raise ValueError("workflow id must be a lowercase slug")
        return value

    @model_validator(mode="after")
    def graph_matches_its_bindings(self) -> WorkflowManifest:
        if not self.enabled:
            if self.workflow is not None:
                raise ValueError("a disabled workflow must not contain a graph")
            if not self.disabled_reason:
                raise ValueError("a disabled workflow needs a reason")
            return self
        if not isinstance(self.workflow, dict) or not self.workflow:
            raise ValueError("an enabled workflow needs an API-format graph")
        _validate_graph(self.workflow)
        known = set(self.bindings)
        unknown_required = [name for name in self.required if name not in known]
        if unknown_required:
            names = ", ".join(unknown_required)
            raise ValueError(f"required parameters have no binding: {names}")
        unknown_defaults = [name for name in self.defaults if name not in known]
        if unknown_defaults:
            names = ", ".join(unknown_defaults)
            raise ValueError(f"defaults have no binding: {names}")
        for name, binding in self.bindings.items():
            _require_writable_input(self.workflow, name, binding)
        for name, value in self.defaults.items():
            _check_value(name, value, self.workflow, self.bindings[name])
        return self


class WorkflowRegistry:
    """Workflows loaded from one directory. Unknown files are skipped."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self._workflows: dict[str, WorkflowManifest] = {}
        self.errors: list[str] = []
        self._load()

    def get(self, workflow_id: str) -> WorkflowManifest | None:
        return self._workflows.get(workflow_id)

    def list(self) -> list[WorkflowManifest]:
        return [self._workflows[key] for key in sorted(self._workflows)]

    def _load(self) -> None:
        if not self.directory.is_dir():
            self.errors.append(f"workflow directory is missing: {self.directory.name}")
            return
        for path in sorted(self.directory.glob("*.json")):
            try:
                manifest = load_manifest(path)
            except (OSError, WorkflowError, json.JSONDecodeError) as exc:
                self.errors.append(f"{path.name}: {exc}")
                continue
            if manifest.id in self._workflows:
                self.errors.append(f"{path.name}: duplicate workflow id {manifest.id}")
                continue
            self._workflows[manifest.id] = manifest


def load_manifest(path: Path) -> WorkflowManifest:
    """Read one manifest. Raises WorkflowError when the file is not usable."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WorkflowError(f"invalid JSON: {exc}") from exc
    return validate_manifest(payload)


def validate_manifest(payload: object) -> WorkflowManifest:
    """Validate a manifest document."""

    try:
        return WorkflowManifest.model_validate(payload)
    except ValueError as exc:
        raise WorkflowError(str(exc)) from exc


def request_parameters(manifest: WorkflowManifest, body: object) -> dict[str, Any]:
    """Return bound values, or raise WorkflowError without changing the graph."""

    if not isinstance(body, dict):
        raise WorkflowError("Request body must be a JSON object")
    workflow_id = body.get("workflow_id")
    if not isinstance(workflow_id, str) or workflow_id != manifest.id:
        raise WorkflowError("workflow_id does not match the selected workflow")
    unknown = sorted(key for key in body if key != "workflow_id" and key not in manifest.bindings)
    if unknown:
        raise WorkflowError(f"Unsupported parameters: {', '.join(unknown)}")
    if not manifest.enabled or manifest.workflow is None:
        reason = manifest.disabled_reason or (
            "A real ComfyUI API-format workflow export is required before image generation can be enabled."
        )
        raise WorkflowError(reason)
    values = dict(manifest.defaults)
    for name in manifest.bindings:
        if name in body:
            values[name] = body[name]
    missing = [name for name in manifest.required if name not in values]
    if missing:
        raise WorkflowError(f"Missing parameters: {', '.join(missing)}")
    apply_bindings(manifest, values)
    return values


def apply_bindings(manifest: WorkflowManifest, values: dict[str, Any]) -> dict[str, Any]:
    """Copy the API graph and write bound values onto existing inputs."""

    if manifest.workflow is None:
        raise WorkflowError("workflow graph is not available")
    graph: dict[str, Any] = deepcopy(manifest.workflow)
    for name, raw in values.items():
        binding = manifest.bindings.get(name)
        if binding is None:
            raise WorkflowError(f"Unsupported parameters: {name}")
        _check_value(name, raw, graph, binding)
        node = graph[binding.node]
        node["inputs"][binding.input] = raw
    return graph


def _validate_graph(workflow: dict[str, Any]) -> None:
    for node_id, node in workflow.items():
        if not isinstance(node_id, str) or not node_id:
            raise ValueError("workflow node ids must be strings")
        if not isinstance(node, dict):
            raise ValueError(f"workflow node {node_id} must be an object")
        class_type = node.get("class_type")
        inputs = node.get("inputs")
        if not isinstance(class_type, str) or not class_type:
            raise ValueError(f"workflow node {node_id} needs a class_type")
        if not isinstance(inputs, dict):
            raise ValueError(f"workflow node {node_id} needs inputs")


def _require_writable_input(workflow: dict[str, Any], name: str, binding: Binding) -> None:
    node = workflow.get(binding.node)
    if not isinstance(node, dict):
        raise ValueError(f"binding {name} references missing node {binding.node}")
    inputs = node.get("inputs")
    if not isinstance(inputs, dict) or binding.input not in inputs:
        raise ValueError(f"binding {name} references missing input {binding.node}.{binding.input}")
    if isinstance(inputs[binding.input], list):
        raise ValueError(f"binding {name} targets a linked input")


def _check_value(name: str, value: object, workflow: dict[str, Any], binding: Binding) -> None:
    current = workflow[binding.node]["inputs"][binding.input]
    if isinstance(current, str):
        if not isinstance(value, str) or not value.strip():
            raise WorkflowError(f"{name} must be a non-empty string")
        if len(value) > _PROMPT_LIMIT:
            raise WorkflowError(f"{name} is too long")
        return
    if isinstance(current, float) and not isinstance(current, bool):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise WorkflowError(f"{name} must be a number")
        return
    if isinstance(current, bool) or not isinstance(current, int):
        raise WorkflowError(f"{name} has an unsupported workflow value")
    if isinstance(value, bool) or not isinstance(value, int):
        raise WorkflowError(f"{name} must be an integer")
    if (name in {"width", "height"} or binding.input in {"width", "height"}) and not 64 <= value <= 2048:
        raise WorkflowError(f"{name} must be between 64 and 2048")
    if binding.input in {"batch", "batch_size"} and not 1 <= value <= 4:
        raise WorkflowError(f"{name} must be between 1 and 4")
    if value < 0 or value > 2**31 - 1:
        raise WorkflowError(f"{name} is out of range")
