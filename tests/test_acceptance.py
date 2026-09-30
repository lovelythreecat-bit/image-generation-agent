"""Adversarial acceptance cases from the r2 specification, kept offline."""

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from pydantic import ValidationError

from image_agent import AgentConfig, CreationRequest, CreationRequestDTO, MaterialAnalysis
from image_agent.errors import ProviderError
from image_agent.pipeline import run_pipeline
from image_agent.selection import compile_element_plan, resolve_selection
from tests.fakes import analysis_data
from tests.test_pipeline import setup
from tests.test_quality import audit, plan, target
from tests.test_selection import context


def two_subjects():
    d = analysis_data()
    d["materials"].append(
        dict(material_id="m2", sha256="hash", observed_role="accessory", summary="lid")
    )
    d["subjects"].append(
        dict(
            subject_id="s2",
            material_ids=["m2"],
            identity_fact_ids=["f2"],
            representative_material_id="m2",
            matches_product=True,
        )
    )
    d["facts"].append(
        dict(
            fact_id="f2",
            subject_id="s2",
            description="lid",
            evidence=[dict(material_id="m2", observation="lid")],
            confidence=1,
        )
    )
    d["elements"].append(
        dict(element_id="e2", kind="accessory", subject_id="s2", description="lid", fact_ids=["f2"])
    )
    d["intent"]["proposal"].update(subject_ids=["s1", "s2"], preferred_element_ids=["e1", "e2"])
    return d


def test_user_required_not_downgraded_by_focus(request_data):
    from image_agent.prompt import build_targets

    d = two_subjects()
    d["intent"]["proposal"].update(required_element_ids=["e2"], preferred_element_ids=["e1"])
    r = CreationRequest(**(request_data | {"output_types": ["detail_page"]}))
    ctx = context(r, d)
    targets = build_targets(r, "product_only")
    for t in targets[:2]:
        p = compile_element_plan(r, t, ctx, AgentConfig())
        assert any(x.target_id == "e2" and x.applicability == "must_show" for x in p.requirements)
    with pytest.raises(ProviderError, match="微距"):
        compile_element_plan(r, targets[2], ctx, AgentConfig())


def test_both_visible_subjects_and_primary_reference_order(request_data):
    from image_agent.prompt import build_targets

    d = two_subjects()
    d["intent"]["proposal"].update(primary_subject_id="s2", subject_ids=["s2", "s1"])
    r = CreationRequest(**request_data)
    p = compile_element_plan(r, build_targets(r, "product_only")[0], context(r, d), AgentConfig())
    assert p.generation_material_ids == ["m2", "m1"]
    assert [
        q.target_id
        for q in p.requirements
        if q.target_kind == "subject" and q.applicability == "must_show"
    ] == ["s2", "s1"]


def test_optional_absent_but_required_not_applicable_fails():
    from image_agent.models import ElementCheck, PixelChecks, Requirement
    from image_agent.quality import evaluate_quality

    p = plan()
    p.requirements.append(
        Requirement(
            target_kind="element",
            target_id="e1",
            origin="user_required",
            applicability="must_show",
            reason="required",
        )
    )
    a = audit()
    a.element_checks = [
        ElementCheck(
            element_id="e1", presence="not_applicable", fidelity_score=None, reason="hidden"
        )
    ]
    pixels = PixelChecks(passed=True, width=800, height=800, reason="ok")
    assert not evaluate_quality(a, pixels, target(), p).passed
    p.requirements[-1].origin = "preferred"
    assert evaluate_quality(a, pixels, target(), p).passed


async def test_staged_capacity_fallback_and_amazon_strict(request_data):
    r, deps, vision, gen = setup(request_data, scores={"main": [{"identity": 70}, {}]})
    result = await run_pipeline(r, AgentConfig(generation_reference_limit=1), dependencies=deps)
    assert result.status == "succeeded" and len(gen.calls) == 2
    assert result.assets[0].generation_mode == "strict"
    assert any("staged" in w for w in result.warnings)
    request_data["platforms"] = ["amazon"]
    r, deps, vision, gen = setup(request_data, scores={"main": [{"identity": 70}, {}]})
    result = await run_pipeline(r, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded" and len(gen.calls) == 2


async def test_preparation_transport_error_has_result_and_analysis_error(request_data):
    r, deps, vision, gen = setup(request_data)

    async def failed(*args):
        raise ProviderError("timeout", kind="transport")

    vision.analyze_materials = failed
    result = await run_pipeline(r, AgentConfig(), dependencies=deps)
    assert result.status == "failed" and result.assets == [] and not gen.calls
    assert result.analysis.error_info == result.error_info
    assert result.error_info.code == "provider_transport" and result.error_info.retryable


async def test_group_error_does_not_downgrade_assets(request_data):
    request_data["output_types"] = ["detail_page"]
    r, deps, vision, gen = setup(request_data)

    async def fail(*args):
        raise ProviderError("bad schema")

    vision.audit_detail_set = fail
    result = await run_pipeline(r, AgentConfig(), dependencies=deps)
    assert result.status == "partial" and result.detail_set_audits[0].error_info.kind == "protocol"
    assert result.detail_set_audits[0].distinctiveness is None


async def test_quality_failure_partial_keeps_candidate_outside_approved_outputs(
    request_data, tmp_path
):
    from image_agent.contracts import result_to_bundle
    from image_agent.output import save_result

    request_data["output_types"] = ["main_image", "detail_page"]
    r, deps, vision, gen = setup(request_data, scores={"feature": [{"output_intent": 50}]})
    result = await run_pipeline(r, AgentConfig(), dependencies=deps)
    assert result.status == "partial" and result.error_info is None
    failed = result.assets[2]
    assert (
        failed.image is not None
        and failed.file_path is None
        and failed.error_info.code == "quality_failed"
    )
    assert failed.error == failed.error_info.message and not result.detail_set_audits[0].passed
    await save_result(result, tmp_path)
    assert (
        tmp_path / "candidates/taobao.detail_page.feature/c0002.png"
    ).read_bytes() == failed.image
    assert not (tmp_path / "taobao/detail_page/feature.png").exists()
    bundle = result_to_bundle(result)
    assert failed.asset_id not in bundle.blobs
    assert len(bundle.blobs) == 3


async def test_staged_scene_failure_no_fusion(request_data):
    from tests.test_images import picture

    r, deps, vision, gen = setup(request_data, scores={"main": [{"identity": 70}]})

    async def generate(**kwargs):
        gen.calls.append(kwargs)
        if len(gen.calls) == 2:
            raise ProviderError("bad response")
        return picture((1600, 1600))

    gen.generate = generate
    result = await run_pipeline(r, AgentConfig(), dependencies=deps)
    assert len(gen.calls) == 2 and result.assets[0].error_info.kind == "protocol"


@pytest.mark.parametrize(
    "exception", [httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout]
)
async def test_transport_exception_retry_exactly_three(exception):
    from image_agent.transport import HttpTransport

    calls = []
    delays = []

    def handler(req):
        calls.append(1)
        raise exception("private request")

    async def sleep(n):
        delays.append(n)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as error:
            await HttpTransport(AgentConfig(), client=client, sleep=sleep).post_json("/images", {})
    assert len(calls) == 3 and delays == [2, 4] and error.value.kind == "transport"


def test_shared_permit_across_threads_and_event_loops():
    from image_agent.transport import model_permit

    lock = threading.Lock()
    state = {"active": 0, "peak": 0}
    barrier = threading.Barrier(3)

    async def run():
        async with model_permit("thread-test", 2):
            with lock:
                state["active"] += 1
                state["peak"] = max(state["peak"], state["active"])
            await asyncio.sleep(0.03)
            with lock:
                state["active"] -= 1

    def worker():
        barrier.wait()
        asyncio.run(run())

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [pool.submit(worker) for _ in range(3)]
        for future in futures:
            future.result(timeout=10)
    assert state["peak"] <= 2 and state["active"] == 0


def test_analysis_must_have_selected_primary(request_data):
    d = analysis_data()
    d["intent"]["proposal"]["primary_subject_id"] = None
    with pytest.raises(ValidationError):
        MaterialAnalysis(**d)


def test_materials_empty_cannot_be_hidden_by_legacy(request_data):
    with pytest.raises(ValidationError):
        CreationRequest(**(request_data | {"materials": [], "product_image": {"data": b"x"}}))


def test_required_fact_low_confidence_in_explicit_snapshot_needs_recheck(request_data):
    d = analysis_data()
    d["facts"][0]["confidence"] = 0.1
    r = CreationRequest(**request_data)
    draft = resolve_selection(r, MaterialAnalysis(**d))
    assert draft.pending_issues


def test_public_schema_has_platform_and_output_enums():
    schema = CreationRequestDTO.model_json_schema()
    assert "amazon" in schema["properties"]["platforms"]["items"]["enum"]
    assert "pdd_white_background" in schema["properties"]["output_types"]["items"]["enum"]
