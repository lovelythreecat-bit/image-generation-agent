"""Exercise real selection and generation around optional AI planning completion."""

import copy

import pytest

from image_agent.config import AgentConfig
from image_agent.errors import ProviderError, StaleAnalysisError
from image_agent.models import CreationRequest, MaterialAnalysis
from image_agent.pipeline import Dependencies, prepare_analysis, prepare_context, run_pipeline
from tests.fakes import FakeGenerator, FakeVision, analysis_data
from tests.test_images import picture


def creative_plan():
    return dict(
        user_requirements=[],
        suggestions=[
            dict(aspect="composition", instruction="Center the source cup", reason="Readable hero"),
            dict(aspect="lighting", instruction="Soft even light", reason="Visible silhouette"),
        ],
    )


def planning_gap():
    data = analysis_data()
    data["creative_plan"] = None
    data["status"] = "needs_input"
    data["issues"] = [
        dict(issue_id="scene", code="missing_scene", resolution="reanalyze", message="Need scene")
    ]
    return data


class CompletionVision(FakeVision):
    """Only external model responses are scripted; pipeline and audits remain real."""

    def __init__(self, discovery=None, *, response=None, error=None):
        super().__init__(discovery=discovery)
        self.response, self.error = response, error
        self.completion_inputs = []

    async def complete_material_analysis(self, request, materials, analysis, problems):
        self.calls.append("complete")
        self.completion_inputs.append((request.model_dump(), tuple(materials), analysis, problems))
        if self.error:
            raise self.error
        data = analysis.model_dump()
        data.update(status="ready", issues=[], creative_plan=creative_plan(), error_info=None)
        data["intent"]["unmet_requirements"] = []
        if self.response:
            self.response(data)
        return MaterialAnalysis.model_validate(data)


def setup_completion(request_data, vision):
    request = CreationRequest(**request_data)
    generator = FakeGenerator()

    async def load(source):
        return picture()

    return request, Dependencies(vision, generator, load), generator


@pytest.mark.parametrize(
    "platforms,outputs,asset_count",
    [
        (["taobao"], ["main_image"], 1),
        (["taobao"], ["detail_page"], 3),
        (["pinduoduo"], ["pdd_white_background"], 1),
    ],
)
async def test_ai_fills_scene_gap_before_real_assets(request_data, platforms, outputs, asset_count):
    request_data.update(platforms=platforms, output_types=outputs)
    vision = CompletionVision(planning_gap())
    request, deps, generator = setup_completion(request_data, vision)
    before = request.model_dump()
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert len(result.assets) == asset_count
    assert vision.calls[:2] == ["discover", "complete"]
    assert vision.calls.count("complete") == 1
    assert len(generator.calls) == asset_count
    assert result.analysis.creative_plan.suggestions[0].aspect == "composition"
    assert result.assets[0].element_plan.subject_ids == ["s1"]
    assert request.model_dump() == before
    original_request, originals, original_analysis, problems = vision.completion_inputs[0]
    assert original_request == before and originals[0].data == picture()
    assert original_analysis.issues[0].issue_id == "scene" and problems


async def test_missing_creative_plan_uses_ai_with_all_source_photos(request_data):
    request_data["materials"].append(dict(material_id="m2", source={"data": b"other"}))
    data = analysis_data()
    data["creative_plan"] = None
    data["materials"].append(
        dict(material_id="m2", sha256="hash", observed_role="detail", summary="cup side")
    )
    data["subjects"][0]["material_ids"].append("m2")
    vision = CompletionVision(data)
    request, deps, _ = setup_completion(request_data, vision)
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    assert prepared.analysis.creative_plan is not None
    assert vision.calls == ["discover", "complete"]
    assert [m.material_id for m in vision.completion_inputs[0][1]] == ["m1", "m2"]


async def test_explicit_snapshot_keeps_selection_and_rechecks(request_data):
    data = analysis_data()
    data["creative_plan"] = None
    vision = CompletionVision(data)
    request, deps, _ = setup_completion(request_data, vision)
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    snapshot = prepared.analysis.model_copy(deep=True)
    snapshot.creative_plan = None
    snapshot.status = "needs_input"
    request.selection = request.selection.model_validate(
        dict(mode="explicit", subject_ids=["s1"], required_element_ids=["e1"])
    )
    context, result = await prepare_context(
        request, AgentConfig(), dependencies=deps, analysis=snapshot
    )
    assert context is not None and context.selection.subject_ids == ["s1"]
    assert context.selection.required_element_ids == ["e1"]
    assert vision.calls == ["discover", "complete", "complete", "evidence"]
    assert snapshot.creative_plan is None and snapshot.status == "needs_input"
    assert result.analysis.analysis_id == snapshot.analysis_id


@pytest.mark.parametrize("code", ["conflicting_identity", "ambiguous_subject"])
async def test_identity_or_required_appearance_gaps_stay_honest(request_data, code):
    data = analysis_data()
    data.update(status="needs_input", creative_plan=creative_plan())
    data["issues"] = [
        dict(
            issue_id="identity",
            code=code,
            resolution="reanalyze",
            message="Need real subject",
            subject_ids=["s1"],
            fact_ids=["f1"],
        )
    ]
    vision = CompletionVision(data)
    request, deps, generator = setup_completion(request_data, vision)
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "needs_input" and not generator.calls
    assert vision.calls == ["discover"]
    assert result.issues[0].issue_id == "identity"


@pytest.mark.parametrize("mutation", ["fact", "subject", "constraint", "selection", "element"])
async def test_completion_cannot_replace_truth_or_user_intent(request_data, mutation):
    request_data["creative_brief"] = "保留白色"
    data = planning_gap()
    data["intent"]["constraints"] = [
        dict(
            constraint_id="keep",
            kind="appearance",
            subject_ids=["s1"],
            element_ids=[],
            instruction="保留白色",
            source="brief",
            source_quote="保留白色",
            source_material_id=None,
            priority="required",
        )
    ]

    def mutate(candidate):
        if mutation == "fact":
            candidate["facts"][0]["description"] = "unproven certified cup"
        elif mutation == "subject":
            candidate["subjects"][0]["matches_product"] = False
        elif mutation == "constraint":
            candidate["intent"]["constraints"] = []
        elif mutation == "selection":
            candidate["intent"]["proposal"]["preferred_element_ids"] = []
        else:
            candidate["elements"][0]["description"] = "bonus lid"

    vision = CompletionVision(data, response=mutate)
    request, deps, generator = setup_completion(request_data, vision)
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "failed" and not generator.calls
    assert result.error_info.code == "provider_protocol"


async def test_completion_transport_failure_is_visible_and_not_ignored(request_data):
    vision = CompletionVision(planning_gap(), error=ProviderError("down", kind="transport"))
    request, deps, generator = setup_completion(request_data, vision)
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "failed" and result.error_info.kind == "transport"
    assert vision.calls == ["discover", "complete"] and not generator.calls


async def test_completion_nonconformance_is_bounded_failure_not_user_question(request_data):
    def still_missing(candidate):
        candidate["creative_plan"] = None

    vision = CompletionVision(planning_gap(), response=still_missing)
    request, deps, generator = setup_completion(request_data, vision)
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "failed" and result.error_info.kind == "protocol"
    assert vision.calls == ["discover", "complete"] and not generator.calls


async def test_legacy_injected_provider_without_completion_remains_supported(request_data):
    vision = FakeVision()
    request, deps, generator = setup_completion(request_data, vision)
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded" and len(generator.calls) == 1


async def test_present_empty_plan_does_not_charge_for_unneeded_creative_repair(request_data):
    data = analysis_data()
    data["creative_plan"] = dict(user_requirements=[], suggestions=[])
    vision = CompletionVision(data)
    request, deps, generator = setup_completion(request_data, vision)
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded" and len(generator.calls) == 1
    assert "complete" not in vision.calls


async def test_stale_explicit_snapshot_never_sends_completion(request_data):
    vision = CompletionVision()
    request, deps, _ = setup_completion(request_data, vision)
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    snapshot = copy.deepcopy(prepared.analysis)
    snapshot.creative_plan = None
    request.creative_brief = "changed"
    request.selection = request.selection.model_validate(dict(mode="explicit", subject_ids=["s1"]))
    before = list(vision.calls)
    with pytest.raises(StaleAnalysisError):
        await prepare_context(request, AgentConfig(), dependencies=deps, analysis=snapshot)
    assert vision.calls == before


class EvidenceGapVision(CompletionVision):
    def __init__(self, *, code="missing_scene", persistent=False):
        data = analysis_data()
        data["creative_plan"] = creative_plan()
        super().__init__(data)
        self.code, self.persistent = code, persistent

    async def validate_evidence(self, request, materials, analysis, draft):
        from image_agent.models import Issue

        evidence = await super().validate_evidence(request, materials, analysis, draft)
        if self.calls.count("evidence") == 1 or self.persistent:
            evidence.outcome = "needs_input"
            evidence.issues = [
                Issue(
                    issue_id="review-gap",
                    code=self.code,
                    resolution="reanalyze",
                    message="Need scene or source",
                )
            ]
        return evidence


async def test_evidence_discovered_creative_gap_is_completed_before_user_question(request_data):
    vision = EvidenceGapVision()
    request, deps, _ = setup_completion(request_data, vision)
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    prepared.analysis.status = "needs_input"
    context, result = await prepare_context(
        request, AgentConfig(), dependencies=deps, analysis=prepared.analysis
    )
    assert context is not None and result.input_check.matches
    assert vision.calls == ["discover", "evidence", "complete", "evidence"]
    assert any(problem["code"] == "missing_scene" for problem in vision.completion_inputs[0][3])


async def test_persistent_evidence_creative_gap_is_bounded_provider_failure(request_data):
    vision = EvidenceGapVision(persistent=True)
    request, deps, _ = setup_completion(request_data, vision)
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    prepared.analysis.status = "needs_input"
    context, result = await prepare_context(
        request, AgentConfig(), dependencies=deps, analysis=prepared.analysis
    )
    assert context is None and result.status == "failed"
    assert result.error_info.kind == "protocol"
    assert vision.calls == ["discover", "evidence", "complete", "evidence"]


async def test_fact_proof_review_continues_without_creative_repair(request_data):
    vision = EvidenceGapVision(code="missing_evidence")
    request, deps, _ = setup_completion(request_data, vision)
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    prepared.analysis.status = "needs_input"
    context, result = await prepare_context(
        request, AgentConfig(), dependencies=deps, analysis=prepared.analysis
    )
    assert context is not None
    assert vision.calls == ["discover", "evidence"]


async def test_warning_only_snapshot_keeps_existing_supported_view_path(request_data):
    data = analysis_data()
    data["creative_plan"] = creative_plan()
    data["status"] = "needs_input"
    data["issues"] = [
        dict(
            issue_id="optional",
            code="low_confidence",
            resolution="recheck",
            message="Use known view",
            fact_ids=["f1"],
        )
    ]
    vision = CompletionVision(data)
    request, deps, _ = setup_completion(request_data, vision)
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    context, result = await prepare_context(
        request, AgentConfig(), dependencies=deps, analysis=prepared.analysis
    )
    assert context is not None and result.input_check.matches
    assert vision.calls == ["discover"]


@pytest.mark.parametrize("supplied", [False, True], ids=["fresh", "snapshot"])
@pytest.mark.parametrize("supports_completion", [False, True], ids=["legacy", "completion"])
@pytest.mark.parametrize("has_plan", [False, True], ids=["missing-plan", "valid-plan"])
@pytest.mark.parametrize(
    "status,error",
    [
        (
            "failed",
            dict(
                code="provider_transport", kind="transport", message="upstream down", retryable=True
            ),
        ),
        (
            "ready",
            dict(
                code="provider_http",
                kind="http",
                message="service unavailable",
                retryable=True,
                status_code=503,
            ),
        ),
        (
            "needs_input",
            dict(code="vision_capacity_exceeded", kind="capability", message="too many sources"),
        ),
        ("failed", None),
    ],
    ids=[
        "failed-transport",
        "ready-http-error",
        "needs-input-capability-error",
        "failed-without-error",
    ],
)
async def test_failed_or_error_analysis_never_completes_or_generates(
    request_data, supplied, supports_completion, has_plan, status, error
):
    from image_agent.models import ErrorInfo
    from image_agent.pipeline import _load
    from image_agent.selection import analysis_fingerprint

    data = analysis_data()
    data.update(
        status=status, error_info=error, creative_plan=creative_plan() if has_plan else None
    )
    vision = CompletionVision(data) if supports_completion else FakeVision(discovery=data)
    request, deps, generator = setup_completion(request_data, vision)
    snapshot = None
    if supplied:
        request.selection = request.selection.model_validate(
            dict(mode="explicit", subject_ids=["s1"])
        )
        materials = await _load(request, deps)
        snapshot = MaterialAnalysis.model_validate(data)
        snapshot.fingerprint = analysis_fingerprint(request, materials)
        snapshot.materials[0].sha256 = materials[0].sha256
    before = snapshot.model_dump() if supplied else None

    result = await run_pipeline(request, AgentConfig(), dependencies=deps, analysis=snapshot)

    assert result.status == "failed"
    assert not result.assets and not generator.calls
    assert vision.calls == ([] if supplied else ["discover"])
    if error:
        assert result.error_info == ErrorInfo.model_validate(error)
    else:
        assert (
            result.error_info.code == "provider_protocol" and result.error_info.kind == "protocol"
        )
    assert result.analysis.intent is not None and result.analysis.subjects[0].subject_id == "s1"
    if supplied:
        assert snapshot.model_dump() == before


async def test_terminal_discovery_error_is_not_overwritten_by_material_validation(request_data):
    from image_agent.models import ErrorInfo

    error = dict(
        code="provider_transport", kind="transport", message="upstream down", retryable=True
    )
    data = analysis_data()
    data.update(status="failed", creative_plan=None, error_info=error)
    data["materials"][0]["material_id"] = "unavailable"
    data["subjects"][0].update(
        material_ids=["unavailable"], representative_material_id="unavailable"
    )
    data["facts"][0]["evidence"][0]["material_id"] = "unavailable"
    vision = CompletionVision(data)
    request, deps, generator = setup_completion(request_data, vision)
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "failed" and result.error_info == ErrorInfo.model_validate(error)
    assert not result.assets and not generator.calls and vision.calls == ["discover"]
