import pytest
from pydantic import ValidationError

from image_agent.long_image_models import (
    LongImageOptions,
    LongImagePlan,
    LongImageRequest,
    LongImageUsage,
    LongPlanningRecord,
    LongSlotAsset,
)


def test_nonproduct_needs_no_platform_or_materials():
    request = LongImageRequest(scene="tutorial", title="操作指南", content_brief="按步骤介绍")
    assert request.materials == [] and request.product_context is None
    with pytest.raises(ValidationError):
        LongImageRequest(scene="product_detail", title="杯子", content_brief="介绍")
    with pytest.raises(ValidationError):
        LongImageRequest(
            scene="product_detail",
            title="杯子",
            content_brief="介绍",
            product_context={"product_name": "杯子", "category": "日用品"},
        )


@pytest.mark.parametrize("change", [{"width": 399}, {"width": 2401}, {"slice_height": 4001}])
def test_render_options_reject_outside_bounds(change):
    with pytest.raises(ValidationError):
        LongImageOptions(**change)


def test_source_asset_does_not_forge_generated_status_or_audit():
    assert LongSlotAsset(slot_id="s", source="source", source_status="available").status is None
    with pytest.raises(ValidationError):
        LongSlotAsset(slot_id="s", source="source", source_status="available", status="succeeded")


def test_record_cannot_assert_trusted_usage():
    with pytest.raises(ValidationError):
        LongPlanningRecord(
            record_id="r", planning_id="p", request_hash="a", plan_hash="b", trusted=True
        )
    record = LongPlanningRecord(record_id="r", planning_id="p", request_hash="a", plan_hash="b")
    assert record.usage is None


def test_plan_rejects_duplicate_section_ids():
    with pytest.raises(ValidationError):
        LongImagePlan(
            input_fingerprint="hash",
            scene="tutorial",
            sections=[{"section_id": "s", "role": "hero"}, {"section_id": "s", "role": "closing"}],
        )


def test_usage_deduplicates_planning_and_preserves_unknown_counts():
    usage = LongImageUsage(
        execution={"vision": 2, "image": 1}, linked_planning={"p": {"vision": 3}}
    )
    assert usage.display_total == {"vision": 5, "image": 1}
    usage.linked_planning["p"] = {"vision": 3}
    assert LongImageUsage.model_validate(usage.model_dump()).display_total == {
        "vision": 5,
        "image": 1,
    }
    usage.linked_planning["unknown"] = None
    assert LongImageUsage.model_validate(usage.model_dump()).display_total is None
