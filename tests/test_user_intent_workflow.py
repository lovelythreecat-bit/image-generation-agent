"""User inputs survive discovery and creative copy is not a provenance exercise."""

import json

import pytest

from image_agent.config import AgentConfig
from image_agent.errors import ProviderError
from image_agent.marketing import creative_input
from image_agent.product_long_image import (
    build_product_long_image_prompt,
    validate_product_long_image_plan,
)
from image_agent.product_long_image_models import ProductLongImagePlan
from image_agent.selection import analysis_fingerprint, compile_element_plan
from image_agent.vision import VisionClient
from tests.fakes import analysis_data
from tests.test_product_long_image_planning import authored_plan, planning_context
from tests.test_vision import ScriptTransport, materials
from tests.test_vision_resilience import RawTransport


async def test_ordinary_copy_needs_no_provenance_or_internal_theme_name(request_data):
    request, target, context = await planning_context(request_data)
    data = authored_plan(request, target, context).model_dump(mode="json")
    data.pop("input_fingerprint")
    data["sections"][0]["title"] = "让创意飞起来"
    data["sections"][0]["role"] = "opening"
    data["sections"][1]["copy_items"] = [{"text": "享受属于你的游戏时光"}]
    transport = ScriptTransport([data] * 4)
    plan = await VisionClient(transport, AgentConfig()).plan_product_long_image(
        request, target, context
    )
    assert len(transport.calls) == 1
    assert plan.input_fingerprint
    assert plan.sections[1].copy_items[0].text == "享受属于你的游戏时光"
    elements = compile_element_plan(request, target, context, AgentConfig())
    prompt = build_product_long_image_prompt(
        request, target, context, elements, plan, plan.sections[0]
    )
    assert prompt.count("让创意飞起来") == 1


async def test_source_annotations_cannot_block_valid_copy(request_data):
    request, target, context = await planning_context(request_data)
    plan = authored_plan(request, target, context)
    plan.sections[0].copy_items[0].source_quote = "优化后的措辞，不是原文子串"
    plan.sections[0].copy_items[0].fact_ids = ["provider-guessed-id"]
    result = validate_product_long_image_plan(request, target, context, plan)
    assert result.sections[0].copy_items[0].text == "white ceramic cup"
    assert result.sections[0].copy_items[0].fact_ids == []
    assert "provider-guessed-id" not in str(result.source_evidence)


async def test_required_copy_as_title_is_preserved_once(request_data):
    request, target, context = await planning_context(request_data)
    plan = authored_plan(request, target, context)
    original = request.long_image.required_copy[0]
    plan.sections[0].title = original
    plan.sections[0].copy_items = plan.sections[0].copy_items[:1]
    result = validate_product_long_image_plan(request, target, context, plan)
    elements = compile_element_plan(request, target, context, AgentConfig())
    prompt = build_product_long_image_prompt(
        request, target, context, elements, result, result.sections[0]
    )
    exact_copy = prompt.split("EXACT COPY (render each complete string):\n")[1].split(
        "SELECTED PRODUCT EVIDENCE", 1
    )[0]
    assert exact_copy.count(original) == 1


async def test_missing_required_copy_still_fails(request_data):
    request, target, context = await planning_context(request_data)
    plan = authored_plan(request, target, context)
    plan.sections[0].copy_items = plan.sections[0].copy_items[:1]
    with pytest.raises(ProviderError, match="required copy missing"):
        validate_product_long_image_plan(request, target, context, plan)


async def test_product_planning_format_retry_respects_configured_two_attempts(request_data):
    request, target, context = await planning_context(request_data)
    transport = RawTransport([{"content": "invalid json"}] * 4)
    with pytest.raises(ProviderError, match="attempt=2/2"):
        await VisionClient(
            transport, AgentConfig(model_completion_attempts=2)
        ).plan_product_long_image(request, target, context)
    assert len(transport.calls) == 2


async def test_long_image_information_is_in_user_input_and_analysis_identity(request_data):
    request, _, _ = await planning_context(request_data)
    original = analysis_fingerprint(request, materials())
    inputs = creative_input(request)
    assert inputs["long_image"]["known_info"] == request.long_image.known_info
    assert inputs["long_image"]["required_copy"] == request.long_image.required_copy
    for change in [{"known_info": "用户说续航1000小时"}, {"required_copy": ["悬空飞行"]}]:
        changed = request.model_copy(deep=True)
        changed.long_image = changed.long_image.model_copy(update=change)
        assert analysis_fingerprint(changed, materials()) != original
    changed = request.model_copy(deep=True)
    changed.long_image.width = 800
    assert analysis_fingerprint(changed, materials()) == original


async def test_discovery_receives_long_inputs_without_concrete_teaching_parts(request_data):
    request, _, _ = await planning_context(request_data)
    captured = {}

    class Capture(VisionClient):
        async def _call(self, schema, instruction, images, **kwargs):
            captured["instruction"] = instruction
            return kwargs["validate"](schema(**analysis_data()))

    await Capture(None, AgentConfig()).analyze_materials(request, materials())
    instruction = captured["instruction"]
    assert "rear zipper" not in instruction
    assert request.long_image.known_info in instruction
    assert json.dumps(request.long_image.required_copy[0], ensure_ascii=False) in instruction


async def test_audit_uses_displayed_title_and_original_creative_requirements(request_data):
    request, target, context = await planning_context(request_data)
    request.creative_brief = "让商品悬空飞行，保持商品外观不变"
    request.materials[0].subject_hint = "保留原来的颜色"
    plan = authored_plan(request, target, context)
    plan.sections[0].title = "自由飞行"
    captured = {}

    class Capture(VisionClient):
        async def _call(self, schema, instruction, images, **kwargs):
            captured["instruction"] = instruction
            return schema(
                passed=True, text_matches=True, no_invented_claims=True, layout_readable=True
            )

    await Capture(None, AgentConfig()).audit_product_long_image_copy(
        request, plan.sections[0], plan, materials()[0].data
    )
    instruction = captured["instruction"]
    assert '"displayed_copy": ["自由飞行"' in instruction
    assert "保留原来的颜色" in instruction
    assert "physically impossible" in instruction
    assert "Check each planned text itself against its source_quote" not in instruction


@pytest.mark.parametrize("field", ["visual_prompt", "style_direction"])
async def test_blank_generation_direction_is_rejected(request_data, field):
    request, target, context = await planning_context(request_data)
    data = authored_plan(request, target, context).model_dump(mode="json")
    if field == "visual_prompt":
        data["sections"][0][field] = " \n "
    else:
        data[field] = " \n "
    with pytest.raises(ValueError, match="must not be blank"):
        ProductLongImagePlan.model_validate(data)


async def test_long_input_constraints_keep_original_source_during_evidence_review(request_data):
    request, _, context = await planning_context(request_data)
    captured = {}

    class Capture(VisionClient):
        async def _call(self, schema, instruction, images, **kwargs):
            captured["instruction"] = instruction
            return schema(
                outcome="verified",
                subject_checks=[],
                fact_checks=[],
                intent_valid=True,
                issue_resolutions=[],
            )

    from image_agent.selection import resolve_selection

    await Capture(None, AgentConfig()).validate_evidence(
        request, materials(), context.analysis, resolve_selection(request, context.analysis)
    )
    assert request.long_image.known_info in captured["instruction"]
    assert (
        json.dumps(request.long_image.required_copy[0], ensure_ascii=False)
        in captured["instruction"]
    )
