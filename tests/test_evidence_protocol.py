import json

import pytest

from image_agent.config import AgentConfig
from image_agent.errors import ProviderError
from image_agent.models import CreationRequest, MaterialAnalysis
from image_agent.selection import resolve_selection
from image_agent.vision import VisionClient
from tests.fakes import analysis_data
from tests.test_vision import ScriptTransport, materials


@pytest.fixture
def evidence_input(request_data):
    data = analysis_data()
    # The full snapshot contains an optional fact that must not be audited here.
    data["facts"].append({**data["facts"][0], "fact_id": "f_optional"})
    data["elements"][0]["fact_ids"] = ["f_optional"]
    analysis = MaterialAnalysis(**data)
    request = CreationRequest(**request_data)
    return request, materials(), analysis, resolve_selection(request, analysis)


def evidence_response(fact_ids=("f1",)):
    return dict(
        outcome="verified",
        subject_checks=[dict(subject_id="s1", score=95, same_product=True, reason="same")],
        fact_checks=[
            dict(fact_id=f, presence="present", fidelity_score=95, reason="visible")
            for f in fact_ids
        ],
        intent_valid=True,
        issue_resolutions=[],
    )


async def test_evidence_request_declares_exact_check_ids(evidence_input):
    transport = ScriptTransport([evidence_response()])
    result = await VisionClient(transport, AgentConfig()).validate_evidence(*evidence_input)
    assert result.outcome == "verified"
    assert len(transport.calls) == 1
    text = transport.calls[0][1]["messages"][0]["content"][0]["text"]
    payload, _ = json.JSONDecoder().raw_decode(text[text.index("{") :])
    assert payload["expected_check_ids"] == {
        "subject_id": ["s1"],
        "fact_id": ["f1"],
        "issue_id": [],
    }


@pytest.mark.parametrize("ids", [[], ["wrong"]])
async def test_evidence_corrects_id_mismatch_once(evidence_input, ids):
    transport = ScriptTransport([evidence_response(ids), evidence_response()])
    result = await VisionClient(transport, AgentConfig()).validate_evidence(*evidence_input)
    assert [c.fact_id for c in result.fact_checks] == ["f1"]
    assert len(transport.calls) == 2
    first, second = [p["messages"][0]["content"] for _, p in transport.calls]
    assert first[1:] == second[1:]  # Recheck against the same original images.
    assert first[0]["text"] != second[0]["text"]


async def test_evidence_repeated_mismatch_fails_with_id_details(evidence_input):
    transport = ScriptTransport([evidence_response(["wrong", "wrong"])] * 2)
    with pytest.raises(ProviderError) as error:
        await VisionClient(transport, AgentConfig()).validate_evidence(*evidence_input)
    assert len(transport.calls) == 2
    assert error.value.code == "provider_protocol"
    assert 'missing=["f1"]' in str(error.value)


@pytest.mark.parametrize("ids", [["f1", "f1"], ["f1", "f_optional"]])
async def test_evidence_harmless_surplus_and_identical_duplicates_need_no_correction(
    evidence_input, ids
):
    transport = ScriptTransport([evidence_response(ids)])
    result = await VisionClient(transport, AgentConfig()).validate_evidence(*evidence_input)
    assert [c.fact_id for c in result.fact_checks] == ["f1"]
    assert len(transport.calls) == 1


@pytest.mark.parametrize("kind", ["transport", "http", "protocol"])
async def test_evidence_does_not_retry_call_errors(evidence_input, kind):
    transport = ScriptTransport([ProviderError("failed", kind=kind)])
    with pytest.raises(ProviderError):
        await VisionClient(transport, AgentConfig()).validate_evidence(*evidence_input)
    assert len(transport.calls) == 1


async def test_evidence_does_not_retry_semantic_failure(evidence_input):
    response = evidence_response()
    response.update(outcome="failed", intent_valid=False)
    response["subject_checks"][0].update(score=20, same_product=False)
    transport = ScriptTransport([response])
    result = await VisionClient(transport, AgentConfig()).validate_evidence(*evidence_input)
    assert result.outcome == "failed" and not result.intent_valid
    assert not result.subject_checks[0].same_product
    assert len(transport.calls) == 1


async def test_schema_and_id_errors_share_one_correction_budget(evidence_input):
    invalid_schema = evidence_response()
    del invalid_schema["subject_checks"]
    transport = ScriptTransport([invalid_schema, evidence_response(["wrong"]), evidence_response()])
    with pytest.raises(ProviderError) as error:
        await VisionClient(transport, AgentConfig()).validate_evidence(*evidence_input)
    assert len(transport.calls) == 2
    assert "fact_id" in error.value.message and "missing" in error.value.message
