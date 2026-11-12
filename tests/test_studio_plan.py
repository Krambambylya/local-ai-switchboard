"""Plan validation for Qwen JSON."""

from __future__ import annotations

import pytest

from local_ai_switchboard.studio.plan import PlanError, choose_route, needs_approval, parse_plan, replace_regions


def test_parse_valid_plan() -> None:
    plan = parse_plan(
        """
        {
          "summary": "Found a hero",
          "operations": ["extract_and_edit"],
          "assets": [{
            "id": "asset_001",
            "label": "Hero",
            "type": "photograph",
            "bbox": {"x": 0.1, "y": 0.2, "width": 0.3, "height": 0.4},
            "confidence": 0.9,
            "operation": "extract_and_edit",
            "edit_instruction": "Keep the subject"
          }]
        }
        """
    )
    assert plan.operations == ("extract_and_edit",)
    assert plan.assets[0].bbox is not None
    assert needs_approval(choose_route(plan, None, has_images=True))


def test_invalid_json_and_bbox_are_rejected() -> None:
    with pytest.raises(PlanError, match="JSON"):
        parse_plan("not json")
    with pytest.raises(PlanError, match="0..1"):
        parse_plan(
            '{"summary":"x","operations":["extract"],"assets":[{"id":"a","operation":"extract","bbox":{"x":-0.1,"y":0,"width":0.2,"height":0.2},"confidence":1}]}'
        )
    with pytest.raises(PlanError, match="outside"):
        parse_plan(
            '{"summary":"x","operations":["extract"],"assets":[{"id":"a","operation":"extract","bbox":{"x":0.8,"y":0,"width":0.3,"height":0.2},"confidence":1}]}'
        )


def test_choose_route_respects_override_and_images() -> None:
    plan = parse_plan('{"summary":"ready","operations":["analyze"],"assets":[]}')
    assert choose_route(plan, "generate", has_images=False) == "generate"
    with pytest.raises(PlanError, match="attached image"):
        choose_route(plan, "edit", has_images=False)


def test_replace_regions_updates_boxes() -> None:
    plan = parse_plan(
        '{"summary":"x","operations":["extract"],"assets":[{"id":"a","operation":"extract","bbox":{"x":0.1,"y":0.1,"width":0.2,"height":0.2},"confidence":1}]}'
    )
    updated = replace_regions(
        plan,
        [{"id": "a", "operation": "extract", "bbox": {"x": 0.2, "y": 0.2, "width": 0.2, "height": 0.2}, "confidence": 1}],
    )
    assert updated.assets[0].bbox is not None
    assert updated.assets[0].bbox.x == 0.2
