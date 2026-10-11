import copy

import pytest

from image_agent.config import AgentConfig
from image_agent.errors import ProviderError
from image_agent.pipeline import prepare_context
from image_agent.prompt import build_targets
from image_agent.selection import compile_element_plan
from tests.test_pipeline import setup


def authored_plan(request, target, context):
    from image_agent.product_long_image import product_long_image_fingerprint
    from image_agent.product_long_image_models import ProductLongImagePlan

    sections = []
    for i in range(request.long_image.segment_count):
        rows = [
            dict(
                copy_id=f"c{i}",
                text="white ceramic cup",
                source_kind="fact",
                source_quote="white ceramic cup",
                fact_ids=["f1"],
            )
        ]
        if i == 0:
            rows += [
                dict(
                    copy_id=f"required{j}",
                    text=text,
                    source_kind="required_copy",
                    source_quote=text,
                    fact_ids=[],
                )
                for j, text in enumerate(request.long_image.required_copy)
            ]
        sections.append(
            dict(
                section_id=f"s{i}",
                role="hero" if i == 0 else "custom",
                title="",
                copy=rows,
                visual_prompt=f"第{i + 1}段：白色杯子的不同构图，完整设计商品、背景与文字层级",
            )
        )
    return ProductLongImagePlan(
        input_fingerprint=product_long_image_fingerprint(request, target, context),
        style_direction="米白与暖灰，中文清晰，空间连续",
        sections=sections,
    )


async def planning_context(request_data):
    request, deps, _, _ = setup(
        request_data
        | {
            "output_types": ["long_image"],
            "long_image": {"known_info": "容量500ml", "required_copy": ["  超强99.9%\n原文  "]},
        }
    )
    context, _ = await prepare_context(request, AgentConfig(), dependencies=deps)
    return request, build_targets(request, context.presentation_mode)[0], context


@pytest.mark.parametrize("count", [3, 6, 9])
async def test_planning_wire_schema_requires_requested_section_count(request_data, count):
    import json

    from image_agent.vision import VisionClient
    from tests.test_vision import ScriptTransport

    request, target, context = await planning_context(request_data)
    request.long_image.segment_count = count
    plan = authored_plan(request, target, context)
    transport = ScriptTransport([plan.model_dump(mode="json")])
    await VisionClient(transport, AgentConfig()).plan_product_long_image(request, target, context)
    text = transport.calls[0][1]["messages"][0]["content"][0]["text"]
    schema = json.loads(text.split("JSON booleans. Never omit required audit items.\n", 1)[1])
    assert schema["properties"]["sections"]["minItems"] == count
    assert schema["properties"]["sections"]["maxItems"] == count
    assert f"exactly {count} ordered" in text


@pytest.mark.parametrize("damage", ["count"])
async def test_plan_retry_identifies_actual_structure_mismatch(request_data, damage):
    from image_agent.vision import VisionClient
    from tests.test_vision import ScriptTransport

    request, target, context = await planning_context(request_data)
    valid = authored_plan(request, target, context)
    invalid = valid.model_copy(deep=True)
    if damage == "count":
        invalid.sections.pop()
        expected = "expected 6 sections, received 5"
    else:
        invalid.sections[0].role = "custom"
        expected = "first section role must be hero, received custom"
    transport = ScriptTransport([invalid.model_dump(mode="json"), valid.model_dump(mode="json")])
    plan = await VisionClient(transport, AgentConfig()).plan_product_long_image(
        request, target, context
    )
    assert len(plan.sections) == 6 and plan.sections[0].role == "hero"
    assert len(transport.calls) == 2
    first, second = [payload["messages"][0]["content"] for _, payload in transport.calls]
    assert expected in second[0]["text"]
    assert first[1:] == second[1:]


async def test_planning_receives_selected_product_and_known_inputs(request_data):
    from image_agent.product_long_image_models import ProductLongImagePlan
    from image_agent.vision import VisionClient

    request, target, context = await planning_context(request_data)
    # Additional discovered product must never become the selected product's copy evidence.
    unselected = context.analysis.facts[0].model_copy(
        update={"fact_id": "red", "subject_id": "other", "description": "red cup"}
    )
    context.analysis.facts.append(unselected)
    captured = {}

    class RecordingVision(VisionClient):
        async def _call(self, schema, instruction, images, **kwargs):
            captured.update(
                schema=schema, instruction=instruction, images=images, stage=kwargs["stage"]
            )
            return kwargs["validate"](authored_plan(request, target, context))

    plan = await RecordingVision(None, AgentConfig()).plan_product_long_image(
        request, target, context
    )
    assert captured["schema"] is ProductLongImagePlan
    assert captured["stage"] == "plan_product_long_image"
    assert "red cup" not in captured["instruction"]
    assert "容量500ml" in captured["instruction"] and "99.9%" in captured["instruction"]
    assert captured["images"][0][1] == context.materials[0].data
    assert len({s.visual_prompt for s in plan.sections}) == 6
    assert not {"materials", "usage", "pain_points"} & {s.role for s in plan.sections}


@pytest.mark.parametrize(
    "damage",
    [
        "missing_copy",
        "duplicate_ids",
        "count",
    ],
)
async def test_plan_rejects_unknown_fact_or_missing_exact_copy(request_data, damage):
    from image_agent.product_long_image import validate_product_long_image_plan

    request, target, context = await planning_context(request_data)
    plan = authored_plan(request, target, context)
    if damage == "unknown_fact":
        plan.sections[0].copy[0].fact_ids = ["red"]
    elif damage == "unknown_quote":
        plan.sections[0].copy[0].source_quote = "nonexistent"
    elif damage == "missing_copy":
        plan.sections[0].copy.pop()
    elif damage == "duplicate_ids":
        plan.sections[1].section_id = plan.sections[0].section_id
    elif damage == "count":
        plan.sections.pop()
    elif damage == "claim_from_editorial":
        plan.sections[0].copy[0].source_kind = "editorial"
    else:
        context.analysis.facts[0].evidence[0].source_type = "inference"
    with pytest.raises(ProviderError):
        validate_product_long_image_plan(request, target, context, plan)


async def test_full_section_prompt_contains_copy_and_identity(request_data):
    from image_agent.product_long_image import build_product_long_image_prompt

    request, target, context = await planning_context(request_data)
    plan = authored_plan(request, target, context)
    element_plan = compile_element_plan(request, target, context, AgentConfig())
    prompt = build_product_long_image_prompt(
        request, target, context, element_plan, plan, plan.sections[0]
    )
    assert request.long_image.required_copy[0] in prompt
    assert plan.style_direction in prompt and "white ceramic cup" in prompt
    assert "Reference" in prompt and "m1" in prompt
    assert "entire" in prompt.lower() and "文字由程序" not in prompt


async def test_fingerprint_updates_without_invalidating_analysis(request_data):
    from image_agent.product_long_image import product_long_image_fingerprint

    request, target, context = await planning_context(request_data)
    original = product_long_image_fingerprint(request, target, context)
    before = context.analysis.fingerprint
    for changed in [
        {"width": 800},
        {"known_info": "new"},
        {"required_copy": ["new"]},
        {"segment_count": 7},
    ]:
        altered = request.model_copy(deep=True)
        altered.long_image = altered.long_image.model_copy(update=changed)
        assert product_long_image_fingerprint(altered, target, context) != original
    assert (
        product_long_image_fingerprint(
            request, target.model_copy(update={"platform": "jd"}), context
        )
        != original
    )
    changed_context = copy.deepcopy(context)
    changed_context.selection.preferred_element_ids = []
    assert product_long_image_fingerprint(request, target, changed_context) != original
    assert context.analysis.fingerprint == before


async def test_fact_copy_can_translate_and_editorial_heading_is_not_a_claim(request_data):
    from image_agent.product_long_image import validate_product_long_image_plan

    request, target, context = await planning_context(request_data)
    plan = authored_plan(request, target, context)
    plan.sections[0].copy[0].text = "白色陶瓷杯"
    plan.sections[1].copy[0].source_kind = "editorial"
    plan.sections[1].copy[0].source_quote = ""
    plan.sections[1].copy[0].fact_ids = []
    plan.sections[1].copy[0].text = "材质细节"
    assert (
        validate_product_long_image_plan(request, target, context, plan).sections[0].copy[0].text
        == "白色陶瓷杯"
    )


@pytest.mark.parametrize("mixed", [False, True])
async def test_shared_style_element_cannot_import_unselected_product_facts(request_data, mixed):
    from image_agent.models import LoadedMaterial, MaterialAnalysis
    from image_agent.product_long_image import (
        selected_product_evidence,
        validate_product_long_image_plan,
    )
    from image_agent.selection import resolve_selection
    from image_agent.vision import VisionClient
    from tests.fakes import analysis_data

    request, target, context = await planning_context(request_data)
    data = analysis_data()
    data["materials"].append(
        dict(material_id="m2", sha256="hash2", observed_role="identity", summary="red cup")
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
    analysis = MaterialAnalysis(**data)
    resolved = resolve_selection(request, analysis)
    assert not resolved.blocking_issues and resolved.candidate.subject_ids == ["s1"]
    context = context.model_copy(
        update={
            "analysis": analysis,
            "selection": resolved.candidate,
            "materials": (
                *context.materials,
                LoadedMaterial(
                    material_id="m2",
                    sha256="hash2",
                    data=context.materials[0].data,
                    order=1,
                    role_hint="style",
                ),
            ),
        }
    )
    evidence = selected_product_evidence(context)
    assert "red ceramic cup" not in str(evidence)
    assert "f2" not in [fid for element in evidence["elements"] for fid in element["fact_ids"]]
    captured = {}

    class Capture(VisionClient):
        async def _call(self, schema, instruction, images, **kwargs):
            captured.update(instruction=instruction, images=images)
            return kwargs["validate"](authored_plan(request, target, context))

    await Capture(None, AgentConfig()).plan_product_long_image(request, target, context)
    assert "red ceramic cup" not in captured["instruction"]
    assert [label for label, _ in captured["images"]] == ["Selected original m1"]
    plan = authored_plan(request, target, context)
    plan.sections[0].copy[0].fact_ids = ["f2"]
    plan.sections[0].copy[0].source_quote = "red ceramic cup"
    plan.sections[0].copy[0].text = "红色陶瓷杯"
    validated = validate_product_long_image_plan(request, target, context, plan)
    assert validated.sections[0].copy[0].fact_ids == []
    assert "red ceramic cup" not in str(validated.source_evidence)


async def test_copy_audit_checks_planned_claim_against_bound_selected_evidence(request_data):
    import json

    from image_agent.product_long_image import validate_product_long_image_plan
    from image_agent.product_long_image_models import ProductLongImageCopyAudit
    from image_agent.vision import VisionClient

    request, target, context = await planning_context(request_data)
    plan = authored_plan(request, target, context)
    # A valid source quote alone is not proof that a semantic paraphrase is faithful.
    plan.sections[1].copy[0].text = "抗菌99.9%"
    plan = validate_product_long_image_plan(request, target, context, plan)
    captured = {}

    class Capture(VisionClient):
        async def _call(self, schema, instruction, images, **kwargs):
            captured.update(instruction=instruction)
            return ProductLongImageCopyAudit(
                passed=False,
                text_matches=True,
                no_invented_claims=False,
                layout_readable=True,
                issues=["计划中的功效没有来源"],
            )

    audit = await Capture(None, AgentConfig()).audit_product_long_image_copy(
        request, plan.sections[1], plan, context.materials[0].data
    )
    instruction = captured["instruction"]
    assert "actual user inputs and source images" in instruction
    assert "faithful translation" in instruction
    assert "not optional source annotations" in instruction
    assert json.dumps(context.analysis.facts[0].model_dump(), ensure_ascii=False) in instruction
    assert not audit.passed and not audit.no_invented_claims
