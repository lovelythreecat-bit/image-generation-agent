import asyncio
import json
from pathlib import Path

import pytest

from image_agent.config import AgentConfig
from image_agent.pipeline import run_pipeline
from tests.test_pipeline import setup


def continuity_bindings(asset):
    return [b for b in asset.reference_bindings if b.role == "shoot_continuity"]


async def test_details_inherit_approved_main_and_keep_original_evidence(request_data):
    request, deps, _, generator = setup(
        request_data | {"output_types": ["main_image", "detail_page"], "style_hint": "江南巷弄"},
        scores={"feature": [{"output_intent": 74}, {}]},
    )
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert not continuity_bindings(result.assets[0])
    for asset in result.assets[1:]:
        assert [b.stage_id for b in continuity_bindings(asset)] == ["shoot-taobao-main"]
        assert asset.element_plan.generation_material_ids == ["m1"]
        assert all("shoot-taobao-main" in a.reference_ids for a in asset.attempts)
    for call in generator.calls[1:]:
        reference = next(r for r in call["references"] if r.role == "shoot_continuity")
        assert reference.data == result.assets[0].image
        assert reference.element_ids == []
    scene_prompt = result.assets[1].prompt
    assert "visibly different from the main catalog image through setting" not in scene_prompt


@pytest.mark.parametrize(
    "platform,main_scores", [("amazon", {}), ("taobao", {"visual_quality": 69})]
)
async def test_catalog_or_failed_main_uses_approved_scene_as_anchor(
    request_data, platform, main_scores
):
    request, deps, _, _ = setup(
        request_data | {"platforms": [platform], "output_types": ["main_image", "detail_page"]},
        scores={"main": [main_scores]},
    )
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert not continuity_bindings(result.assets[0])
    assert not continuity_bindings(result.assets[1])
    for asset in result.assets[2:]:
        assert [b.stage_id for b in continuity_bindings(asset)] == [f"shoot-{platform}-scene"]


async def test_detail_only_set_establishes_scene_anchor(request_data):
    request, deps, _, _ = setup(request_data | {"output_types": ["detail_page"]})
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert not continuity_bindings(result.assets[0])
    assert [b.stage_id for b in continuity_bindings(result.assets[1])] == ["shoot-taobao-scene"]


async def test_anchors_are_scoped_to_platform(request_data):
    request, deps, _, _ = setup(
        request_data
        | {"platforms": ["taobao", "jd"], "output_types": ["main_image", "detail_page"]}
    )
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    for asset in result.assets[2:]:
        assert [b.stage_id for b in continuity_bindings(asset)] == [f"shoot-{asset.platform}-main"]


async def test_capacity_keeps_required_evidence_and_reports_continuity_fallback(request_data):
    request, deps, _, _ = setup(request_data | {"output_types": ["main_image", "detail_page"]})
    result = await run_pipeline(
        request, AgentConfig(generation_reference_limit=1), dependencies=deps
    )
    assert result.status == "succeeded"
    assert all(a.element_plan.generation_material_ids == ["m1"] for a in result.assets)
    assert not any(continuity_bindings(a) for a in result.assets)
    assert any(
        "shoot continuity" in warning and "capacity" in warning for warning in result.warnings
    )


async def test_saved_anchor_survives_cancel_and_resume(request_data, tmp_path):
    from image_agent.graph_runtime import resume_pipeline

    request, deps, _, generator = setup(
        request_data | {"output_types": ["main_image", "detail_page"], "output_dir": tmp_path}
    )

    def progress(event):
        if event["stage"] == "generate" and event["asset_id"] == "taobao.detail_page.scene":
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_pipeline(request, AgentConfig(), dependencies=deps, on_progress=progress)
    run_dir = next(tmp_path.iterdir())
    state = json.loads((run_dir / "state.json").read_text("utf-8"))
    anchor = state["shoot_anchors"]["taobao"]
    assert (run_dir / anchor["path"]).is_file()
    resumed = await resume_pipeline(run_dir, AgentConfig(), dependencies=deps)
    assert resumed.status == "succeeded"
    assert len(generator.calls) == 4
    assert [b.stage_id for b in continuity_bindings(resumed.assets[1])] == ["shoot-taobao-main"]
    manifest = json.loads((Path(resumed.run_dir) / "result.json").read_text("utf-8"))
    assert any(b["role"] == "shoot_continuity" for b in manifest["assets"][1]["reference_bindings"])


@pytest.mark.parametrize("limit", [2, 4])
async def test_staged_repair_retains_anchor_when_capacity_allows(request_data, limit):
    request, deps, _, generator = setup(
        request_data | {"output_types": ["main_image", "detail_page"]},
        scores={"feature": [{"identity": 95, "same": False}, {}]},
    )
    result = await run_pipeline(
        request, AgentConfig(generation_reference_limit=limit), dependencies=deps
    )
    assert result.status == "succeeded"
    feature = result.assets[2]
    assert feature.generation_mode == "staged"
    assert "shoot-taobao-main" in feature.attempts[1].reference_ids
    assert "SHARED SHOOT DIRECTION" in feature.attempts[1].prompt
    fusion = next(
        call for call in generator.calls if any(r.stage_id == "stage" for r in call["references"])
    )
    assert len(fusion["references"]) <= limit
    assert fusion["references"][0].material_id == "m1"
    assert any(r.stage_id == "shoot-taobao-main" for r in fusion["references"]) == (limit == 4)


async def test_shared_shoot_plan_is_saved_from_existing_creative_analysis(request_data, tmp_path):
    from tests.fakes import analysis_data
    from tests.test_creative_planning import creative_plan_data

    request, deps, vision, _ = setup(
        request_data
        | {
            "output_types": ["main_image", "detail_page"],
            "output_dir": tmp_path,
            "creative_brief": "高级感，适合电商",
        },
        discovery=analysis_data() | {"creative_plan": creative_plan_data()},
    )
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    state = json.loads((Path(result.run_dir) / "state.json").read_text("utf-8"))
    shared = state["context"]["shoot_plan"]["shared_visual_suggestions"]
    assert shared == {
        "scene": "浅灰石材台面",
        "lighting": "柔和侧光",
        "palette": "低饱和暖灰背景",
        "mood": "克制安静的氛围",
    }
    assert vision.calls.count("discover") == 1
    assert all("浅灰石材台面" in a.prompt for a in result.assets)


@pytest.mark.parametrize("vision_limit,image_count", [(12, 5), (4, 4)])
async def test_group_audit_compares_details_with_approved_main(
    request_data, vision_limit, image_count
):
    from image_agent.vision import VisionClient
    from tests.test_vision import ScriptTransport

    request, deps, vision, _ = setup(request_data | {"output_types": ["main_image", "detail_page"]})
    transport = ScriptTransport(
        [
            dict(
                platform="taobao",
                passed=True,
                distinctiveness=90,
                role_coverage=90,
                issues=[],
                reason="ok",
            )
        ]
    )
    config = AgentConfig(vision_image_limit=vision_limit)
    vision.audit_detail_set = VisionClient(transport, config).audit_detail_set
    result = await run_pipeline(request, config, dependencies=deps)
    assert result.status == "succeeded"
    content = transport.calls[0][1]["messages"][0]["content"]
    assert sum(block["type"] == "image_url" for block in content) == image_count
    assert any("APPROVED SHOOT ANCHOR" in block.get("text", "") for block in content) == (
        vision_limit == 12
    )
    assert any("main-to-detail shoot continuity audit omitted" in w for w in result.warnings) == (
        vision_limit == 4
    )


async def test_anchor_slot_replaces_optional_style_but_never_product_evidence(request_data):
    from tests.fakes import analysis_data

    discovery = analysis_data()
    discovery["materials"].append(
        dict(material_id="m2", sha256="hash", observed_role="style", summary="warm room")
    )
    discovery["facts"].append(
        dict(
            fact_id="f2",
            subject_id=None,
            description="warm room",
            evidence=[dict(material_id="m2", observation="warm room")],
            confidence=1,
        )
    )
    discovery["elements"].append(
        dict(
            element_id="e2", kind="style", subject_id=None, description="warm room", fact_ids=["f2"]
        )
    )
    discovery["intent"]["proposal"]["preferred_element_ids"].append("e2")
    request, deps, _, _ = setup(
        request_data
        | {
            "output_types": ["main_image", "detail_page"],
            "materials": [
                dict(material_id="m1", source={"data": b"x"}),
                dict(material_id="m2", source={"data": b"y"}),
            ],
        },
        discovery=discovery,
    )
    result = await run_pipeline(
        request, AgentConfig(generation_reference_limit=2), dependencies=deps
    )
    assert result.status == "succeeded"
    assert result.assets[0].element_plan.generation_material_ids == ["m1", "m2"]
    for asset in result.assets[1:]:
        assert asset.element_plan.generation_material_ids == ["m1"]
        assert asset.element_plan.text_only_element_ids == ["e2"]
        assert [b.stage_id for b in continuity_bindings(asset)] == ["shoot-taobao-main"]


async def test_saved_generation_journal_restores_continuity_bindings(
    request_data, tmp_path, monkeypatch
):
    from image_agent.graph_runtime import resume_pipeline
    from image_agent.run_store import RunStore

    request, deps, _, generator = setup(
        request_data | {"output_types": ["main_image", "detail_page"], "output_dir": tmp_path}
    )
    original = RunStore.mark_operation

    def crash_after_saved(store, operation, status, **extra):
        original(store, operation, status, **extra)
        if operation == "taobao.detail_page.scene-1" and status == "saved":
            raise asyncio.CancelledError()

    with monkeypatch.context() as patch:
        patch.setattr(RunStore, "mark_operation", crash_after_saved)
        with pytest.raises(asyncio.CancelledError):
            await run_pipeline(request, AgentConfig(), dependencies=deps)
    resumed = await resume_pipeline(next(tmp_path.iterdir()), AgentConfig(), dependencies=deps)
    assert resumed.status == "succeeded"
    assert len(generator.calls) == 4
    assert [b.stage_id for b in continuity_bindings(resumed.assets[1])] == ["shoot-taobao-main"]
    assert "shoot-taobao-main" in resumed.assets[1].prompt
