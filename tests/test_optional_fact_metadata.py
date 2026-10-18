"""Optional fact bookkeeping must never prevent creation from real product images."""

import pytest

from image_agent.config import AgentConfig
from image_agent.models import CreationRequest, MaterialAnalysis
from image_agent.pipeline import run_pipeline
from image_agent.prompt import build_targets
from image_agent.selection import compile_element_plan, finalize_selection, resolve_selection
from image_agent.vision import VisionClient
from tests.fakes import analysis_data
from tests.test_pipeline import setup
from tests.test_selection import context, multi_view_data
from tests.test_vision import ScriptTransport, materials


@pytest.mark.parametrize("links", [[], None, ["missing"], ["f1", "f1"]])
async def test_fact_links_do_not_require_model_correction(request_data, links):
    data = analysis_data()
    data["elements"][0]["fact_ids"] = links
    data["subjects"][0]["identity_fact_ids"] = links
    transport = ScriptTransport([data])
    result = await VisionClient(transport, AgentConfig()).analyze_materials(
        CreationRequest(**request_data), materials()
    )
    assert result.status == "ready"
    assert len(transport.calls) == 1


@pytest.mark.parametrize("facts", [None, [], [{"fact_id": "bad"}]])
async def test_absent_or_malformed_facts_do_not_block_discovery(request_data, facts):
    data = analysis_data()
    data["facts"] = facts
    del data["elements"][0]["fact_ids"]
    del data["subjects"][0]["identity_fact_ids"]
    transport = ScriptTransport([data])
    result = await VisionClient(transport, AgentConfig()).analyze_materials(
        CreationRequest(**request_data), materials()
    )
    assert result.status == "ready"
    assert result.facts == []
    assert len(transport.calls) == 1


async def test_fact_provenance_is_optional_context(request_data):
    data = analysis_data()
    del data["facts"][0]["verifiability"]
    del data["facts"][0]["evidence"][0]["source_type"]
    transport = ScriptTransport([data])
    result = await VisionClient(transport, AgentConfig()).analyze_materials(
        CreationRequest(**request_data), materials()
    )
    assert result.status == "ready"
    assert len(transport.calls) == 1


@pytest.mark.parametrize("code", ["missing_evidence", "low_confidence", "unverifiable_claim"])
async def test_fact_proof_issues_do_not_stop_pipeline(request_data, code):
    data = analysis_data()
    data["facts"][0]["verifiability"] = "unverified"
    data["issues"] = [
        dict(
            issue_id="proof",
            code=code,
            resolution="reanalyze",
            message="missing fact proof",
            fact_ids=["f1"],
            subject_ids=["s1"],
            element_ids=["e1"],
        )
    ]
    request, deps, vision, generator = setup(request_data, discovery=data)
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert len(generator.calls) == 1
    assert "evidence" not in vision.calls


async def test_main_and_detail_images_work_without_any_facts(request_data):
    data = analysis_data()
    data["facts"] = []
    data["subjects"][0]["identity_fact_ids"] = []
    data["elements"][0]["fact_ids"] = []
    request, deps, _, generator = setup(
        request_data | {"output_types": ["main_image", "detail_page"]}, discovery=data
    )
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert len(generator.calls) == 4
    assert all(call["references"] for call in generator.calls)


def test_fact_evidence_does_not_exhaust_reference_capacity(request_data):
    data = multi_view_data()
    data["facts"].append(
        dict(
            data["facts"][0],
            fact_id="back",
            evidence=[dict(material_id="m2", observation="back view")],
        )
    )
    data["subjects"][0]["identity_fact_ids"].append("back")
    request = CreationRequest(**request_data)
    plan = compile_element_plan(
        request,
        build_targets(request, "product_only")[0],
        context(request, data),
        AgentConfig(generation_reference_limit=1),
    )
    assert plan.generation_material_ids == ["m1"]
    assert "m2" in plan.audit_material_ids


def test_unknown_product_selection_still_blocks(request_data):
    data = analysis_data()
    data["issues"] = [
        dict(
            issue_id="identity",
            code="ambiguous_subject",
            resolution="selection",
            message="choose a product",
            subject_ids=["s1"],
        )
    ]
    request = CreationRequest(**request_data)
    result = finalize_selection(resolve_selection(request, MaterialAnalysis(**data)))
    assert result.status == "needs_input"


@pytest.mark.parametrize("outcome", ["failed", "needs_input"])
@pytest.mark.parametrize("same_product", [True, False])
def test_review_fact_issues_do_not_override_product_identity(request_data, outcome, same_product):
    from image_agent.models import EvidenceValidation

    request = CreationRequest(**request_data)
    evidence = EvidenceValidation(
        outcome=outcome,
        intent_valid=True,
        issue_resolutions=[],
        subject_checks=[
            dict(subject_id="s1", score=95, same_product=same_product, reason="source")
        ],
        issues=[
            dict(
                issue_id="proof",
                code="missing_evidence",
                resolution="reanalyze",
                message="fact proof missing",
                fact_ids=["f1"],
            )
        ],
    )
    result = finalize_selection(
        resolve_selection(request, MaterialAnalysis(**analysis_data())), evidence
    )
    assert result.status == ("ready" if same_product else "failed")


def test_long_image_keeps_elements_without_fact_links(request_data):
    from image_agent.product_long_image import product_long_image_context

    data = analysis_data()
    data["elements"][0]["fact_ids"] = []
    data["intent"]["proposal"].update(required_element_ids=["e1"], preferred_element_ids=[])
    request = CreationRequest(**request_data)
    ctx = product_long_image_context(context(request, data))
    assert ctx.selection.required_element_ids == ["e1"]
    assert ctx.analysis.elements[0].element_id == "e1"


def test_product_style_without_fact_ids_does_not_conflict_with_white_background(request_data):
    data = analysis_data()
    data["elements"][0].update(kind="style", description="source product pattern", fact_ids=[])
    data["intent"]["proposal"].update(required_element_ids=["e1"], preferred_element_ids=[])
    request = CreationRequest(
        **(
            request_data
            | {
                "platforms": ["pinduoduo"],
                "output_types": ["pdd_white_background"],
            }
        )
    )
    plan = compile_element_plan(
        request,
        build_targets(request, "product_only")[0],
        context(request, data),
        AgentConfig(),
    )
    assert plan.required_element_ids == ["e1"]
    assert plan.generation_material_ids == ["m1"]
