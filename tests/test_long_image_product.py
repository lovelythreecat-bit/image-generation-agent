import hashlib
import json

import pytest

from image_agent.config import AgentConfig
from image_agent.errors import AgentError
from image_agent.execution import BudgetLedger, ExecutionPolicy, call_scope, reserve_post
from image_agent.long_image_models import LongAnalysisRef, LongImagePlan, LongImageRequest
from image_agent.long_image_requirements import collect_text_requirements, planning_fingerprint
from image_agent.long_image_store import content_hash
from image_agent.models import LoadedMaterial, MaterialAnalysis
from tests.fakes import FakeVision, analysis_data
from tests.test_long_image_planning import png


def inputs(brief="商品介绍", **changes):
    request = LongImageRequest(
        scene="product_detail",
        title="真实杯子",
        content_brief=brief,
        product_context={"product_name": "瓷杯", "category": "家居"},
        materials=[{"material_id": "m1", "source": {"data": png()}}],
        **changes,
    )
    materials = (
        LoadedMaterial(
            material_id="m1",
            data=png(),
            sha256=hashlib.sha256(png()).hexdigest(),
            order=0,
            role_hint="auto",
        ),
    )
    return request, materials


def product_plan(request, materials, product):
    requirements = collect_text_requirements(request)
    return LongImagePlan(
        input_fingerprint=planning_fingerprint(
            request, {m.material_id: m.sha256 for m in materials}
        ),
        scene="product_detail",
        visual_direction="自然光、浅色背景",
        analysis_ref=LongAnalysisRef(
            analysis_id=product.analysis.analysis_id,
            fingerprint=product.analysis.fingerprint,
            content_hash=content_hash(product.analysis.model_dump(mode="json")),
        ),
        resolved_selection=product.context.selection,
        requirements=requirements,
        image_slots=[
            dict(
                slot_id="pic",
                source="generate",
                role="hero",
                prompt="杯子全貌",
                aspect_ratio="1:1",
                section_ids=["hero"],
            )
        ],
        sections=[
            dict(
                section_id="hero",
                role="hero",
                slot_ids=["pic"],
                text_units=[
                    dict(
                        unit_id=f"u{i}",
                        kind="body",
                        text=r.text,
                        source=r.source,
                        requirement_ids=[r.requirement_id],
                    )
                    for i, r in enumerate(requirements)
                ],
            ),
            dict(section_id="end", role="closing"),
        ],
    )


class CountingVision(FakeVision):
    async def analyze_materials(self, request, materials):
        reserve_post()
        self.real_request = request
        return await super().analyze_materials(request, materials)

    async def validate_evidence(self, request, materials, analysis, draft):
        reserve_post()
        return await super().validate_evidence(request, materials, analysis, draft)


@pytest.mark.asyncio
async def test_product_adapter_uses_actual_long_context_and_source_hashes():
    from image_agent.long_image_product import prepare_long_product

    request, materials = inputs()
    vision = CountingVision()
    ledger = BudgetLedger(None, ExecutionPolicy())
    with call_scope(ledger, "vision"):
        product = await prepare_long_product(request, materials, analysis=None, vision=vision)
    assert product.status == "ready"
    assert product.context.selection.primary_subject_id == "s1"
    assert product.analysis.materials[0].sha256 == materials[0].sha256
    assert vision.real_request.product_name == "瓷杯"
    assert vision.real_request.category == "家居"
    assert vision.real_request.creative_brief == "商品介绍"
    assert vision.real_request.platforms == []
    assert ledger.usage()["vision_calls"] == 1
    ledger.close()


@pytest.mark.asyncio
async def test_two_products_selection_uses_same_snapshot():
    from image_agent.long_image_product import prepare_long_product

    request, materials = inputs()
    data = analysis_data()
    data["subjects"].append(
        dict(
            subject_id="s2",
            material_ids=["m1"],
            identity_fact_ids=["f2"],
            representative_material_id="m1",
            matches_product=True,
        )
    )
    data["facts"].append(
        dict(
            fact_id="f2",
            subject_id="s2",
            description="红杯",
            verifiability="visible_appearance",
            confidence=0.9,
            evidence=[dict(material_id="m1", observation="红杯", source_type="visual")],
        )
    )
    data["status"] = "needs_input"
    data["intent"]["proposal"].update(
        primary_subject_id=None, subject_ids=[], preferred_element_ids=[]
    )
    data["issues"] = [
        dict(
            issue_id="choose",
            code="ambiguity",
            resolution="selection",
            message="选择一个杯子",
            subject_ids=["s1", "s2"],
        )
    ]
    vision = CountingVision(discovery=data)
    ledger = BudgetLedger(None, ExecutionPolicy())
    with call_scope(ledger, "vision"):
        first = await prepare_long_product(request, materials, analysis=None, vision=vision)
        selected = request.model_copy(
            update={
                "selection": request.selection.model_validate(
                    {"mode": "explicit", "subject_ids": ["s2"]}
                )
            }
        )
        second = await prepare_long_product(
            selected, materials, analysis=first.analysis, vision=vision
        )
    assert first.status == "needs_input"
    assert second.status == "ready"
    assert first.analysis == second.analysis
    assert second.context.selection.primary_subject_id == "s2"
    assert ledger.usage()["vision_calls"] == 2
    assert vision.calls == ["discover", "evidence"]
    ledger.close()


@pytest.mark.asyncio
async def test_exact_copy_does_not_enter_slot_must_show_but_native_label_does():
    from image_agent.long_image_product import compile_long_product_slot, prepare_long_product

    request, materials = inputs(
        "展示杯子。页面写“2026年10月12日开售”",
        required_copy=[{"requirement_id": "claim", "text": "销量第一"}],
    )
    data = analysis_data()
    data["facts"][0].update(
        description="杯上原生印字 ACME",
        evidence=[dict(material_id="m1", observation="印字 ACME", source_type="product_label")],
    )
    data["intent"]["constraints"] = [
        dict(
            constraint_id="page-date",
            kind="marketing_claim",
            subject_ids=[],
            element_ids=[],
            instruction="2026年10月12日开售",
            source="brief",
            source_quote="2026年10月12日开售",
            source_material_id=None,
            priority="required",
        )
    ]
    product = await prepare_long_product(
        request, materials, analysis=None, vision=FakeVision(discovery=data)
    )
    plan = product_plan(request, materials, product)
    prepared = compile_long_product_slot(request, plan.image_slots[0], plan, product)
    serialized = json.dumps(prepared.element_plan.model_dump(), ensure_ascii=False)
    assert "page-date" not in serialized
    assert "2026年10月12日开售" not in prepared.prompt
    assert "销量第一" not in prepared.prompt
    assert "ACME" in prepared.prompt
    assert any(
        r.target_id == "f1" and r.applicability == "must_show"
        for r in prepared.element_plan.requirements
    )
    assert prepared.references[0].material_id == "m1"
    assert prepared.target.platform == ""


@pytest.mark.asyncio
async def test_product_global_direction_preserves_native_label_with_same_page_copy():
    from image_agent.long_image_product import compile_long_product_slot, prepare_long_product
    from image_agent.long_image_vision import validate_slot_copy_isolation

    request, materials = inputs(
        "商品介绍", required_copy=[{"requirement_id": "label", "text": "ACME"}]
    )
    discovery = analysis_data()
    discovery["facts"][0].update(
        description="杯上原生印字 ACME",
        evidence=[dict(material_id="m1", observation="印字 ACME", source_type="product_label")],
    )
    product = await prepare_long_product(
        request, materials, analysis=None, vision=FakeVision(discovery=discovery)
    )
    plan = product_plan(request, materials, product)
    plan.visual_direction = "自然光；保留杯上原生印字 ACME"
    validate_slot_copy_isolation(plan, product.analysis)
    compiled = compile_long_product_slot(request, plan.image_slots[0], plan, product)
    assert "自然光" in compiled.prompt
    assert "ACME" in compiled.prompt
    assert any(r.target_id == "f1" for r in compiled.element_plan.requirements)


@pytest.mark.asyncio
async def test_changed_discovery_content_rejects_snapshot_but_layout_does_not():
    from image_agent.long_image_product import prepare_long_product

    request, materials = inputs()
    vision = FakeVision()
    product = await prepare_long_product(request, materials, analysis=None, vision=vision)
    layout = request.model_copy(
        update={"options": request.options.model_copy(update={"width": 1000})}
    )
    assert (
        await prepare_long_product(layout, materials, analysis=product.analysis, vision=vision)
    ).status == "ready"
    changed = request.model_copy(update={"content_brief": "新的商品说明"})
    with pytest.raises(AgentError) as caught:
        await prepare_long_product(changed, materials, analysis=product.analysis, vision=vision)
    assert caught.value.code == "stale_analysis"


@pytest.mark.asyncio
async def test_product_scene_planning_and_trusted_reuse_skips_verification():
    from image_agent.long_image_planning import prepare_long_plan
    from image_agent.long_image_product import LongProductEvidenceRequest, PreparedLongProduct
    from image_agent.long_image_store import LongImageRunStore
    from image_agent.long_image_vision import LongImageVisionClient
    from image_agent.models import PreparedContext, ProductInputAudit
    from image_agent.selection import resolve_selection
    from tests.test_long_image_planning import Deps, Transport

    request, materials = inputs()

    def plan_response(payload):
        instruction = payload["messages"][0]["content"][0]["text"]
        # The real planner sends a frozen analysis/ref/selection in the instruction.
        start = instruction.index('{"request":')
        end = instruction.index("\nReturn one JSON object")
        context = json.loads(instruction[start:end])
        analysis = MaterialAnalysis(**context["analysis"])
        selection = resolve_selection(LongProductEvidenceRequest(request), analysis).candidate
        product = PreparedLongProduct(
            materials,
            analysis,
            "ready",
            context=PreparedContext(
                materials=materials,
                analysis=analysis,
                selection=selection,
                input_check=ProductInputAudit(matches=True, observed_product="杯子", reason="真实"),
                presentation_mode="product_only",
            ),
        )
        return {
            "status": "ready",
            "plan": product_plan(request, materials, product).model_dump()
            | {
                "sections": [
                    *product_plan(request, materials, product).model_dump()["sections"],
                    {"section_id": "more", "role": "bullets"},
                    {"section_id": "close", "role": "closing"},
                ]
            },
        }

    transport = Transport([analysis_data(), plan_response])
    config = AgentConfig()
    deps = Deps(LongImageVisionClient(transport, config))
    store = LongImageRunStore()
    store.initialize(request, materials, config, ExecutionPolicy())
    with store.budget() as ledger:
        result = await prepare_long_plan(request, config, deps, store, ledger)
    assert result.status == "ready"
    assert result.usage == {"vision_calls": 2, "image_calls": 0}
    transport.responses = []
    new_store = LongImageRunStore()
    new_store.initialize(request, materials, config, ExecutionPolicy())
    with new_store.budget() as ledger:
        reused = await prepare_long_plan(
            request,
            config,
            deps,
            new_store,
            ledger,
            plan=result.plan,
            analysis=result.analysis,
            planning_record=result.record,
        )
        assert ledger.usage()["vision_calls"] == 0
    assert reused.planning_id == result.planning_id
    assert len(transport.payloads) == 2


@pytest.mark.asyncio
async def test_product_audit_uses_scoped_quality_report_and_strict_ids():
    import io

    from PIL import Image

    from image_agent.long_image_product import compile_long_product_slot, prepare_long_product
    from image_agent.long_image_vision import LongImageVisionClient
    from tests.test_long_image_planning import Transport

    request, materials = inputs()
    product = await prepare_long_product(request, materials, analysis=None, vision=FakeVision())
    plan = product_plan(request, materials, product)
    prepared = compile_long_product_slot(request, plan.image_slots[0], plan, product)
    audit = await FakeVision().audit_image(
        prepared.evidence_request, prepared.target, prepared.context, prepared.element_plan, png()
    )
    out = io.BytesIO()
    Image.new("RGB", (768, 768), "white").save(out, "PNG")
    transport = Transport([audit.model_dump()])
    ledger = BudgetLedger(None, ExecutionPolicy())
    with call_scope(ledger, "vision"):
        quality = await LongImageVisionClient(transport, AgentConfig()).audit_product_slot(
            request, plan.image_slots[0], prepared, out.getvalue(), size="1K", model="test"
        )
    assert quality.passed
    assert quality.deterministic_checks.width == 768
    assert [f.fact_id for f in quality.fact_checks] == ["f1"]
    assert ledger.usage()["vision_calls"] == 1
    ledger.close()


@pytest.mark.asyncio
async def test_tampered_snapshot_primary_product_requires_real_evidence():
    from image_agent.long_image_product import prepare_long_product
    from image_agent.models import EvidenceValidation

    request, materials = inputs()
    product = await prepare_long_product(request, materials, analysis=None, vision=FakeVision())

    class RejectVision(CountingVision):
        async def validate_evidence(self, request, materials, analysis, draft):
            reserve_post()
            result = await FakeVision.validate_evidence(self, request, materials, analysis, draft)
            return EvidenceValidation(
                **(
                    result.model_dump()
                    | {
                        "outcome": "failed",
                        "subject_checks": [
                            {
                                "subject_id": "s1",
                                "score": 0,
                                "same_product": False,
                                "reason": "not original",
                            }
                        ],
                    }
                )
            )

    ledger = BudgetLedger(None, ExecutionPolicy())
    with call_scope(ledger, "vision"):
        checked = await prepare_long_product(
            request, materials, analysis=product.analysis, vision=RejectVision()
        )
    assert checked.status == "failed"
    assert checked.context is None
    assert checked.error_info.code == "input_mismatch"
    assert ledger.usage()["vision_calls"] == 1
    ledger.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("optional_element", [True, False])
async def test_product_native_label_is_not_exempted_as_optional_decoration(optional_element):
    from image_agent.long_image_product import compile_long_product_slot, prepare_long_product
    from image_agent.models import PixelChecks
    from image_agent.quality import evaluate_quality

    request, materials = inputs()
    data = analysis_data()
    data["facts"].append(
        dict(
            fact_id="native",
            subject_id="s1",
            description="杯上 ACME 原生印字",
            verifiability="visible_appearance",
            confidence=0.95,
            evidence=[dict(material_id="m1", observation="物理印字", source_type="product_label")],
        )
    )
    data["elements"].append(
        dict(
            element_id="label",
            subject_id="s1",
            kind="style",
            description="杯上 ACME 印字",
            fact_ids=["native"],
        )
    )
    data["intent"]["proposal"]["preferred_element_ids"].append("label")
    if not optional_element:
        data["intent"]["proposal"]["preferred_element_ids"].remove("label")
    product = await prepare_long_product(
        request, materials, analysis=None, vision=FakeVision(discovery=data)
    )
    plan = product_plan(request, materials, product)
    compiled = compile_long_product_slot(request, plan.image_slots[0], plan, product)
    assert "native" in compiled.element_plan.reference_bindings[0].fact_ids
    report = await FakeVision().audit_image(
        compiled.evidence_request, compiled.target, compiled.context, compiled.element_plan, png()
    )
    next(check for check in report.fact_checks if check.fact_id == "native").fidelity_score = 30
    quality = evaluate_quality(
        report,
        PixelChecks(passed=True, width=768, height=768, reason="ok"),
        compiled.target,
        compiled.element_plan,
    )
    assert not quality.passed
    assert any("native" in defect for defect in quality.failed_checks)


@pytest.mark.asyncio
async def test_product_source_slots_do_not_post_audits():
    from image_agent.long_image_models import LongImageSlot
    from image_agent.long_image_product import compile_long_product_slot, prepare_long_product
    from image_agent.long_image_vision import LongImageVisionClient
    from tests.test_long_image_planning import Transport

    request, materials = inputs()
    product = await prepare_long_product(request, materials, analysis=None, vision=FakeVision())
    plan = product_plan(request, materials, product)
    compiled = compile_long_product_slot(request, plan.image_slots[0], plan, product)
    source = LongImageSlot(
        slot_id="original",
        source="source",
        material_ids=["m1"],
        section_ids=["hero"],
        role="original",
    )
    transport = Transport([])
    with pytest.raises(AgentError) as caught:
        await LongImageVisionClient(transport, AgentConfig()).audit_product_slot(
            request, source, compiled, png(), size="1K"
        )
    assert caught.value.code == "audit_error"
    assert transport.payloads == []
