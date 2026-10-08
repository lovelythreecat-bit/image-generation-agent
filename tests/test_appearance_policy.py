"""Appearance rules at model boundaries; local quality still uses the existing contract."""

import pytest

from image_agent.compliance import apply_compliance
from image_agent.config import AgentConfig
from image_agent.models import CreationRequest, PixelChecks
from image_agent.platforms import audit_instruction, generation_instruction
from image_agent.prompt import build_prompt, build_repair_prompt, build_stage_prompt, build_targets
from image_agent.quality import evaluate_quality
from image_agent.selection import compile_element_plan, resolve_selection
from image_agent.vision import VisionClient
from tests.fakes import analysis_data
from tests.test_evidence_protocol import evidence_response
from tests.test_images import picture
from tests.test_repair_semantics import report_for
from tests.test_selection import context
from tests.test_vision import ScriptTransport, materials


@pytest.mark.parametrize("mode", ["standard", "strict", "staged"])
@pytest.mark.parametrize("platform", ["taobao", "temu", "shopee", "shein"])
def test_detail_generation_falls_back_to_supported_views_in_every_mode(
    request_data, mode, platform
):
    request = CreationRequest(
        **(
            request_data
            | {
                "platforms": [platform],
                "output_types": ["detail_page"],
                "creative_brief": "展示背面，保温999小时，背景加花朵装饰",
            }
        )
    )
    ctx = context(request)
    for target in build_targets(request, "model_wear"):
        plan = compile_element_plan(request, target, ctx, AgentConfig())
        result = apply_compliance(request, target, build_prompt(request, target, ctx, plan, mode))
        assert not result.blocked
        assert request.creative_brief in result.prompt
        assert "sole authority for physical product appearance" in result.prompt
        assert (
            "If only a front view is supported, keep a front-facing product view" in result.prompt
        )
        assert (
            "Alternate views are allowed when supported by source-product images" in result.prompt
        )
        assert "Do not invent detail to sharpen an unclear source region" in result.prompt
        assert "small perspective changes" in result.prompt
        assert "natural wearing folds" in result.prompt
        assert "Do not fact-check user-provided claims" in result.prompt
        assert "from a different camera angle" not in result.prompt
        assert "Do not repeat a front catalog portrait" not in result.prompt
        assert "Across the detail set, cover the front, back" not in result.prompt


@pytest.mark.parametrize("platform", ["shein", "temu"])
def test_platform_detail_rules_require_only_source_supported_angles(platform):
    for instruction in (
        generation_instruction(platform, "detail_page"),
        audit_instruction(platform, "detail_page"),
    ):
        assert "source-supported" in instruction
        assert "show accurate alternate angles" not in instruction
        assert "accurately shows alternate angles" not in instruction


def test_stage_slots_allow_supported_side_or_rear_views(request_data):
    request = CreationRequest(**request_data)
    target = build_targets(request, "model_wear")[0]
    ctx = context(request)
    plan = compile_element_plan(request, target, ctx, AgentConfig())
    prompt = build_stage_prompt(request, target, plan, "final composition")
    assert "Keep product-slot orientation flexible" in prompt
    assert "source-supported product view" in prompt
    assert "compatible with a front-facing presentation" not in prompt


@pytest.mark.parametrize("stage", ["discover", "evidence", "garments", "audit", "review", "set"])
async def test_vision_boundaries_keep_appearance_separate_from_copy(request_data, stage):
    request = CreationRequest(
        **(request_data | {"creative_brief": "保温999小时", "output_types": ["detail_page"]})
    )
    ctx = context(request)
    ctx.materials = materials()
    plans = [
        compile_element_plan(request, target, ctx, AgentConfig())
        for target in build_targets(request, "product_only")
    ]
    audit = report_for(plans[0])
    responses = {
        "discover": analysis_data(),
        "evidence": evidence_response(),
        "garments": {"items": []},
        "audit": audit.model_dump(),
        "review": {"subject_checks": [s.model_dump() for s in audit.subject_checks]},
        "set": dict(
            platform="taobao",
            passed=True,
            distinctiveness=90,
            role_coverage=90,
            issues=[],
            reason="ok",
        ),
    }
    transport = ScriptTransport([responses[stage]])
    client = VisionClient(transport, AgentConfig())
    if stage == "discover":
        await client.analyze_materials(request, ctx.materials)
    elif stage == "evidence":
        await client.validate_evidence(
            request, ctx.materials, ctx.analysis, resolve_selection(request, ctx.analysis)
        )
    elif stage == "garments":
        await client.extract_garments(request, ctx.materials, "s1")
    elif stage == "audit":
        await client.audit_image(
            request, build_targets(request, "product_only")[0], ctx, plans[0], picture()
        )
    elif stage == "review":
        await client.review_product(ctx, plans[0], ["s1"], picture())
    else:
        await client.audit_detail_set("taobao", ctx, plans, [picture()] * 3)
    instruction = transport.calls[0][1]["messages"][0]["content"][0]["text"]
    assert "sole authority for physical product appearance" in instruction
    assert "marketing wording never authorizes a physical product change" in instruction
    if stage in ("discover", "audit", "review", "set"):
        assert "If only a front view is supported, keep a front-facing product view" in instruction
    if stage in ("audit", "review"):
        assert "including additions absent from the frozen fact list" in instruction
        assert "same_product=false" in instruction
        assert "Unclear regions alone are not grounds for rejection" in instruction
        assert "must not lower output_intent or platform_compliance" in instruction
        assert "Do not fact-check user-provided claims" in instruction


async def test_reported_invented_detail_uses_existing_repair_without_rechecking_claims(
    request_data,
):
    from image_agent.pipeline import run_pipeline
    from tests.test_pipeline import setup

    request, deps, vision, generator = setup(
        request_data | {"creative_brief": "保温999小时，花朵装饰"},
        scores={"main": [{"identity": 95, "same": False}, {}]},
    )
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert result.assets[0].generation_mode == "staged"
    assert "evidence" not in vision.calls
    repair = generator.calls[-1]["prompt"]
    assert "Remove clearly unsupported added product details" in repair
    assert "return to a source-supported view" in repair
    assert "保温999小时" in repair
    assert "花朵装饰" in repair


def test_repair_retains_audited_addition_reason_and_uses_original_view(request_data):
    request = CreationRequest(**request_data)
    ctx = context(request)
    target = build_targets(request, "product_only")[0]
    plan = compile_element_plan(request, target, ctx, AgentConfig())
    audit = report_for(plan, subject_score=95)
    audit.subject_checks[0].same_product = False
    audit.subject_checks[0].reason = "Invented rear zipper; only the front is supported."
    quality = evaluate_quality(
        audit, PixelChecks(passed=True, width=800, height=800, reason="ok"), target, plan
    )
    assert not quality.passed
    repair = build_repair_prompt(request, target, ctx, plan, quality)
    assert "Invented rear zipper" in repair
    assert "Remove clearly unsupported added product details" in repair
    assert "return to a source-supported view" in repair
