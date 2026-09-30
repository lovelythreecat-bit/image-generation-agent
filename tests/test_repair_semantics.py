import pytest

from image_agent.config import AgentConfig
from image_agent.models import (
    Asset,
    CreationRequest,
    CreationResult,
    Fact,
    GeneratedImageAudit,
    PixelChecks,
    ProductInputAudit,
)
from image_agent.prompt import build_targets
from image_agent.quality import evaluate_quality
from image_agent.selection import compile_element_plan, finalize_selection, resolve_selection
from tests.fakes import analysis_data
from tests.test_selection import context


@pytest.mark.parametrize(
    "status",
    [
        "pending_audit",
        "audit_error",
        "quality_failed",
        "needs_input",
        "budget_exhausted",
        "accepted",
        "generation_uncertain",
    ],
)
def test_candidate_states_are_serializable_without_claiming_success(status):
    asset = Asset(
        asset_id="a1",
        platform="taobao",
        output_type="main_image",
        model="test",
        status=status,
        candidates=[{"candidate_id": "c1", "status": status}],
        stop_reason="retained",
    )
    result = CreationResult(
        status=status,
        input_check=ProductInputAudit(matches=True, observed_product="cup", reason="ok"),
        assets=[asset],
        run_dir="run-1",
        usage={"generation": 1},
    )
    assert result.schema_version == "2.0"
    assert result.model_dump()["assets"][0]["candidates"][0]["status"] == status
    assert CreationResult.model_validate(result.model_dump()).usage == {"generation": 1}


def test_legacy_appearance_requirements_keep_compatibility_defaults():
    fact = Fact(
        fact_id="f1",
        subject_id="s1",
        description="promised thermal performance",
        evidence=[{"material_id": "m1", "observation": "text claims hours of insulation"}],
        confidence=0.99,
    )
    assert getattr(fact, "verifiability", None) == "visible_appearance"
    assert getattr(fact.evidence[0], "source_type", None) == "unknown"


def test_historical_result_version_can_still_be_read():
    result = CreationResult(
        schema_version="1.0",
        status="failed",
        input_check=ProductInputAudit(matches=False, observed_product="", reason="historical"),
    )
    assert result.schema_version == "1.0"


def fact_data(*, verifiability="functional_claim", source_type="promotional_text"):
    data = analysis_data()
    data["facts"].append(
        dict(
            fact_id="performance",
            subject_id="s1",
            description="keeps contents cold for 48 hours",
            evidence=[
                dict(
                    material_id="m1",
                    observation="advertising headline asserts 48 hours",
                    source_type=source_type,
                )
            ],
            confidence=0.99,
            verifiability=verifiability,
        )
    )
    data["elements"].append(
        dict(
            element_id="benefit",
            subject_id="s1",
            kind="detail",
            description="thermal performance",
            fact_ids=["performance"],
        )
    )
    data["subjects"][0]["identity_fact_ids"].append("performance")
    data["intent"]["proposal"]["required_element_ids"].append("benefit")
    return data


def compile_data(request_data, data):
    request = CreationRequest(**request_data)
    ctx = context(request, data)
    target = build_targets(request, "product_only")[0]
    return request, ctx, target, compile_element_plan(request, target, ctx, AgentConfig())


def test_promotion_function_is_not_a_required_visible_structure(request_data):
    from image_agent.vision import selected_fact_ids

    request, ctx, target, plan = compile_data(request_data, fact_data())
    rows = {r.target_id: r for r in plan.requirements}
    assert rows["performance"].applicability == "not_applicable"
    assert rows["benefit"].applicability == "not_applicable"
    assert "performance" not in plan.required_fact_ids
    assert "performance" not in selected_fact_ids(ctx.analysis, ctx.selection)
    assert any(
        i.code == "unverifiable_claim" and i.fact_ids == ["performance"] for i in plan.issues
    )


@pytest.mark.parametrize("source_type", ["visual", "product_label"])
def test_verified_appearance_and_physical_product_print_remain_required(request_data, source_type):
    data = fact_data(verifiability="visible_appearance", source_type=source_type)
    data["facts"][-1]["description"] = "printed original label 48 on the cup body"
    _, _, _, plan = compile_data(request_data, data)
    rows = {r.target_id: r for r in plan.requirements}
    assert rows["performance"].applicability == "must_show"
    assert rows["benefit"].applicability == "must_show"


@pytest.mark.parametrize(
    "verifiability,source_type",
    [
        ("unverified", "unknown"),
        ("visible_appearance", "inference"),
        ("visible_appearance", "promotional_text"),
    ],
)
def test_unverified_required_appearance_needs_evidence_before_generation(
    request_data, verifiability, source_type
):
    from image_agent.models import MaterialAnalysis

    request = CreationRequest(**request_data)
    draft = resolve_selection(
        request, MaterialAnalysis(**fact_data(verifiability=verifiability, source_type=source_type))
    )
    result = finalize_selection(draft)
    assert result.status == "needs_input"
    assert any(i.code == "missing_evidence" and "performance" in i.fact_ids for i in result.issues)


def report_for(plan, *, subject_score=90, visual_quality=90):
    rows = dict(subject_checks=[], fact_checks=[], element_checks=[], constraint_checks=[])
    for requirement in plan.requirements:
        kind = requirement.target_kind
        if kind == "subject":
            row = dict(
                subject_id=requirement.target_id,
                score=subject_score,
                same_product=True,
                reason="rim shape changed" if subject_score < 85 else "correct cup identity",
            )
        elif kind == "constraint":
            row = dict(constraint_id=requirement.target_id, satisfied=True, reason="correct")
        else:
            applicable = requirement.applicability != "not_applicable"
            row = {
                kind + "_id": requirement.target_id,
                "presence": "present" if applicable else "not_applicable",
                "fidelity_score": 95 if applicable else None,
                "reason": "source supported" if applicable else "claim not visually verifiable",
            }
        rows[kind + "_checks"].append(row)
    return GeneratedImageAudit(
        **rows,
        visual_quality=visual_quality,
        platform_compliance=90,
        garment_fusion=0,
        model_preference=0,
        output_intent=90,
        passed=False,
        reason="blurred edges" if visual_quality < 70 else "rim shape changed",
    )


def test_quality_accepts_no_visual_proof_of_promotional_performance(request_data):
    _, _, target, plan = compile_data(request_data, fact_data())
    audit = report_for(plan)
    audit.fact_checks = [
        check.model_copy(update={"presence": "absent", "fidelity_score": None})
        if check.fact_id == "performance"
        else check
        for check in audit.fact_checks
    ]
    quality = evaluate_quality(
        audit, PixelChecks(passed=True, width=800, height=800, reason="ok"), target, plan
    )
    assert quality.passed


def test_repair_prompt_targets_failed_identity_and_preserves_correct_parts(request_data):
    from image_agent import prompt

    request, ctx, target, plan = compile_data(request_data, analysis_data())
    quality = evaluate_quality(
        report_for(plan, subject_score=60),
        PixelChecks(passed=True, width=800, height=800, reason="ok"),
        target,
        plan,
    )
    builder = getattr(prompt, "build_repair_prompt", None)
    assert callable(builder)
    repair = builder(request, target, ctx, plan, quality, previous_prompt="original composition")
    assert "rim shape changed" in repair
    assert "s1" in repair and "60" in repair
    assert "original source materials" in repair
    assert "previous candidate" in repair and "only to locate" in repair
    assert "Preserve already-correct" in repair
    assert "original composition" in repair
    assert "garment_fusion" not in repair
    visual = evaluate_quality(
        report_for(plan, visual_quality=40),
        PixelChecks(passed=True, width=800, height=800, reason="ok"),
        target,
        plan,
    )
    other = builder(request, target, ctx, plan, visual)
    assert other != repair and "blurred edges" in other and "visual_quality" in other


def test_nonapplicable_scores_do_not_select_garment_repair():
    from image_agent.quality import choose_retry
    from tests.test_quality import audit, plan, target

    quality = evaluate_quality(
        audit().model_copy(update={"visual_quality": 40}),
        PixelChecks(passed=True, width=800, height=800, reason="ok"),
        target(),
        plan(),
    )
    quality.garment_fusion = 0
    quality.reason = "unrelated clothing fusion not applicable"
    assert choose_retry(target(), quality, plan()) == "strict"


@pytest.mark.parametrize("required", [True, False])
def test_catalog_retains_product_native_artwork_style(request_data, required):
    data = analysis_data()
    data["facts"][0]["evidence"][0]["source_type"] = "product_label"
    data["facts"][0]["description"] = "original illustrated character printed on cup"
    data["elements"][0]["kind"] = "style"
    data["elements"][0]["description"] = "original product illustration"
    if required:
        data["intent"]["proposal"]["required_element_ids"] = ["e1"]
        data["intent"]["proposal"]["preferred_element_ids"] = []
    request = CreationRequest(
        **(request_data | {"platforms": ["pinduoduo"], "output_types": ["pdd_white_background"]})
    )
    ctx = context(request, data)
    target = build_targets(request, "product_only")[0]
    plan = compile_element_plan(request, target, ctx, AgentConfig())
    assert "e1" not in plan.excluded_element_ids
    assert any(
        r.target_id == "e1" and r.applicability != "not_applicable" for r in plan.requirements
    )


def test_subject_identity_is_not_exempt_when_all_claims_are_functional(request_data):
    data = fact_data()
    data["facts"][0]["verifiability"] = "functional_claim"
    _, _, target, plan = compile_data(request_data, data)
    audit = report_for(plan, subject_score=40)
    quality = evaluate_quality(
        audit, PixelChecks(passed=True, width=800, height=800, reason="ok"), target, plan
    )
    assert not quality.passed
    assert any("s1" in check for check in quality.failed_checks)


def test_claim_only_constraint_does_not_reintroduce_visual_requirement(request_data):
    data = fact_data()
    data["intent"]["constraints"] = [
        dict(
            constraint_id="thermal",
            kind="appearance",
            subject_ids=["s1"],
            element_ids=["benefit"],
            instruction="highlight insulation",
            source="brief",
            source_quote="highlight insulation",
            source_material_id=None,
            priority="required",
        )
    ]
    _, _, _, plan = compile_data(request_data | {"creative_brief": "highlight insulation"}, data)
    assert (
        next(r for r in plan.requirements if r.target_id == "thermal").applicability
        == "not_applicable"
    )


def test_repair_does_not_accumulate_previous_feedback(request_data):
    from image_agent.prompt import REPAIR_MARKER, build_repair_prompt

    request, ctx, target, plan = compile_data(request_data, analysis_data())
    pixels = PixelChecks(passed=True, width=800, height=800, reason="ok")
    old = evaluate_quality(report_for(plan, subject_score=50), pixels, target, plan)
    previous = build_repair_prompt(request, target, ctx, plan, old)
    current = evaluate_quality(report_for(plan, visual_quality=40), pixels, target, plan)
    repair = build_repair_prompt(request, target, ctx, plan, current, previous_prompt=previous)
    assert repair.count(REPAIR_MARKER) == 1
    assert "rim shape changed" not in repair
    assert "blurred edges" in repair


def test_repair_rebuilds_bindings_from_current_plan(request_data):
    import json

    from image_agent.prompt import STRUCTURED_MARKER, build_prompt, build_repair_prompt

    request, ctx, target, plan = compile_data(request_data, analysis_data())
    previous = build_prompt(request, target, ctx, plan)
    plan.reference_bindings[0].index = 2
    quality = evaluate_quality(
        report_for(plan, subject_score=50),
        PixelChecks(passed=True, width=800, height=800, reason="ok"),
        target,
        plan,
    )
    repair = build_repair_prompt(request, target, ctx, plan, quality, previous_prompt=previous)
    structured, _ = json.JSONDecoder().raw_decode(repair.split(STRUCTURED_MARKER, 1)[1])
    assert structured["reference_bindings"][0]["index"] == 2


async def test_repair_restores_optional_reference_after_staged_fusion(request_data):
    from image_agent.pipeline import run_pipeline
    from tests.test_pipeline import setup

    data = analysis_data()
    data["materials"].append(
        dict(material_id="m2", sha256="hash", observed_role="style", summary="garden")
    )
    data["facts"].append(
        dict(
            fact_id="f2",
            subject_id=None,
            description="garden",
            verifiability="visible_appearance",
            evidence=[dict(material_id="m2", observation="garden visible", source_type="visual")],
            confidence=1,
        )
    )
    data["elements"].append(
        dict(
            element_id="e2",
            kind="style",
            subject_id=None,
            description="garden style",
            fact_ids=["f2"],
        )
    )
    data["intent"]["proposal"]["preferred_element_ids"].append("e2")
    request_data["materials"].append(dict(material_id="m2", source={"data": b"x"}))
    request, deps, _, generator = setup(
        request_data, discovery=data, scores={"main": [{"identity": 70}, {"output_intent": 60}, {}]}
    )
    result = await run_pipeline(
        request, AgentConfig(generation_reference_limit=2), dependencies=deps
    )
    assert result.status == "succeeded"
    assert len(generator.calls) == 4
    assert [r.material_id for r in generator.calls[2]["references"] if r.material_id] == ["m1"]
    assert [r.material_id for r in generator.calls[3]["references"] if r.material_id] == [
        "m1",
        "m2",
    ]
    bindings = result.assets[0].reference_bindings
    restored = next(b for b in bindings if b.material_id == "m2")
    assert restored.index == 2 and restored.role == "style"
    assert restored.fact_ids == ["f2"] and restored.element_ids == ["e2"]
    assert [b.material_id or b.stage_id for b in bindings] == [
        r.material_id or r.stage_id for r in generator.calls[-1]["references"]
    ]


@pytest.mark.parametrize("focus", [["e1"], ["benefit", "e1"]])
def test_nonvisual_claims_do_not_block_detail_shot_feasibility(request_data, focus):
    data = fact_data()
    data["intent"]["focus_element_ids"] = focus
    request = CreationRequest(**(request_data | {"output_types": ["detail_page"]}))
    ctx = context(request, data)
    targets = build_targets(request, "product_only")
    assert [target.variant for target in targets] == ["scene", "feature", "closeup"]
    for target in targets:
        plan = compile_element_plan(request, target, ctx, AgentConfig())
        assert plan.focus_element_id == "e1"
        rows = {r.target_id: r for r in plan.requirements}
        assert (
            rows["benefit"].applicability == rows["performance"].applicability == "not_applicable"
        )
        assert plan.required_fact_ids == ["f1"]
        assert rows["s1"].applicability == "must_show"
        if target.variant == "closeup":
            assert rows["e1"].applicability == "must_show"


@pytest.mark.parametrize(
    "reason",
    [
        "Frontal view: change to a three-quarter angle.",
        "Angle fixed; handle cropped: widen the crop.",
    ],
)
def test_quality_keeps_original_critique_for_actionable_repair(request_data, reason):
    from image_agent.prompt import build_repair_prompt

    request, ctx, target, plan = compile_data(request_data, analysis_data())
    audit = report_for(plan).model_copy(update={"output_intent": 60, "reason": reason})
    quality = evaluate_quality(
        audit, PixelChecks(passed=True, width=800, height=800, reason="ok"), target, plan
    )
    assert getattr(quality, "model_reason", None) == reason
    assert quality.failed_checks == ["镜头意图 60<75"]
    repair = build_repair_prompt(request, target, ctx, plan, quality)
    assert reason in repair
    assert '"model_reason"' in repair
