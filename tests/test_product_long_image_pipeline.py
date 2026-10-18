from io import BytesIO

import pytest
from PIL import Image, ImageDraw

from image_agent.config import AgentConfig
from image_agent.execution import ExecutionPolicy, reserve_post
from image_agent.pipeline import run_pipeline
from tests.test_pipeline import setup
from tests.test_product_long_image_planning import authored_plan


def long_setup(request_data, *, scores=None, error=None, discovery=None):
    from image_agent.product_long_image_models import ProductLongImageCopyAudit

    request, deps, vision, gen = setup(
        request_data
        | {
            "output_types": ["long_image"],
            "long_image": {"required_copy": ["  原文99.9%\n保留  "]},
        },
        scores=scores,
        error=error,
        discovery=discovery,
    )

    async def plan(request, target, context):
        reserve_post()
        vision.calls.append("long_plan")
        return authored_plan(request, target, context)

    async def audit_copy(request, section, plan, image):
        reserve_post()
        vision.calls.append("copy:" + section.section_id)
        return ProductLongImageCopyAudit(
            passed=True, text_matches=True, no_invented_claims=True, layout_readable=True
        )

    vision.plan_product_long_image = plan
    vision.audit_product_long_image_copy = audit_copy
    generate = gen.generate

    async def counted(**kwargs):
        reserve_post()
        return await generate(**kwargs)

    gen.generate = counted
    return request, deps, vision, gen


async def test_mixed_outputs_share_discovery_and_complete_long_sections(request_data):
    request, deps, vision, gen = long_setup(request_data)
    request.output_types = ["main_image", "detail_page", "long_image"]
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert vision.calls.count("discover") == 1 and vision.calls.count("long_plan") == 1
    assert len(gen.calls) == 10
    asset = result.assets[-1]
    assert asset.asset_id == "taobao.long_image.default" and asset.variant is None
    assert Image.open(BytesIO(asset.image)).size == (750, 6000)
    assert len([c for c in asset.candidates if c["status"] != "stage"]) == 1
    assert asset.quality is None
    final = asset.candidates[-1]
    assert len(final["metadata"]["segments"]) == 6
    assert all(
        s["quality"]["passed"] and s["copy_audit"]["passed"] for s in final["metadata"]["segments"]
    )
    assert all(call["references"][0].material_id == "m1" for call in gen.calls[-6:])
    assert request.long_image.required_copy[0] in gen.calls[-6]["prompt"]


async def test_unselected_long_has_no_planning_calls(request_data):
    request, deps, vision, gen = long_setup(request_data)
    request.output_types = ["main_image"]
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded" and len(gen.calls) == 1
    assert "long_plan" not in vision.calls


async def test_product_path_never_draws_text_or_uses_old_renderer(request_data, monkeypatch):
    import image_agent.long_image_render as old

    def forbidden(*args, **kwargs):
        raise AssertionError("local text rendering forbidden")

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", forbidden)
    monkeypatch.setattr(ImageDraw.ImageDraw, "multiline_text", forbidden)
    monkeypatch.setattr(old, "render_long_layout", forbidden)
    request, deps, _, _ = long_setup(request_data)
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"


async def test_long_quality_is_aggregate_and_repairs_only_failed_segment(request_data):
    from image_agent.product_long_image_models import ProductLongImageCopyAudit

    request, deps, vision, gen = long_setup(request_data)
    original = vision.audit_product_long_image_copy
    calls = []

    async def failed_once(request, section, plan, image):
        calls.append(section.section_id)
        if calls.count(section.section_id) == 1 and section.section_id == "s2":
            return ProductLongImageCopyAudit(
                passed=False,
                text_matches=False,
                no_invented_claims=True,
                layout_readable=True,
                issues=["文字错误"],
            )
        return await original(request, section, plan, image)

    vision.audit_product_long_image_copy = failed_once
    result = await run_pipeline(
        request, AgentConfig(), dependencies=deps, policy=ExecutionPolicy(max_quality_repairs=1)
    )
    assert result.status == "succeeded" and len(gen.calls) == 7
    assert calls == ["s0", "s1", "s2", "s2", "s3", "s4", "s5"]
    assert "文字错误" in gen.calls[3]["prompt"]
    assert gen.calls[3]["references"][0].material_id == "m1"


async def test_failed_text_audit_delivers_failed_full_candidate_and_accepts_only_final(
    request_data, tmp_path
):
    from image_agent.errors import AgentError
    from image_agent.pipeline import accept_candidate
    from image_agent.product_long_image_models import ProductLongImageCopyAudit

    request, deps, vision, gen = long_setup(request_data | {"output_dir": tmp_path})

    async def failed(*args):
        return ProductLongImageCopyAudit(
            passed=True, text_matches=False, no_invented_claims=True, layout_readable=True
        )

    vision.audit_product_long_image_copy = failed
    result = await run_pipeline(
        request, AgentConfig(), dependencies=deps, policy=ExecutionPolicy(max_quality_repairs=0)
    )
    asset = result.assets[0]
    assert asset.status == "succeeded" and asset.image and asset.delivery
    assert asset.candidates[-1]['status'] == 'quality_failed'
    assert len(gen.calls) == 6
    with pytest.raises(AgentError, match="final candidate"):
        await accept_candidate(
            result.run_dir, asset.asset_id, asset.candidates[0]["candidate_id"], reason="reviewed"
        )
    accepted = await accept_candidate(
        result.run_dir, asset.asset_id, asset.candidates[-1]["candidate_id"], reason="reviewed"
    )
    assert accepted.assets[0].status == "accepted"
    assert not accepted.assets[0].candidates[-1]["metadata"]["segments"][0]["copy_audit"]["passed"]


async def test_missing_segment_has_no_complete_candidate(request_data):
    from image_agent.errors import ProviderError

    request, deps, _, gen = long_setup(request_data)
    original = gen.generate

    async def fail_second(**kwargs):
        if len(gen.calls) == 1:
            raise ProviderError("unavailable")
        return await original(**kwargs)

    gen.generate = fail_second
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "failed"
    assert result.assets[0].image is None
    assert all(c["status"] == "stage" for c in result.assets[0].candidates)


async def test_memory_export_preserves_source_plan_raw_and_slices(request_data, tmp_path):
    from image_agent.output import save_result

    request, deps, _, _ = long_setup(request_data)
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    saved = await save_result(result, tmp_path)
    metadata = saved.assets[0].candidates[-1]["metadata"]
    for ref in [
        metadata["plan_ref"],
        *metadata["slices"],
        *[s["candidates"][0]["raw"] for s in metadata["segments"]],
    ]:
        assert (tmp_path / ref["path"]).is_file()


def shared_foreign_discovery(mixed):
    from tests.fakes import analysis_data

    data = analysis_data()
    data["materials"].append(
        dict(material_id="m2", sha256="hash2", observed_role="style", summary="red cup")
    )
    data["subjects"].append(
        dict(
            subject_id="s2",
            material_ids=["m2"],
            identity_fact_ids=["f2"],
            representative_material_id="m2",
            matches_product=True,
        )
    )
    data["facts"].append(
        dict(
            fact_id="f2",
            subject_id="s2",
            description="red ceramic cup",
            verifiability="visible_appearance",
            evidence=[dict(material_id="m2", observation="red cup visible", source_type="visual")],
            confidence=0.95,
        )
    )
    data["elements"].append(
        dict(
            element_id="shared",
            subject_id=None,
            kind="style",
            description="red ceramic cup inspiration",
            fact_ids=["f1", "f2"] if mixed else ["f2"],
        )
    )
    data["intent"]["proposal"]["preferred_element_ids"].append("shared")
    return data


@pytest.mark.parametrize("mixed", [False, True])
async def test_shared_foreign_evidence_never_reaches_long_generation_or_physical_audit(
    request_data, mixed
):
    data = shared_foreign_discovery(mixed)
    inputs = request_data | {
        "materials": [
            request_data["materials"][0],
            dict(material_id="m2", source={"data": b"red"}, role_hint="style"),
        ]
    }
    request, deps, vision, gen = long_setup(inputs, discovery=data)
    request.output_types = ["main_image", "long_image"]
    request.long_image.segment_count = 3
    audit_records = []
    original_audit = vision.audit_image

    async def capture(request, target, context, element_plan, image):
        if target.output_type == "long_image":
            audit_records.append(
                dict(
                    facts=[f.fact_id for f in context.analysis.facts],
                    subjects=[s.subject_id for s in context.analysis.subjects],
                    audit_ids=element_plan.audit_material_ids,
                    requirements=[r.target_id for r in element_plan.requirements],
                )
            )
        return await original_audit(request, target, context, element_plan, image)

    vision.audit_image = capture
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert [r.material_id for r in gen.calls[0]["references"]] == ["m1", "m2"]
    long_asset = result.assets[-1]
    assert long_asset.element_plan.generation_material_ids == ["m1"]
    assert long_asset.element_plan.audit_material_ids == ["m1"]
    for call in gen.calls[1:]:
        assert [r.material_id for r in call["references"] if r.material_id] == ["m1"]
        assert "f2" not in call["prompt"] and "red ceramic cup" not in call["prompt"]
    assert len(audit_records) == 3
    for record in audit_records:
        assert record["facts"] == ["f1"] and record["subjects"] == ["s1"]
        assert record["audit_ids"] == ["m1"] and "f2" not in record["requirements"]


async def test_required_foreign_long_element_preserves_completed_ordinary_result(request_data):
    from image_agent.models import SelectionSpec
    from image_agent.pipeline import prepare_analysis

    data = shared_foreign_discovery(True)
    inputs = request_data | {
        "materials": [
            request_data["materials"][0],
            dict(material_id="m2", source={"data": b"red"}, role_hint="style"),
        ]
    }
    request, deps, _, gen = long_setup(inputs, discovery=data)
    request.output_types = ["main_image", "long_image"]
    prepared = await prepare_analysis(request, AgentConfig(), deps)
    request.selection = SelectionSpec(
        mode="explicit",
        subject_ids=["s1"],
        required_element_ids=["shared"],
        preferred_element_ids=["e1"],
    )
    result = await run_pipeline(
        request, AgentConfig(), dependencies=deps, analysis=prepared.analysis
    )
    assert result.status == "partial"
    assert result.assets[0].status == "succeeded"
    assert result.assets[1].status == "failed"
    assert result.assets[1].error_info.code == "long_evidence_outside_selection"
    assert len(gen.calls) == 1
