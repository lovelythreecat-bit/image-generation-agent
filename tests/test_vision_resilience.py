import json

import pytest

from image_agent.config import AgentConfig
from image_agent.errors import ProviderError
from image_agent.models import GeneratedImageAudit
from image_agent.vision import VisionClient
from tests.test_images import picture


def audit_data():
    return dict(
        subject_checks=[],
        fact_checks=[],
        element_checks=[],
        constraint_checks=[],
        visual_quality=90,
        platform_compliance=90,
        garment_fusion=90,
        model_preference=90,
        output_intent=90,
        passed=True,
        reason="ok",
    )


class RawTransport:
    def __init__(self, messages):
        self.messages = list(messages)
        self.calls = []

    async def post_json(self, path, payload):
        self.calls.append((path, payload))
        message = self.messages.pop(0)
        return {"choices": [{"message": message}]}


@pytest.mark.parametrize("shape", ["fence", "text_blocks", "object"])
async def test_supported_vision_content_wrappers_do_not_trigger_an_extra_call(shape):
    value = audit_data()
    raw = json.dumps(value)
    content = {
        "fence": f"```json\n{raw}\n```",
        "text_blocks": [{"type": "text", "text": raw}],
        "object": value,
    }[shape]
    transport = RawTransport([{"content": content}])
    result = await VisionClient(transport, AgentConfig())._call(
        GeneratedImageAudit, "audit", [], stage="audit_image"
    )
    assert result.visual_quality == 90 and result.passed
    assert len(transport.calls) == 1


async def test_schema_failure_is_corrected_once_with_same_images_and_field_diagnostics():
    bad = audit_data()
    bad["visual_quality"] = "not-a-score-private-value"
    transport = RawTransport([{"content": json.dumps(bad)}, {"content": json.dumps(audit_data())}])
    result = await VisionClient(transport, AgentConfig())._call(
        GeneratedImageAudit, "audit", [("generated", picture())], stage="audit_image"
    )
    assert result.passed and len(transport.calls) == 2
    first, second = [p["messages"][0]["content"] for _, p in transport.calls]
    assert first[1:] == second[1:]
    assert "visual_quality" in second[0]["text"]
    assert "not-a-score-private-value" not in second[0]["text"]


async def test_repeated_schema_failure_is_bounded_and_names_stage_and_fields():
    bad = audit_data()
    del bad["fact_checks"]
    transport = RawTransport([{"content": json.dumps(bad)}] * 3)
    with pytest.raises(ProviderError) as error:
        await VisionClient(transport, AgentConfig())._call(
            GeneratedImageAudit, "audit", [], stage="audit_image"
        )
    assert len(transport.calls) == 2
    assert "audit_image" in error.value.message and "fact_checks" in error.value.message
    assert "missing" in error.value.message


async def test_invalid_json_is_corrected_but_refusal_is_not_retried():
    transport = RawTransport(
        [{"content": '{"visual_quality":'}, {"content": json.dumps(audit_data())}]
    )
    result = await VisionClient(transport, AgentConfig())._call(GeneratedImageAudit, "audit", [])
    assert result.passed and len(transport.calls) == 2
    transport = RawTransport([{"content": None, "refusal": "private refusal text"}])
    with pytest.raises(ProviderError) as error:
        await VisionClient(transport, AgentConfig())._call(GeneratedImageAudit, "audit", [])
    assert len(transport.calls) == 1
    assert "refusal" in error.value.code
    assert "private refusal text" not in error.value.message


async def test_valid_negative_audit_is_not_rewritten_as_success():
    bad = audit_data() | {"passed": False, "visual_quality": 30}
    transport = RawTransport([{"content": json.dumps(bad)}])
    result = await VisionClient(transport, AgentConfig())._call(GeneratedImageAudit, "audit", [])
    assert not result.passed and result.visual_quality == 30
    assert len(transport.calls) == 1


async def test_focused_identity_ids_are_corrected_with_original_images(request_data):
    from image_agent.models import CreationRequest
    from image_agent.prompt import build_targets
    from image_agent.selection import compile_element_plan
    from tests.test_selection import context

    request = CreationRequest(**request_data)
    ctx = context(request)
    ctx.materials[0].data = picture()
    plan = compile_element_plan(
        request, build_targets(request, "product_only")[0], ctx, AgentConfig()
    )
    wrong = {
        "subject_checks": [
            {"subject_id": "wrong", "score": 90, "same_product": True, "reason": "same"}
        ]
    }
    correct = {
        "subject_checks": [
            {"subject_id": "s1", "score": 81, "same_product": False, "reason": "rim differs"}
        ]
    }
    transport = RawTransport([{"content": wrong}, {"content": correct}])
    result = await VisionClient(transport, AgentConfig()).review_product(
        ctx, plan, ["s1"], picture()
    )
    assert result.subject_checks[0].subject_id == "s1"
    assert not result.subject_checks[0].same_product
    assert len(transport.calls) == 2
    first, second = [payload["messages"][0]["content"] for _, payload in transport.calls]
    assert first[1:] == second[1:]
    assert "missing=" in second[0]["text"] and "unknown=" in second[0]["text"]


@pytest.mark.parametrize("missing", ["verifiability", "source_type", "unknown"])
async def test_fresh_discovery_corrects_missing_semantic_provenance(request_data, missing):
    from copy import deepcopy

    from image_agent.models import CreationRequest
    from tests.fakes import analysis_data
    from tests.test_vision import materials

    correct = analysis_data()
    correct["facts"][0]["verifiability"] = "visible_appearance"
    correct["facts"][0]["evidence"][0]["source_type"] = "visual"
    bad = deepcopy(correct)
    if missing == "verifiability":
        del bad["facts"][0]["verifiability"]
    elif missing == "source_type":
        del bad["facts"][0]["evidence"][0]["source_type"]
    else:
        bad["facts"][0]["evidence"][0]["source_type"] = "unknown"
    transport = RawTransport([{"content": bad}, {"content": correct}])
    result = await VisionClient(transport, AgentConfig()).analyze_materials(
        CreationRequest(**request_data), materials()
    )
    assert result.facts[0].verifiability == "visible_appearance"
    assert result.facts[0].evidence[0].source_type == "visual"
    assert len(transport.calls) == 2
    assert "physical label" in transport.calls[0][1]["messages"][0]["content"][0]["text"]


@pytest.mark.parametrize("source", ["brief", "hint"])
@pytest.mark.parametrize("correct_second", [True, False])
async def test_discovery_source_quote_uses_bounded_correction_and_real_budget(
    request_data, tmp_path, correct_second, source
):
    from copy import deepcopy

    import httpx

    from image_agent.execution import BudgetLedger, ExecutionPolicy, call_scope
    from image_agent.models import CreationRequest
    from image_agent.transport import HttpTransport
    from tests.fakes import analysis_data
    from tests.test_vision import materials

    brief = "手机壳\n磁吸\n液态壳\n二次元\n可爱\nMiku\n初音未来"
    request = CreationRequest(**(request_data | {"creative_brief": brief}))
    request.materials[0].element_hints = ["Miku", "初音未来"]
    exact_quote = "Miku\n初音未来" if source == "brief" else "Miku"
    bad = analysis_data()
    bad["intent"]["constraints"] = [
        dict(
            constraint_id="style",
            kind="appearance",
            subject_ids=["s1"],
            element_ids=["e1"],
            instruction="preserve the requested illustration",
            source=source,
            source_quote="二次元 可爱 Miku" if source == "brief" else "可爱",
            source_material_id=None if source == "brief" else "m1",
            priority="required",
        )
    ]
    good = deepcopy(bad)
    good["intent"]["constraints"][0]["source_quote"] = exact_quote
    replies = [bad, good if correct_second else bad]
    submitted = []

    def respond(http_request):
        submitted.append(json.loads(http_request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(replies.pop(0))}}]}
        )

    ledger = BudgetLedger(tmp_path / "quote-budget.sqlite", ExecutionPolicy(max_vision_calls=2))
    try:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            vision = VisionClient(HttpTransport(AgentConfig(), client=client), AgentConfig())
            with call_scope(ledger, "vision"):
                if correct_second:
                    result = await vision.analyze_materials(request, materials())
                    assert result.intent.constraints[0].source_quote == exact_quote
                    result.validate_intent_source(request)
                else:
                    with pytest.raises(ProviderError, match="source_quote") as caught:
                        await vision.analyze_materials(request, materials())
                    assert "analyze_materials" in caught.value.message
                    assert "attempt=2/2" in caught.value.message
        assert len(submitted) == 2
        assert ledger.usage() == {"image_calls": 0, "vision_calls": 2}
        first, second = [payload["messages"][0]["content"] for payload in submitted]
        assert first[1:] == second[1:]
        assert "source_quote" in second[0]["text"]
        assert "previous response failed validation" in second[0]["text"]
        for prompt in (first[0]["text"], second[0]["text"]):
            assert "source=brief" in prompt and "source=hint" in prompt
            assert "verbatim contiguous substring" in prompt
            assert "Never summarize or join separate lines/hints" in prompt
            assert "product_name, category or image OCR" in prompt
    finally:
        ledger.close()


async def test_discovery_material_ids_are_corrected_against_loaded_sources(request_data):
    from image_agent.models import CreationRequest
    from tests.fakes import analysis_data
    from tests.test_vision import materials

    valid = analysis_data()
    wrong = json.loads(json.dumps(valid).replace('"m1"', '"other"'))
    transport = RawTransport([{"content": wrong}, {"content": valid}])
    result = await VisionClient(transport, AgentConfig()).analyze_materials(
        CreationRequest(**request_data), materials()
    )
    assert [item.material_id for item in result.materials] == ["m1"]
    assert len(transport.calls) == 2
    first, second = [payload["messages"][0]["content"] for _, payload in transport.calls]
    assert first[1:] == second[1:]
    assert "material IDs/order mismatch" in second[0]["text"]
