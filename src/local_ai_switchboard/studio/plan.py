"""Validate the JSON plan Qwen returns. An invalid plan is not executed."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

OPERATIONS = frozenset({"analyze", "extract", "generate", "edit", "extract_and_edit"})
REGION_OPERATIONS = frozenset({"extract", "extract_and_edit"})
_MAX_ASSETS = 12
_PRIORITY = ("extract_and_edit", "extract", "edit", "generate", "analyze")


class PlanError(ValueError):
    """The model text is not a plan the backend will run."""


@dataclass(frozen=True)
class BBox:
    x: float
    y: float
    width: float
    height: float

    def as_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class PlanAsset:
    id: str
    label: str
    type: str
    bbox: BBox | None
    confidence: float
    operation: str
    edit_instruction: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "type": self.type,
            "bbox": None if self.bbox is None else self.bbox.as_dict(),
            "confidence": self.confidence,
            "operation": self.operation,
            "edit_instruction": self.edit_instruction,
        }


@dataclass(frozen=True)
class TaskPlan:
    summary: str
    description: str
    operations: tuple[str, ...]
    assets: tuple[PlanAsset, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "summary": self.summary,
            "description": self.description,
            "operations": list(self.operations),
            "assets": [asset.as_dict() for asset in self.assets],
        }


def parse_plan(text: str) -> TaskPlan:
    """Accept one JSON object, including a plan wrapped in a short fence."""

    payload = _json_object(text)
    if not isinstance(payload, dict):
        raise PlanError("The plan is not a JSON object.")
    summary = _text(payload.get("summary"), "summary")
    description = payload.get("description")
    if description is None:
        description = summary
    description = _text(description, "description")
    operations = _operations(payload.get("operations"), required=False)
    assets = _assets(payload.get("assets", []))
    if not operations:
        operations = tuple(dict.fromkeys(asset.operation for asset in assets))
    if not operations:
        operations = ("analyze",)
    for name in operations:
        if name in REGION_OPERATIONS and not any(asset.bbox is not None for asset in assets):
            raise PlanError(f"{name} needs at least one region.")
    return TaskPlan(summary, description, operations, assets)


def replace_regions(plan: TaskPlan, regions: object) -> TaskPlan:
    """Replace detected regions with the user's boxes. The summary stays."""

    assets = _assets(regions)
    if not assets:
        raise PlanError("At least one region is required.")
    operations = tuple(dict.fromkeys(asset.operation for asset in assets))
    return TaskPlan(plan.summary, plan.description, operations, assets)


def choose_route(plan: TaskPlan, override: str | None, *, has_images: bool) -> str:
    """Pick the backend route. An explicit operation overrides the model's list."""

    if override is not None:
        if override not in OPERATIONS:
            raise PlanError("The operation is not supported.")
        route = override
    else:
        present = set(plan.operations) | {asset.operation for asset in plan.assets}
        route = next((name for name in _PRIORITY if name in present), "analyze")
    if route in {"edit", "extract", "extract_and_edit"} and not has_images:
        raise PlanError(f"{route} needs an attached image.")
    if route in REGION_OPERATIONS and not any(asset.bbox is not None for asset in plan.assets):
        raise PlanError(f"{route} needs at least one region.")
    return route


def needs_approval(route: str) -> bool:
    return route in REGION_OPERATIONS


def _json_object(text: str) -> object:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = [line for line in stripped.splitlines() if not line.strip().startswith("```")]
        stripped = "\n".join(lines).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start < 0 or end <= start:
        raise PlanError("The plan is not valid JSON.")
    try:
        return json.loads(stripped[start : end + 1])
    except json.JSONDecodeError as exc:
        raise PlanError("The plan is not valid JSON.") from exc


def _text(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PlanError(f"{name} is required.")
    if len(value) > 4000:
        raise PlanError(f"{name} is too long.")
    return value.strip()


def _operations(value: object, *, required: bool) -> tuple[str, ...]:
    if value is None:
        if required:
            raise PlanError("operations is required.")
        return ()
    if not isinstance(value, list):
        raise PlanError("operations must be a list.")
    names: list[str] = []
    for item in value:
        if not isinstance(item, str) or item not in OPERATIONS:
            raise PlanError("operations contains an unsupported value.")
        if item not in names:
            names.append(item)
    return tuple(names)


def _assets(value: object) -> tuple[PlanAsset, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise PlanError("assets must be a list.")
    if len(value) > _MAX_ASSETS:
        raise PlanError("Too many regions were returned.")
    assets: list[PlanAsset] = []
    seen: set[str] = set()
    for index, item in enumerate(value, start=1):
        if not isinstance(item, dict):
            raise PlanError("Each region must be an object.")
        asset_id = item.get("id")
        if not isinstance(asset_id, str) or not asset_id.strip():
            asset_id = f"asset_{index:03d}"
        asset_id = asset_id.strip()
        if asset_id in seen or len(asset_id) > 64:
            raise PlanError("Region ids must be unique.")
        seen.add(asset_id)
        label = item.get("label") if isinstance(item.get("label"), str) else f"Region {index}"
        kind = item.get("type") if isinstance(item.get("type"), str) else "region"
        operation = item.get("operation")
        if not isinstance(operation, str) or operation not in OPERATIONS:
            raise PlanError("A region has an unsupported operation.")
        confidence = item.get("confidence", 1)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise PlanError("confidence must be a number.")
        if not 0 <= float(confidence) <= 1:
            raise PlanError("confidence must be between 0 and 1.")
        bbox = _bbox(item.get("bbox"))
        if operation in REGION_OPERATIONS and bbox is None:
            raise PlanError(f"{operation} needs a bbox.")
        instruction = item.get("edit_instruction")
        if instruction is not None and not isinstance(instruction, str):
            raise PlanError("edit_instruction must be a string.")
        if isinstance(instruction, str):
            instruction = instruction.strip() or None
            if instruction is not None and len(instruction) > 4000:
                raise PlanError("edit_instruction is too long.")
        assets.append(
            PlanAsset(
                asset_id,
                label.strip()[:200] or f"Region {index}",
                kind.strip()[:80] or "region",
                bbox,
                float(confidence),
                operation,
                instruction,
            )
        )
    return tuple(assets)


def _bbox(value: object) -> BBox | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise PlanError("bbox must be an object.")
    numbers: dict[str, float] = {}
    for name in ("x", "y", "width", "height"):
        raw = value.get(name)
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise PlanError(f"bbox.{name} must be a number.")
        numbers[name] = float(raw)
    if numbers["width"] <= 0 or numbers["height"] <= 0:
        raise PlanError("bbox width and height must be positive.")
    if any(number < 0 or number > 1 for number in numbers.values()):
        raise PlanError("bbox values must stay inside 0..1.")
    if numbers["x"] + numbers["width"] > 1 or numbers["y"] + numbers["height"] > 1:
        raise PlanError("bbox extends outside the image.")
    return BBox(numbers["x"], numbers["y"], numbers["width"], numbers["height"])
