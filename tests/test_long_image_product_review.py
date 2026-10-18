"""Review regressions for product slot scope at real protocol boundaries."""

import json

import httpx
import pytest

from image_agent import create_long_image
from image_agent.errors import AgentError
from image_agent.long_image_models import LongImageRequirement, LongImageSlot
from image_agent.long_image_product import compile_long_product_slot, prepare_long_product
from image_agent.long_image_requirements import validate_long_image_plan
from scripts.long_image_offline_demo import (
    OfflineProvider,
    instruction_object,
    offline_config,
    offline_http,
)
from tests.fakes import FakeVision, analysis_data
from tests.test_long_image_product import inputs, product_plan

RED = "首屏配图使用红色背景"
BLUE = "细节配图使用蓝色背景"
GLOBAL = "柔和自然光、胶片摄影风格"


async def scoped_product(*, blue_required=False, **changes):
    request, materials = inputs(
        f"结构：{RED}；{BLUE}。", style_hint=GLOBAL, image_size="1K", **changes
    )
    discovery = analysis_data()
    discovery["facts"].append(
        dict(
            fact_id="native",
            subject_id="s1",
            description="杯上 ACME 原生印字",
            verifiability="visible_appearance",
            confidence=0.95,
            evidence=[dict(material_id="m1", observation="ACME", source_type="product_label")],
        )
    )
    discovery["elements"].append(
        dict(
            element_id="label",
            subject_id="s1",
            kind="style",
            description="杯上 ACME 原生印字",
            fact_ids=["native"],
        )
    )
    discovery["intent"]["proposal"]["preferred_element_ids"].append("label")
    discovery["intent"]["constraints"] = [
        dict(
            constraint_id=f"discovery-{color}",
            kind="atmosphere",
            subject_ids=[],
            element_ids=[],
            instruction=text,
            source="brief",
            source_quote=text,
            source_material_id=None,
            priority="required",
        )
        for color, text in [("red", RED), ("blue", BLUE)]
    ]
    product = await prepare_long_product(
        request, materials, analysis=None, vision=FakeVision(discovery=discovery)
    )
    assert product.status == "ready"
    plan = product_plan(request, materials, product)
    plan.visual_direction = GLOBAL
    plan.image_slots[0].requirement_ids = ["red"]
    plan.image_slots.append(
        LongImageSlot(
            slot_id="detail",
            source="generate",
            role="feature",
            prompt="杯子细节",
            aspect_ratio="1:1",
            section_ids=["end"],
            requirement_ids=["blue"],
        )
    )
    plan.sections[1].slot_ids = ["detail"]
    for color, text, slot_id, required in [
        ("red", RED, "pic", True),
        ("blue", BLUE, "detail", blue_required),
    ]:
        start = request.content_brief.index(text)
        plan.requirements.append(
            LongImageRequirement(
                requirement_id=color,
                kind="slot_visual",
                required=required,
                text=text,
                source=dict(pointer="/content_brief", start=start, end=start + len(text)),
                slot_ids=[slot_id],
            )
        )
    validate_long_image_plan(
        request, plan, material_hashes={"m1": materials[0].sha256}, analysis=product.analysis
    )
    return request, materials, product, plan


@pytest.mark.asyncio
@pytest.mark.parametrize("blue_required", [True, False])
async def test_slot_constraints_follow_plan_without_mutating_product_snapshot(blue_required):
    request, _, product, plan = await scoped_product(blue_required=blue_required)
    original_analysis = product.analysis.model_dump_json()
    original_context = product.context.model_dump_json()
    original_plan = plan.model_dump_json()
    for slot, own, other, priority, applicability in [
        (plan.image_slots[0], "red", BLUE, "required", "must_show"),
        (
            plan.image_slots[1],
            "blue",
            RED,
            "required" if blue_required else "preferred",
            "must_show" if blue_required else "preserve_if_visible",
        ),
    ]:
        compiled = compile_long_product_slot(request, slot, plan, product)
        assert [(c.constraint_id, c.priority) for c in compiled.element_plan.constraints] == [
            (own, priority)
        ]
        constraint = next(
            r for r in compiled.element_plan.requirements if r.target_kind == "constraint"
        )
        assert constraint.target_id == own and constraint.applicability == applicability
        assert other not in compiled.prompt
        assert GLOBAL in compiled.prompt and "ACME" in compiled.prompt
        assert {"f1", "native"} <= set(compiled.element_plan.required_fact_ids)
    assert product.analysis.model_dump_json() == original_analysis
    assert product.context.model_dump_json() == original_context
    assert plan.model_dump_json() == original_plan


class ScopedProvider(OfflineProvider):
    """Only HTTP responses are scripted; the graph and all clients remain real."""

    def __init__(self, request):
        super().__init__(request)
        self.image_payloads = []
        self.audit_payloads = []

    def respond(self, request):
        response = super().respond(request)
        if "/images/" in request.url.path:
            self.image_payloads.append(request.content.decode("utf-8", errors="replace"))
        else:
            payload = json.loads(request.content)
            text = payload["messages"][0]["content"][0]["text"]
            if "Audit the frozen subject, element and constraint requirements" in text:
                data = instruction_object(text, "expected_check_ids")
                self.audit_payloads.append(data)
                if len(self.audit_payloads) == 1:
                    value = json.loads(response.json()["choices"][0]["message"]["content"])
                    # A real failed current-slot constraint must enter the repair branch.
                    next(c for c in value["constraint_checks"] if c["constraint_id"] == "red")[
                        "satisfied"
                    ] = False
                    value["passed"] = False
                    response = httpx.Response(
                        200,
                        json={
                            "choices": [
                                {"message": {"content": json.dumps(value, ensure_ascii=False)}}
                            ]
                        },
                    )
        return response


@pytest.mark.asyncio
@pytest.mark.parametrize("blue_required", [True, False])
async def test_generation_audit_and_repair_payloads_only_consume_current_product_slot(
    tmp_path, blue_required
):
    request, _, product, plan = await scoped_product(
        output_dir=tmp_path, blue_required=blue_required
    )
    provider = ScopedProvider(request)
    with offline_http(provider):
        result = await create_long_image(
            request, offline_config(), plan=plan, analysis=product.analysis
        )
    assert result.status == "succeeded", result.error_info
    assert len(provider.image_payloads) == 3
    assert len(provider.audit_payloads) == 3
    for index, (own, own_text, other) in enumerate(
        [("red", RED, BLUE), ("red", RED, BLUE), ("blue", BLUE, RED)]
    ):
        generation = provider.image_payloads[index]
        audit = provider.audit_payloads[index]
        assert own_text in generation and other not in generation
        assert "discovery-red" not in generation and "discovery-blue" not in generation
        assert GLOBAL in generation and "ACME" in generation
        assert audit["expected_check_ids"]["constraint_id"] == [own]
        assert audit["plan"]["constraints"][0]["priority"] == (
            "preferred" if own == "blue" and not blue_required else "required"
        )
        assert "fact_id" not in audit["expected_check_ids"]
        assert {"f1", "native"} <= set(audit["plan"]["required_fact_ids"])
        serialized_audit = json.dumps(audit, ensure_ascii=False)
        assert own_text in serialized_audit and other not in serialized_audit
        assert "ACME" in serialized_audit
    assert "TARGETED REPAIR FEEDBACK" in provider.image_payloads[1]
    assert len(result.image_assets[0].candidates) == 2
    assert len(result.image_assets[1].candidates) == 1


@pytest.mark.asyncio
async def test_product_compiler_rejects_global_visual_direction_page_copy(tmp_path):
    request, materials = inputs("结构：介绍与结尾。必须展示“10 月 12 日开售”", output_dir=tmp_path)
    product = await prepare_long_product(request, materials, analysis=None, vision=FakeVision())
    plan = product_plan(request, materials, product)
    plan.visual_direction = "黑金风格；每张配图写出10 月 12 日开售"
    assert any(r.text == "10 月 12 日开售" for r in plan.requirements)
    with pytest.raises(AgentError) as caught:
        compile_long_product_slot(request, plan.image_slots[0], plan, product)
    assert caught.value.code == "plan_invalid"
    provider = ScopedProvider(request)
    with offline_http(provider):
        result = await create_long_image(
            request, offline_config(), plan=plan, analysis=product.analysis
        )
    assert result.status == "failed" and result.error_info.code == "plan_invalid"
    assert provider.image_payloads == [] and provider.audit_payloads == []


@pytest.mark.asyncio
async def test_product_compiler_allows_global_style_and_source_bound_native_label():
    request, _, product, plan = await scoped_product(
        required_copy=[dict(requirement_id="brand", text="ACME")]
    )
    plan.visual_direction = GLOBAL + "，保留杯上 ACME 原生印字"
    compiled = compile_long_product_slot(request, plan.image_slots[0], plan, product)
    assert "ACME" in compiled.prompt and GLOBAL in compiled.prompt
    assert "native" in compiled.element_plan.required_fact_ids
