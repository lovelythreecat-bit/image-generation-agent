import asyncio

import pytest

from image_agent.config import AgentConfig
from image_agent.errors import AgentError, ProviderError, StaleAnalysisError
from image_agent.models import CreationRequest
from tests.fakes import FakeGenerator, FakeVision, analysis_data
from tests.test_images import picture


def setup(request_data, *, scores=None, discovery=None, error=None):
    from image_agent.pipeline import Dependencies

    vision, gen = FakeVision(scores=scores, discovery=discovery), FakeGenerator(error=error)

    async def load(source):
        return picture()

    return (
        CreationRequest(**request_data),
        Dependencies(vision=vision, generator=gen, load=load),
        vision,
        gen,
    )


async def test_four_assets_one_preparation_and_one_strict_repair(request_data):
    from image_agent.pipeline import run_pipeline

    request_data["output_types"] = ["detail_page", "main_image"]
    r, deps, vision, gen = setup(request_data, scores={"feature": [{"output_intent": 74}, {}]})
    before = r.model_dump()
    result = await run_pipeline(r, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert [a.variant for a in result.assets] == [None, "scene", "feature", "closeup"]
    assert len(gen.calls) == 5 and vision.calls.count("discover") == 1 and vision.calls[-1] == "set"
    assert [len(a.attempts) for a in result.assets] == [1, 1, 2, 1]
    assert result.assets[2].generation_mode == "strict" and result.assets[2].error_info is None
    assert result.assets[0].image == picture((1600, 1600))
    assert r.model_dump() == before


@pytest.mark.parametrize(
    "scores,expected_calls,mode",
    [
        ({"identity": 95, "same": False}, 3, "staged"),
        ({"identity": 84}, 1, "standard"),
        ({"output_intent": 74}, 2, "strict"),
    ],
)
async def test_identity_and_intent_repair_modes(request_data, scores, expected_calls, mode):
    from image_agent.pipeline import run_pipeline

    r, deps, vision, gen = setup(request_data, scores={"main": [scores, {}]})
    result = await run_pipeline(r, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded" and len(gen.calls) == expected_calls
    assert result.assets[0].generation_mode == mode
    if mode == "staged":
        assert not any(ref.role == "identity" for ref in gen.calls[1]["references"])
        assert any(ref.stage_id == "stage" for ref in gen.calls[2]["references"])
        assert gen.calls[2]["references"][-1].stage_id == "previous-candidate"


async def test_quality_service_failure_no_creative_retry(request_data):
    from image_agent.pipeline import run_pipeline

    r, deps, vision, gen = setup(
        request_data, scores={"main": [ProviderError("timeout", kind="transport")]}
    )
    result = await run_pipeline(r, AgentConfig(), dependencies=deps)
    assert result.status == "audit_error" and len(gen.calls) == 1
    assert result.assets[0].image == picture((1600, 1600))
    assert result.assets[0].error_info.retryable
    assert result.error_info is None


async def test_audit_protocol_failure_preserves_candidate_and_saved_manifest(
    request_data, tmp_path
):
    import json

    from image_agent.contracts import result_to_bundle
    from image_agent.output import save_result
    from image_agent.pipeline import run_pipeline

    r, deps, vision, gen = setup(
        request_data, scores={"main": [ProviderError("audit_image: fact_checks missing")]}
    )
    result = await run_pipeline(r, AgentConfig(), dependencies=deps)
    saved = await save_result(result, tmp_path)
    asset = saved.assets[0]
    assert saved.status == asset.status == "audit_error"
    assert len(gen.calls) == 1 and asset.image == picture((1600, 1600))
    assert (tmp_path / "candidates/taobao.main_image.default/c0001.png").read_bytes() == asset.image
    assert not (tmp_path / "taobao/main_image.png").exists()
    manifest = json.loads((tmp_path / "result.json").read_text(encoding="utf-8"))
    assert manifest["assets"][0]["file_path"] == "candidates/taobao.main_image.default/c0001.png"
    assert manifest["assets"][0]["status"] == "audit_error"
    bundle = result_to_bundle(saved)
    assert bundle.blobs[asset.asset_id] == asset.image
    assert bundle.dto.assets[0].status == "audit_error" and bundle.dto.assets[0].quality is None


async def test_snapshot_recovery_and_stale_detection(request_data):
    from image_agent.pipeline import prepare_analysis, run_pipeline

    r, deps, vision, gen = setup(request_data)
    prepared = await prepare_analysis(r, AgentConfig(), deps)
    snapshot = prepared.analysis
    snapshot.status = "needs_input"
    r.selection = r.selection.model_validate(
        dict(mode="explicit", subject_ids=["s1"], preferred_element_ids=["e1"])
    )
    result = await run_pipeline(r, AgentConfig(), dependencies=deps, analysis=snapshot)
    assert result.status == "succeeded" and vision.calls.count("evidence") == 1
    assert vision.calls.count("discover") == 1
    r.creative_brief = "changed"
    with pytest.raises(StaleAnalysisError):
        await run_pipeline(r, AgentConfig(), dependencies=deps, analysis=snapshot)


async def test_explicit_requires_snapshot_and_input_mismatch(request_data):
    from image_agent.pipeline import run_pipeline

    r, deps, vision, gen = setup(request_data)
    r.selection = r.selection.model_validate(dict(mode="explicit", subject_ids=["s1"]))
    with pytest.raises(AgentError):
        await run_pipeline(r, AgentConfig(), dependencies=deps)
    assert not vision.calls and not gen.calls
    d = analysis_data()
    d["subjects"][0]["matches_product"] = False
    r, deps, vision, gen = setup(request_data, discovery=d)
    result = await run_pipeline(r, AgentConfig(), dependencies=deps)
    assert (
        result.status == "failed" and not gen.calls and result.error_info.code == "input_mismatch"
    )


async def test_ambiguous_no_generation_and_cancel_propagates(request_data):
    from image_agent.pipeline import run_pipeline

    d = analysis_data()
    d["issues"] = [
        dict(
            issue_id="mixed",
            code="conflicting_identity",
            resolution="reanalyze",
            message="replace mixed image",
            subject_ids=["s1"],
        )
    ]
    r, deps, vision, gen = setup(request_data, discovery=d)
    result = await run_pipeline(r, AgentConfig(), dependencies=deps)
    assert result.status == "needs_input" and result.error_info is None and not gen.calls
    r, deps, vision, gen = setup(request_data, error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await run_pipeline(r, AgentConfig(), dependencies=deps)


async def test_white_only_uses_export_pixels_and_product_only(request_data):
    from image_agent.pipeline import run_pipeline

    request_data.update(
        platforms=["pinduoduo"], output_types=["pdd_white_background"], model_preference="female"
    )
    r, deps, vision, gen = setup(request_data)
    result = await run_pipeline(r, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded" and result.presentation_mode == "product_only"
    assert result.assets[0].quality.deterministic_checks.width == 480
