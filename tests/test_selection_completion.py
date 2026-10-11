"""Planning recovery uses existing selection and never fills physical unknowns."""

import pytest

from image_agent.completion import completion_problems, validate_material_completion
from image_agent.config import AgentConfig
from image_agent.errors import ProviderError
from image_agent.models import CreationRequest, MaterialAnalysis
from image_agent.prompt import build_prompt, build_targets
from image_agent.selection import compile_element_plan, normalize_discovery_attribution
from tests.fakes import analysis_data
from tests.test_selection import context


@pytest.mark.parametrize(
    "code", ["missing_specs", "missing_copy", "missing_scene", "incomplete_brief"]
)
def test_optional_information_gap_can_be_completed_without_new_user_claims(request_data, code):
    data = analysis_data()
    data["status"] = "needs_input"
    data["issues"] = [
        dict(issue_id="optional", code=code, resolution="reanalyze", message="Fill planning")
    ]
    request = CreationRequest(**request_data)
    analysis = MaterialAnalysis.model_validate(data)
    problems = completion_problems(request, analysis)
    assert any(problem["code"] == code for problem in problems)
    assert analysis.facts[0].description == "white ceramic cup"


@pytest.mark.parametrize("resolution", ["selection", "reanalyze"])
def test_planning_code_cannot_disguise_required_identity_unknown(request_data, resolution):
    data = analysis_data()
    data["creative_plan"] = None
    data["status"] = "needs_input"
    data["issues"] = [
        dict(
            issue_id="must",
            code="missing_specs",
            resolution=resolution,
            message="Physical identity missing",
            fact_ids=["f1"],
        )
    ]
    assert not completion_problems(CreationRequest(**request_data), MaterialAnalysis(**data))


def test_no_selected_subject_does_not_get_chosen_by_creative_completion(request_data):
    data = analysis_data()
    data["creative_plan"] = None
    data["status"] = "needs_input"
    data["intent"]["proposal"]["primary_subject_id"] = None
    data["intent"]["proposal"]["subject_ids"] = []
    data["intent"]["proposal"]["preferred_element_ids"] = []
    assert not completion_problems(CreationRequest(**request_data), MaterialAnalysis(**data))


def test_source_supported_optional_detail_fallback_does_not_inflate_claims(request_data):
    data = analysis_data()
    data["facts"].append(
        dict(
            fact_id="hidden",
            subject_id="s1",
            description="unseen underside engraving",
            verifiability="unverified",
            evidence=[
                dict(material_id="m1", observation="cannot see underside", source_type="inference")
            ],
            confidence=0.2,
        )
    )
    data["elements"].append(
        dict(
            element_id="hidden",
            kind="detail",
            subject_id="s1",
            description="unseen engraving",
            fact_ids=["hidden"],
        )
    )
    data["intent"]["proposal"]["preferred_element_ids"].append("hidden")
    data["intent"]["focus_element_ids"] = ["hidden"]
    data["issues"] = [
        dict(
            issue_id="hidden",
            code="missing_evidence",
            resolution="reanalyze",
            message="No underside",
            fact_ids=["hidden"],
        )
    ]
    request_data["output_types"] = ["detail_page"]
    request = CreationRequest(**request_data)
    normalized = normalize_discovery_attribution(MaterialAnalysis(**data), request)
    ctx = context(request, normalized.model_dump())
    target = build_targets(request, "product_only")[-1]
    plan = compile_element_plan(request, target, ctx, AgentConfig())
    assert target.variant == "closeup" and plan.focus_element_id == "e1"
    assert "hidden" in plan.excluded_element_ids and "hidden" not in plan.required_fact_ids
    prompt = build_prompt(request, target, ctx, plan)
    assert "unseen underside engraving" not in prompt


def test_white_catalog_filters_model_scene_supplement(request_data):
    data = analysis_data()
    data["creative_plan"] = dict(
        user_requirements=[],
        suggestions=[
            dict(aspect="scene", instruction="Set in a garden", reason="Optional aesthetic"),
            dict(aspect="lighting", instruction="Even soft light", reason="Show appearance"),
        ],
    )
    request_data.update(platforms=["pinduoduo"], output_types=["pdd_white_background"])
    request = CreationRequest(**request_data)
    ctx = context(request, data)
    target = build_targets(request, "product_only")[0]
    plan = compile_element_plan(request, target, ctx, AgentConfig())
    prompt = build_prompt(request, target, ctx, plan)
    assert "Set in a garden" not in prompt and "Even soft light" in prompt
    assert plan.subject_ids == ["s1"]


@pytest.mark.parametrize("status", ["failed", "ready", "needs_input"])
def test_error_analysis_cannot_be_reclassified_as_creative_gap(request_data, status):
    data = analysis_data()
    data.update(
        status=status,
        creative_plan=None,
        error_info=dict(
            code="provider_transport", kind="transport", message="upstream down", retryable=True
        ),
    )
    original = MaterialAnalysis.model_validate(data)
    request = CreationRequest(**request_data)
    assert completion_problems(request, original) == []


@pytest.mark.parametrize(
    "error", [None, dict(code="provider_transport", kind="transport", message="upstream down")]
)
def test_completion_validator_cannot_erase_failed_analysis(request_data, error):
    data = analysis_data()
    data.update(status="failed", creative_plan=None, error_info=error)
    original = MaterialAnalysis.model_validate(data)
    completed = original.model_copy(deep=True)
    completed.status = "ready"
    completed.error_info = None
    completed.creative_plan = MaterialAnalysis.model_validate(analysis_data()).creative_plan
    with pytest.raises(ProviderError):
        validate_material_completion(original, completed, CreationRequest(**request_data))


def test_completion_validator_cannot_erase_error_from_ready_analysis(request_data):
    data = analysis_data()
    data["error_info"] = dict(code="provider_transport", kind="transport", message="upstream down")
    original = MaterialAnalysis.model_validate(data)
    completed = original.model_copy(deep=True)
    completed.error_info = None
    with pytest.raises(ProviderError):
        validate_material_completion(original, completed, CreationRequest(**request_data))
