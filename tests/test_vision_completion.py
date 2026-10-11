"""Model completion uses previous output and source evidence; audits stay strict."""

import json
from copy import deepcopy

import pytest

from image_agent.config import AgentConfig
from image_agent.errors import ProviderError
from image_agent.models import CreationRequest, GeneratedImageAudit, MaterialAnalysis
from image_agent.vision import VisionClient
from tests.fakes import analysis_data
from tests.test_images import picture
from tests.test_product_long_image_planning import authored_plan, planning_context
from tests.test_vision import ScriptTransport, materials
from tests.test_vision_resilience import RawTransport, audit_data


def complete_discovery():
    return analysis_data() | {
        "creative_plan": {
            "user_requirements": [],
            "suggestions": [
                {"aspect": "lighting", "instruction": "柔和侧光展示杯身", "reason": "突出可见质感"}
            ],
        }
    }


async def test_product_plan_repairs_format_once_with_previous_json(request_data):
    request, target, context = await planning_context(request_data)
    valid = authored_plan(request, target, context).model_dump(mode="json")
    schema_bad = deepcopy(valid)
    schema_bad["style_direction"] = ""
    schema_bad["sections"][5]["role"] = "unsupported-theme"
    transport = ScriptTransport([schema_bad, valid])
    result = await VisionClient(transport, AgentConfig()).plan_product_long_image(
        request, target, context
    )
    assert result.style_direction == valid["style_direction"]
    assert len(result.sections) == 6 and result.sections[0].role == "hero"
    assert result.sections[1].visual_prompt == schema_bad["sections"][1]["visual_prompt"]
    calls = [p["messages"][0]["content"] for _, p in transport.calls]
    assert len(calls) == 2
    for content, previous in zip(calls[1:], [schema_bad]):
        assert json.dumps(previous) in content[0]["text"]
        assert content[1:] == calls[0][1:]
        assert (
            json.dumps(request.long_image.required_copy[0], ensure_ascii=False)
            in content[0]["text"]
        )


async def test_planning_malformed_json_exhaustion_is_bounded_and_has_stage():
    transport = RawTransport([{"content": '{"style_prompt":'}] * 6)
    with pytest.raises(ProviderError) as error:
        await VisionClient(transport, AgentConfig())._call(
            GeneratedImageAudit, "plan", [], stage="plan_long_image"
        )
    assert len(transport.calls) == 4
    assert "plan_long_image" in error.value.message and "attempt=4/4" in error.value.message
    assert '{"style_prompt":' in transport.calls[1][1]["messages"][0]["content"][0]["text"]


async def test_completion_budget_can_disable_corrective_calls():
    transport = RawTransport([{"content": "bad json"}] * 3)
    with pytest.raises(ProviderError):
        await VisionClient(transport, AgentConfig(model_completion_attempts=1))._call(
            GeneratedImageAudit, "plan", [], stage="plan_long_image"
        )
    assert len(transport.calls) == 1


@pytest.mark.parametrize("attempts", [0, 9])
def test_completion_budget_rejects_unbounded_configuration(attempts):
    with pytest.raises(ValueError):
        AgentConfig(model_completion_attempts=attempts)


@pytest.mark.parametrize("missing", ["omitted", "null"])
async def test_usable_discovery_supplements_missing_creative_plan_with_original_images(
    request_data, missing
):
    bad = analysis_data()
    bad.pop("creative_plan", None)
    if missing == "null":
        bad["creative_plan"] = None
    valid = complete_discovery()
    transport = ScriptTransport([bad, valid])
    result = await VisionClient(transport, AgentConfig()).analyze_materials(
        CreationRequest(**request_data), materials()
    )
    assert result.creative_plan.suggestions[0].instruction == "柔和侧光展示杯身"
    assert len(transport.calls) == 2
    first, second = [p["messages"][0]["content"] for _, p in transport.calls]
    assert json.dumps(bad) in second[0]["text"]
    assert first[1:] == second[1:]
    assert not any("未返回创作策划" in warning for warning in result.warnings)


async def test_completion_api_preserves_ids_inputs_and_initializes_runtime_identity(request_data):
    from image_agent.selection import analysis_fingerprint

    request = CreationRequest(**(request_data | {"creative_brief": "杯子促销，柔和光线"}))
    original = MaterialAnalysis(**(analysis_data() | {"creative_plan": None}))
    original.fingerprint = analysis_fingerprint(request, materials())
    supplemented = complete_discovery()
    supplemented["analysis_id"] = "provider-new-id"
    supplemented["materials"][0]["sha256"] = "provider-guess"
    transport = ScriptTransport([supplemented])
    result = await VisionClient(transport, AgentConfig()).complete_material_analysis(
        request, materials(), original, ["creative_plan missing", "use supported front view"]
    )
    assert result.analysis_id == "a1"
    assert result.fingerprint == analysis_fingerprint(request, materials())
    assert result.materials[0].sha256 == "hash"
    assert result.subjects == original.subjects and result.facts == original.facts
    content = transport.calls[0][1]["messages"][0]["content"]
    assert json.dumps(original.model_dump(mode="json"), ensure_ascii=False) in content[0]["text"]
    assert "creative_plan missing" in content[0]["text"]
    assert request.creative_brief in content[0]["text"]
    assert sum(row["type"] == "image_url" for row in content) == len(materials())


@pytest.mark.parametrize(
    "damage", ["subject_id", "visible_identity_fact", "element", "selected_elements", "unresolved"]
)
async def test_completion_rejects_identity_changes_instead_of_adopting_a_new_product(
    request_data, damage
):
    original = MaterialAnalysis(**(analysis_data() | {"creative_plan": None}))
    wrong = complete_discovery()
    if damage == "subject_id":
        wrong["subjects"][0]["subject_id"] = "replacement"
        wrong["facts"][0]["subject_id"] = "replacement"
        wrong["elements"][0]["subject_id"] = "replacement"
        wrong["intent"]["proposal"]["subject_ids"] = ["replacement"]
        wrong["intent"]["proposal"]["primary_subject_id"] = "replacement"
    elif damage == "visible_identity_fact":
        wrong["facts"][0]["description"] = "red ceramic cup"
        wrong["facts"][0]["evidence"][0]["observation"] = "red cup visible"
    elif damage == "element":
        wrong["elements"][0]["description"] = "invented hidden feature"
    elif damage == "selected_elements":
        wrong["intent"]["proposal"]["preferred_element_ids"] = []
    else:
        wrong["status"] = "needs_input"
    transport = ScriptTransport([wrong, complete_discovery()])
    result = await VisionClient(transport, AgentConfig()).complete_material_analysis(
        CreationRequest(**request_data), materials(), original, ["creative_plan missing"]
    )
    assert result.subjects == original.subjects and result.facts == original.facts
    assert len(transport.calls) == 2


async def test_audit_retry_limit_remains_two_and_negative_result_is_not_completed():
    malformed = audit_data()
    del malformed["visual_quality"]
    transport = RawTransport([{"content": malformed}] * 5)
    with pytest.raises(ProviderError):
        await VisionClient(transport, AgentConfig(model_completion_attempts=8))._call(
            GeneratedImageAudit, "audit", [], stage="audit_image"
        )
    assert len(transport.calls) == 2
    negative = audit_data() | {"passed": False, "visual_quality": 20}
    transport = RawTransport([{"content": negative}, {"content": audit_data()}])
    result = await VisionClient(transport, AgentConfig())._call(
        GeneratedImageAudit, "audit", [], stage="audit_image"
    )
    assert not result.passed and result.visual_quality == 20
    assert len(transport.calls) == 1


async def test_completion_does_not_retry_transport_errors_or_refusal():
    transport = ScriptTransport([ProviderError("timeout", kind="transport")])
    with pytest.raises(ProviderError):
        await VisionClient(transport, AgentConfig())._call(
            GeneratedImageAudit, "plan", [], stage="plan_long_image"
        )
    assert len(transport.calls) == 1
    transport = RawTransport([{"content": None, "refusal": "declined"}])
    with pytest.raises(ProviderError) as error:
        await VisionClient(transport, AgentConfig())._call(
            GeneratedImageAudit, "plan", [], stage="plan_long_image"
        )
    assert error.value.code == "vision_refusal" and len(transport.calls) == 1


async def test_completion_supports_long_product_adapter_without_creation_request_fields():
    from image_agent.long_image_product import LongProductEvidenceRequest
    from tests.test_long_image_product import inputs

    long_request, loaded = inputs()
    adapter = LongProductEvidenceRequest(long_request)
    original = MaterialAnalysis(**(analysis_data() | {"creative_plan": None}))
    result = await VisionClient(
        ScriptTransport([complete_discovery()]), AgentConfig()
    ).complete_material_analysis(adapter, loaded, original, ["creative_plan missing"])
    assert result.analysis_id == original.analysis_id
    assert result.fingerprint == original.fingerprint
    assert result.materials[0].sha256 == loaded[0].sha256
    assert result.creative_plan.suggestions


async def test_completion_reuses_every_original_picture_across_repairs(request_data):
    loaded = materials() + (
        materials()[0].model_copy(
            update={
                "material_id": "m2",
                "data": picture(color="gray"),
                "sha256": "hash2",
                "order": 1,
            }
        ),
    )
    request = CreationRequest(
        **(
            request_data
            | {
                "materials": [
                    {"material_id": m.material_id, "source": {"data": m.data}} for m in loaded
                ]
            }
        )
    )
    data = complete_discovery()
    data["materials"].append(
        {"material_id": "m2", "sha256": "hash2", "observed_role": "identity", "summary": "cup view"}
    )
    data["subjects"][0]["material_ids"].append("m2")
    original = MaterialAnalysis(**(data | {"creative_plan": None}))
    bad = deepcopy(data)
    bad["style_direction"] = "ignored provider extra"
    bad["creative_plan"] = None
    transport = ScriptTransport([bad, data])
    result = await VisionClient(transport, AgentConfig()).complete_material_analysis(
        request, loaded, original, ["creative_plan missing"]
    )
    assert [m.material_id for m in result.materials] == ["m1", "m2"]
    first, second = [p["messages"][0]["content"] for _, p in transport.calls]
    urls = [row["image_url"]["url"] for row in first if row["type"] == "image_url"]
    assert len(urls) == 2 and urls[0] != urls[1]
    assert first[1:] == second[1:]


async def test_fresh_missing_plan_repair_preserves_usable_discovered_product(request_data):
    original = analysis_data() | {"creative_plan": None}
    wrong = complete_discovery()
    wrong["facts"][0]["description"] = "red ceramic cup"
    transport = ScriptTransport([original, wrong, complete_discovery()])
    result = await VisionClient(transport, AgentConfig()).analyze_materials(
        CreationRequest(**request_data), materials()
    )
    assert result.facts[0].description == "white ceramic cup"
    assert result.creative_plan.suggestions
    assert len(transport.calls) == 3


async def test_needs_input_planning_gap_freezes_discovered_product_during_repair(request_data):
    original = analysis_data() | {
        "status": "needs_input",
        "creative_plan": None,
        "issues": [
            dict(
                issue_id="scene",
                code="missing_scene",
                resolution="reanalyze",
                message="Choose an optional scene for the confirmed product",
            )
        ],
    }
    wrong = complete_discovery()
    wrong["facts"][0]["description"] = "red ceramic cup"
    transport = ScriptTransport([original, wrong, complete_discovery()])
    result = await VisionClient(transport, AgentConfig()).analyze_materials(
        CreationRequest(**request_data), materials()
    )
    assert result.status == "ready" and result.creative_plan.suggestions
    assert result.facts[0].description == "white ceramic cup"
    assert result.subjects[0].identity_fact_ids == ["f1"]
    assert len(transport.calls) == 3
    contents = [payload["messages"][0]["content"] for _, payload in transport.calls]
    assert "changed frozen facts" in contents[2][0]["text"]
    assert contents[0][1:] == contents[1][1:] == contents[2][1:]


@pytest.mark.parametrize("issue_kind", ["ambiguous_subject", "missing_evidence", "missing_scene"])
async def test_fresh_identity_blocker_is_not_forced_ready_to_fill_missing_plan(
    request_data, issue_kind
):
    original = analysis_data() | {"status": "needs_input", "creative_plan": None}
    original["issues"] = [
        dict(
            issue_id="identity",
            code=issue_kind,
            resolution="selection" if issue_kind == "ambiguous_subject" else "reanalyze",
            message="Clarify the actual product identity or required visible appearance",
            subject_ids=["s1"],
            fact_ids=[] if issue_kind == "ambiguous_subject" else ["f1"],
        )
    ]
    transport = ScriptTransport([original, complete_discovery()])
    result = await VisionClient(transport, AgentConfig()).analyze_materials(
        CreationRequest(**request_data), materials()
    )
    assert result.status == "needs_input" and result.creative_plan is None
    assert result.issues[0].issue_id == "identity"
    assert result.facts[0].description == "white ceramic cup"
    assert len(transport.calls) == 1


async def test_locked_planning_snapshot_preserves_legacy_fingerprint_during_repair(request_data):
    original_data = analysis_data() | {"status": "needs_input", "creative_plan": None}
    original_data["issues"] = [
        dict(issue_id="scene", code="missing_scene", resolution="reanalyze", message="Choose scene")
    ]
    original_data["fingerprint"] = "legacy-standalone-discovery-fingerprint"
    original = MaterialAnalysis(**original_data)
    wrong = complete_discovery()
    wrong["facts"][0]["description"] = "red ceramic cup"
    transport = ScriptTransport([wrong, complete_discovery()])
    result = await VisionClient(transport, AgentConfig()).complete_material_analysis(
        CreationRequest(**request_data), materials(), original, ["missing_scene"]
    )
    assert result.fingerprint == "legacy-standalone-discovery-fingerprint"
    assert result.analysis_id == "a1" and result.facts == original.facts
    assert original.creative_plan is None and original.status == "needs_input"
    assert len(transport.calls) == 2


async def test_locked_snapshot_cannot_skip_validation_when_repair_invents_identity_blocker(
    request_data,
):
    original = MaterialAnalysis(**(analysis_data() | {"creative_plan": None}))
    wrong = analysis_data() | {"status": "needs_input", "creative_plan": None}
    wrong["issues"] = [
        dict(
            issue_id="invented",
            code="ambiguous_subject",
            resolution="selection",
            message="Choose product",
        )
    ]
    transport = ScriptTransport([wrong, complete_discovery()])
    result = await VisionClient(transport, AgentConfig()).complete_material_analysis(
        CreationRequest(**request_data), materials(), original, ["creative_plan missing"]
    )
    assert result.status == "ready" and result.creative_plan.suggestions
    assert result.fingerprint == original.fingerprint and result.facts == original.facts
    assert not result.issues and len(transport.calls) == 2


@pytest.mark.parametrize("provenance", ["unknown", "omitted"])
async def test_legacy_snapshot_completes_creative_plan_without_rewriting_provenance(
    request_data, provenance
):
    original_data = analysis_data() | {"creative_plan": None, "fingerprint": "legacy-fingerprint"}
    if provenance == "unknown":
        original_data["facts"][0]["evidence"][0]["source_type"] = "unknown"
    else:
        del original_data["facts"][0]["verifiability"]
        del original_data["facts"][0]["evidence"][0]["source_type"]
    original = MaterialAnalysis(**original_data)
    supplemented = original.model_dump(mode="json")
    supplemented["creative_plan"] = complete_discovery()["creative_plan"]
    changed = deepcopy(supplemented)
    changed["facts"][0]["evidence"][0]["source_type"] = "visual"
    transport = ScriptTransport([changed, supplemented, supplemented, supplemented])
    result = await VisionClient(transport, AgentConfig()).complete_material_analysis(
        CreationRequest(**request_data), materials(), original, ["creative_plan missing"]
    )
    assert result.creative_plan.suggestions
    assert result.facts == original.facts
    assert result.facts[0].evidence[0].source_type == "unknown"
    assert result.fingerprint == "legacy-fingerprint"
    assert len(transport.calls) == 2


async def test_fresh_unknown_provenance_still_exhausts_without_becoming_valid(request_data):
    invalid = complete_discovery()
    invalid["facts"][0]["evidence"][0]["source_type"] = "unknown"
    transport = ScriptTransport([invalid] * 5)
    with pytest.raises(ProviderError, match="source_type required"):
        await VisionClient(transport, AgentConfig()).analyze_materials(
            CreationRequest(**request_data), materials()
        )
    assert len(transport.calls) == 4
