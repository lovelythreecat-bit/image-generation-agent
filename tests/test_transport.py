import asyncio

import httpx
import pytest

from image_agent.config import AgentConfig
from image_agent.errors import ConfigurationError, ProviderError


@pytest.mark.parametrize(
    "statuses,attempts,delays",
    [([429, 503, 200], 3, [2, 4]), ([400], 1, []), ([503, 503, 503], 3, [2, 4])],
)
async def test_retry_statuses(statuses, attempts, delays):
    from image_agent.transport import HttpTransport

    calls, slept = [], []

    def handler(req):
        calls.append(req)
        return httpx.Response(statuses[len(calls) - 1], json={"ok": True})

    async def sleep(n):
        slept.append(n)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with HttpTransport(AgentConfig(), client=client, sleep=sleep) as transport:
            if statuses[-1] == 200:
                assert await transport.post_json("/images", {}) == {"ok": True}
            else:
                with pytest.raises(ProviderError) as err:
                    await transport.post_json("/images", {})
                assert err.value.kind == "http"
        assert not client.is_closed
    assert len(calls) == attempts
    assert slept == delays


async def test_bad_json_not_retried():
    from image_agent.transport import HttpTransport

    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(200, text="not json")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="JSON"):
            await HttpTransport(AgentConfig(), client=client).post_json("/x", {})
    assert len(calls) == 1


async def test_http_error_preserves_provider_reason_without_credentials():
    from image_agent.transport import HttpTransport

    key = "private-test-credential-123"
    calls = []

    def handler(req):
        calls.append(req)
        return httpx.Response(
            403,
            json={
                "error": {"message": f"Model access denied for {key}; token=secret-value"},
                "debug": "private debug data",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as caught:
            await HttpTransport(AgentConfig(openrouter_api_key=key), client=client).post_json(
                "/chat/completions", {}
            )
    error = caught.value
    assert error.status_code == 403 and error.kind == "http"
    assert "Model access denied" in error.message
    assert key not in error.message and "secret-value" not in error.message
    assert "private debug data" not in error.message
    assert len(calls) == 1


@pytest.mark.parametrize("body", [b"<html>private gateway page</html>", b"[]", b'{"error": 403}'])
async def test_http_error_with_unstructured_body_keeps_status(body):
    from image_agent.transport import HttpTransport

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda req: httpx.Response(403, content=body))
    ) as client:
        with pytest.raises(ProviderError) as caught:
            await HttpTransport(AgentConfig(), client=client).post_json("/chat/completions", {})
    assert caught.value.status_code == 403
    assert "HTTP 403" in caught.value.message
    assert "private gateway page" not in caught.value.message


async def test_shared_permits_cancel_and_conflict():
    from image_agent.transport import model_permit

    active = peak = 0
    entered = asyncio.Event()
    release = asyncio.Event()

    async def worker():
        nonlocal active, peak
        async with model_permit("test-model", 2):
            active += 1
            peak = max(peak, active)
            if active == 2:
                entered.set()
            try:
                await release.wait()
            finally:
                active -= 1

    tasks = [asyncio.create_task(worker()) for _ in range(5)]
    await entered.wait()
    async with model_permit("different-model", 1):
        with pytest.raises(ConfigurationError):
            async with model_permit("test-model", 3):
                pass
    tasks[-1].cancel()
    with pytest.raises(asyncio.CancelledError):
        await tasks[-1]
    release.set()
    await asyncio.gather(*tasks[:-1])
    assert peak == 2 and active == 0


def test_permits_reusable_across_asyncio_run():
    from image_agent.transport import model_permit

    async def run():
        async with model_permit("loops", 1):
            return 1

    assert asyncio.run(run()) == asyncio.run(run()) == 1
