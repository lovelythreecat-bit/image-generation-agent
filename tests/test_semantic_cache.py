import asyncio
import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from pydantic import BaseModel

from image_agent import cache as cache_module
from image_agent.cache import SemanticCache, cache_diagnostic_scope, cached_jpeg
from image_agent.config import AgentConfig
from image_agent.errors import ProviderError
from image_agent.execution import BudgetLedger, ExecutionPolicy, call_scope, reserve_post
from image_agent.run_store import RunStore, config_fingerprint
from image_agent.transport import HttpTransport


class Payload(BaseModel):
    items: list[str]


def make_cache(tmp_path, **overrides):
    config = AgentConfig(cache_directory=str(tmp_path / "cache"), **overrides)
    return SemanticCache(config, HttpTransport(config))


async def get(cache, factory, inputs=None, **kwargs):
    return await cache.run(
        "extract_garments", inputs or {"image": b"original"}, factory, schema=Payload, **kwargs
    )


def clear_memory():
    with cache_module._lock:
        cache_module._memory.clear()
        cache_module._memory_bytes = 0


async def test_concurrent_success_is_once_and_returns_independent_validated_copies(tmp_path):
    cache = make_cache(tmp_path)
    calls = 0
    stores = [RunStore() for _ in range(30)]

    async def factory():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.02)
        return Payload(items=["source"])

    async def consume(store):
        with cache_diagnostic_scope(store):
            return await get(cache, factory)

    results = await asyncio.gather(*(consume(store) for store in stores))
    assert calls == 1
    results[0].items.append("local change")
    assert all(result.items == ["source"] for result in results[1:])
    assert (await get(cache, factory)).items == ["source"]
    events = [json.loads(value) for store in stores for value in store.memory.values()]
    assert len({event["source"] for event in events}) == 1
    assert sum(event["outcome"] == "miss" for event in events) == 1
    assert len(events) == 30


async def test_fast_factory_does_not_duplicate_after_stale_thread_read(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)
    calls = 0
    original_read = cache._read
    first_written = threading.Event()
    counter = 0
    counter_lock = threading.Lock()

    def delayed_read(key):
        nonlocal counter
        result = original_read(key) if (cache.directory / (key + ".json")).exists() else None
        with counter_lock:
            counter += 1
            second = counter == 2
        if second:
            assert first_written.wait(5)
        return result

    original_write = cache._write

    def write(*args):
        original_write(*args)
        first_written.set()

    monkeypatch.setattr(cache, "_read", delayed_read)
    monkeypatch.setattr(cache, "_write", write)

    async def factory():
        nonlocal calls
        calls += 1
        return Payload(items=["done"])

    await asyncio.gather(*(get(cache, factory) for _ in range(20)))
    assert calls == 1


async def test_owner_cancellation_keeps_ledger_and_transport_context_until_retries_finish(tmp_path):
    cache = make_cache(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    owner_closed = False
    owner_ledger = BudgetLedger(None, ExecutionPolicy())
    waiter_ledger = BudgetLedger(None, ExecutionPolicy())
    final_owner_usage = None

    async def factory():
        reserve_post()
        started.set()
        await release.wait()
        assert not owner_closed
        reserve_post()  # The owner's corrective/HTTP retry, never a waiter charge.
        return Payload(items=["ready"])

    async def owner():
        nonlocal owner_closed, final_owner_usage
        try:
            with call_scope(owner_ledger, "vision"):
                return await get(cache, factory)
        finally:
            final_owner_usage = owner_ledger.usage()
            owner_ledger.close()
            owner_closed = True

    async def waiter():
        with call_scope(waiter_ledger, "vision"):
            return await get(cache, factory)

    task = asyncio.create_task(owner())
    await started.wait()
    waiting = asyncio.create_task(waiter())
    await asyncio.sleep(0.03)
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done() and not owner_closed
    release.set()
    assert (await waiting).items == ["ready"]
    with pytest.raises(asyncio.CancelledError):
        await task
    assert final_owner_usage == {"image_calls": 0, "vision_calls": 2}
    assert waiter_ledger.usage() == {"image_calls": 0, "vision_calls": 0}
    assert (await waiter()).items == ["ready"]
    assert waiter_ledger.usage()["vision_calls"] == 0
    waiter_ledger.close()


async def test_cancelling_one_waiter_leaves_other_consumers_running(tmp_path):
    cache = make_cache(tmp_path)
    started, release = asyncio.Event(), asyncio.Event()
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return Payload(items=["ready"])

    owner = asyncio.create_task(get(cache, factory))
    await started.wait()
    cancelled = asyncio.create_task(get(cache, factory))
    survivor = asyncio.create_task(get(cache, factory))
    await asyncio.sleep(0.03)
    cancelled.cancel()
    with pytest.raises(asyncio.CancelledError):
        await cancelled
    assert not owner.done()
    release.set()
    assert (await survivor).items == (await owner).items == ["ready"]
    assert calls == 1


async def test_failed_flight_is_shared_without_waiter_retries_and_not_cached(tmp_path):
    cache = make_cache(tmp_path)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.05)
        raise ProviderError("upstream failed")

    errors = await asyncio.gather(*(get(cache, factory) for _ in range(12)), return_exceptions=True)
    assert all(isinstance(error, ProviderError) for error in errors)
    assert calls == 1
    with pytest.raises(ProviderError):
        await get(cache, factory)
    assert calls == 2
    assert not list(cache.directory.glob("*.json"))


async def test_budget_policy_and_shared_domains_do_not_merge(tmp_path):
    cache = make_cache(tmp_path)
    policies = [
        ExecutionPolicy(),
        ExecutionPolicy(max_vision_calls=1),
        ExecutionPolicy(shared_ledger_path=tmp_path / "shared.db"),
    ]
    ledgers = [BudgetLedger(None, policy) for policy in policies]
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        reserve_post()
        await asyncio.sleep(0.02)
        return Payload(items=["ready"])

    async def consume(ledger):
        with call_scope(ledger, "vision"):
            return await get(cache, factory)

    try:
        await asyncio.gather(*(consume(ledger) for ledger in ledgers))
        assert calls == 3
        assert [ledger.usage()["vision_calls"] for ledger in ledgers] == [1, 1, 1]
    finally:
        for ledger in ledgers:
            ledger.close()


async def test_disk_reuse_survives_process_salt_and_memory_change(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return Payload(items=["disk"])

    await get(cache, factory)
    clear_memory()
    monkeypatch.setattr(cache_module, "_process_salt", b"another process salt")
    fresh = make_cache(tmp_path)
    assert (await get(fresh, factory)).items == ["disk"]
    assert calls == 1


@pytest.mark.parametrize("damage", ["json", "checksum", "schema", "expired", "validator"])
async def test_corrupt_expired_and_invalid_disk_results_are_cache_misses(tmp_path, damage):
    cache = make_cache(tmp_path)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return Payload(items=["ready"])

    await get(cache, factory)
    path = next(cache.directory.glob("*.json"))
    envelope = json.loads(path.read_text(encoding="utf8"))
    if damage == "json":
        path.write_text("{broken", encoding="utf8")
    else:
        if damage == "checksum":
            envelope["value"]["items"] = ["edited"]
        if damage == "schema":
            envelope["value"] = {"items": 42}
        if damage == "validator":
            envelope["value"] = {"items": ["invalid business links"]}
        if damage in {"schema", "validator"}:
            envelope["checksum"] = hashlib.sha256(
                cache_module._json(envelope["value"]).encode()
            ).hexdigest()
        if damage == "expired":
            envelope["created"] = 0
        path.write_text(json.dumps(envelope), encoding="utf8")
    clear_memory()

    def validate(result):
        if result.items != ["ready"]:
            raise ProviderError("invalid business links")
        return result

    assert (await get(cache, factory, validate=validate)).items == ["ready"]
    assert calls == 2


async def test_cache_io_failure_never_fails_successful_computation(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)

    def denied(*args):
        raise PermissionError("unavailable")

    monkeypatch.setattr(cache, "_read", denied)
    monkeypatch.setattr(cache, "_write", denied)
    monkeypatch.setattr(cache_module, "_salt", denied)

    async def factory():
        return Payload(items=["ready"])

    assert (await get(cache, factory)).items == ["ready"]


async def test_capacity_and_disable(tmp_path):
    cache = make_cache(tmp_path, cache_max_bytes=550)

    async def factory():
        return Payload(items=["x" * 80])

    for n in range(10):
        await get(cache, factory, inputs={"n": n})
    assert sum(path.stat().st_size for path in cache.directory.glob("*.json")) <= 550
    disabled = make_cache(tmp_path / "disabled", cache_enabled=False)
    calls = 0

    async def counted():
        nonlocal calls
        calls += 1
        return Payload(items=["ready"])

    await asyncio.gather(get(disabled, counted), get(disabled, counted))
    assert calls == 2
    assert not disabled.directory.exists()


async def test_configuration_credentials_and_injected_transports_are_isolated(tmp_path):
    caches = [
        make_cache(tmp_path, openrouter_api_key="secret-one"),
        make_cache(tmp_path, openrouter_api_key="secret-two"),
        make_cache(tmp_path, quality_model="different-model"),
        make_cache(tmp_path, model_completion_attempts=5),
    ]
    config = caches[0].config
    caches.extend([SemanticCache(config, object()), SemanticCache(config, object())])
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return Payload(items=["ready"])

    for cache in caches:
        await get(cache, factory)
    assert calls == len(caches)
    for path in caches[0].directory.glob("*.json"):
        assert "secret" not in path.read_text(encoding="utf8")


async def test_owned_clients_with_custom_backends_are_instance_isolated(tmp_path):
    import httpx

    config = AgentConfig(cache_directory=str(tmp_path / "cache"))
    transports = [HttpTransport(config), HttpTransport(config)]
    caches = [SemanticCache(config, transport) for transport in transports]
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return Payload(items=["custom"])

    clients = [
        httpx.AsyncClient(transport=httpx.MockTransport(lambda request: None)) for _ in transports
    ]
    try:
        for transport, client, cache in zip(transports, clients, caches):
            transport.client = client  # Simulates an owned client supplied by a factory.
            await get(cache, factory)
            await get(cache, factory)
        assert calls == 2
    finally:
        for client in clients:
            await client.aclose()


async def test_normal_owned_http_clients_share_results_without_any_network(tmp_path):
    import httpx

    config = AgentConfig(cache_directory=str(tmp_path / "cache"))
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return Payload(items=["real backend, offline factory"])

    for _ in range(2):
        async with httpx.AsyncClient(trust_env=False) as client:
            transport = HttpTransport(config)
            transport.client = client
            await get(SemanticCache(config, transport), factory)
    assert calls == 1


def test_result_cache_reuses_across_event_loops_without_future_sharing(tmp_path):
    cache = make_cache(tmp_path)
    calls = 0

    async def factory():
        nonlocal calls
        calls += 1
        return Payload(items=["ready"])

    asyncio.run(get(cache, factory))
    asyncio.run(get(cache, factory))
    assert calls == 1


def test_jpeg_cache_singleflight_does_not_hold_semantic_lock_or_block_other_images():
    calls = []
    started, release = threading.Event(), threading.Event()

    def encode(data, size):
        calls.append((data, size))
        if data == b"slow":
            started.set()
            assert release.wait(5)
        return data + str(size).encode()

    with ThreadPoolExecutor(max_workers=4) as pool:
        first = pool.submit(cached_jpeg, encode, b"slow", 1024, 1024)
        assert started.wait(5)
        second = pool.submit(cached_jpeg, encode, b"slow", 1024, 1024)
        try:
            assert cache_module._lock.acquire(timeout=0.2)
            cache_module._lock.release()
            other = pool.submit(cached_jpeg, encode, b"other", 768, 1024)
            assert other.result(timeout=2) == b"other768"
        finally:
            release.set()
        assert first.result() == second.result() == b"slow1024"
    assert calls.count((b"slow", 1024)) == 1
    assert cached_jpeg(encode, b"slow", 768, 1024) == b"slow768"
    assert cached_jpeg(encode, b"slow", 768, 0) == b"slow768"
    assert calls.count((b"slow", 768)) == 2


def test_cache_parameters_preserve_old_run_fingerprint_and_stay_out_of_http_parameters():
    from image_agent.server.schemas import AgentParameters

    old = AgentConfig()
    changed = AgentConfig(
        cache_enabled=False,
        cache_directory="custom/cache",
        cache_max_bytes=2048,
        cache_ttl_seconds=30,
        jpeg_cache_max_bytes=0,
    )
    assert config_fingerprint(old) == config_fingerprint(changed)
    store = RunStore()
    store.json(
        "run.json", {"version": 2, "config_fingerprint": config_fingerprint(old), "inputs": []}
    )
    assert store.load_metadata(changed)["version"] == 2
    params = AgentParameters(
        **{name: getattr(changed, name) for name in AgentParameters.model_fields}
    )
    assert not any("cache" in name for name in params.model_dump())
    assert AgentConfig(**params.model_dump()).cache_directory is None


def test_cold_salt_creation_cannot_expose_an_empty_file_to_another_worker(tmp_path, monkeypatch):
    cache = make_cache(tmp_path)
    created, finish_write = threading.Event(), threading.Event()
    original_open = Path.open

    class SlowStream:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            created.set()
            assert finish_write.wait(5)
            return self.stream

        def __exit__(self, *args):
            self.stream.close()

    def delayed_open(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return SlowStream(stream) if path.name == ".scope-salt" and mode == "xb" else stream

    monkeypatch.setattr(Path, "open", delayed_open)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(cache._key, "extract_garments", {"bytes": b"same"}, Payload)
        assert created.wait(5)
        second = pool.submit(cache._key, "extract_garments", {"bytes": b"same"}, Payload)
        finish_write.set()
        assert first.result() == second.result()


async def test_disk_pruning_preserves_unrelated_json_files(tmp_path):
    cache = make_cache(tmp_path, cache_max_bytes=550)
    cache.directory.mkdir(parents=True)
    unrelated = cache.directory / "config.json"
    unrelated.write_text("user configuration" * 100, encoding="utf8")

    async def factory():
        return Payload(items=["ready"])

    for n in range(5):
        await get(cache, factory, inputs={"n": n})
    assert unrelated.read_text(encoding="utf8") == "user configuration" * 100
