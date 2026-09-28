import pytest
from pydantic import ValidationError

from image_agent import AgentConfig, CreationRequest, CreationRequestDTO
from tests.fakes import analysis_data
from tests.test_contracts import dto_data
from tests.test_pipeline import setup


@pytest.mark.parametrize("value", [None, [None], [123], 123, "taobao"])
def test_invalid_platform_container_returns_validation_error(request_data, value):
    with pytest.raises(ValidationError):
        CreationRequest(**(request_data | {"platforms": value}))
    with pytest.raises(ValidationError):
        CreationRequestDTO(**(dto_data() | {"platforms": value}))


@pytest.mark.parametrize(
    "name,want",
    [
        ("clipboard", True),
        ("img_123", True),
        ("upload-123", True),
        ("sku_ABC1", True),
        ("uploaded dress", False),
        ("pasted luxury cup", False),
    ],
)
def test_source_placeholder_regex_compatibility(request_data, name, want):
    from image_agent.prompt import is_placeholder

    r = CreationRequest(**(request_data | {"product_name": name, "category": "general"}))
    assert is_placeholder(r) is want


async def test_staged_final_plan_matches_actual_material_references(request_data):
    from image_agent.pipeline import run_pipeline

    d = analysis_data()
    d["materials"].append(
        dict(material_id="m2", sha256="hash", observed_role="scene", summary="garden")
    )
    d["facts"].append(
        dict(
            fact_id="f2",
            subject_id=None,
            description="garden",
            evidence=[dict(material_id="m2", observation="garden")],
            confidence=1,
        )
    )
    d["elements"].append(
        dict(element_id="e2", kind="scene", subject_id=None, description="garden", fact_ids=["f2"])
    )
    d["intent"]["proposal"]["preferred_element_ids"].append("e2")
    request_data["materials"].append(dict(material_id="m2", source={"data": b"x"}))
    r, deps, vision, gen = setup(request_data, discovery=d, scores={"main": [{"identity": 70}, {}]})
    result = await run_pipeline(r, AgentConfig(generation_reference_limit=2), dependencies=deps)
    assert result.status == "succeeded"
    actual = [ref.material_id for ref in gen.calls[-1]["references"] if ref.material_id]
    assert result.assets[0].element_plan.generation_material_ids == actual
    assert [
        binding.material_id
        for binding in result.assets[0].reference_bindings
        if binding.material_id
    ] == actual
    assert [b.material_id or b.stage_id for b in result.assets[0].reference_bindings] == [
        "m1",
        "stage",
    ]
    from image_agent.prompt import STRUCTURED_MARKER

    final_prompt = gen.calls[-1]["prompt"]
    assert '"material_id": "m2"' not in final_prompt.split(STRUCTURED_MARKER, 1)[1]
    assert '"stage_id": "stage"' in final_prompt


def test_primary_subject_reference_precedes_original_subject_order(request_data):
    from image_agent.prompt import build_targets
    from image_agent.selection import compile_element_plan
    from tests.test_acceptance import two_subjects
    from tests.test_selection import context

    data = two_subjects()
    data["intent"]["proposal"]["primary_subject_id"] = "s2"
    request = CreationRequest(**request_data)
    result = compile_element_plan(
        request, build_targets(request, "product_only")[0], context(request, data), AgentConfig()
    )
    assert result.generation_material_ids == ["m2", "m1"]


async def test_review_ignores_exempt_subjects(request_data):
    from image_agent.pipeline import run_pipeline
    from tests.test_acceptance import two_subjects

    data = two_subjects()
    request_data["materials"].append(dict(material_id="m2", source={"data": b"x"}))
    request_data["output_types"] = ["detail_page"]
    request, deps, vision, gen = setup(
        request_data,
        discovery=data,
        scores={"feature": [{"identity": 82}], "closeup": [{"identity": 82}]},
    )
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert len(gen.calls) == 3
    assert vision.calls.count("review") == 2


def test_model_messages_are_redacted_before_export_and_cli():
    from image_agent import result_to_bundle
    from image_agent.models import ErrorInfo, MaterialAnalysis, SubjectCheck
    from tests.test_output import result

    raw = "C:\\private\\input.jpg token=secret data:image/png;base64,AAAA " + "long " * 200
    data = analysis_data()
    data["warnings"] = [raw]
    data["issues"] = [
        dict(
            issue_id="i1",
            code="ambiguous_subject",
            resolution="selection",
            message=raw,
            options=[dict(id="o1", label=raw, selection=None)],
        )
    ]
    data["error_info"] = dict(
        code="provider_protocol", kind="protocol", message=raw, retryable=False
    )
    analysis = MaterialAnalysis(**data)
    assert len(analysis.warnings[0]) <= 500
    assert (
        "secret" not in analysis.model_dump_json()
        and "C:\\\\private" not in analysis.model_dump_json()
    )
    assert "data:image" not in analysis.issues[0].message
    check = SubjectCheck(subject_id="s1", score=80, same_product=False, reason=raw)
    assert len(check.reason) <= 500 and "secret" not in check.reason
    r = result()
    r.analysis = analysis
    r.warnings = [raw]
    r.error_info = ErrorInfo(code="provider_protocol", kind="protocol", message=raw)
    assert "secret" not in result_to_bundle(r).dto.model_dump_json()


async def test_exempt_subject_does_not_trigger_staged_repair(request_data):
    from image_agent.pipeline import run_pipeline
    from tests.test_acceptance import two_subjects

    data = two_subjects()
    request_data["materials"].append(dict(material_id="m2", source={"data": b"x"}))
    request_data["output_types"] = ["detail_page"]
    checks = [
        dict(subject_id="s1", score=90, same_product=True, reason="same"),
        dict(subject_id="s2", score=0, same_product=False, reason="not in crop"),
    ]
    request, deps, vision, gen = setup(
        request_data,
        discovery=data,
        scores={"feature": [{"subject_checks": checks, "output_intent": 74}, {}]},
    )
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert result.assets[1].generation_mode == "strict"
    assert len(gen.calls) == 4
