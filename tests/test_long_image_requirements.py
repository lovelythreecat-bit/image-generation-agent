import pytest

from image_agent.errors import AgentError
from image_agent.long_image_models import LongImageOptions, LongImagePlan, LongImageRequest
from image_agent.long_image_requirements import (
    collect_text_requirements,
    planning_fingerprint,
    validate_long_image_plan,
)
from image_agent.models import MaterialAnalysis
from tests.fakes import analysis_data


def request_and_plan(**changes):
    request = LongImageRequest(scene="tutorial", title="入门", content_brief="介绍操作", **changes)
    requirements = collect_text_requirements(request)
    units = [
        {
            "unit_id": f"u{i}",
            "kind": "heading" if i == 0 else "body",
            "text": r.text,
            "source": r.source,
            "requirement_ids": [r.requirement_id],
        }
        for i, r in enumerate(requirements)
    ]
    plan = LongImagePlan(
        scene=request.scene,
        input_fingerprint=planning_fingerprint(request, {}),
        requirements=requirements,
        sections=[
            {"section_id": "first", "role": "hero", "text_units": units},
            {"section_id": "last", "role": "closing"},
        ],
    )
    return request, plan


def invalid(request, plan):
    with pytest.raises(AgentError) as caught:
        validate_long_image_plan(request, plan, material_hashes={})
    assert caught.value.code == "plan_invalid"


def test_exact_unicode_sources_and_local_quote_extraction():
    request = LongImageRequest(
        scene="promotion",
        title="发布 🚀",
        content_brief="写得有气势。必须展示“10 月 12 日\n０９:30 开售”",
        product_specs=[{"name": "规格", "value": "１２ ml"}],
        service_notes=["七天退换"],
    )
    requirements = collect_text_requirements(request)
    assert [r.text for r in requirements] == [
        "发布 🚀",
        "规格",
        "１２ ml",
        "七天退换",
        "10 月 12 日\n０９:30 开售",
    ]
    quote = requirements[-1]
    assert request.content_brief[quote.source.start : quote.source.end] == quote.text
    assert quote.source.pointer == "/content_brief"
    assert "写得有气势。" not in [r.text for r in requirements]


def test_same_text_distinct_ids():
    request, plan = request_and_plan(required_copy=[{"requirement_id": "copy", "text": "入门"}])
    validate_long_image_plan(request, plan, material_hashes={})
    plan.sections[0].text_units.pop()
    plan.sections[0].text_units[0].requirement_ids.append("copy")
    invalid(request, plan)


def test_missing_copy_fails_before_generation():
    request, plan = request_and_plan(
        required_copy=[{"requirement_id": "date", "text": "10 月 12 日"}]
    )
    plan.sections[0].text_units[-1].text = "10月12日"
    invalid(request, plan)


def test_text_order_and_occurrences_are_checked_by_id():
    request, plan = request_and_plan(required_copy=[{"requirement_id": "copy", "text": "第二句"}])
    plan.sections[0].text_units.reverse()
    invalid(request, plan)
    request, plan = request_and_plan()
    plan.sections[1].text_units = [
        plan.sections[0].text_units[0].model_copy(update={"unit_id": "duplicate"})
    ]
    invalid(request, plan)


def test_forged_sources_and_dropped_requirements_are_rejected():
    request, plan = request_and_plan()
    plan.requirements[0].source.pointer = "/content_brief"
    invalid(request, plan)
    request, plan = request_and_plan()
    plan.requirements.clear()
    plan.sections[0].text_units.clear()
    invalid(request, plan)


@pytest.mark.parametrize(
    "bad", ["dangling", "duplicate", "unreferenced", "reverse", "seventh", "text_visual"]
)
def test_slot_references_and_six_slot_limit(bad):
    request, plan = request_and_plan()
    slot = {
        "slot_id": "pic",
        "source": "generate",
        "role": "illustration",
        "prompt": "操作示意图",
        "aspect_ratio": "1:1",
        "section_ids": ["first"],
    }
    data = plan.model_dump()
    data["image_slots"] = [slot]
    data["sections"][0]["slot_ids"] = ["pic"]
    if bad == "dangling":
        data["sections"][0]["slot_ids"] = ["unknown"]
    elif bad == "duplicate":
        data["image_slots"].append(slot)
    elif bad == "unreferenced":
        data["sections"][0]["slot_ids"] = []
    elif bad == "reverse":
        slot["section_ids"] = ["last"]
    elif bad == "seventh":
        data["image_slots"] = [slot | {"slot_id": f"pic{i}"} for i in range(7)]
        data["sections"][0]["slot_ids"] = [f"pic{i}" for i in range(7)]
    else:
        slot["requirement_ids"] = ["title"]
    # Bypass constructor so the public pre-generation boundary must defend itself.
    invalid(request, LongImagePlan.model_construct(**data))


def test_material_hashes_and_input_changes_invalidate_fingerprint():
    request, plan = request_and_plan()
    request.content_brief = "不同的操作"
    invalid(request, plan)
    assert planning_fingerprint(request, {"m": "a"}) != planning_fingerprint(request, {"m": "b"})


@pytest.mark.parametrize(
    "options",
    [
        {"width": 1000},
        {"theme": "warm"},
        {"slice_height": 2000},
        {"font_path": "local-font.ttf"},
        {"width": 1000, "theme": "premium", "slice_height": 2000, "font_path": "font.ttf"},
    ],
)
def test_local_options_preserve_planning_fingerprint_and_plan(options):
    request, plan = request_and_plan()
    changed = request.model_copy(update={"options": LongImageOptions(**options)})
    assert planning_fingerprint(changed, {}) == plan.input_fingerprint
    validate_long_image_plan(changed, plan, material_hashes={})


@pytest.mark.parametrize(
    "changes",
    [
        {"title": "新标题"},
        {"content_brief": "不同的操作"},
        {"scene": "brand_intro"},
        {"style_hint": "温暖插画"},
        {"product_specs": [{"name": "容量", "value": "１２ ml"}]},
        {"service_notes": ["七天退换"]},
        {"required_copy": [{"requirement_id": "copy", "text": "必须保留"}]},
    ],
)
def test_content_changes_still_invalidate_planning_fingerprint(changes):
    request, plan = request_and_plan()
    changed = LongImageRequest.model_validate(request.model_dump() | changes)
    assert planning_fingerprint(changed, {}) != plan.input_fingerprint
    invalid(changed, plan)


@pytest.mark.parametrize(
    "changes",
    [
        {"product_context": {"product_name": "新杯子", "category": "日用品"}},
        {"selection": {"mode": "explicit", "subject_ids": ["subject-1"]}},
        {
            "materials": [
                {"material_id": "m1", "source": {"data": b"material"}, "subject_hint": "杯身"}
            ]
        },
    ],
)
def test_product_context_selection_and_material_hints_invalidate_fingerprint(changes):
    request = LongImageRequest(
        scene="product_detail",
        title="杯子",
        content_brief="介绍杯子",
        product_context={"product_name": "杯子", "category": "日用品"},
        materials=[{"material_id": "m1", "source": {"data": b"material"}}],
    )
    changed = LongImageRequest.model_validate(
        request.model_dump() | {"materials": request.materials} | changes
    )
    hashes = {"m1": "material-sha256"}
    assert planning_fingerprint(changed, hashes) != planning_fingerprint(request, hashes)


def test_table_cells_keep_original_names_and_values():
    request, plan = request_and_plan(product_specs=[{"name": "原始规格", "value": "１２ ml"}])
    cells = [u.model_copy(update={"kind": "cell"}) for u in plan.sections[0].text_units[1:]]
    plan.sections[0].text_units = plan.sections[0].text_units[:1]
    plan.sections[1].role = "table"
    plan.sections[1].table_rows = [cells]
    validate_long_image_plan(request, plan, material_hashes={})
    plan.sections[1].table_rows[0][0].text = "参数"
    invalid(request, plan)


def test_explicit_repetition_needs_separate_text_units():
    request, plan = request_and_plan(
        required_copy=[{"requirement_id": "copy", "text": "欢迎", "occurrences": 2}]
    )
    invalid(request, plan)
    plan.sections[1].text_units = [
        plan.sections[0].text_units[-1].model_copy(update={"unit_id": "repeat"})
    ]
    validate_long_image_plan(request, plan, material_hashes={})


def test_material_and_visual_requirement_references_are_checked():
    request, plan = request_and_plan()
    data = plan.model_dump()
    data["image_slots"] = [
        {
            "slot_id": "pic",
            "source": "source",
            "role": "reference",
            "material_ids": ["absent"],
            "section_ids": ["first"],
        }
    ]
    data["sections"][0]["slot_ids"] = ["pic"]
    invalid(request, LongImagePlan(**data))


def test_additional_model_recognized_copy_requires_real_source_range():
    request, plan = request_and_plan()
    data = plan.model_dump()
    data["requirements"].append(
        {
            "requirement_id": "extra",
            "text": "操作",
            "source": {"pointer": "/content_brief", "start": 2, "end": 4},
        }
    )
    data["sections"][1]["text_units"] = [
        {
            "unit_id": "extra",
            "kind": "body",
            "text": "操作",
            "source": {"pointer": "/content_brief", "start": 2, "end": 4},
            "requirement_ids": ["extra"],
        }
    ]
    validate_long_image_plan(request, LongImagePlan(**data), material_hashes={})
    data["requirements"][-1]["source"]["end"] = 400
    invalid(request, LongImagePlan(**data))


def test_product_plan_requires_exact_analysis_snapshot_binding():
    import hashlib
    import json

    analysis = MaterialAnalysis(**analysis_data())
    hashes = {m.material_id: m.sha256 for m in analysis.materials}
    request = LongImageRequest(
        scene="product_detail",
        title="杯子",
        content_brief="介绍杯子",
        product_context={"product_name": "杯子", "category": "日用品"},
        materials=[{"material_id": mid, "source": {"data": b"real-material"}} for mid in hashes],
    )
    requirement = collect_text_requirements(request)[0]
    proposal = analysis.intent.proposal.model_dump()
    plan = LongImagePlan(
        scene="product_detail",
        input_fingerprint=planning_fingerprint(request, hashes),
        requirements=[requirement],
        analysis_ref={
            "analysis_id": analysis.analysis_id,
            "fingerprint": analysis.fingerprint,
            "content_hash": hashlib.sha256(
                json.dumps(
                    analysis.model_dump(mode="json"),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode()
            ).hexdigest(),
        },
        resolved_selection=proposal | {"constraints": [], "focus_element_ids": []},
        sections=[
            {
                "section_id": "first",
                "role": "hero",
                "text_units": [
                    {
                        "unit_id": "title",
                        "kind": "heading",
                        "text": "杯子",
                        "source": {"pointer": "/title"},
                        "requirement_ids": ["title"],
                    }
                ],
            },
            {"section_id": "last", "role": "closing"},
        ],
    )
    validate_long_image_plan(request, plan, material_hashes=hashes, analysis=analysis)
    selected_primary = plan.resolved_selection.primary_subject_id
    plan.resolved_selection.primary_subject_id = "unknown"
    with pytest.raises(AgentError) as caught:
        validate_long_image_plan(request, plan, material_hashes=hashes, analysis=analysis)
    assert caught.value.code == "plan_invalid"
    plan.resolved_selection.primary_subject_id = selected_primary
    plan.analysis_ref.content_hash = "forged"
    with pytest.raises(AgentError) as caught:
        validate_long_image_plan(request, plan, material_hashes=hashes, analysis=analysis)
    assert caught.value.code == "plan_invalid"
