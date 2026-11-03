import asyncio
import base64
from dataclasses import replace

import httpx
import pytest

from image_agent.config import AgentConfig
from image_agent.errors import AgentError, ProviderError, make_error_info
from image_agent.execution import BudgetLedger, ExecutionPolicy, call_scope
from image_agent.generate import ImageGenerator
from image_agent.run_store import RunStore
from image_agent.transport import HttpTransport
from tests.test_images import picture


def response(outcome):
    if outcome == "timeout":
        raise httpx.ReadTimeout("private response")
    if outcome == "cancel":
        raise asyncio.CancelledError
    if outcome == "bad_json":
        return httpx.Response(200, text="private invalid JSON")
    if isinstance(outcome, int):
        return httpx.Response(outcome, json={"error": "unavailable"})
    data = b"not-an-image" if outcome == "bad_image" else picture((32, 32))
    return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(data).decode()}]})


@pytest.mark.parametrize(
    "outcomes",
    [
        ["ok"],
        [524, "ok"],
        ["timeout", "bad_json", "ok"],
        [524, 524, 524],
        [503, 503, 503],
        [400, 400, 400],
        ["timeout"] * 3,
        ["bad_json"] * 3,
        ["bad_image"] * 3,
    ],
)
async def test_three_actual_posts_max_with_persistent_attempt_history(tmp_path, outcomes):
    calls, delays = [], []
    ledger = BudgetLedger(tmp_path / "budget.sqlite", ExecutionPolicy())
    store = RunStore(tmp_path / "run")
    store.mark_operation("main-1", "started")

    def handler(request):
        calls.append(request)
        return response(outcomes[len(calls) - 1])

    async def sleep(seconds):
        delays.append(seconds)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        config = AgentConfig()
        generator = ImageGenerator(
            HttpTransport(config, client=client, sleep=sleep),
            config,
            encoder=lambda data, limit: data,
        )
        with call_scope(ledger, "image", store, "main-1"):
            if outcomes[-1] == "ok":
                data = await generator.generate(
                    prompt="x",
                    model=config.model_fast,
                    size="2K",
                    aspect_ratio="1:1",
                    references=(),
                )
                assert data == picture((32, 32))
            else:
                with pytest.raises(ProviderError) as caught:
                    await generator.generate(
                        prompt="x",
                        model=config.model_fast,
                        size="2K",
                        aspect_ratio="1:1",
                        references=(),
                    )
                assert caught.value.code == "generation_attempts_exhausted"
                assert make_error_info(caught.value).retryable is False
                assert "3 attempts" in caught.value.message
                assert "private" not in caught.value.message
    assert len(calls) == ledger.usage()["image_calls"] == len(outcomes)
    assert delays == [2.0, 4.0][: len(outcomes) - 1]
    operation = store.operation("main-1")
    assert operation["submissions"] == len(outcomes)
    assert [row["attempt"] for row in operation["attempt_history"]] == list(
        range(1, len(outcomes) + 1)
    )
    store.validate_operations(ledger)
    ledger.close()


@pytest.mark.parametrize("stop", ["budget", "cancel"])
async def test_budget_or_cancellation_never_forces_extra_attempts(tmp_path, stop):
    ledger = BudgetLedger(tmp_path / "budget.sqlite", ExecutionPolicy(max_image_calls=2))
    calls = []

    def handler(request):
        calls.append(request)
        return response("cancel" if stop == "cancel" else 524)

    async def sleep(_):
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        config = AgentConfig()
        generator = ImageGenerator(
            HttpTransport(config, client=client, sleep=sleep),
            config,
            encoder=lambda data, limit: data,
        )
        with call_scope(ledger, "image"):
            with pytest.raises(
                asyncio.CancelledError if stop == "cancel" else AgentError
            ) as caught:
                await generator.generate(
                    prompt="x",
                    model=config.model_fast,
                    size="2K",
                    aspect_ratio="1:1",
                    references=(),
                )
    assert len(calls) == (1 if stop == "cancel" else 2)
    if stop == "budget":
        assert caught.value.code == "budget_exhausted"
    ledger.close()


async def test_exhaustion_fails_whole_pipeline_and_resume_does_not_submit_again(
    request_data, tmp_path
):
    from image_agent.graph_runtime import resume_pipeline
    from image_agent.pipeline import run_pipeline
    from tests.test_pipeline import setup

    request_data.update(output_dir=tmp_path, output_types=["main_image", "detail_page"])
    request, deps, _, _ = setup(request_data)
    calls = []

    def handler(request):
        calls.append(request)
        return response(524)

    async def sleep(_):
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        config = AgentConfig()
        generator = ImageGenerator(
            HttpTransport(config, client=client, sleep=sleep),
            config,
            encoder=lambda data, limit: data,
        )
        deps = replace(deps, generator=generator)
        result = await run_pipeline(request, config, dependencies=deps)
        assert result.status == "failed"
        assert result.error_info.code == "generation_attempts_exhausted"
        assert result.usage["image_calls"] == len(calls) == 3
        store = RunStore(result.run_dir)
        operations = store.json("operations.json")
        assert len(operations) == 1
        assert next(iter(operations.values()))["status"] == "failed"
        resumed = await resume_pipeline(result.run_dir, config, dependencies=deps)
        assert resumed.status == "failed" and len(calls) == 3


async def test_long_image_exhaustion_is_terminal_and_not_uncertain(tmp_path):
    from image_agent.long_image_runtime import execute_long_image, resume_long_pipeline
    from tests.test_long_image_pipeline import Transport, deps_for, request_plan

    request, plan = request_plan(tmp_path)
    calls = []

    def handler(request):
        calls.append(request)
        return response(524)

    async def sleep(_):
        pass

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        config = AgentConfig()
        generator = ImageGenerator(
            HttpTransport(config, client=client, sleep=sleep),
            config,
            encoder=lambda data, limit: data,
        )
        deps = replace(deps_for(Transport()), generator=generator)
        result = await execute_long_image(request, config, dependencies=deps, plan=plan)
        assert result.status == "failed"
        assert result.error_info.code == "generation_attempts_exhausted"
        assert result.image_assets[0].status == "failed"
        assert result.usage.execution["image_calls"] == len(calls) == 3
        resumed = await resume_long_pipeline(result.run_dir, config, dependencies=deps)
        assert resumed.status == "failed" and len(calls) == 3
