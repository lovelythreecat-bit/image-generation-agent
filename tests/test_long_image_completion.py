"""Legacy product adapter shares source-bound AI planning completion."""

import pytest

from image_agent.execution import BudgetLedger, ExecutionPolicy, call_scope, reserve_post
from image_agent.long_image_product import long_discovery_fingerprint, prepare_long_product
from image_agent.models import CreativePlan, Issue, MaterialAnalysis
from tests.fakes import analysis_data
from tests.test_long_image_product import CountingVision, inputs


class CompletingVision(CountingVision):
    def __init__(self):
        super().__init__(discovery=analysis_data() | {"creative_plan": None})

    async def complete_material_analysis(self, request, materials, analysis, problems):
        reserve_post()
        assert request.product_name == "瓷杯"
        assert materials[0].material_id == "m1" and problems
        repaired = analysis.model_copy(deep=True)
        repaired.creative_plan = CreativePlan(
            user_requirements=[],
            suggestions=[
                dict(aspect="lighting", instruction="柔和侧光", reason="展示可见杯身轮廓")
            ],
        )
        return repaired


async def test_legacy_product_fills_missing_creative_plan_with_existing_sources():
    request, materials = inputs()
    ledger = BudgetLedger(None, ExecutionPolicy())
    try:
        with call_scope(ledger, "vision"):
            product = await prepare_long_product(
                request, materials, analysis=None, vision=CompletingVision()
            )
        assert product.status == "ready"
        assert product.context.analysis.creative_plan is not None
        assert product.context.analysis.creative_plan.suggestions[0].instruction == "柔和侧光"
        assert product.analysis.facts[0].description == "white ceramic cup"
        assert product.analysis.fingerprint == long_discovery_fingerprint(request, materials)
        assert ledger.usage() == {"image_calls": 0, "vision_calls": 2}
    finally:
        ledger.close()


class EvidenceCompletingVision(CompletingVision):
    def __init__(self, code):
        super().__init__()
        self.code = code

    async def validate_evidence(self, request, materials, analysis, draft):
        evidence = await super().validate_evidence(request, materials, analysis, draft)
        if self.calls.count("evidence") == 1:
            evidence.outcome = "needs_input"
            evidence.issues = [
                Issue(
                    issue_id="review-gap",
                    code=self.code,
                    resolution="reanalyze",
                    message="Missing presentation information",
                )
            ]
        return evidence


@pytest.mark.parametrize(
    "code,expected,calls", [("missing_scene", "ready", 3), ("missing_evidence", "needs_input", 1)]
)
async def test_legacy_evidence_completion_preserves_real_source_failure(code, expected, calls):
    request, materials = inputs(selection={"mode": "explicit", "subject_ids": ["s1"]})
    snapshot = MaterialAnalysis.model_validate(analysis_data())
    snapshot.fingerprint = long_discovery_fingerprint(request, materials)
    snapshot.materials[0].sha256 = materials[0].sha256
    ledger = BudgetLedger(None, ExecutionPolicy())
    try:
        with call_scope(ledger, "vision"):
            product = await prepare_long_product(
                request, materials, analysis=snapshot, vision=EvidenceCompletingVision(code)
            )
        assert product.status == expected
        assert product.analysis.facts[0].description == "white ceramic cup"
        assert ledger.usage() == {"image_calls": 0, "vision_calls": calls}
    finally:
        ledger.close()


async def test_legacy_explicit_selection_supplements_plan_without_mutating_snapshot():
    request, materials = inputs(selection={"mode": "explicit", "subject_ids": ["s1"]})
    snapshot = MaterialAnalysis.model_validate(analysis_data())
    snapshot.creative_plan = None
    snapshot.fingerprint = long_discovery_fingerprint(request, materials)
    snapshot.materials[0].sha256 = materials[0].sha256
    original = snapshot.model_dump_json()
    ledger = BudgetLedger(None, ExecutionPolicy())
    try:
        with call_scope(ledger, "vision"):
            product = await prepare_long_product(
                request, materials, analysis=snapshot, vision=CompletingVision()
            )
        assert product.status == "ready"
        assert product.context.selection.subject_ids == ["s1"]
        assert product.context.analysis.creative_plan is not None
        assert snapshot.model_dump_json() == original
        assert ledger.usage() == {"image_calls": 0, "vision_calls": 2}
    finally:
        ledger.close()


@pytest.mark.parametrize("supplied", [False, True])
@pytest.mark.parametrize("status", ["ready", "needs_input"])
async def test_legacy_returned_service_error_cannot_continue_to_planning(supplied, status):
    request, materials = inputs()
    data = analysis_data() | {
        "status": status,
        "error_info": {
            "code": "provider_transport",
            "kind": "transport",
            "message": "source service unavailable",
            "retryable": True,
        },
    }
    snapshot = MaterialAnalysis.model_validate(data)
    snapshot.fingerprint = long_discovery_fingerprint(request, materials)
    snapshot.materials[0].sha256 = materials[0].sha256
    ledger = BudgetLedger(None, ExecutionPolicy())
    try:
        with call_scope(ledger, "vision"):
            product = await prepare_long_product(
                request,
                materials,
                analysis=snapshot if supplied else None,
                vision=CountingVision(discovery=data),
            )
        assert product.status == "failed" and product.context is None
        assert product.error_info == snapshot.error_info
        assert ledger.usage() == {"image_calls": 0, "vision_calls": 0 if supplied else 1}
    finally:
        ledger.close()
