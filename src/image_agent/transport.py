"""Dedicated HTTP clients and process-wide, event-loop-independent model permits."""

import asyncio
import ipaddress
import threading
from contextlib import asynccontextmanager
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from .errors import ConfigurationError, ProviderError
from .execution import reserve_post

RETRY_STATUSES = {429, 502, 503, 504}
RETRY_EXCEPTIONS = (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout)
MAX_IMAGE_DOWNLOAD_BYTES = 32 * 1024 * 1024


def _validate_image_url(url):
    try:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username
            or parsed.password
            or host == "localhost"
            or host.endswith(".localhost")
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError
        # Validate the port without including the untrusted URL in errors.
        parsed.port
    except (ValueError, TypeError):
        raise ProviderError("provider returned an invalid image download URL") from None


class JsonTransport(Protocol):
    async def post_json(self, path: str, payload: dict) -> dict: ...
    async def download_image(self, url: str) -> bytes: ...


class HttpTransport:
    def __init__(self, config, *, client=None, sleep=asyncio.sleep, services=None):
        self.config, self.client, self.sleep = config, client, sleep
        self.owns_client = client is None
        self.services = services

    async def __aenter__(self):
        if self.client is None:
            self.config.validate_credentials(self.services)
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
        if self.config.services:
            raise ConfigurationError("structured API requests must select a service")
        key = (
            self.config.openrouter_api_key.get_secret_value()
            if self.config.openrouter_api_key
            else None
        )
        return await self._post(self.config.openrouter_base_url, key, path, json=payload)

    def for_service(self, name):
        if name not in self.config.services:
            raise ConfigurationError("unknown API service")
        return ServiceTransport(self, self.config.services[name])

    async def download_image(self, url):
        """Fetch a supplier result without API auth, client defaults, or cookies."""
        if self.client is None:
            raise ConfigurationError("HttpTransport requires an async context")
        for attempt in range(3):
            try:
                return await self._download_image_once(url)
            except RETRY_EXCEPTIONS:
                if attempt == 2:
                    raise ProviderError(
                        "image download connection or timeout exhausted", kind="transport"
                    ) from None
            except ProviderError as error:
                if error.kind != "http" or error.status_code not in RETRY_STATUSES or attempt == 2:
                    raise
            except httpx.InvalidURL:
                raise ProviderError("provider returned an invalid image download URL") from None
            except httpx.HTTPError:
                raise ProviderError("image download transport error", kind="transport") from None
            await self.sleep((2.0, 4.0)[attempt])

    async def _download_image_once(self, url):
        for redirect in range(6):
            _validate_image_url(url)
            # A standalone Request avoids default headers/cookies; auth=None also
            # bypasses an injected client's auth. Rebuild it after each redirect.
            request = httpx.Request(
                "GET",
                url,
                extensions={
                    "timeout": httpx.Timeout(self.config.request_timeout_seconds).as_dict()
                },
            )
            response = await self.client.send(
                request, auth=None, follow_redirects=False, stream=True
            )
            try:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location or redirect == 5:
                        raise ProviderError("image download redirect limit or missing location")
                    url = str(response.url.join(location))
                    continue
                if not response.is_success:
                    raise ProviderError(
                        f"image download HTTP {response.status_code}",
                        kind="http",
                        status_code=response.status_code,
                    )
                length = response.headers.get("content-length")
                if length and length.isdecimal() and int(length) > MAX_IMAGE_DOWNLOAD_BYTES:
                    raise ProviderError("image download exceeds size limit (32 MiB)")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    if len(data) + len(chunk) > MAX_IMAGE_DOWNLOAD_BYTES:
                        raise ProviderError("image download exceeds size limit (32 MiB)")
                    data.extend(chunk)
                return bytes(data)
            finally:
                await response.aclose()

    async def _post(self, base_url, key, path, **body):
        if self.client is None:
            raise ConfigurationError("HttpTransport requires an async context")
        headers = {}
        if key:
            headers["Authorization"] = "Bearer " + key
        for attempt in range(3):
            call_kind = reserve_post()
            try:
                response = await self.client.post(
                    base_url.rstrip("/") + "/" + path.lstrip("/"),
                    **body,
                    headers=headers,
                    timeout=self.config.request_timeout_seconds,
                )
            except RETRY_EXCEPTIONS:
                if call_kind == "image":
                    raise ProviderError(
                        "generation response uncertain after submission",
                        code="generation_uncertain",
                        kind="transport",
                    ) from None
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
                    message = f"provider HTTP {response.status_code}"
                    try:
                        body = response.json()
                    except ValueError:
                        body = None
                    if isinstance(body, dict):
                        detail = body.get("error", body.get("message"))
                        if isinstance(detail, dict):
                            detail = detail.get("message")
                        if isinstance(detail, str) and detail.strip():
                            if key:
                                detail = detail.replace(key, "[redacted]")
                            message += ": " + " ".join(detail.split())
                    raise ProviderError(
                        message,
                        kind="http",
                        status_code=response.status_code,
                    )
            await self.sleep((2.0, 4.0)[attempt])


class ServiceTransport:
    """A service binding shares lifecycle/retries but never credentials with another binding."""

    def __init__(self, owner, service):
        self.owner, self.service = owner, service

    async def post_json(self, path, payload):
        return await self.owner._post(
            self.service.base_url, self.service.credential(), path, json=payload
        )

    async def post_multipart(self, path, fields, files):
        return await self.owner._post(
            self.service.base_url, self.service.credential(), path, data=fields, files=files
        )


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
