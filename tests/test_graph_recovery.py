import httpx
import pytest

from image_agent.config import AgentConfig
from image_agent.errors import AgentError, ProviderError
from tests.test_pipeline import setup


async def test_saved_candidate_audit_resume_never_generates(request_data, tmp_path):
    from image_agent.execution import ExecutionPolicy
    from image_agent.graph_runtime import resume_pipeline
    from image_agent.pipeline import run_pipeline

    request_data["output_dir"] = tmp_path
    request, deps, vision, gen = setup(request_data, scores={"main": [ProviderError("bad audit")]})
    result = await run_pipeline(
        request, AgentConfig(), dependencies=deps, policy=ExecutionPolicy(max_audit_retries=0)
    )
    asset = result.assets[0]
    assert asset.status == "audit_error"
    assert asset.candidates[0]["status"] == "audit_error"
    assert len(gen.calls) == 1
    assert (tmp_path / result.run_dir).is_dir()
    vision.scores = {}
    resumed = await resume_pipeline(result.run_dir, AgentConfig(), dependencies=deps)
    assert resumed.status == "succeeded"
    assert len(gen.calls) == 1
    assert len(resumed.assets[0].candidates) == 1
    assert "approved" in resumed.assets[0].file_path
    assert (tmp_path / result.run_dir / "checkpoints.sqlite").exists()


async def test_candidates_exist_before_audit_and_repairs_retained(request_data, tmp_path):
    from pathlib import Path

    from image_agent.pipeline import run_pipeline

    request_data["output_dir"] = tmp_path
    request, deps, vision, gen = setup(request_data, scores={"main": [{"output_intent": 74}, {}]})
    original = vision.audit_image

    async def audit(*args):
        assert list(tmp_path.glob("*/candidates/*/*.png"))
        return await original(*args)

    vision.audit_image = audit
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert len(result.assets[0].candidates) == 2
    assert len(list(Path(result.run_dir).glob("candidates/*/*.png"))) == 1
    assert gen.calls[-1]["references"][-1].stage_id == "previous-candidate"
    assert result.assets[0].candidates[0]["quality"]["passed"] is False


async def test_input_copy_integrity_and_config_validation(request_data, tmp_path):
    from pathlib import Path

    from image_agent.execution import ExecutionPolicy
    from image_agent.graph_runtime import resume_pipeline
    from image_agent.pipeline import run_pipeline

    request_data["output_dir"] = tmp_path
    request, deps, vision, gen = setup(request_data, scores={"main": [ProviderError("bad audit")]})
    result = await run_pipeline(
        request, AgentConfig(), dependencies=deps, policy=ExecutionPolicy(max_audit_retries=0)
    )
    with pytest.raises(AgentError, match="configuration"):
        await resume_pipeline(
            result.run_dir, AgentConfig(generation_reference_limit=1), dependencies=deps
        )
    next((Path(result.run_dir) / "inputs").iterdir()).write_bytes(b"corrupt")
    with pytest.raises(AgentError, match="integrity"):
        await resume_pipeline(result.run_dir, AgentConfig(), dependencies=deps)
    assert len(gen.calls) == 1


async def test_actual_post_retries_share_persistent_budget(tmp_path):
    from image_agent.execution import BudgetLedger, ExecutionPolicy, call_scope
    from image_agent.transport import HttpTransport

    policy = ExecutionPolicy(max_image_calls=2)
    ledger = BudgetLedger(tmp_path / "budget.sqlite", policy)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, json={"error": "busy"})

    async def sleep(_):
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with call_scope(ledger, "image"):
            with pytest.raises(AgentError) as caught:
                await HttpTransport(AgentConfig(), client=client, sleep=sleep).post_json("/x", {})
    assert caught.value.code == "budget_exhausted"
    assert len(calls) == 2
    assert BudgetLedger(tmp_path / "budget.sqlite", policy).usage()["image_calls"] == 2


def test_policy_caps_quality_repairs():
    from image_agent.execution import ExecutionPolicy

    with pytest.raises(ValueError):
        ExecutionPolicy(max_quality_repairs=11)


async def test_cancelled_generation_pauses_uncertain_without_replay(request_data, tmp_path):
    import asyncio

    from image_agent.graph_runtime import resume_pipeline
    from image_agent.pipeline import run_pipeline

    request_data["output_dir"] = tmp_path
    request, deps, vision, gen = setup(request_data, error=asyncio.CancelledError())
    with pytest.raises(asyncio.CancelledError):
        await run_pipeline(request, AgentConfig(), dependencies=deps)
    run = next(tmp_path.iterdir())
    gen.error = None
    result = await resume_pipeline(run, AgentConfig(), dependencies=deps)
    assert result.status == "generation_uncertain"
    assert len(gen.calls) == 1


async def test_cancel_after_candidate_save_resumes_only_audit(request_data, tmp_path):
    import asyncio

    from image_agent.graph_runtime import resume_pipeline
    from image_agent.pipeline import run_pipeline

    request_data["output_dir"] = tmp_path
    request, deps, vision, gen = setup(request_data)

    def progress(event):
        if event["stage"] == "audit":
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_pipeline(request, AgentConfig(), dependencies=deps, on_progress=progress)
    result = await resume_pipeline(next(tmp_path.iterdir()), AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert len(gen.calls) == 1


def test_run_lock_excludes_second_owner(tmp_path):
    from image_agent.run_store import RunStore

    with RunStore(tmp_path).lock():
        with pytest.raises(AgentError, match="active"):
            with RunStore(tmp_path).lock():
                pass
    with RunStore(tmp_path).lock():
        pass


def test_shared_budget_across_runs_is_atomic_and_cannot_reset(tmp_path):
    from image_agent.execution import BudgetLedger, ExecutionPolicy

    policy = ExecutionPolicy(
        max_image_calls=9, shared_ledger_path=tmp_path / "shared.sqlite", shared_max_image_calls=2
    )
    first = BudgetLedger(tmp_path / "one.sqlite", policy)
    second = BudgetLedger(tmp_path / "two.sqlite", policy)
    first.reserve("image")
    second.reserve("image")
    with pytest.raises(AgentError) as error:
        second.reserve("image")
    assert error.value.code == "budget_exhausted"
    assert first.usage()["image_calls"] == second.usage()["image_calls"] == 1
    first.close()
    second.close()
    third = BudgetLedger(tmp_path / "three.sqlite", policy)
    with pytest.raises(AgentError):
        third.reserve("image")
    assert third.usage()["image_calls"] == 0
    third.close()


async def test_success_promotes_candidate_away_from_failed_directory(request_data, tmp_path):
    from pathlib import Path

    from image_agent.pipeline import run_pipeline

    request_data["output_dir"] = tmp_path
    request, deps, vision, gen = setup(request_data, scores={"main": [{"output_intent": 50}, {}]})
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    asset = result.assets[0]
    assert len(asset.candidates) == 2
    assert asset.candidates[0]["file_path"].startswith("candidates/")
    assert asset.candidates[1]["file_path"].startswith("approved/")
    assert len(list(Path(result.run_dir).glob("candidates/*/*.png"))) == 1


async def test_accept_older_candidate_survives_resume_with_exact_image(request_data, tmp_path):
    from image_agent.graph_runtime import resume_pipeline
    from image_agent.pipeline import accept_candidate, run_pipeline
    from tests.test_images import picture

    request_data["output_dir"] = tmp_path
    request, deps, vision, gen = setup(request_data, scores={"main": [{"output_intent": 50}]})
    images = [picture((1600, 1600)), picture((1599, 1599))]

    async def generate(**kwargs):
        gen.calls.append(kwargs)
        return images[min(len(gen.calls) - 1, 1)]

    gen.generate = generate
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    asset = result.assets[0]
    accepted = await accept_candidate(
        result.run_dir, asset.asset_id, "c0001", reason="Reviewed first candidate"
    )
    assert accepted.status == accepted.assets[0].status == "accepted"
    assert accepted.assets[0].image == images[0]
    resumed = await resume_pipeline(result.run_dir, AgentConfig(), dependencies=deps)
    assert resumed.assets[0].image == images[0]
    assert not resumed.assets[0].quality.passed
    assert resumed.assets[0].candidates[0]["file_path"].startswith("accepted/")
    assert len(gen.calls) == 2


async def test_image_timeout_is_uncertain_and_does_not_retry(tmp_path):
    from image_agent.execution import BudgetLedger, ExecutionPolicy, call_scope
    from image_agent.transport import HttpTransport

    ledger = BudgetLedger(tmp_path / "budget.sqlite", ExecutionPolicy())
    calls = []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("lost response")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with call_scope(ledger, "image"):
            with pytest.raises(AgentError) as error:
                await HttpTransport(AgentConfig(), client=client).post_json("/x", {})
    assert error.value.code == "generation_uncertain"
    assert len(calls) == ledger.usage()["image_calls"] == 1
    ledger.close()


async def test_memory_run_later_export_preserves_all_candidate_history(request_data, tmp_path):
    from image_agent.output import save_result
    from image_agent.pipeline import run_pipeline

    request, deps, vision, gen = setup(request_data, scores={"main": [{"output_intent": 50}, {}]})
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    await save_result(result, tmp_path)
    for candidate in result.assets[0].candidates:
        assert (tmp_path / candidate["file_path"]).is_file()
    assert result.assets[0].candidates[0]["status"] == "quality_failed"
    assert result.assets[0].candidates[1]["status"] == "succeeded"


@pytest.mark.parametrize("damage", ["missing", "empty", "missing_row", "lowered_counter"])
async def test_resume_refuses_damaged_budget_ledger(request_data, tmp_path, damage):
    import sqlite3
    from pathlib import Path

    from image_agent.execution import ExecutionPolicy, reserve_post
    from image_agent.graph_runtime import resume_pipeline
    from image_agent.pipeline import run_pipeline

    request_data["output_dir"] = tmp_path
    request, deps, vision, gen = setup(request_data, scores={"main": [ProviderError("bad audit")]})
    original = vision.audit_image

    async def counted(*args):
        reserve_post()
        return await original(*args)

    vision.audit_image = counted
    result = await run_pipeline(
        request,
        AgentConfig(),
        dependencies=deps,
        policy=ExecutionPolicy(max_vision_calls=1, max_audit_retries=0),
    )
    ledger = Path(result.run_dir) / "budget.sqlite"
    if damage == "missing":
        ledger.unlink()
    elif damage == "empty":
        ledger.write_bytes(b"")
    else:
        with sqlite3.connect(ledger) as db:
            db.execute(
                "DELETE FROM budget WHERE kind='vision'"
                if damage == "missing_row"
                else "UPDATE budget SET used=0 WHERE kind='vision'"
            )
    vision.scores = {}
    before = len(vision.calls)
    with pytest.raises(AgentError, match="budget"):
        await resume_pipeline(result.run_dir, AgentConfig(), dependencies=deps)
    assert len(vision.calls) == before


def test_shared_ledger_cannot_be_recreated_after_deletion(tmp_path):
    from image_agent.execution import BudgetLedger, ExecutionPolicy

    shared = tmp_path / "shared.sqlite"
    policy = ExecutionPolicy(shared_ledger_path=shared, shared_max_image_calls=1)
    first = BudgetLedger(tmp_path / "one.sqlite", policy)
    first.reserve("image")
    first.close()
    shared.unlink()
    with pytest.raises(AgentError, match="budget"):
        BudgetLedger(tmp_path / "two.sqlite", policy)


async def test_same_scores_with_new_actionable_audit_evidence_get_repair(request_data):
    from image_agent.pipeline import run_pipeline

    request, deps, vision, gen = setup(
        request_data,
        scores={
            "main": [
                {
                    "output_intent": 60,
                    "reason": "Front view hides handle; rotate to three-quarter view.",
                },
                {
                    "output_intent": 60,
                    "reason": "Angle fixed; handle cropped at edge, widen framing.",
                },
                {},
            ]
        },
    )
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded"
    assert len(gen.calls) == 3
    assert "widen framing" in gen.calls[-1]["prompt"]


def test_uncertain_generation_metadata_forbids_automatic_retry():
    from image_agent.errors import make_error_info

    error = make_error_info(
        ProviderError("response lost", code="generation_uncertain", kind="transport")
    )
    assert error.model_dump(mode="json")["retryable"] is False
    assert make_error_info(ProviderError("audit timeout", kind="transport")).retryable is True


@pytest.mark.parametrize("damage", ["missing", "empty"])
async def test_uncertain_submission_cannot_replay_when_journal_is_lost(
    request_data, tmp_path, damage
):
    import asyncio

    from image_agent.execution import reserve_post
    from image_agent.graph_runtime import resume_pipeline
    from image_agent.pipeline import run_pipeline

    request_data["output_dir"] = tmp_path
    request, deps, vision, gen = setup(request_data, error=asyncio.CancelledError())
    original = gen.generate

    async def counted(**kwargs):
        reserve_post()
        return await original(**kwargs)

    gen.generate = counted
    with pytest.raises(asyncio.CancelledError):
        await run_pipeline(request, AgentConfig(), dependencies=deps)
    run = next(tmp_path.iterdir())
    journal = run / "operations.json"
    if damage == "missing":
        journal.unlink()
    else:
        journal.write_text("{}", encoding="utf-8")
    gen.error = None
    with pytest.raises(AgentError) as error:
        await resume_pipeline(run, AgentConfig(), dependencies=deps)
    assert error.value.code == "generation_uncertain"
    assert len(gen.calls) == 1
