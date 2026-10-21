"""Material review must not prevent generation or trigger additional model calls."""

import pytest

from image_agent.config import AgentConfig
from image_agent.contracts import result_to_bundle
from image_agent.long_image_product import long_discovery_fingerprint, prepare_long_product
from image_agent.models import MaterialAnalysis
from image_agent.pipeline import prepare_analysis, run_pipeline
from tests.fakes import FakeVision, analysis_data
from tests.test_long_image_product import inputs
from tests.test_pipeline import setup
from tests.test_service_config import deployment


def disputed_analysis():
    data = analysis_data()
    data["status"] = "needs_input"
    data["subjects"][0]["matches_product"] = False
    data["issues"] = [
        dict(
            issue_id="identity",
            code="conflicting_identity",
            resolution="reanalyze",
            message="模型认为素材与商品描述不一致",
            subject_ids=["s1"],
        ),
        dict(
            issue_id="review",
            code="input_mismatch",
            resolution="recheck",
            message="要求重复核对素材",
            subject_ids=["s1"],
        ),
    ]
    return data


async def unavailable_review(*args):
    raise AssertionError("removed material review must never run")


@pytest.mark.parametrize("supplied", [False, True])
async def test_generation_continues_with_disputed_materials(request_data, supplied):
    request, deps, vision, generator = setup(request_data, discovery=disputed_analysis())
    vision.validate_evidence = unavailable_review
    snapshot = (await prepare_analysis(request, AgentConfig(), deps)).analysis if supplied else None
    result = await run_pipeline(request, AgentConfig(), dependencies=deps, analysis=snapshot)
    assert result.status == "succeeded"
    assert result.assets[0].image and len(generator.calls) == 1
    assert result.input_check is None
    assert result_to_bundle(result).dto.input_check is None
    assert vision.calls == ["discover"]
    assert result.warnings


@pytest.mark.parametrize("supplied", [False, True])
async def test_long_product_continues_with_disputed_materials(supplied):
    request, materials = inputs()
    vision = FakeVision(discovery=disputed_analysis())
    vision.validate_evidence = unavailable_review
    snapshot = MaterialAnalysis.model_validate(disputed_analysis()) if supplied else None
    if snapshot:
        snapshot.fingerprint = long_discovery_fingerprint(request, materials)
        snapshot.materials[0].sha256 = materials[0].sha256
    product = await prepare_long_product(
        request, materials, analysis=snapshot, vision=vision
    )
    assert product.status == "ready"
    assert product.context.selection.subject_ids == ["s1"]
    assert product.context.input_check is None
    assert product.evidence is None
    assert vision.calls == ([] if supplied else ["discover"])


def test_retired_material_review_route_does_not_require_credentials(monkeypatch):
    data = deployment()
    data["services"]["retired_review"] = {
        "base_url": "https://review.example/v1",
        "protocol": "chat_completions",
        "api_key_env": "REMOVED_REVIEW_KEY",
    }
    data["stages"]["validate_evidence"] = {"service": "retired_review", "model": "old-review"}
    monkeypatch.delenv("REMOVED_REVIEW_KEY", raising=False)
    monkeypatch.setenv("TEST_IMAGE_KEY", "test-image")
    monkeypatch.setenv("TEST_VISION_KEY", "test-vision")
    config = AgentConfig.from_mapping(data)
    config.validate_credentials()
    config.validate_credentials(config.service_names("fast"))
    assert config.service_names("fast") == {"images", "vision"}

    from image_agent.run_store import config_fingerprint
    from image_agent.server.task_service import config_mapping

    mapping = config_mapping(config)
    restored = AgentConfig.from_mapping(mapping)
    assert restored.service_names("fast") == {"images", "vision"}
    assert config_fingerprint(restored) == config_fingerprint(config)
    assert mapping["services"]["retired_review"]["api_key"] is None
    data["services"]["unused_backup"] = dict(data["services"]["retired_review"])
    AgentConfig.from_mapping(data).validate_credentials()


async def test_disputed_long_snapshot_can_still_complete_visual_planning():
    from image_agent.long_image_requirements import collect_text_requirements
    from image_agent.long_image_vision import LongImageVisionClient
    from tests.test_long_image_planning import Transport
    from tests.test_long_image_product import product_plan

    request, materials = inputs(brief="商品介绍，分成两段")
    product = await prepare_long_product(
        request, materials, analysis=None, vision=FakeVision(discovery=disputed_analysis())
    )
    ready = product_plan(request, materials, product)
    transport = Transport([
        {
            "status": "needs_input",
            "issues": [{
                "issue_id": "style",
                "code": "missing_style",
                "field": "style_hint",
                "message": "Choose style",
            }],
        },
        {"status": "ready", "plan": ready.model_dump(mode="json")},
    ])
    result = await LongImageVisionClient(transport, AgentConfig()).plan(
        request, materials, collect_text_requirements(request), analysis=product.analysis
    )
    assert result.status == "ready" and result.plan is not None
    assert len(transport.payloads) == 2
