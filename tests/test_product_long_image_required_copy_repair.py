"""Preserve user copy through harmless planner edits and bounded correction."""

from copy import deepcopy

import pytest

from image_agent.config import AgentConfig
from image_agent.errors import ProviderError
from image_agent.pipeline import run_pipeline
from image_agent.product_long_image import validate_product_long_image_plan
from image_agent.product_long_image_models import displayed_copy
from image_agent.vision import VisionClient
from tests.test_product_long_image_pipeline import long_setup
from tests.test_product_long_image_planning import authored_plan, planning_context
from tests.test_vision import ScriptTransport


@pytest.mark.parametrize("field", ["title", "body"])
@pytest.mark.parametrize("draft", ["  原文99.9%\n保留。  ", "原文99.9%\n保留"])
async def test_planning_restores_exact_user_copy_after_boundary_edits(request_data, field, draft):
    request, target, context = await planning_context(request_data)
    request.long_image.required_copy = ["  原文99.9%\n保留  "]
    plan = authored_plan(request, target, context)
    if field == "title":
        plan.sections[0].copy_items.pop()
        plan.sections[0].title = draft
    else:
        plan.sections[0].copy_items[-1].text = draft
    result = validate_product_long_image_plan(request, target, context, plan)
    assert "  原文99.9%\n保留  " in displayed_copy(result.sections[0])
    assert draft not in displayed_copy(result.sections[0])
    assert request.long_image.required_copy == ["  原文99.9%\n保留  "]


@pytest.mark.parametrize("draft", ["原文98.9%\n保留。", "原文99.9%\n保留，永久有效", "保留。"])
async def test_boundary_repair_cannot_accept_changed_words_numbers_or_added_claims(
    request_data, draft
):
    request, target, context = await planning_context(request_data)
    request.long_image.required_copy = ["原文99.9%\n保留"]
    plan = authored_plan(request, target, context)
    plan.sections[0].copy_items[-1].text = draft
    with pytest.raises(ProviderError, match="required copy missing or changed"):
        validate_product_long_image_plan(request, target, context, plan)


async def test_repair_does_not_overwrite_another_exact_required_string(request_data):
    request, target, context = await planning_context(request_data)
    request.long_image.required_copy = ["立即体验", "立即体验。"]
    plan = authored_plan(request, target, context)
    plan.sections[0].copy_items.pop(-2)
    with pytest.raises(ProviderError, match="required copy missing or changed"):
        validate_product_long_image_plan(request, target, context, plan)
    assert plan.sections[0].copy_items[-1].text == "立即体验。"


async def test_long_planning_can_recover_after_two_invalid_drafts(request_data):
    request, target, context = await planning_context(request_data)
    valid = authored_plan(request, target, context).model_dump(mode="json")
    invalid = deepcopy(valid)
    invalid["sections"][0]["copy_items"].pop()
    transport = ScriptTransport([invalid, invalid, valid])
    plan = await VisionClient(
        transport, AgentConfig(model_completion_attempts=3)
    ).plan_product_long_image(request, target, context)
    assert request.long_image.required_copy[0] in displayed_copy(plan.sections[0])
    assert len(transport.calls) == 3
    correction = transport.calls[1][1]["messages"][0]["content"][0]["text"]
    assert "Missing exact strings:" in correction


async def test_long_planning_stops_at_configured_attempt_limit(request_data):
    request, target, context = await planning_context(request_data)
    invalid = authored_plan(request, target, context).model_dump(mode="json")
    invalid["sections"][0]["copy_items"].pop()
    transport = ScriptTransport([invalid] * 5)
    with pytest.raises(ProviderError, match="attempt=4/4"):
        await VisionClient(
            transport, AgentConfig(model_completion_attempts=4)
        ).plan_product_long_image(request, target, context)
    assert len(transport.calls) == 4


async def test_added_period_does_not_prevent_long_image_generation(request_data):
    request, dependencies, vision, generator = long_setup(request_data)
    original_planner = vision.plan_product_long_image

    async def planner_with_added_period(request, target, context):
        plan = await original_planner(request, target, context)
        plan.sections[0].copy_items[-1].text = "  原文99.9%\n保留。  "
        return plan

    vision.plan_product_long_image = planner_with_added_period
    result = await run_pipeline(request, AgentConfig(), dependencies=dependencies)
    assert result.status == "succeeded"
    assert len(generator.calls) == 6
    assert "\n  原文99.9%\n保留  \n" in generator.calls[0]["prompt"]
    assert "保留。" not in generator.calls[0]["prompt"]
