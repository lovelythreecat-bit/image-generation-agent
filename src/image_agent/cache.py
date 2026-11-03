"""Optional, disposable semantic results; durable run snapshots remain authoritative.

Only the initiating caller owns the model work and its call context. It drains that
work before cancellation unwinds its transport/ledger. Other consumers may leave
immediately. In-flight tasks never cross event loops or budget policy domains.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
import weakref
from collections import Counter, OrderedDict
from concurrent.futures import Future
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
from pathlib import Path

from httpx import AsyncHTTPTransport
from pydantic import BaseModel

from .errors import AgentError
from .execution import cache_budget_domain
from .transport import HttpTransport, compute, settle

log = logging.getLogger(__name__)
_process_salt = os.urandom(32)
_diagnostics = ContextVar("cache_diagnostics", default=None)
_lock = threading.RLock()
_disk_lock = threading.Lock()
_memory = OrderedDict()
_memory_bytes = 0
_flights = weakref.WeakKeyDictionary()
_stats = Counter()
_MEMORY_LIMIT = 64 * 1024 * 1024


def _value(value):
    if isinstance(value, BaseModel):
        # LoadedMaterial excludes image bytes from public serialization. Keys
        # must still hash the actual source, never trust its claimed sha256 alone.
        return {name: _value(getattr(value, name)) for name in type(value).model_fields}
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {key: _value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_value(item) for item in value]
    return value


def _json(value):
    return json.dumps(_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@lru_cache(maxsize=1)
def rules_version():
    """Invalidate on changes to prompts, schemas, normalization or selection rules."""
    digest = hashlib.sha256(b"semantic-cache-v1")
    try:
        paths = sorted(Path(__file__).parent.glob("*.py"))
        if not paths:
            return _process_salt.hex()
        for path in paths:
            digest.update(path.name.encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()
    except OSError:
        # No source available (e.g. a frozen executable): isolate this process.
        return _process_salt.hex()


@contextmanager
def cache_diagnostic_scope(store):
    token = _diagnostics.set(store)
    try:
        yield
    finally:
        _diagnostics.reset(token)


def cache_statistics():
    """Process counters, including callers outside the graph runtime."""
    with _lock:
        return dict(_stats)


def _event(stage, key, outcome, source):
    with _lock:
        _stats[outcome] += 1
    event = dict(stage=stage, key=key, outcome=outcome, source=source)
    log.info("semantic_cache %s", _json(event))
    store = _diagnostics.get()
    if store is not None:
        try:
            store.json(f"diagnostics/cache/{uuid.uuid4().hex}.json", event)
        except Exception:
            log.warning("Cache diagnostic unavailable")


def _salt(directory):
    if directory is None:
        return _process_salt
    # Protect the create/write/read window from another local worker observing
    # an empty just-created file and choosing a different isolation key.
    with _disk_lock:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / ".scope-salt"
        try:
            # Exclusive creation avoids replacing another process's isolation salt.
            with path.open("xb") as stream:
                stream.write(os.urandom(32))
        except FileExistsError:
            pass
        result = path.read_bytes()
    if len(result) != 32:
        raise OSError("invalid cache salt")
    return result


class SemanticCache:
    def __init__(self, config, transport):
        self.config = config
        self.transport = transport
        self.directory = Path(config.cache_directory).resolve() if config.cache_directory else None
        # Custom/injected clients may return different results for identical routes.
        # Only ordinary owned HTTP clients share results across instances/restarts.
        self.namespace = getattr(transport, "_semantic_cache_namespace", None)
        if self.namespace is None:
            self.namespace = uuid.uuid4().hex
            try:
                transport._semantic_cache_namespace = self.namespace
            except AttributeError:
                pass

    def _transport_namespace(self):
        transport = self.transport
        if type(transport) is not HttpTransport or not transport.owns_client:
            return self.namespace
        client = transport.client
        if client is not None:
            # A factory can inject MockTransport/custom mounts even when this
            # HttpTransport owns the resulting client. Inspect effective backends
            # at call time (a VisionClient may predate HttpTransport.__aenter__).
            backends = [getattr(client, "_transport", None)]
            backends.extend(getattr(client, "_mounts", {}).values())
            if (
                any(
                    backend is not None and type(backend) is not AsyncHTTPTransport
                    for backend in backends
                )
                or backends[0] is None
            ):
                return self.namespace
        return None

    def _key(self, stage, inputs, schema):
        config = self.config
        route = config.vision_route(stage)
        if route:
            service = config.services[route.service]
            credentials = (
                os.environ.get(service.api_key_env, "")
                if service.api_key_env
                else service.api_key.get_secret_value()
                if service.api_key
                else ""
            )
            routing = dict(
                route=route.model_dump(),
                service=service.model_dump(mode="json", exclude={"api_key", "api_key_env"}),
            )
        else:
            credentials = (
                config.openrouter_api_key.get_secret_value() if config.openrouter_api_key else ""
            )
            routing = dict(base_url=config.openrouter_base_url, model=config.quality_model)
            if stage == "describe_style":
                routing["style_model"] = config.style_model
        try:
            salt = _salt(self.directory)
        except OSError:
            salt = _process_salt
        # Credentials and proxy credentials never enter filenames, logs or entries.
        isolation = hmac.new(
            salt, _json([credentials, config.http_proxy]).encode(), "sha256"
        ).hexdigest()
        return hashlib.sha256(
            _json(
                dict(
                    stage=stage,
                    inputs=inputs,
                    rules=rules_version(),
                    schema=schema.model_json_schema() if schema else "string-v1",
                    routing=routing,
                    isolation=isolation,
                    namespace=self._transport_namespace(),
                    budget=cache_budget_domain(),
                    enable_thinking=config.enable_thinking,
                    limits=[
                        config.vision_image_limit,
                        config.model_completion_attempts,
                        config.request_timeout_seconds,
                    ],
                )
            ).encode()
        ).hexdigest()

    def _read(self, key):
        with _lock:
            raw = _memory.get(key)
            if raw is not None:
                _memory.move_to_end(key)
        if raw is None and self.directory:
            path = self.directory / (key + ".json")
            if path.stat().st_size > self.config.cache_max_bytes:
                return None
            raw = path.read_bytes()
        if raw is None:
            return None
        if len(raw) > self.config.cache_max_bytes:
            return None
        envelope = json.loads(raw)
        if (
            envelope["key"] != key
            or not 0 <= time.time() - envelope["created"] < self.config.cache_ttl_seconds
            or hashlib.sha256(_json(envelope["value"]).encode()).hexdigest() != envelope["checksum"]
        ):
            return None
        return envelope

    def _write(self, key, envelope):
        global _memory_bytes
        raw = _json(envelope).encode()
        limit = min(self.config.cache_max_bytes, _MEMORY_LIMIT)
        if len(raw) <= limit:
            with _lock:
                _memory_bytes -= len(_memory.pop(key, b""))
                _memory[key] = raw
                _memory_bytes += len(raw)
                while _memory_bytes > limit:
                    _, removed = _memory.popitem(last=False)
                    _memory_bytes -= len(removed)
        if self.directory and len(raw) <= self.config.cache_max_bytes:
            self.directory.mkdir(parents=True, exist_ok=True)
            temporary = self.directory / (uuid.uuid4().hex + ".tmp")
            try:
                temporary.write_bytes(raw)
                with _disk_lock:
                    temporary.replace(self.directory / (key + ".json"))
                    # Never prune unrelated JSON files in a configured directory.
                    files = sorted(
                        (
                            path
                            for path in self.directory.glob("*.json")
                            if len(path.stem) == 64
                            and all(c in "0123456789abcdef" for c in path.stem)
                        ),
                        key=lambda path: path.stat().st_mtime,
                    )
                    total = sum(path.stat().st_size for path in files)
                    for path in files:
                        if total <= self.config.cache_max_bytes:
                            break
                        total -= path.stat().st_size
                        path.unlink(missing_ok=True)
            finally:
                temporary.unlink(missing_ok=True)

    async def run(self, stage, inputs, factory, *, schema=None, validate=None):
        if not self.config.cache_enabled:
            return await factory()
        key = await compute(self._key, stage, inputs, schema)

        def restore(value):
            result = schema.model_validate(value) if schema else value
            if schema is None and not isinstance(result, str):
                raise ValueError("invalid cached string")
            validated = validate(result) if validate else None
            return result if validated is None else validated

        def read():
            try:
                envelope = self._read(key)
                if envelope is not None:
                    return restore(envelope["value"]), envelope["source"]
            except (OSError, ValueError, KeyError, TypeError, AgentError):
                pass
            return None

        cached = await compute(read)
        if cached is not None:
            result, source = cached
            _event(stage, key, "hit", source)
            return result
        loop = asyncio.get_running_loop()
        with _lock:
            flights = _flights.setdefault(loop, {})
            flight = flights.get(key)
            owner = flight is None
            if owner:
                source = uuid.uuid4().hex

                async def execute():
                    try:
                        # A previous executor may have completed while this caller's
                        # worker-thread read was still returning a stale miss.
                        second = await compute(read)
                        if second is not None:
                            result, previous_source = second
                            _event(stage, key, "hit", previous_source)
                            return (
                                result.model_dump(mode="json") if schema else result
                            ), previous_source
                        _event(stage, key, "miss", source)
                        result = await factory()
                        value = result.model_dump(mode="json") if schema else result
                        # Failures and unresolved discovery never enter the result cache.
                        if (
                            getattr(result, "status", "ready") == "ready"
                            and getattr(result, "error_info", None) is None
                        ):
                            envelope = dict(
                                key=key,
                                created=time.time(),
                                source=source,
                                value=value,
                                checksum=hashlib.sha256(_json(value).encode()).hexdigest(),
                            )
                            try:
                                await compute(self._write, key, envelope)
                            except OSError:
                                log.warning("Cache write unavailable; using computed result")
                        return value, source
                    finally:
                        with _lock:
                            flights.pop(key, None)

                flight = (asyncio.create_task(execute()), source)
                flights[key] = flight
        task, source = flight
        # settle preserves the owner's call_scope AND transport until all retries
        # finish. A shield alone would leak work into a closed BudgetLedger.
        value, source = await settle(task) if owner else await asyncio.shield(task)
        if not owner:
            _event(stage, key, "coalesced", source)
        return restore(json.loads(_json(value)))


_jpeg = OrderedDict()
_jpeg_bytes = 0
_jpeg_lock = threading.Lock()
_jpeg_inflight = {}


def cached_jpeg(encoder, data, max_px, max_bytes):
    """Bounded process cache; encoder identity prevents custom encoder collisions."""
    global _jpeg_bytes
    if max_bytes <= 0:
        return encoder(data, max_px)
    key = (encoder, hashlib.sha256(data).digest(), max_px)
    # Only worker threads wait here. Encoding never holds semantic-cache locks
    # or prevents unrelated source images from being encoded concurrently.
    with _jpeg_lock:
        if key in _jpeg:
            _jpeg.move_to_end(key)
            with _lock:
                _stats["jpeg_hit"] += 1
            return _jpeg[key]
        pending = _jpeg_inflight.get(key)
        owner = pending is None
        if owner:
            pending = _jpeg_inflight[key] = Future()
    if not owner:
        with _lock:
            _stats["jpeg_coalesced"] += 1
        return pending.result()
    try:
        result = encoder(data, max_px)
        with _lock:
            _stats["jpeg_miss"] += 1
        with _jpeg_lock:
            limit = min(max_bytes, _MEMORY_LIMIT)
            if len(result) <= limit:
                _jpeg[key] = result
                _jpeg_bytes += len(result)
                while _jpeg_bytes > limit:
                    _, removed = _jpeg.popitem(last=False)
                    _jpeg_bytes -= len(removed)
        pending.set_result(result)
        return result
    except BaseException as error:
        pending.set_exception(error)
        raise
    finally:
        with _jpeg_lock:
            _jpeg_inflight.pop(key, None)
