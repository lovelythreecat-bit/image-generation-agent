"""Optional model planning completion with frozen source and selection evidence."""

from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from .errors import ProviderError
from .models import MaterialAnalysis
from .selection import normalize_discovery_attribution, resolve_selection


@runtime_checkable
class MaterialCompletionProtocol(Protocol):
    """Injected providers opt in by implementing this complete async interface.

    Older providers remain supported. Once opted in, completion failures propagate;
    a failed call is never treated as an absent capability.
    """

    async def complete_material_analysis(self, request, materials, analysis, problems): ...


# These describe missing creative/structural choices, never product identity.
PLANNING_ISSUE_CODES = frozenset(
    {
        "missing_creative_plan",
        "incomplete_creative_plan",
        "missing_plan",
        "incomplete_plan",
        "missing_scene",
        "missing_style",
        "missing_composition",
        "missing_lighting",
        "missing_palette",
        "missing_mood",
        "missing_layout",
        "missing_structure",
        "missing_content",
        "missing_copy",
        "missing_marketing_copy",
        "missing_selling_points",
        "missing_specs",
        "missing_specifications",
        "missing_product_information",
        "insufficient_product_information",
        "incomplete_brief",
        "missing_brief",
        "missing_creative_direction",
        "missing_target_audience",
        "missing_optional_information",
    }
)


def completion_problems(request, analysis, *, additional_issues=()):
    """Only complete usable selected products whose gaps can be solved creatively."""
    if analysis.status == "failed" or analysis.error_info is not None or analysis.intent is None:
        return []
    if additional_issues:
        known = {i.issue_id for i in analysis.issues + analysis.intent.unmet_requirements}
        analysis = analysis.model_copy(
            update={
                "issues": analysis.issues
                + [i for i in additional_issues if i.issue_id not in known]
            }
        )
    draft = resolve_selection(request, analysis)
    if draft.candidate is None:
        return []
    active_issues = draft.blocking_issues + draft.pending_issues
    # Subject selection and explicit requirement conflicts need user input,
    # even when creative guidance is also absent.
    if any(
        i.code not in PLANNING_ISSUE_CODES or i.resolution == "selection" for i in active_issues
    ):
        return []
    critical = {
        fid
        for s in analysis.subjects
        if s.subject_id in draft.candidate.subject_ids
        for fid in s.identity_fact_ids
    }
    required = set(draft.candidate.required_element_ids)
    required.update(
        eid
        for c in draft.candidate.constraints
        if c.priority == "required"
        for eid in c.element_ids
    )
    critical.update(
        fid for e in analysis.elements if e.element_id in required for fid in e.fact_ids
    )
    if any(set(i.fact_ids) & critical or set(i.element_ids) & required for i in active_issues):
        return []
    problems = [i.model_dump(mode="json") for i in active_issues]
    if analysis.creative_plan is None:
        problems.append(
            {
                "code": "missing_creative_plan",
                "message": "Fill optional visual composition and lighting from original inputs.",
            }
        )
    return problems


def validate_material_completion(original, completed, request):
    """A planning repair cannot mutate observed products or user obligations."""
    if original.status == "failed" or original.error_info is not None:
        raise ProviderError(
            "failed material analysis cannot be repaired as a creative planning gap"
        )
    for field in ("analysis_id", "fingerprint", "materials", "subjects", "facts", "elements"):
        if getattr(original, field) != getattr(completed, field):
            raise ProviderError("material planning completion changed frozen " + field)
    if completed.intent is None:
        raise ProviderError("material planning completion removed intent")
    for field in ("proposal", "constraints", "focus_element_ids"):
        if getattr(original.intent, field) != getattr(completed.intent, field):
            raise ProviderError("material planning completion changed frozen " + field)
    old_issues = original.issues + original.intent.unmet_requirements
    remaining = {i.issue_id: i for i in completed.issues + completed.intent.unmet_requirements}
    for issue in old_issues:
        if issue.code not in PLANNING_ISSUE_CODES and remaining.get(issue.issue_id) != issue:
            raise ProviderError("material planning completion removed source evidence issue")
    draft = resolve_selection(request, completed)
    if (
        completed.status != "ready"
        or completed.error_info is not None
        or completion_problems(request, completed)
        or draft.blocking_issues
        or draft.pending_issues
    ):
        raise ProviderError("material planning completion did not resolve creative planning gaps")


async def maybe_complete_analysis(
    request, materials, analysis, vision, config, *, additional_issues=()
):
    """Make one completion invocation; the provider owns bounded model retries.

    Fingerprint/hash validation must precede this function for supplied snapshots.
    The runtime and standalone long-image callers retain their own fingerprints.
    """
    if not isinstance(vision, MaterialCompletionProtocol):
        return analysis
    problems = completion_problems(request, analysis, additional_issues=additional_issues)
    if not problems:
        return analysis
    if len(materials) > config.vision_image_limit:
        raise ProviderError(
            "planning completion exceeds vision capacity",
            kind="capability",
            code="vision_capacity_exceeded",
        )
    try:
        result = await vision.complete_material_analysis(
            request, tuple(materials), analysis.model_copy(deep=True), problems
        )
        completed = MaterialAnalysis.model_validate(
            result.model_dump() if isinstance(result, MaterialAnalysis) else result
        )
        completed = normalize_discovery_attribution(completed, request)
        validate_material_completion(analysis, completed, request)
    except ValidationError:
        raise ProviderError("material planning completion schema invalid") from None
    completed.warnings = list(dict.fromkeys(analysis.warnings + completed.warnings))
    return completed
