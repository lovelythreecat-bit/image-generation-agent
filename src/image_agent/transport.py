"""Dedicated HTTP clients and process-wide, event-loop-independent model permits."""

import asyncio
import threading
from contextlib import asynccontextmanager
from typing import Protocol

import httpx

from .errors import ConfigurationError, ProviderError

RETRY_STATUSES = {429, 502, 503, 504}
RETRY_EXCEPTIONS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout)


class JsonTransport(Protocol):
    async def post_json(self, path: str, payload: dict) -> dict: ...


class HttpTransport:
    def __init__(self, config, *, client=None, sleep=asyncio.sleep):
        self.config, self.client, self.sleep = config, client, sleep
        self.owns_client = client is None

    async def __aenter__(self):
        if self.client is None:
            if (
                not self.config.openrouter_api_key
                or not self.config.openrouter_api_key.get_secret_value().strip()
            ):
                raise ConfigurationError("IMAGE_AGENT_OPENROUTER_API_KEY is required")
            self.client = httpx.AsyncClient(
                trust_env=False,
                proxy=self.config.http_proxy,
                timeout=self.config.request_timeout_seconds,
                follow_redirects=False,
            )
        return self

    async def __aexit__(self, *args):
        if self.owns_client and self.client:
            await settle(asyncio.create_task(self.client.aclose()))

    async def post_json(self, path, payload):
        if self.client is None:
            raise ConfigurationError("HttpTransport requires an async context")
        headers = {"Content-Type": "application/json"}
        if self.config.openrouter_api_key:
            headers["Authorization"] = "Bearer " + self.config.openrouter_api_key.get_secret_value()
        for attempt in range(3):
            try:
                response = await self.client.post(
                    self.config.openrouter_base_url.rstrip("/") + "/" + path.lstrip("/"),
                    json=payload,
                    headers=headers,
                    timeout=self.config.request_timeout_seconds,
                )
            except RETRY_EXCEPTIONS:
                if attempt == 2:
                    raise ProviderError(
                        "provider connection or timeout exhausted", kind="transport"
                    ) from None
            except httpx.HTTPError:
                raise ProviderError("provider transport error", kind="transport") from None
            else:
                if response.is_success:
                    try:
                        data = response.json()
                        if not isinstance(data, dict):
                            raise ValueError
                        return data
                    except ValueError:
                        raise ProviderError("provider returned invalid JSON object") from None
                if response.status_code not in RETRY_STATUSES or attempt == 2:
                    raise ProviderError(
                        f"provider HTTP {response.status_code}",
                        kind="http",
                        status_code=response.status_code,
                    )
            await self.sleep((2.0, 4.0)[attempt])


async def settle(task):
    """Finish an owned task even under repeated cancellation, then propagate cancellation."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
        except BaseException:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def compute(function, *args):
    return await settle(asyncio.create_task(asyncio.to_thread(function, *args)))


_permit_lock = threading.Lock()
_permits = {}


@asynccontextmanager
async def model_permit(model, limit):
    registered = acquired = False
    try:
        with _permit_lock:
            state = _permits.setdefault(model, {"limit": limit, "users": 0, "active": 0})
            if state["limit"] != limit:
                raise ConfigurationError("overlapping requests disagree on model concurrency")
            state["users"] += 1
            registered = True
        while not acquired:
            with _permit_lock:
                if state["active"] < limit:
                    state["active"] += 1
                    acquired = True
            if not acquired:
                await asyncio.sleep(0.01)
        yield
    finally:
        if registered:
            with _permit_lock:
                state["active"] -= int(acquired)
                state["users"] -= 1
                if state["users"] == 0:
                    del _permits[model]
