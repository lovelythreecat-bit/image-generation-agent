"""Fact checks no longer gate selection or generated-image delivery."""

import json

import pytest

from image_agent.config import AgentConfig
from image_agent.models import (
    CreationRequest,
    EvidenceValidation,
    GeneratedImageAudit,
    MaterialAnalysis,
    PixelChecks,
    Requirement,
)
from image_agent.quality import evaluate_quality
from image_agent.selection import finalize_selection, resolve_selection
from image_agent.vision import VisionClient
from tests.fakes import analysis_data
from tests.test_quality import audit, plan, target
from tests.test_vision import ScriptTransport, materials


@pytest.mark.parametrize("obsolete", [None, {"fact_checks": "invalid obsolete payload"}])
async def test_evidence_without_fact_checks_needs_no_correction(request_data, obsolete):
    request = CreationRequest(**request_data)
    analysis = MaterialAnalysis(**analysis_data())
    draft = resolve_selection(request, analysis)
    transport = ScriptTransport(
        [
            dict(
                outcome="verified",
                subject_checks=[dict(subject_id="s1", score=95, same_product=True, reason="same")],
                intent_valid=True,
                issue_resolutions=[],
            )
            | (obsolete or {})
        ]
    )
    evidence = await VisionClient(transport, AgentConfig()).validate_evidence(
        request, materials(), analysis, draft
    )
    assert finalize_selection(draft, evidence).status == "ready"
    assert len(transport.calls) == 1
    payload = transport.calls[0][1]
    text = payload["messages"][0]["content"][0]["text"]
    data, _ = json.JSONDecoder().raw_decode(text[text.index("{") :])
    assert data["expected_check_ids"] == {"subject_id": ["s1"], "issue_id": []}
    assert "fact_checks" not in EvidenceValidation.model_json_schema()["properties"]


def test_legacy_negative_fact_checks_do_not_block_selection(request_data):
    request = CreationRequest(**request_data)
    analysis = MaterialAnalysis(**analysis_data())
    evidence = EvidenceValidation(
        outcome="verified",
        subject_checks=[dict(subject_id="s1", score=95, same_product=True, reason="same")],
        fact_checks=[dict(fact_id="f1", presence="absent", fidelity_score=None, reason="no proof")],
        intent_valid=True,
        issue_resolutions=[],
    )
    assert finalize_selection(resolve_selection(request, analysis), evidence).status == "ready"


def test_legacy_fact_requirements_and_scores_do_not_fail_quality():
    frozen = plan()
    frozen.requirements.append(
        Requirement(
            target_kind="fact",
            target_id="f1",
            origin="user_required",
            applicability="must_show",
            reason="legacy requirement",
        )
    )
    value = audit().model_dump()
    value["fact_checks"] = [
        dict(fact_id="f1", presence="absent", fidelity_score=None, reason="no proof")
    ]
    quality = evaluate_quality(
        GeneratedImageAudit(**value),
        PixelChecks(passed=True, width=800, height=800, reason="ok"),
        target(),
        frozen,
    )
    assert quality.passed
    assert "fact_checks" not in quality.model_dump()


def test_generated_audit_without_fact_checks_is_accepted():
    value = audit().model_dump()
    value.pop("fact_checks", None)
    assert GeneratedImageAudit(**value).subject_checks[0].same_product
    assert "fact_checks" not in GeneratedImageAudit.model_json_schema()["properties"]
