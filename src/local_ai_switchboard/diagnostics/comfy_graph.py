"""Compare an API graph with ComfyUI object_info. This module does not open sockets."""

from __future__ import annotations

from typing import Any


def validate_graph_support(graph: dict[str, Any], object_info: dict[str, Any]) -> list[str]:
    """Return one message per missing class, input, model, or broken connection."""

    problems: list[str] = []
    if not isinstance(graph, dict) or not graph:
        return ["workflow graph is empty"]
    for node_id, node in graph.items():
        if not isinstance(node, dict):
            problems.append(f"node {node_id} is not an object")
            continue
        class_type = node.get("class_type")
        if not isinstance(class_type, str) or not class_type:
            problems.append(f"node {node_id} has no class_type")
            continue
        info = object_info.get(class_type)
        if not isinstance(info, dict):
            problems.append(f"ComfyUI has no node class {class_type}")
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            problems.append(f"node {node_id} has no inputs")
            continue
        specs = _input_specs(info)
        for name, value in inputs.items():
            if name not in specs:
                problems.append(f"node {node_id} {class_type} has no input {name}")
                continue
            if isinstance(value, list):
                problems.extend(_link_problems(graph, object_info, node_id, name, value))
                continue
            choices = _combo_choices(specs[name])
            if choices is not None and value not in choices:
                problems.append(
                    f"node {node_id} input {name} value {value!r} is not installed"
                )
    return problems


def _input_specs(info: dict[str, Any]) -> dict[str, Any]:
    raw = info.get("input")
    if not isinstance(raw, dict):
        return {}
    specs: dict[str, Any] = {}
    for group in ("required", "optional", "hidden"):
        section = raw.get(group)
        if isinstance(section, dict):
            specs.update(section)
    return specs


def _combo_choices(spec: object) -> list[str] | None:
    if not isinstance(spec, list) or not spec or not isinstance(spec[0], list):
        return None
    choices = spec[0]
    if not all(isinstance(item, str) for item in choices):
        return None
    return choices


def _link_problems(
    graph: dict[str, Any],
    object_info: dict[str, Any],
    node_id: str,
    name: str,
    value: list[Any],
) -> list[str]:
    if (
        len(value) != 2
        or not isinstance(value[0], str)
        or isinstance(value[1], bool)
        or not isinstance(value[1], int)
    ):
        return [f"node {node_id} input {name} has a malformed connection"]
    target_id, slot = value
    target = graph.get(target_id)
    if not isinstance(target, dict):
        return [f"node {node_id} input {name} links to missing node {target_id}"]
    target_class = target.get("class_type")
    if not isinstance(target_class, str):
        return [f"node {target_id} has no class_type"]
    info = object_info.get(target_class)
    outputs = info.get("output") if isinstance(info, dict) else None
    if not isinstance(outputs, list):
        return [f"node {target_id} {target_class} does not report output slots"]
    if slot < 0 or slot >= len(outputs):
        return [
            f"node {node_id} input {name} links to {target_id} slot {slot}, "
            f"which is outside {len(outputs)} outputs"
        ]
    return []
