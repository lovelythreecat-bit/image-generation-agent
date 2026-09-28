import pytest

from image_agent.config import AgentConfig
from image_agent.errors import AgentError, ProviderError
from image_agent.models import (
    CreationRequest,
    EvidenceValidation,
    LoadedMaterial,
    MaterialAnalysis,
    PreparedContext,
    ProductInputAudit,
    SelectionSpec,
)
from image_agent.prompt import build_targets
from tests.fakes import analysis_data


def context(request, data=None):
    from image_agent.selection import finalize_selection, resolve_selection

    analysis = MaterialAnalysis(**(data or analysis_data()))
    selection = finalize_selection(resolve_selection(request, analysis)).selection
    return PreparedContext(
        materials=tuple(
            LoadedMaterial(
                material_id=m.material_id, data=b"x", sha256=m.sha256, order=i, role_hint="auto"
            )
            for i, m in enumerate(analysis.materials)
        ),
        analysis=analysis,
        selection=selection,
        input_check=ProductInputAudit(matches=True, observed_product="cup", reason="ok"),
        presentation_mode="product_only",
    )


def multi_view_data():
    d = analysis_data()
    d["materials"] += [
        dict(material_id=f"m{i}", sha256="hash", observed_role="detail", summary="back")
        for i in range(2, 6)
    ]
    d["subjects"][0]["material_ids"] = [f"m{i}" for i in range(1, 6)]
    d["facts"][0]["evidence"] += [dict(material_id=f"m{i}", observation="cup") for i in range(2, 6)]
    return d


def test_fact_or_and_coverage_and_audit_union(request_data):
    from image_agent.selection import compile_element_plan

    r = CreationRequest(**request_data)
    data = multi_view_data()
    ctx = context(r, data)
    p = compile_element_plan(
        r, build_targets(r, "product_only")[0], ctx, AgentConfig(generation_reference_limit=1)
    )
    assert p.generation_material_ids == ["m1"]
    assert p.audit_material_ids == ["m1", "m2", "m3", "m4", "m5"]
    data["facts"].append(
        dict(
            fact_id="back",
            subject_id="s1",
            description="rear zipper",
            evidence=[dict(material_id="m2", observation="zipper")],
            confidence=1,
        )
    )
    data["subjects"][0]["identity_fact_ids"].append("back")
    with pytest.raises(ProviderError) as error:
        compile_element_plan(
            r,
            build_targets(r, "product_only")[0],
            context(r, data),
            AgentConfig(generation_reference_limit=1),
        )
    assert error.value.code == "reference_capacity_exceeded"


def test_optional_scene_white_excluded_required_conflicts(request_data):
    from image_agent.selection import compile_element_plan

    d = analysis_data()
    d["facts"].append(
        dict(
            fact_id="f2",
            subject_id=None,
            description="garden",
            evidence=[dict(material_id="m1", observation="garden")],
            confidence=1,
        )
    )
    d["elements"].append(
        dict(
            element_id="garden",
            kind="scene",
            subject_id=None,
            description="garden",
            fact_ids=["f2"],
        )
    )
    d["intent"]["proposal"]["preferred_element_ids"].append("garden")
    r = CreationRequest(
        **(request_data | dict(platforms=["pinduoduo"], output_types=["pdd_white_background"]))
    )
    p = compile_element_plan(r, build_targets(r, "product_only")[0], context(r, d), AgentConfig())
    assert "garden" in p.excluded_element_ids and "garden" not in p.preferred_element_ids
    d["intent"]["proposal"]["preferred_element_ids"].remove("garden")
    d["intent"]["proposal"]["required_element_ids"].append("garden")
    with pytest.raises(ProviderError) as error:
        compile_element_plan(r, build_targets(r, "product_only")[0], context(r, d), AgentConfig())
    assert error.value.code == "requirement_conflict"


def test_pending_snapshot_can_be_resolved(request_data):
    from image_agent.selection import finalize_selection, resolve_selection

    d = analysis_data()
    d["status"] = "needs_input"
    d["issues"] = [
        dict(
            issue_id="conflict",
            code="conflicting_identity",
            resolution="recheck",
            message="check",
            subject_ids=["s1"],
        )
    ]
    r = CreationRequest(**request_data)
    draft = resolve_selection(r, MaterialAnalysis(**d))
    assert (
        draft.candidate is not None and len(draft.pending_issues) == 1 and not draft.blocking_issues
    )
    assert finalize_selection(draft).status == "needs_input"
    evidence = EvidenceValidation(
        outcome="verified",
        subject_checks=[dict(subject_id="s1", score=90, same_product=True, reason="same")],
        fact_checks=[dict(fact_id="f1", presence="present", fidelity_score=90, reason="visible")],
        intent_valid=True,
        issue_resolutions=[dict(issue_id="conflict", status="resolved", reason="verified")],
    )
    assert finalize_selection(draft, evidence).status == "ready"
    evidence.issue_resolutions[0].status = "unresolved"
    assert finalize_selection(draft, evidence).status == "needs_input"


def test_explicit_unknown_and_exclusion_semantics(request_data):
    from image_agent.selection import resolve_selection

    r = CreationRequest(
        **(
            request_data
            | dict(
                selection=dict(
                    mode="explicit", subject_ids=["s1"], preferred_element_ids=["missing"]
                )
            )
        )
    )
    with pytest.raises(AgentError):
        resolve_selection(r, MaterialAnalysis(**analysis_data()))
    d = analysis_data()
    d["intent"]["constraints"] = [
        dict(
            constraint_id="c1",
            kind="exclusion",
            subject_ids=[],
            element_ids=["e1"],
            instruction="no texture",
            source="brief",
            source_quote="no texture",
            source_material_id=None,
            priority="required",
        )
    ]
    r = CreationRequest(
        **(
            request_data
            | dict(
                creative_brief="no texture",
                selection=dict(mode="explicit", subject_ids=["s1"], excluded_element_ids=["e1"]),
            )
        )
    )
    assert not resolve_selection(r, MaterialAnalysis(**d)).blocking_issues
    r.selection = SelectionSpec(mode="explicit", subject_ids=["s1"], required_element_ids=["e1"])
    assert (
        resolve_selection(r, MaterialAnalysis(**d)).blocking_issues[0].code == "selection_conflict"
    )


def test_fingerprint_changes_content_not_selection_or_target(request_data):
    from image_agent.selection import analysis_fingerprint

    r = CreationRequest(**request_data)
    mats = (LoadedMaterial(material_id="m1", data=b"x", sha256="hash", order=0, role_hint="auto"),)
    fp = analysis_fingerprint(r, mats)
    assert fp == analysis_fingerprint(r.model_copy(update={"platforms": ["amazon"]}), mats)
    assert fp != analysis_fingerprint(r.model_copy(update={"creative_brief": "new"}), mats)
    assert fp != analysis_fingerprint(r, (mats[0].model_copy(update={"data": b"y"}),))
