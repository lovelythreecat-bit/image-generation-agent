import asyncio
import json

import pytest

from image_agent.execution import reserve_post
from image_agent.server.worker import Worker
from tests import test_server_tasks as task_fixtures
from tests import test_server_worker as worker_fixtures
from tests.test_server_tasks import submit
from tests.test_server_worker import once

service = task_fixtures.service
core = worker_fixtures.core


async def test_restart_interrupts_running_without_automatic_paid_replay(service, core):
    task, _ = await submit(service)
    claimed = await service.repository.claim()
    assert claimed.id == task["task_id"]
    worker = Worker(service)
    await worker.start()
    await asyncio.sleep(0.05)
    await worker.stop()
    state = await service.get_task(task["task_id"])
    assert state["status"] == "interrupted"
    assert core.generator.calls == []


async def test_explicit_resume_preserves_uncertain_submission_and_budget(
    service, core, monkeypatch
):
    from image_agent import pipeline
    from image_agent.graph_runtime import resume_pipeline

    task, _ = await submit(service)
    core.generator.error = asyncio.CancelledError()
    original = core.generator.generate

    async def counted(**kwargs):
        reserve_post()
        return await original(**kwargs)

    core.generator.generate = counted
    with pytest.raises(asyncio.CancelledError):
        await once(service)
    assert (await service.get_task(task["task_id"]))["status"] == "interrupted"
    stored = await service.repository.get(task["task_id"])
    run = service.media_store.path(stored.run_ref)
    policy_before = json.loads((run / "run.json").read_text(encoding="utf-8"))["policy"]
    core.generator.error = None

    async def resume(run_dir, config, **kwargs):
        return await resume_pipeline(run_dir, config, dependencies=core, **kwargs)

    monkeypatch.setattr(pipeline, "resume_images", resume)
    results = await asyncio.gather(
        service.recover(task["task_id"]), service.recover(task["task_id"]), return_exceptions=True
    )
    assert sum(isinstance(r, dict) for r in results) == 1
    assert next(r for r in results if isinstance(r, Exception)).status_code == 409
    await once(service)
    state = await service.get_task(task["task_id"])
    assert state["status"] == "completed" and state["core_status"] == "generation_uncertain"
    assert len(core.generator.calls) == 1
    assert json.loads((run / "run.json").read_text(encoding="utf-8"))["policy"] == policy_before
    import sqlite3

    with sqlite3.connect(run / "budget.sqlite") as db:
        assert db.execute("SELECT used FROM budget WHERE kind='image'").fetchone()[0] == 1


async def test_accept_saved_candidate_retains_quality_and_audit_reason(service, core):
    task, _ = await submit(service)
    core.vision.scores = {"main": [{"output_intent": 30}]}
    await once(service)
    result = await service.get_result(task["task_id"])
    asset = result["assets"][0]
    before = len(core.generator.calls)
    acceptance = {
        "asset_id": asset["asset_id"],
        "candidate_id": asset["candidates"][0]["candidate_id"],
        "reason": "Reviewed exact candidate",
    }
    attempts = await asyncio.gather(
        service.recover(task["task_id"], "accept", acceptance),
        service.recover(task["task_id"], "accept", acceptance),
        return_exceptions=True,
    )
    assert sum(isinstance(r, dict) for r in attempts) == 1
    await once(service)
    result = await service.get_result(task["task_id"])
    assert result["status"] == "accepted"
    assert result["assets"][0]["quality"]["passed"] is False
    assert len(core.generator.calls) == before
    record = await service.repository.get(task["task_id"])
    assert record.operation_input["reason"] == "Reviewed exact candidate"
    assert "Reviewed exact candidate" in (
        service.media_store.path(record.run_ref) / "state.json"
    ).read_text(encoding="utf-8")


async def test_analysis_text_and_error_results_redact_secrets_and_local_paths(service, monkeypatch):
    from image_agent import pipeline
    from image_agent.models import MaterialAnalysis
    from tests.fakes import analysis_data

    task, _ = await submit(service, kind="analyze_materials")

    async def analyze(*args):
        data = analysis_data()
        data["warnings"] = ["private-credential " + str(service.media_store.root)]
        return MaterialAnalysis.model_validate(data)

    monkeypatch.setattr(pipeline, "analyze_materials", analyze)
    await once(service)
    result = await service.get_result(task["task_id"])
    assert "private-credential" not in str(result)
    assert str(service.media_store.root) not in str(result)


async def test_progress_persists_only_safe_event_fields(service, core):
    task, _ = await submit(service)
    await once(service)
    state = await service.get_task(task["task_id"])
    assert state["progress"]["stage"]
    assert "run_dir" not in state["progress"]
    assert "private-credential" not in str(state)


async def test_database_outage_after_core_completion_becomes_interrupted_on_reconnect(
    service, core, monkeypatch
):
    from image_agent.server.settings import ServerError
    from image_agent.server.worker import Worker

    task, _ = await submit(service)
    original = service.repository.finish

    async def fail_completion(task_id, **values):
        if "status" in values:
            raise ServerError(503, "database_unavailable", "Task database is unavailable")
        return await original(task_id, **values)

    monkeypatch.setattr(service.repository, "finish", fail_completion)
    worker = Worker(service)
    with pytest.raises(ServerError):
        await worker.run_once()
    assert len(core.generator.calls) == 1
    monkeypatch.setattr(service.repository, "finish", original)
    await worker.run_once()
    state = await service.get_task(task["task_id"])
    assert state["status"] == "interrupted"
    assert state["error"]["code"] == "database_unavailable"
    assert len(core.generator.calls) == 1


async def test_running_or_queued_recovery_returns_conflict_even_when_queue_is_full(service, core):
    task, _ = await submit(service)
    await once(service)
    service.settings.queue_capacity = 1
    await service.recover(task["task_id"])
    with pytest.raises(Exception) as caught:
        await service.recover(task["task_id"])
    assert caught.value.status_code == 409
