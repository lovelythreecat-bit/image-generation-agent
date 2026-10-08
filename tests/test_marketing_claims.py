import pytest

from image_agent.config import AgentConfig
from image_agent.models import CreationRequest, MaterialAnalysis
from image_agent.prompt import build_prompt, build_targets
from image_agent.selection import compile_element_plan, finalize_selection, resolve_selection
from tests.fakes import analysis_data
from tests.test_repair_semantics import fact_data
from tests.test_selection import context


@pytest.mark.parametrize(
    "claim", ["全国销量第一，100%纯棉，保温999小时", "官方认证冠军杯", "一口就爱上"]
)
def test_user_claims_reach_generation_without_local_rejection(request_data, claim):
    from image_agent.compliance import apply_compliance

    request = CreationRequest(**(request_data | {"creative_brief": claim}))
    ctx = context(request)
    target = build_targets(request, "product_only")[0]
    plan = compile_element_plan(request, target, ctx, AgentConfig())
    result = apply_compliance(request, target, build_prompt(request, target, ctx, plan))
    assert claim in result.prompt
    assert not result.blocked
    assert not result.warnings
    assert "Do not fact-check user-provided claims" in result.prompt


def test_material_marketing_hints_are_passed_verbatim(request_data):
    request_data["materials"][0].update(
        subject_hint="官方认证冠军杯", element_hints=["48小时保温", "全网第一"]
    )
    request = CreationRequest(**request_data)
    ctx = context(request)
    target = build_targets(request, "product_only")[0]
    plan = compile_element_plan(request, target, ctx, AgentConfig())
    prompt = build_prompt(request, target, ctx, plan)
    for text in ["官方认证冠军杯", "48小时保温", "全网第一"]:
        assert text in prompt


def test_user_input_claim_does_not_need_evidence_or_confidence(request_data):
    data = fact_data(verifiability="unverified", source_type="user_input")
    data["facts"][-1]["confidence"] = 0.1
    data["issues"] = [
        dict(
            issue_id="proof",
            code="missing_evidence",
            resolution="reanalyze",
            message="请提供证明",
            fact_ids=["performance"],
            subject_ids=["s1"],
        )
    ]
    analysis = MaterialAnalysis(**data)
    request = CreationRequest(**request_data)
    assert finalize_selection(resolve_selection(request, analysis)).status == "ready"
    ctx = context(request, data)
    target = build_targets(request, "product_only")[0]
    plan = compile_element_plan(request, target, ctx, AgentConfig())
    assert "performance" not in plan.required_fact_ids
    assert not plan.issues


def marketing_constraint_context(request_data):
    claim = "全国销量第一"
    data = analysis_data()
    data["intent"]["constraints"] = [
        dict(
            constraint_id="copy",
            kind="marketing_claim",
            subject_ids=[],
            element_ids=[],
            instruction="在画面写上：" + claim,
            source="brief",
            source_quote=claim,
            source_material_id=None,
            priority="required",
        )
    ]
    request = CreationRequest(**(request_data | {"creative_brief": "在画面写上：" + claim}))
    ctx = context(request, data)
    target = build_targets(request, "product_only")[0]
    plan = compile_element_plan(request, target, ctx, AgentConfig())
    return request, ctx, target, plan


def test_requested_marketing_copy_is_checked_for_presence_not_truth(request_data):
    from image_agent.models import PixelChecks
    from image_agent.prompt import build_repair_prompt
    from image_agent.quality import evaluate_quality
    from tests.test_repair_semantics import report_for

    request, ctx, target, plan = marketing_constraint_context(request_data)
    report = report_for(plan)
    pixels = PixelChecks(passed=True, width=800, height=800, reason="ok")
    assert evaluate_quality(report, pixels, target, plan).passed
    report.constraint_checks[0].satisfied = False
    report.constraint_checks[0].reason = "Requested marketing text is missing"
    quality = evaluate_quality(report, pixels, target, plan)
    assert not quality.passed
    repair = build_repair_prompt(request, target, ctx, plan, quality)
    assert '"id": "copy"' in repair
    assert "Restore the user-requested marketing copy" in repair
    assert "全国销量第一" in repair


async def test_low_confidence_functional_claim_is_not_a_discovery_issue(request_data):
    from image_agent.vision import VisionClient
    from tests.test_vision import ScriptTransport, materials

    data = fact_data()
    data["facts"][-1]["confidence"] = 0.1
    result = await VisionClient(ScriptTransport([data]), AgentConfig()).analyze_materials(
        CreationRequest(**request_data), materials()
    )
    assert result.status == "ready"
    assert not result.issues


async def test_claims_survive_generation_and_quality_repair(request_data):
    from image_agent.pipeline import run_pipeline
    from tests.test_pipeline import setup

    claim = "保温999小时，全网销量第一"
    request, deps, vision, generator = setup(
        request_data | {"creative_brief": claim},
        scores={"main": [{"output_intent": 74}, {}]},
        discovery=fact_data(),
    )
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert "evidence" not in vision.calls
    assert len(generator.calls) == 2
    assert not result.issues
    for call in generator.calls:
        assert claim in call["prompt"]
        assert "Do not fact-check user-provided claims" in call["prompt"]
        assert "Do not add structures or promotional text" not in call["prompt"]


@pytest.mark.parametrize("source_type", ["visual", "product_label"])
def test_marketing_policy_keeps_visible_identity_evidence_requirements(request_data, source_type):
    data = fact_data(verifiability="unverified", source_type=source_type)
    request = CreationRequest(**request_data)
    result = finalize_selection(resolve_selection(request, MaterialAnalysis(**data)))
    assert result.status == "needs_input"
    assert any(i.code == "missing_evidence" for i in result.issues)


async def test_audit_receives_original_claim_and_does_not_check_its_truth(request_data):
    from image_agent.vision import VisionClient
    from tests.fakes import FakeVision
    from tests.test_vision import ScriptTransport

    claim = "保温999小时，全网销量第一"
    request = CreationRequest(**(request_data | {"creative_brief": claim}))
    ctx = context(request)
    ctx.materials = tuple(m.model_copy(update={"data": b"unused"}) for m in ctx.materials)
    target = build_targets(request, "product_only")[0]
    plan = compile_element_plan(request, target, ctx, AgentConfig())
    response = await FakeVision().audit_image(request, target, ctx, plan, b"unused")
    transport = ScriptTransport([response.model_dump()])
    client = VisionClient(transport, AgentConfig())
    from tests.test_images import picture

    ctx.materials = tuple(m.model_copy(update={"data": picture()}) for m in ctx.materials)
    await client.audit_image(request, target, ctx, plan, picture())
    instruction = transport.calls[0][1]["messages"][0]["content"][0]["text"]
    assert claim in instruction
    assert "Do not fact-check user-provided claims" in instruction
    assert "do not lower platform_compliance" in instruction


def test_style_hint_is_part_of_discovery_fingerprint(request_data):
    from image_agent.selection import analysis_fingerprint
    from tests.test_vision import materials

    request = CreationRequest(**request_data)
    changed = request.model_copy(update={"style_hint": "在画面写上全国销量第一"})
    assert analysis_fingerprint(request, materials()) != analysis_fingerprint(changed, materials())


async def test_style_hint_can_be_the_source_of_requested_marketing_copy(request_data):
    from image_agent.vision import VisionClient
    from tests.test_vision import ScriptTransport, materials

    request, ctx, _, _ = marketing_constraint_context(request_data)
    text = request.creative_brief
    request = request.model_copy(update={"creative_brief": None, "style_hint": text})
    data = ctx.analysis.model_dump()
    data["intent"]["constraints"][0]["source"] = "style_hint"
    result = await VisionClient(ScriptTransport([data, data]), AgentConfig()).analyze_materials(
        request, materials()
    )
    assert result.intent.constraints[0].source_quote in text
    result.validate_intent_source(request)


async def test_raw_claim_reaches_focused_and_detail_set_audits(request_data):
    from image_agent.pipeline import prepare_context
    from image_agent.vision import VisionClient
    from tests.test_images import picture
    from tests.test_pipeline import setup
    from tests.test_vision import ScriptTransport

    claim = "全网销量第一"
    request, deps, _, _ = setup(
        request_data | {"style_hint": claim, "output_types": ["detail_page"]}
    )
    ctx, _ = await prepare_context(request, AgentConfig(), dependencies=deps)
    targets = build_targets(request, "product_only")
    plans = [compile_element_plan(request, t, ctx, AgentConfig()) for t in targets]
    transport = ScriptTransport(
        [
            {"subject_checks": [dict(subject_id="s1", score=90, same_product=True, reason="same")]},
            dict(
                platform="taobao",
                passed=True,
                distinctiveness=90,
                role_coverage=90,
                issues=[],
                reason="ok",
            ),
        ]
    )
    client = VisionClient(transport, AgentConfig())
    await client.review_product(ctx, plans[0], ["s1"], picture())
    await client.audit_detail_set("taobao", ctx, plans, [picture()] * 3)
    for _, payload in transport.calls:
        instruction = payload["messages"][0]["content"][0]["text"]
        assert claim in instruction
        assert "Do not fact-check user-provided claims" in instruction
