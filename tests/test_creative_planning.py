import json
from pathlib import Path

import pytest

from image_agent.config import AgentConfig
from image_agent.models import CreationRequest, MaterialAnalysis
from image_agent.prompt import build_prompt, build_targets
from image_agent.selection import compile_element_plan
from image_agent.vision import VisionClient
from tests.fakes import analysis_data
from tests.test_pipeline import setup
from tests.test_selection import context
from tests.test_vision import ScriptTransport, materials


def creative_plan_data():
    return {
        "user_requirements": ["高级感", "适合电商"],
        "suggestions": [
            {"aspect": "scene", "instruction": "浅灰石材台面", "reason": "突出商品"},
            {"aspect": "composition", "instruction": "留出适量负空间", "reason": "保持层次"},
            {"aspect": "lighting", "instruction": "柔和侧光", "reason": "呈现质感"},
            {"aspect": "palette", "instruction": "低饱和暖灰背景", "reason": "体现高级感"},
            {
                "aspect": "product_presentation",
                "instruction": "突出素材中可见的杯柄",
                "reason": "强调真实商品细节",
            },
            {"aspect": "mood", "instruction": "克制安静的氛围", "reason": "贴合用户风格"},
        ],
    }


async def test_discovery_returns_creative_suggestions_without_extra_model_call(request_data):
    request = CreationRequest(**(request_data | {"creative_brief": "高级感，适合电商"}))
    data = analysis_data() | {"creative_plan": creative_plan_data()}
    transport = ScriptTransport([data, data])
    analysis = await VisionClient(transport, AgentConfig()).analyze_materials(request, materials())
    assert analysis.creative_plan.user_requirements == ["高级感", "适合电商"]
    assert analysis.creative_plan.suggestions[0].instruction == "浅灰石材台面"
    assert len(transport.calls) == 1


async def test_model_cannot_label_invented_requirements_as_user_input(request_data):
    request = CreationRequest(**(request_data | {"creative_brief": "高级感，适合电商"}))
    plan = creative_plan_data()
    plan["user_requirements"] = ["必须使用黑金背景"]
    data = analysis_data() | {"creative_plan": plan}
    transport = ScriptTransport([data])
    result = await VisionClient(transport, AgentConfig()).analyze_materials(request, materials())
    assert result.creative_plan.user_requirements == [] and result.warnings
    assert len(transport.calls) == 1


def test_cached_plan_also_checks_user_requirement_provenance(request_data):
    request = CreationRequest(**(request_data | {"creative_brief": "高级感，适合电商"}))
    plan = creative_plan_data()
    plan["user_requirements"] = ["必须使用黑金背景"]
    analysis = MaterialAnalysis(**(analysis_data() | {"creative_plan": plan}))
    with pytest.raises(ValueError, match="creative"):
        analysis.validate_intent_source(request)


@pytest.mark.parametrize(
    "platform,output", [("amazon", "main_image"), ("pinduoduo", "pdd_white_background")]
)
def test_catalog_prompt_omits_scene_palette_and_pose_suggestions(request_data, platform, output):
    request = CreationRequest(
        **(
            request_data
            | {
                "platforms": [platform],
                "output_types": [output],
                "creative_brief": "高级感，适合电商",
            }
        )
    )
    ctx = context(request, analysis_data() | {"creative_plan": creative_plan_data()})
    target = build_targets(request, "product_only")[0]
    plan = compile_element_plan(request, target, ctx, AgentConfig())
    prompt = build_prompt(request, target, ctx, plan)
    assert "柔和侧光" in prompt
    assert "浅灰石材台面" not in prompt
    assert "低饱和暖灰背景" not in prompt
    assert "留出适量负空间" not in prompt
    assert "克制安静的氛围" not in prompt


async def test_pipeline_uses_and_saves_plan_without_promoting_suggestions_to_requirements(
    request_data, tmp_path
):
    from image_agent.pipeline import run_pipeline

    request_data = request_data | {"creative_brief": "高级感，适合电商", "output_dir": tmp_path}
    request, deps, _, generator = setup(
        request_data, discovery=analysis_data() | {"creative_plan": creative_plan_data()}
    )
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert "浅灰石材台面" in generator.calls[0]["prompt"]
    assert "柔和侧光" in generator.calls[0]["prompt"]
    saved = json.loads((Path(result.run_dir) / "result.json").read_text("utf-8"))
    assert saved["analysis"]["creative_plan"]["user_requirements"] == ["高级感", "适合电商"]
    assert not result.assets[0].element_plan.constraints
    assert all(
        r.origin in {"default_identity", "preferred"}
        for r in result.assets[0].element_plan.requirements
    )
