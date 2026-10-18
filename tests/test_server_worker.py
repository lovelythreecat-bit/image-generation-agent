import pytest

from image_agent.errors import ProviderError
from image_agent.pipeline import Dependencies, prepare_analysis, run_pipeline
from tests import test_server_tasks as task_fixtures
from tests.fakes import FakeGenerator, FakeVision
from tests.test_server_media import png
from tests.test_server_tasks import submit

client = task_fixtures.client
service = task_fixtures.service


@pytest.fixture
def core(monkeypatch):
    from image_agent import pipeline

    vision, generator = FakeVision(), FakeGenerator()

    async def load(source):
        return source.data

    deps = Dependencies(vision=vision, generator=generator, load=load)

    async def analyze(request, config):
        return (await prepare_analysis(request, config, deps)).analysis

    async def create(request, config, **kwargs):
        return await run_pipeline(request, config, dependencies=deps, **kwargs)

    monkeypatch.setattr(pipeline, "analyze_materials", analyze)
    monkeypatch.setattr(pipeline, "create_images", create)
    return deps


async def once(service):
    try:
        from image_agent.server.worker import Worker
    except ImportError:
        pytest.fail("Worker must claim and execute durable tasks")
    await Worker(service).run_once()


async def test_real_core_delivery_retains_quality_status_and_blobs(service, core):
    task, request = await submit(service)
    core.vision.scores = {"main": [{"output_intent": 30}]}
    await once(service)
    state = await service.get_task(task["task_id"])
    assert state["status"] == "completed"
    assert state['core_status'] == 'succeeded'
    result = await service.get_result(task["task_id"])
    assert result['status'] == 'succeeded'
    assert result['assets'][0]['quality']['passed'] is False
    assert result['assets'][0]['delivery']['mode'] == 'best_available'
    blob_id = result["assets"][0]["blob_id"]
    data, mime = await service.blob(task["task_id"], blob_id)
    assert data and mime == "image/png"
    from hashlib import sha256
    assert result['assets'][0]['delivery']['sha256'] == sha256(data).hexdigest()
    with pytest.raises(Exception) as caught:
        await service.blob(task["task_id"], "../private")
    assert caught.value.status_code == 404
    assert "private-credential" not in str(result)
    assert not any(
        "private-credential" in p.read_text(errors="ignore")
        for p in service.media_store.root.rglob("*.json")
    )


async def test_analysis_provenance_binds_exact_original_inputs(service, core):
    task, request = await submit(service, kind="analyze_materials")
    await once(service)
    result = await service.get_result(task["task_id"])
    assert result["status"] == "ready"
    queued = await service.submit_task(
        "create_images", "config", request, "reuse", analysis_task_id=task["task_id"]
    )
    stored = await service.repository.get(queued["task_id"])
    assert stored.normalized_request["analysis_snapshot"] == result
    changed = request.model_copy(deep=True)
    changed.category = "clothing"
    with pytest.raises(Exception) as caught:
        await service.submit_task(
            "create_images", "config", changed, "bad-reuse", analysis_task_id=task["task_id"]
        )
    assert caught.value.code == "analysis_binding"
    await once(service)
    assert (await service.get_task(queued["task_id"]))["core_status"] == "succeeded"


async def test_worker_failures_are_durable_and_sanitized(service, monkeypatch):
    from image_agent import pipeline

    task, _ = await submit(service)

    async def fail(*args, **kwargs):
        raise ProviderError("private-credential leaked upstream", code="generation_uncertain")

    monkeypatch.setattr(pipeline, "create_images", fail)
    await once(service)
    state = await service.get_task(task["task_id"])
    assert state["status"] == "failed"
    assert state["error"]["code"] == "generation_uncertain"
    assert "private-credential" not in str(state)
    with pytest.raises(Exception) as caught:
        await service.get_result(task["task_id"])
    assert caught.value.status_code == 409


async def test_result_storage_failure_does_not_report_completed(service, core, monkeypatch):
    task, _ = await submit(service)

    def fail(*args):
        raise OSError("disk full private-credential")

    monkeypatch.setattr(service.media_store, "save_bundle", fail)
    await once(service)
    state = await service.get_task(task["task_id"])
    assert state["status"] == "failed"
    assert "private-credential" not in str(state)


async def test_decryption_failure_does_not_call_provider(service, core):
    task, _ = await submit(service)
    from sqlalchemy import update

    from image_agent.server.db_models import HttpTask

    async with service.repository.transaction() as session:
        await session.execute(
            update(HttpTask)
            .where(HttpTask.id == task["task_id"])
            .values(config_snapshot_encrypted="corrupted")
        )
    await once(service)
    assert (await service.get_task(task["task_id"]))["status"] == "failed"
    assert core.generator.calls == []


async def test_http_real_core_execution_result_and_download(client, service, core):
    headers = {"Authorization": "Bearer test-token", "Idempotency-Key": "http-loop"}
    uploaded = await client.post(
        "/api/v1/media", headers=headers, files={"file": ("cup.png", png(), "image/png")}
    )
    assert uploaded.status_code == 201
    body = {
        "kind": "create_images",
        "agent_config_id": "config",
        "request": {
            "product_name": "cup",
            "category": "kitchen",
            "platforms": ["taobao"],
            "output_types": ["main_image"],
            "materials": [{"material_id": "m1", "media_id": uploaded.json()["media_id"]}],
        },
    }
    response = await client.post("/api/v1/tasks", headers=headers, json=body)
    assert response.status_code == 202
    task_id = response.json()["task_id"]
    assert (await client.get(f"/api/v1/tasks/{task_id}/result", headers=headers)).status_code == 409
    await once(service)
    response = await client.get(f"/api/v1/tasks/{task_id}/result", headers=headers)
    assert response.status_code == 200 and response.json()["status"] == "succeeded"
    blob_id = response.json()["assets"][0]["blob_id"]
    image = await client.get(f"/api/v1/tasks/{task_id}/blobs/{blob_id}", headers=headers)
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert image.content == (await service.blob(task_id, blob_id))[0]


async def test_product_long_image_task_uses_core_export(service, request_data, monkeypatch):
    from image_agent import pipeline
    from tests.test_product_long_image_pipeline import long_setup

    _, deps, _, generator = long_setup(request_data)

    async def create(request, config, **kwargs):
        return await run_pipeline(request, config, dependencies=deps, **kwargs)

    monkeypatch.setattr(pipeline, "create_images", create)
    _, request = await submit(service)
    request.output_types = ["long_image"]
    from image_agent.models import ProductLongImageOptions

    request.long_image = ProductLongImageOptions(segment_count=3)
    task = await service.submit_task("create_images", "config", request, "long")
    await once(service)  # the earlier queued main task
    await once(service)
    result = await service.get_result(task["task_id"])
    assert result["status"] == "succeeded"
    assert result["assets"][0]["output_type"] == "long_image"
    assert result["assets"][0]["byte_length"] == len(
        (await service.blob(task["task_id"], result["assets"][0]["blob_id"]))[0]
    )
    assert len(generator.calls) == 4


async def test_analysis_records_progress_phase(service, core):
    task, _ = await submit(service, kind="analyze_materials")
    await once(service)
    assert (await service.get_task(task["task_id"]))["progress"]["stage"] == "analyze_materials"


async def test_worker_concurrency_does_not_hold_database_transaction_during_provider(
    service, monkeypatch
):
    import asyncio

    from image_agent import pipeline
    from image_agent.server.worker import Worker
    from tests.test_output import result as sample_result

    tasks = [(await submit(service, key=f"bounded-{i}"))[0] for i in range(3)]
    from contextlib import asynccontextmanager
    from contextvars import ContextVar

    in_transaction = ContextVar("test_task_transaction", default=False)
    original_transaction = service.repository.transaction

    @asynccontextmanager
    async def tracked_transaction():
        async with original_transaction() as session:
            token = in_transaction.set(True)
            try:
                yield session
            finally:
                in_transaction.reset(token)

    monkeypatch.setattr(service.repository, "transaction", tracked_transaction)
    active = 0
    maximum = 0
    started, release = asyncio.Event(), asyncio.Event()

    async def create(*args, **kwargs):
        nonlocal active, maximum
        assert not in_transaction.get()
        active += 1
        maximum = max(maximum, active)
        if active == 2:
            started.set()
        await release.wait()
        active -= 1
        return sample_result()

    monkeypatch.setattr(pipeline, "create_images", create)
    worker = Worker(service, concurrency=2)
    await worker.start()
    try:
        await asyncio.wait_for(started.wait(), 2)
        assert maximum == 2
        release.set()

        async def completed():
            while not all(
                [(await service.get_task(t["task_id"]))["status"] == "completed" for t in tasks]
            ):
                await asyncio.sleep(0.01)

        await asyncio.wait_for(completed(), 2)
        assert maximum == 2
    finally:
        await worker.stop()


@pytest.mark.parametrize("status", ["needs_input", "budget_exhausted"])
async def test_completed_scheduler_retains_core_input_and_budget_states(
    service, core, monkeypatch, status
):
    from image_agent import pipeline
    from image_agent.execution import ExecutionPolicy, reserve_post
    from tests.fakes import analysis_data

    task, _ = await submit(service)
    if status == "needs_input":
        data = analysis_data()
        data["issues"] = [
            {
                "issue_id": "mixed",
                "code": "conflicting_identity",
                "resolution": "reanalyze",
                "message": "replace mixed image",
                "subject_ids": ["s1"],
            }
        ]
        core.vision.discovery = data
    else:
        original = core.generator.generate

        async def counted(**kwargs):
            reserve_post()
            return await original(**kwargs)

        core.generator.generate = counted

        async def create(request, config, **kwargs):
            return await run_pipeline(
                request,
                config,
                dependencies=core,
                policy=ExecutionPolicy(max_image_calls=0),
                **kwargs,
            )

        monkeypatch.setattr(pipeline, "create_images", create)
    await once(service)
    state = await service.get_task(task["task_id"])
    assert state["status"] == "completed" and state["core_status"] == status
    assert (await service.get_result(task["task_id"]))["status"] == status
    assert core.generator.calls == []
