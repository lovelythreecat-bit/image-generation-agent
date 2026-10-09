import pytest

from image_agent.config import AgentConfig
from image_agent.errors import AgentError, OutputError
from image_agent.execution import ExecutionPolicy
from image_agent.long_image_store import LongImageRunStore
from tests.test_long_image_pipeline import Transport, deps_for, request_plan


class Crash(BaseException):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["started", "submitted", "ledger_gap"])
async def test_submitted_without_raw_never_regenerates(tmp_path, monkeypatch, boundary):
    from image_agent.execution import reserve_post
    from image_agent.long_image_runtime import execute_long_image, resume_long_pipeline

    request, plan = request_plan(tmp_path)
    transport = Transport()
    original = LongImageRunStore.mark_operation

    def crash(store, operation, status, **extra):
        if boundary == "ledger_gap" and status == "submitted":
            raise Crash()
        original(store, operation, status, **extra)
        if boundary == "started" and status == "started":
            raise Crash()

    monkeypatch.setattr(LongImageRunStore, "mark_operation", crash)
    if boundary == "submitted":

        async def lost(path, payload):
            reserve_post()
            transport.posts.append((path, payload))
            raise Crash()

        monkeypatch.setattr(transport, "post_json", lost)
    with pytest.raises(Crash):
        await execute_long_image(
            request, AgentConfig(), plan=plan, dependencies=deps_for(transport)
        )
    monkeypatch.setattr(LongImageRunStore, "mark_operation", original)
    before = len(transport.posts)
    recovered = await resume_long_pipeline(
        next(tmp_path.iterdir()), AgentConfig(), dependencies=deps_for(transport)
    )
    assert recovered.status == "generation_uncertain"
    assert len(transport.posts) == before
    assert recovered.page is None


@pytest.mark.asyncio
async def test_audit_error_recovers_same_pixels_without_image_post(tmp_path):
    from image_agent.errors import ProviderError
    from image_agent.long_image_runtime import execute_long_image, resume_long_pipeline

    request, plan = request_plan(tmp_path)
    transport = Transport(audits=[ProviderError("audit unavailable")])
    result = await execute_long_image(
        request,
        AgentConfig(),
        plan=plan,
        dependencies=deps_for(transport),
        policy=ExecutionPolicy(max_audit_retries=0),
    )
    assert result.status == "audit_error"
    assert result.page.availability == "current"
    transport.audits = [True]
    updated = await resume_long_pipeline(
        result.run_dir, AgentConfig(), dependencies=deps_for(transport)
    )
    assert updated.status == "succeeded"
    assert updated.page.sha256 == result.page.sha256
    assert all(a.status == "succeeded" for a in [*updated.sections, updated.page, *updated.slices])
    assert updated.image_assets[0].asset_revision == result.image_assets[0].asset_revision + 1
    assert updated.image_assets[0].candidates[0].get("error_info") is None
    assert updated.usage.execution == {"image_calls": 1, "vision_calls": 2}
    assert sum(path == "/images" for path, _ in transport.posts) == 1


@pytest.mark.asyncio
async def test_local_render_resume_requires_no_credentials(tmp_path, monkeypatch):
    import image_agent.long_image_runtime as runtime

    request, plan = request_plan(tmp_path, slots=False)
    result = await runtime.create_long_image(request, AgentConfig(), plan=plan)

    def denied(*args, **kwargs):
        raise AssertionError("local work must not create clients or read model environment")

    monkeypatch.setattr(AgentConfig, "from_env", denied)
    monkeypatch.setattr(runtime, "HttpTransport", denied)
    original = LongImageRunStore.commit_delivery_revision

    def crash(*args, **kwargs):
        raise Crash()

    monkeypatch.setattr(LongImageRunStore, "commit_delivery_revision", crash)
    with pytest.raises(Crash):
        await runtime.render_long_image(result.run_dir, render_options={"width": 800})
    monkeypatch.setattr(LongImageRunStore, "commit_delivery_revision", original)
    recovered = await runtime.resume_long_image(result.run_dir)
    assert recovered.status == "succeeded"
    assert recovered.page.width == 800
    assert recovered.usage == result.usage


@pytest.mark.asyncio
async def test_cancel_preserves_saved_candidates(tmp_path):
    import asyncio

    from image_agent.execution import reserve_post
    from image_agent.long_image_planning import LongImageDependencies
    from image_agent.long_image_runtime import execute_long_image, resume_long_pipeline
    from tests.test_long_image_pipeline import png

    request, plan = request_plan(tmp_path)
    transport = Transport()
    entered, release = asyncio.Event(), asyncio.Event()

    class Generator:
        async def generate(self, **kwargs):
            reserve_post()
            entered.set()
            await release.wait()
            return png()

    deps = deps_for(transport)
    task = asyncio.create_task(
        execute_long_image(
            request,
            AgentConfig(),
            plan=plan,
            dependencies=LongImageDependencies(deps.vision, deps.load, Generator()),
        )
    )
    await entered.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    recovered = await resume_long_pipeline(
        next(tmp_path.iterdir()), AgentConfig(), dependencies=deps_for(transport)
    )
    assert recovered.status == "succeeded"
    assert recovered.usage.execution == {"image_calls": 1, "vision_calls": 1}
    assert len(transport.posts) == 1


@pytest.mark.asyncio
async def test_save_failure_blocks_page_success(tmp_path, monkeypatch):
    from image_agent.long_image_runtime import create_long_image, resume_long_image

    request, plan = request_plan(tmp_path, slots=False)
    original = LongImageRunStore.write

    def fail(store, relative, data):
        if relative.startswith("deliveries/") and "/slice/" in relative:
            raise OutputError("cannot save slice")
        return original(store, relative, data)

    monkeypatch.setattr(LongImageRunStore, "write", fail)
    result = await create_long_image(request, AgentConfig(), plan=plan)
    assert result.status == "failed"
    assert result.output_errors
    assert result.page.availability != "current"
    monkeypatch.setattr(LongImageRunStore, "write", original)
    repaired = await resume_long_image(result.run_dir)
    assert repaired.status == "succeeded"
    assert repaired.page.availability == "current"


@pytest.mark.asyncio
async def test_corrupt_candidate_never_regenerates(tmp_path):
    from image_agent.long_image_runtime import execute_long_image, resume_long_image

    request, plan = request_plan(tmp_path)
    transport = Transport()
    result = await execute_long_image(
        request, AgentConfig(), plan=plan, dependencies=deps_for(transport)
    )
    from pathlib import Path

    (Path(result.run_dir) / result.image_assets[0].file_path).write_bytes(b"broken")
    recovered = await resume_long_image(result.run_dir)
    assert recovered.status == "failed"
    assert recovered.output_errors
    assert recovered.page.availability == "stale"
    assert len(transport.posts) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["resume_long_image", "render_long_image"])
async def test_corrupt_source_returns_structured_failure(tmp_path, entry):
    from pathlib import Path

    import image_agent.long_image_runtime as runtime

    request, plan = request_plan(tmp_path, source=True)
    result = await runtime.create_long_image(request, AgentConfig(), plan=plan)
    (Path(result.run_dir) / result.image_assets[0].file_path).write_bytes(b"broken")
    failed = await getattr(runtime, entry)(result.run_dir)
    assert failed.status == "failed"
    assert failed.output_errors
    assert failed.page.availability == "stale"
    assert failed.page.image is None


@pytest.mark.asyncio
async def test_audit_checkpoint_never_exposes_old_current_status(tmp_path):
    from image_agent.errors import ProviderError
    from image_agent.long_image_runtime import execute_long_image, resume_long_pipeline

    request, plan = request_plan(tmp_path)
    transport = Transport(audits=[ProviderError("temporary audit failure")])
    result = await execute_long_image(
        request,
        AgentConfig(),
        plan=plan,
        dependencies=deps_for(transport),
        policy=ExecutionPolicy(max_audit_retries=0),
    )
    transport.audits = [True]

    def interrupt(event):
        if event["stage"] == "finish_slot":
            raise Crash()

    with pytest.raises(Crash):
        await resume_long_pipeline(
            result.run_dir, AgentConfig(), dependencies=deps_for(transport), on_progress=interrupt
        )
    store = LongImageRunStore(result.run_dir)
    with store.lock():
        loaded = store.hydrate_result()
    assert loaded.image_assets[0].status == "succeeded"
    assert loaded.page.availability != "current" or loaded.page.status == "succeeded"


@pytest.mark.asyncio
async def test_accept_paused_candidate_is_not_reaudited(tmp_path):
    from image_agent.long_image_runtime import (
        accept_long_image_candidate,
        execute_long_image,
        resume_long_pipeline,
    )

    request, plan = request_plan(tmp_path)
    transport = Transport()

    def interrupt(event):
        if event["stage"] == "audit":
            raise Crash()

    with pytest.raises(Crash):
        await execute_long_image(
            request,
            AgentConfig(),
            plan=plan,
            dependencies=deps_for(transport),
            on_progress=interrupt,
        )
    run_dir = next(tmp_path.iterdir())
    accepted = await accept_long_image_candidate(
        run_dir, "illustration", "c0001", reason="核对候选"
    )
    assert accepted.status == "accepted"
    resumed = await resume_long_pipeline(run_dir, AgentConfig(), dependencies=deps_for(transport))
    assert resumed.status == "accepted"
    assert len(transport.posts) == 1


@pytest.mark.asyncio
async def test_journal_gap_refreshes_complete_outputs_without_post(tmp_path):
    from image_agent.long_image_runtime import execute_long_image, resume_long_image

    request, plan = request_plan(tmp_path)
    transport = Transport()
    result = await execute_long_image(
        request, AgentConfig(), plan=plan, dependencies=deps_for(transport)
    )
    store = LongImageRunStore(result.run_dir)
    with store.lock():
        store.remove("operations.json")
    recovered = await resume_long_image(result.run_dir)
    assert recovered.status == "generation_uncertain"
    assert recovered.page.status == "generation_uncertain"
    assert recovered.sections[1].status == "generation_uncertain"
    assert recovered.sections[0].status == "succeeded"
    assert recovered.page.sha256 == result.page.sha256
    assert len(transport.posts) == 2


@pytest.mark.asyncio
async def test_pending_audit_delivery_local_recovery_preserves_actual_usage(tmp_path, monkeypatch):
    import image_agent.long_image_runtime as runtime
    from image_agent.errors import ProviderError

    request, plan = request_plan(tmp_path)
    transport = Transport(audits=[ProviderError("temporarily unavailable")])
    result = await runtime.execute_long_image(
        request,
        AgentConfig(),
        plan=plan,
        dependencies=deps_for(transport),
        policy=ExecutionPolicy(max_audit_retries=0),
    )
    original = runtime.render_long_layout

    def crash(*args):
        raise Crash()

    transport.audits = [True]
    monkeypatch.setattr(runtime, "render_long_layout", crash)
    with pytest.raises(Crash):
        await runtime.resume_long_pipeline(
            result.run_dir, AgentConfig(), dependencies=deps_for(transport)
        )
    monkeypatch.setattr(runtime, "render_long_layout", original)

    def denied(*args, **kwargs):
        raise AssertionError("pending delivery recovery must stay local")

    monkeypatch.setattr(AgentConfig, "from_env", denied)
    recovered = await runtime.resume_long_image(result.run_dir)
    assert recovered.status == "succeeded"
    assert recovered.usage.execution == {"image_calls": 1, "vision_calls": 2}
    assert len(transport.posts) == 3


@pytest.mark.asyncio
async def test_same_bytes_audit_and_accept_update_all_outputs(tmp_path):
    from image_agent.long_image_runtime import (
        accept_long_image_candidate,
        execute_long_image,
        render_long_image,
    )

    request, plan = request_plan(tmp_path)
    transport = Transport(audits=[False])
    result = await execute_long_image(
        request,
        AgentConfig(),
        plan=plan,
        dependencies=deps_for(transport),
        policy=ExecutionPolicy(max_quality_repairs=0),
    )
    assert result.status == "quality_failed"
    accepted = await accept_long_image_candidate(
        result.run_dir, "illustration", "c0001", reason="人工核对通过"
    )
    assert accepted.status == "accepted"
    assert accepted.page.sha256 == result.page.sha256
    assert accepted.delivery_revision == result.delivery_revision + 1
    assert accepted.image_assets[0].asset_revision == result.image_assets[0].asset_revision + 1
    assert accepted.image_assets[0].candidates[0]["acceptance"]["at"]
    assert [s.status for s in accepted.sections] == [
        "succeeded",
        "accepted",
        "succeeded",
        "succeeded",
    ]
    rerender = await render_long_image(result.run_dir, render_options={"width": 800})
    assert rerender.page.width == 800
    assert rerender.usage == accepted.usage
    assert len(transport.posts) == 2


@pytest.mark.asyncio
async def test_saved_raw_recovers_without_post(tmp_path, monkeypatch):
    from image_agent.long_image_runtime import execute_long_image, resume_long_pipeline

    request, plan = request_plan(tmp_path)
    transport = Transport()
    original = LongImageRunStore.mark_operation

    def crash(store, operation, status, **extra):
        original(store, operation, status, **extra)
        if status == "saved":
            raise Crash("crash after raw")

    monkeypatch.setattr(LongImageRunStore, "mark_operation", crash)
    with pytest.raises(Crash):
        await execute_long_image(
            request, AgentConfig(), plan=plan, dependencies=deps_for(transport)
        )
    monkeypatch.setattr(LongImageRunStore, "mark_operation", original)
    result = await resume_long_pipeline(
        next(tmp_path.iterdir()), AgentConfig(), dependencies=deps_for(transport)
    )
    assert result.status == "succeeded"
    assert len(transport.posts) == 2
    assert result.usage.execution["image_calls"] == 1


@pytest.mark.asyncio
async def test_local_render_rejects_content_overrides(tmp_path):
    from image_agent.long_image_runtime import create_long_image, render_long_image

    request, plan = request_plan(tmp_path, slots=False)
    result = await create_long_image(request, AgentConfig(), plan=plan)
    with pytest.raises(AgentError):
        await render_long_image(result.run_dir, render_options={"style_hint": "different"})


@pytest.mark.asyncio
async def test_accept_then_layout_failure_is_failed(tmp_path, monkeypatch):
    import image_agent.long_image_runtime as runtime

    request, plan = request_plan(tmp_path)
    result = await runtime.execute_long_image(
        request,
        AgentConfig(),
        plan=plan,
        dependencies=deps_for(Transport(audits=[False])),
        policy=ExecutionPolicy(max_quality_repairs=0),
    )

    def fail(*args, **kwargs):
        raise OutputError("layout unavailable")

    monkeypatch.setattr(runtime, "render_long_layout", fail)
    # Force a new local render before acceptance; saved override must remain effective.
    failed = await runtime.render_long_image(result.run_dir, render_options={"width": 800})
    assert failed.status == "failed"
    assert failed.page.availability == "stale"
    accepted = await runtime.accept_long_image_candidate(
        result.run_dir, "illustration", "c0001", reason="reviewed"
    )
    assert accepted.status == "failed"
    assert accepted.image_assets[0].status == "accepted"
    assert accepted.page.availability != "current"
