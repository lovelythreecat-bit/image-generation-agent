"""Approved tolerance belongs at provider boundaries, never public contracts."""

from copy import deepcopy

import pytest
from pydantic import ValidationError

from image_agent.config import AgentConfig
from image_agent.contracts import result_to_bundle
from image_agent.errors import ProviderError
from image_agent.models import CreationRequest, MaterialAnalysis
from image_agent.pipeline import prepare_analysis, prepare_context, run_pipeline
from image_agent.quality import evaluate_quality
from image_agent.selection import compile_element_plan, resolve_selection
from image_agent.vision import VisionClient, _parse_vision_response, selected_fact_ids
from tests.fakes import analysis_data
from tests.test_images import picture
from tests.test_pipeline import setup
from tests.test_quality import audit, plan, target
from tests.test_selection import context
from tests.test_vision_resilience import RawTransport, audit_data


def response(value):
    return {"choices": [{"message": {"content": value}}]}


def test_actual_failed_run_surplus_functional_fact_is_discarded_without_mutation():
    from image_agent.models import EvidenceValidation
    from image_agent.quality import normalize_audit_checks

    data = analysis_data()
    data["facts"].append(
        dict(
            fact_id="f2",
            verifiability="functional_claim",
            subject_id="s1",
            description="40 cm",
            confidence=1,
            evidence=[dict(material_id="m1", observation="user claim", source_type="user_input")],
        )
    )
    analysis = MaterialAnalysis(**data)
    assert [(f.fact_id, f.verifiability) for f in analysis.facts] == [
        ("f1", "visible_appearance"),
        ("f2", "functional_claim"),
    ]
    expected = selected_fact_ids(analysis, analysis.intent.proposal)
    assert expected == ["f1"]
    value = EvidenceValidation(
        outcome="verified",
        subject_checks=[],
        intent_valid=True,
        issue_resolutions=[],
        fact_checks=[
            dict(fact_id=fid, presence="present", fidelity_score=90, reason="ok")
            for fid in ["f1", "f2", "f1"]
        ],
    )
    normalized = normalize_audit_checks(value, {"fact_id": expected})
    assert [c.fact_id for c in normalized.fact_checks] == ["f1"]
    assert len(value.fact_checks) == 3


def test_missing_and_conflicting_checks_remain_protocol_failures():
    from image_agent.quality import normalize_audit_checks

    value = audit()
    for checks in [
        [],
        [
            value.subject_checks[0],
            value.subject_checks[0].model_copy(update={"same_product": False}),
        ],
    ]:
        with pytest.raises(ProviderError):
            normalize_audit_checks(
                value.model_copy(update={"subject_checks": checks}), {"subject_id": ["s1"]}
            )


@pytest.mark.parametrize("score", ["90", 90.0])
def test_safe_provider_coercion_does_not_change_strict_models(score):
    from image_agent.models import GeneratedImageAudit

    value = audit_data() | {"visual_quality": score, "passed": "TRUE", "debug": "ignored"}
    assert _parse_vision_response(response(value), GeneratedImageAudit).visual_quality == 90
    with pytest.raises(ValidationError):
        GeneratedImageAudit(**value)


@pytest.mark.parametrize("score", ["90.5", 90.5, True, None, "excellent"])
def test_no_fabricated_provider_score(score):
    from image_agent.models import GeneratedImageAudit

    with pytest.raises(ProviderError):
        _parse_vision_response(
            response(audit_data() | {"visual_quality": score}), GeneratedImageAudit
        )


def test_nonapplicable_scores_never_fabricate_a_missing_required_reason():
    from image_agent.models import GeneratedImageAudit

    value = audit_data()
    for field in ["reason", "garment_fusion", "model_preference"]:
        value.pop(field)
    with pytest.raises(ProviderError, match="reason"):
        _parse_vision_response(
            response(value),
            GeneratedImageAudit,
            nonapplicable_fields=("garment_fusion", "model_preference"),
        )


async def test_surplus_image_and_product_checks_need_one_provider_call(request_data):
    request = CreationRequest(**request_data)
    ctx = context(request)
    ctx.materials[0].data = picture()
    p = compile_element_plan(request, target(), ctx, AgentConfig())
    value = audit_data()
    value["subject_checks"] = [
        dict(subject_id=s, score=90, same_product=True, reason="ok") for s in ["s1", "extra"]
    ]
    for kind in ["fact", "element"]:
        value[kind + "_checks"] = [
            dict(**{kind + "_id": r.target_id}, presence="present", fidelity_score=90, reason="ok")
            for r in p.requirements
            if r.target_kind == kind
        ]
    value["fact_checks"].append(
        dict(fact_id="f2", presence="absent", fidelity_score=None, reason="claim")
    )
    value.pop("garment_fusion")
    value["model_preference"] = None
    transport = RawTransport(
        [{"content": value}, {"content": {"subject_checks": value["subject_checks"]}}]
    )
    client = VisionClient(transport, AgentConfig())
    result = await client.audit_image(request, target(), ctx, p, picture())
    assert [s.subject_id for s in result.subject_checks] == ["s1"]
    assert "f2" not in [f.fact_id for f in result.fact_checks]
    review = await client.review_product(ctx, p, ["s1"], picture())
    assert len(review.subject_checks) == 1 and len(transport.calls) == 2


async def test_unchanged_ready_snapshot_skips_evidence_but_unresolved_rechecks(request_data):
    request, deps, vision, _ = setup(request_data)
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    ctx, result = await prepare_context(
        request, AgentConfig(), dependencies=deps, analysis=prepared.analysis
    )
    assert ctx is not None and "evidence" not in vision.calls
    changed = prepared.analysis.model_copy(deep=True)
    from image_agent.models import Issue

    changed.issues.append(
        Issue(
            issue_id="uncertain",
            code="conflicting_identity",
            resolution="recheck",
            message="identity unclear",
            subject_ids=["s1"],
        )
    )
    ctx, result = await prepare_context(request, AgentConfig(), dependencies=deps, analysis=changed)
    assert ctx is not None and vision.calls.count("evidence") == 1


async def test_legacy_needs_input_low_confidence_only_skips_redundant_recheck(request_data):
    from image_agent.models import Issue

    request, deps, vision, _ = setup(request_data)
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    prepared.analysis.status = "needs_input"
    prepared.analysis.issues.append(
        Issue(
            issue_id="confidence",
            code="low_confidence",
            resolution="recheck",
            message="low confidence only",
            fact_ids=["f1"],
        )
    )
    ctx, result = await prepare_context(
        request, AgentConfig(), dependencies=deps, analysis=prepared.analysis
    )
    assert ctx is not None and "evidence" not in vision.calls and result.warnings


async def test_injected_evidence_surplus_discarded_and_missing_still_fails(request_data):
    from image_agent.models import Issue

    request, deps, vision, _ = setup(request_data)
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    prepared.analysis.issues.append(
        Issue(
            issue_id="uncertain",
            code="conflicting_identity",
            resolution="recheck",
            message="check",
            subject_ids=["s1"],
        )
    )
    original = vision.validate_evidence

    async def extra(*args):
        value = await original(*args)
        value.fact_checks.append(value.fact_checks[0].model_copy(update={"fact_id": "f2"}))
        value.issue_resolutions.append(
            value.issue_resolutions[0].model_copy(update={"issue_id": "extra"})
        )
        return value

    vision.validate_evidence = extra
    ctx, _ = await prepare_context(
        request, AgentConfig(), dependencies=deps, analysis=prepared.analysis
    )
    assert ctx is not None

    async def missing(*args):
        return (await original(*args)).model_copy(update={"fact_checks": []})

    vision.validate_evidence = missing
    ctx, result = await prepare_context(
        request, AgentConfig(), dependencies=deps, analysis=prepared.analysis
    )
    assert ctx is None and result.status == "failed"


async def test_quote_variations_and_unverifiable_requirements_downgrade(request_data):
    data = analysis_data()
    data["creative_plan"] = {
        "user_requirements": ["soft light", "invented beach"],
        "suggestions": [],
    }
    data["intent"]["constraints"] = [
        dict(
            constraint_id="invented",
            kind="atmosphere",
            subject_ids=[],
            element_ids=[],
            instruction="beach",
            source="brief",
            source_quote="invented beach",
            source_material_id=None,
            priority="required",
        )
    ]
    request, deps, _, _ = setup(request_data | {"creative_brief": "soft\n light"}, discovery=data)
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    assert prepared.analysis.status == "ready"
    assert prepared.analysis.creative_plan.user_requirements == ["soft\n light"]
    assert not prepared.analysis.intent.constraints and prepared.analysis.warnings
    assert data["creative_plan"]["user_requirements"] == ["soft light", "invented beach"]


def test_low_confidence_and_optional_missing_evidence_do_not_block(request_data):
    data = deepcopy(analysis_data())
    data["facts"][0]["confidence"] = 0.3
    analysis = MaterialAnalysis(**data)
    assert not resolve_selection(CreationRequest(**request_data), analysis).pending_issues
    data["facts"].append(
        dict(
            fact_id="rear",
            subject_id="s1",
            description="rear",
            confidence=0.2,
            verifiability="unverified",
            evidence=[dict(material_id="m1", observation="inferred", source_type="inference")],
        )
    )
    data["elements"].append(
        dict(
            element_id="rear", kind="detail", subject_id="s1", description="rear", fact_ids=["rear"]
        )
    )
    data["intent"]["proposal"]["preferred_element_ids"].append("rear")
    data["issues"] = [
        dict(
            issue_id="rear",
            code="missing_evidence",
            resolution="reanalyze",
            message="rear unavailable",
            element_ids=["rear"],
        )
    ]
    analysis = MaterialAnalysis(**data)
    draft = resolve_selection(CreationRequest(**request_data), analysis)
    assert not draft.pending_issues and not draft.blocking_issues


def test_unsupported_optional_detail_is_not_a_closeup_focus(request_data):
    data = analysis_data()
    data["facts"].append(
        dict(
            fact_id="rear",
            subject_id="s1",
            description="inferred rear",
            confidence=0.2,
            verifiability="unverified",
            evidence=[dict(material_id="m1", observation="inferred", source_type="inference")],
        )
    )
    data["elements"].append(
        dict(
            element_id="rear", kind="detail", subject_id="s1", description="rear", fact_ids=["rear"]
        )
    )
    data["intent"]["proposal"]["preferred_element_ids"] = ["rear"]
    data["intent"]["focus_element_ids"] = ["rear"]
    request = CreationRequest(**request_data)
    p = compile_element_plan(
        request,
        target().model_copy(update={"variant": "closeup", "output_type": "detail_page"}),
        context(request, data),
        AgentConfig(),
    )
    assert p.focus_element_id is None
    assert "rear" not in p.preferred_element_ids and p.issues
    assert p.required_fact_ids == ["f1"]


async def test_suggestion_cannot_become_mandatory_via_generic_user_quote(request_data):
    data = analysis_data()
    data["creative_plan"] = {
        "user_requirements": ["premium"],
        "suggestions": [
            dict(aspect="scene", instruction="Beach with palm trees", reason="nice mood")
        ],
    }
    data["intent"]["constraints"] = [
        dict(
            constraint_id="scene",
            kind="atmosphere",
            subject_ids=[],
            element_ids=["e1"],
            instruction="Beach with palm trees",
            source="brief",
            source_quote="premium",
            source_material_id=None,
            priority="required",
        )
    ]
    data["intent"]["proposal"].update(required_element_ids=["e1"], preferred_element_ids=[])
    request, deps, _, _ = setup(request_data | {"creative_brief": "premium"}, discovery=data)
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    assert prepared.analysis.status == "ready" and not prepared.analysis.intent.constraints
    assert not prepared.analysis.intent.proposal.required_element_ids and prepared.analysis.warnings


async def test_unattributed_discovery_details_cannot_expand_user_mandatory_elements(request_data):
    data = analysis_data()
    data["intent"]["proposal"].update(required_element_ids=["e1"], preferred_element_ids=[])
    request, deps, _, _ = setup(request_data, discovery=data)
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    assert not prepared.analysis.intent.proposal.required_element_ids
    assert prepared.analysis.intent.proposal.preferred_element_ids == ["e1"]
    assert prepared.analysis.warnings


@pytest.mark.parametrize("suggestion", [None, "Beach with palm trees"])
async def test_generic_atmosphere_cannot_require_a_concrete_model_setting(request_data, suggestion):
    data = analysis_data()
    data["creative_plan"] = {
        "user_requirements": ["premium"],
        "suggestions": []
        if suggestion is None
        else [dict(aspect="scene", instruction=suggestion, reason="aesthetic choice")],
    }
    data["intent"]["constraints"] = [
        dict(
            constraint_id="scene",
            kind="atmosphere",
            subject_ids=[],
            element_ids=[],
            instruction="Use a beach with palm trees",
            source="brief",
            source_quote="premium",
            source_material_id=None,
            priority="required",
        )
    ]
    request, deps, _, _ = setup(request_data | {"creative_brief": "premium"}, discovery=data)
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    constraints = prepared.analysis.intent.constraints
    assert not any(c.priority == "required" for c in constraints)
    assert all("beach" not in c.instruction.lower() for c in constraints)
    assert prepared.analysis.warnings


async def test_concrete_user_atmosphere_retains_quoted_setting_without_model_additions(
    request_data,
):
    data = analysis_data()
    data["intent"]["constraints"] = [
        dict(
            constraint_id="scene",
            kind="atmosphere",
            subject_ids=[],
            element_ids=[],
            instruction="Use a beach with palm trees",
            source="brief",
            source_quote="Use a beach scene",
            source_material_id=None,
            priority="required",
        )
    ]
    request, deps, _, _ = setup(
        request_data | {"creative_brief": "Use a beach scene"}, discovery=data
    )
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    constraint = prepared.analysis.intent.constraints[0]
    assert constraint.priority == "required" and constraint.instruction == "Use a beach scene"


def test_subjective_quality_has_no_critical_failure_but_wrong_product_does():
    from image_agent.models import PixelChecks

    pixels = PixelChecks(passed=True, width=800, height=800, reason="ok")
    subjective = evaluate_quality(
        audit(30).model_copy(update={"visual_quality": 20}), pixels, target(), plan()
    )
    assert not subjective.passed and not subjective.critical_failed_checks
    wrong = evaluate_quality(audit(95, False), pixels, target(), plan())
    assert wrong.critical_failed_checks


def test_natural_language_fidelity_uncertainty_is_not_clear_structure_mismatch():
    from image_agent.models import FactCheck, PixelChecks, Requirement

    p = plan()
    p.requirements.append(
        Requirement(
            target_kind="fact",
            target_id="f1",
            origin="default_identity",
            applicability="must_show",
            reason="visible product",
        )
    )
    value = audit()
    value.fact_checks = [
        FactCheck(
            fact_id="f1",
            presence="present",
            fidelity_score=60,
            reason="Soft lighting makes the texture less convincing; the source detail is unclear.",
        )
    ]
    q = evaluate_quality(
        value, PixelChecks(passed=True, width=800, height=800, reason="ok"), target(), p
    )
    assert not q.passed and not q.critical_failed_checks


def test_focused_review_wrong_product_remains_a_critical_finding():
    from image_agent.models import FocusedProductAudit, PixelChecks

    review = FocusedProductAudit(
        subject_checks=[
            dict(subject_id="s1", score=90, same_product=False, reason="wrong physical structure")
        ]
    )
    q = evaluate_quality(
        audit(82),
        PixelChecks(passed=True, width=800, height=800, reason="ok"),
        target(),
        plan(),
        review=review,
    )
    assert not q.subject_checks[0].same_product and q.critical_failed_checks


async def test_explicit_wrong_product_is_not_pardoned_by_score_band_review(request_data):
    request, deps, vision, _ = setup(
        request_data, scores={"main": [{"identity": 82, "same": False}]}
    )
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "quality_failed" and result.assets[0].quality.critical_failed_checks
    assert "review" not in vision.calls


async def test_unaudited_candidate_blob_keeps_error_status(request_data):
    request, deps, _, _ = setup(
        request_data, scores={"main": [ProviderError("audit unavailable", kind="transport")]}
    )
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    bundle = result_to_bundle(result)
    assert bundle.blobs and bundle.dto.assets[0].status == "audit_error"
    assert bundle.dto.assets[0].quality is None and bundle.dto.assets[0].error_info


def test_historical_quality_does_not_claim_new_critical_assessment():
    from image_agent.models import PixelChecks, QualityReport

    q = evaluate_quality(
        audit(30), PixelChecks(passed=True, width=800, height=800, reason="ok"), target(), plan()
    )
    historical = q.model_dump(exclude={"critical_failed_checks"})
    restored = QualityReport.model_validate(historical)
    assert restored.critical_failed_checks is None and not restored.passed


async def test_subjective_candidate_is_delivered_as_explicit_warning_blob(request_data):
    request, deps, _, _ = setup(request_data, scores={"main": [{"visual_quality": 20}]})
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "quality_failed" and not result.assets[0].quality.passed
    assert result.warnings
    bundle = result_to_bundle(result)
    assert bundle.blobs and bundle.dto.assets[0].status == "quality_failed"
    assert bundle.dto.assets[0].blob_id == result.assets[0].asset_id


async def test_group_scores_are_warnings_preserving_individual_success(request_data):
    from image_agent.models import DetailSetAudit

    request, deps, vision, _ = setup(request_data | {"output_types": ["detail_page"]})

    async def group(*args):
        return DetailSetAudit(
            platform="taobao",
            passed=False,
            distinctiveness=10,
            role_coverage=20,
            issues=["similar shots"],
            reason="weak continuity",
        )

    vision.audit_detail_set = group
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded" and all(a.status == "succeeded" for a in result.assets)
    assert not result.detail_set_audits[0].passed and result.warnings


@pytest.mark.parametrize(
    "text",
    [
        "bombastic presentation",
        "drugstore bottle",
        "ref_weapon_01",
        "no fake platform badge",
        "without weapons",
        "do not include a weapon",
    ],
)
def test_benign_substrings_and_negated_safety_instructions(text, request_data):
    from image_agent.compliance import apply_compliance

    assert not apply_compliance(CreationRequest(**request_data), target(), text).blocked


@pytest.mark.parametrize("text", ["weapon", "sell counterfeit products", "include a bomb"])
def test_clear_safety_keywords_remain_blocked(text, request_data):
    from image_agent.compliance import apply_compliance

    assert apply_compliance(CreationRequest(**request_data), target(), text).blocked
