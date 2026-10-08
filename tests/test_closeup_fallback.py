"""Missing discovery elements must not prevent a source-supported fourth image."""

from pathlib import Path

import pytest

from image_agent.config import AgentConfig
from image_agent.errors import ProviderError
from image_agent.models import CreationRequest
from image_agent.pipeline import run_pipeline
from image_agent.prompt import build_prompt, build_targets
from image_agent.selection import compile_element_plan
from tests.fakes import analysis_data
from tests.test_pipeline import setup
from tests.test_selection import context


def facts_only_data():
    data = analysis_data()
    data["elements"] = []
    data["intent"]["proposal"]["preferred_element_ids"] = []
    data["intent"]["focus_element_ids"] = []
    return data


async def test_four_assets_with_facts_but_no_detail_elements(request_data, tmp_path):
    request, deps, vision, _ = setup(
        request_data | {"output_types": ["main_image", "detail_page"], "output_dir": tmp_path},
        discovery=facts_only_data(),
    )
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert [a.status for a in result.assets] == ["succeeded"] * 4
    closeup = result.assets[-1]
    assert closeup.variant == "closeup" and closeup.image
    focus = next(r for r in closeup.element_plan.requirements if r.origin == "shot_rule")
    assert (focus.target_kind, focus.target_id, focus.applicability) == ("fact", "f1", "must_show")
    assert "white ceramic cup" in closeup.prompt
    assert closeup.file_path and (Path(result.run_dir) / closeup.file_path).is_file()
    assert result.detail_set_audits[0].passed
    assert vision.calls.count("discover") == 1


def test_closeup_fallback_preserves_other_identity_facts_if_visible(request_data):
    data = facts_only_data()
    other = dict(data["facts"][0], fact_id="f2", description="rear handle", confidence=0.8)
    data["facts"].append(other)
    data["subjects"][0]["identity_fact_ids"].append("f2")
    request = CreationRequest(**(request_data | {"output_types": ["detail_page"]}))
    ctx = context(request, data)
    target = build_targets(request, "product_only")[-1]
    plan = compile_element_plan(request, target, ctx, AgentConfig())
    fact_reqs = {r.target_id: r for r in plan.requirements if r.target_kind == "fact"}
    assert fact_reqs["f1"].applicability == "must_show"
    assert fact_reqs["f2"].applicability == "preserve_if_visible"
    prompt = build_prompt(request, target, ctx, plan)
    assert "SOURCE-SUPPORTED CLOSE-UP FOCUS" in prompt
    assert "white ceramic cup" in prompt
    assert ctx.analysis.model_dump() == context(request, data).analysis.model_dump()


@pytest.mark.parametrize(
    "verification,source,confidence",
    [
        ("functional_claim", "user_input", 1.0),
        ("unverified", "visual", 1.0),
        ("visible_appearance", "promotional_text", 1.0),
        ("visible_appearance", "inference", 1.0),
        ("visible_appearance", "unknown", 1.0),
    ],
)
def test_closeup_fallback_rejects_untrusted_facts(request_data, verification, source, confidence):
    data = facts_only_data()
    data["facts"][0]["verifiability"] = verification
    data["facts"][0]["evidence"][0]["source_type"] = source
    data["facts"][0]["confidence"] = confidence
    request = CreationRequest(**(request_data | {"output_types": ["detail_page"]}))
    with pytest.raises(ProviderError, match="特写缺少可信的焦点细节"):
        compile_element_plan(
            request,
            build_targets(request, "product_only")[-1],
            context(request, data),
            AgentConfig(),
        )


def test_closeup_fallback_does_not_reintroduce_excluded_detail(request_data):
    data = analysis_data()
    data["intent"]["proposal"]["preferred_element_ids"] = []
    data["intent"]["proposal"]["excluded_element_ids"] = ["e1"]
    data["intent"]["focus_element_ids"] = []
    request = CreationRequest(**(request_data | {"output_types": ["detail_page"]}))
    with pytest.raises(ProviderError, match="特写缺少可信的焦点细节"):
        compile_element_plan(
            request,
            build_targets(request, "product_only")[-1],
            context(request, data),
            AgentConfig(),
        )
