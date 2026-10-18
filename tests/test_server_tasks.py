import asyncio
from types import SimpleNamespace

import pytest
import pytest_asyncio
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from image_agent.config import AgentConfig
from image_agent.contracts import CreationRequestDTO
from tests.test_server_media import png


def config(model="image-old"):
    return AgentConfig.from_mapping(
        {
            "services": {
                "i": {
                    "base_url": "https://example.test",
                    "protocol": "openai_images",
                    "api_key": "private-credential",
                },
                "v": {
                    "base_url": "https://example.test",
                    "protocol": "chat_completions",
                    "api_key": "private-credential",
                },
            },
            "image_models": {"pro": {"service": "i", "model": model}},
            "stages": {"vision": {"service": "v", "model": "vision"}},
        }
    )


@pytest_asyncio.fixture
async def service(tmp_path):
    from image_agent.server.db_models import Base
    from image_agent.server.media import MediaStore
    from image_agent.server.secrets import SecretCipher

    try:
        from image_agent.server.task_service import TaskService
    except ImportError:
        pytest.fail("TaskService must persist submissions and enforce atomic idempotency")
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.sqlite'}")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    class ConfigService:
        current = config()

        async def resolve_agent_config_in_session(self, session, identifier):
            return self.current, {"agent_config": {identifier: 1}}

    media = MediaStore(tmp_path / "data")
    media.initialize()
    result = TaskService(
        sessions,
        ConfigService(),
        media,
        SecretCipher(Fernet.generate_key().decode()),
        SimpleNamespace(queue_capacity=100),
    )
    yield result
    await engine.dispose()


async def submit(service, key="key", kind="create_images", **kwargs):
    upload = await service.upload(png())
    request = CreationRequestDTO(
        product_name="cup",
        category="kitchen",
        platforms=["taobao"],
        output_types=["main_image"],
        materials=[{"material_id": "m1", "media_id": upload["media_id"]}],
    )
    return await service.submit_task(kind, "config", request, key, **kwargs), request


async def test_idempotency_and_snapshot_are_durable(service):
    task, request = await submit(service)
    service.config_service.current = config("image-new")
    repeated = await service.submit_task("create_images", "config", request, "key")
    assert repeated["task_id"] == task["task_id"]
    stored = await service.repository.get(task["task_id"])
    decrypted = service.cipher.decrypt_mapping(stored.config_snapshot_encrypted)
    assert decrypted["image_models"]["pro"]["model"] == "image-old"
    assert "private-credential" not in str(task)
    with pytest.raises(Exception) as caught:
        await service.submit_task("analyze_materials", "config", request, "key")
    assert caught.value.status_code == 409


async def test_concurrent_submission_and_claim_have_one_owner(service):
    task, request = await submit(service)
    results = await asyncio.gather(
        *[service.submit_task("create_images", "config", request, "race") for _ in range(6)]
    )
    assert len({row["task_id"] for row in results}) == 1
    claims = await asyncio.gather(*[service.repository.claim() for _ in range(6)])
    claimed = [row.id for row in claims if row is not None]
    assert len(claimed) == len(set(claimed)) == 2
    assert task["task_id"] in claimed


async def test_missing_media_and_full_queue_are_rejected(service):
    task, request = await submit(service)
    service.settings.queue_capacity = 1
    with pytest.raises(Exception) as caught:
        await service.submit_task("create_images", "config", request, "full")
    assert caught.value.status_code == 429
    service.settings.queue_capacity = 100
    broken = request.model_copy(deep=True)
    broken.materials[0].media_id = "absent"
    with pytest.raises(Exception) as caught:
        await service.submit_task("create_images", "config", broken, "missing")
    assert caught.value.status_code == 404
    assert (await service.get_task(task["task_id"]))["status"] == "queued"


async def test_duplicate_key_across_service_instances_uses_database_unique_constraint(service):
    from image_agent.server.task_service import TaskService

    task, request = await submit(service)
    other = TaskService(
        service.repository.session_factory,
        service.config_service,
        service.media_store,
        service.cipher,
        service.settings,
    )
    results = await asyncio.gather(
        service.submit_task("create_images", "config", request, "unique"),
        other.submit_task("create_images", "config", request, "unique"),
    )
    assert results[0]["task_id"] == results[1]["task_id"]


@pytest_asyncio.fixture
async def client(service, tmp_path):
    import httpx

    from image_agent.server.app import create_app
    from image_agent.server.settings import ServerSettings

    app = create_app(
        ServerSettings(
            database_url="sqlite+aiosqlite://",
            token="test-token",
            secret_key=Fernet.generate_key().decode(),
            data_dir=tmp_path,
        )
    )
    app.state.database = object()
    app.state.task_service = service
    app.state.media_store = service.media_store
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        yield client


async def test_http_upload_and_task_boundaries_require_authentication(client, service):
    response = await client.post(
        "/api/v1/media", files={"file": ("../../outside.png", png(), "image/png")}
    )
    assert response.status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    response = await client.post(
        "/api/v1/media", headers=headers, files={"file": ("../../outside.png", png(), "image/png")}
    )
    assert response.status_code == 201
    media_id = response.json()["media_id"]
    body = {
        "kind": "create_images",
        "agent_config_id": "config",
        "request": {
            "product_name": "cup",
            "category": "kitchen",
            "platforms": ["taobao"],
            "output_types": ["main_image"],
            "materials": [{"material_id": "m1", "media_id": media_id}],
        },
    }
    response = await client.post("/api/v1/tasks", headers=headers, json=body)
    assert response.status_code == 422
    headers["Idempotency-Key"] = "http-task"
    response = await client.post("/api/v1/tasks", headers=headers, json=body)
    assert response.status_code == 202
    for field, value in (
        ("output_dir", "C:/private"),
        ("analysis", {"trusted": True}),
        ("config", {"api_key": "SECRET"}),
    ):
        response = await client.post(
            "/api/v1/tasks",
            headers=headers,
            json=body | {"request": body["request"] | {field: value}},
        )
        assert response.status_code == 422 and "SECRET" not in response.text
    for extra in ({"path": "C:/private"}, {"url": "https://private.test/image"}):
        request = body["request"] | {"materials": [body["request"]["materials"][0] | extra]}
        assert (
            await client.post("/api/v1/tasks", headers=headers, json=body | {"request": request})
        ).status_code == 422
    request = body["request"] | {
        "output_types": ["long_image"],
        "long_image": {"path": "C:/private"},
    }
    assert (
        await client.post("/api/v1/tasks", headers=headers, json=body | {"request": request})
    ).status_code == 422
    request = body["request"] | {"selection": {"mode": "explicit", "subject_ids": ["s1"]}}
    assert (
        await client.post("/api/v1/tasks", headers=headers, json=body | {"request": request})
    ).status_code == 422
    assert list(service.media_store.root.parent.glob("outside.png")) == []


async def test_same_key_different_unicode_case_or_trailing_space_is_distinct(service):
    task, request = await submit(service)
    results = [
        await service.submit_task("create_images", "config", request, key)
        for key in ("Key", "key ", "键", "鍵")
    ]
    assert len({r["task_id"] for r in [task, *results]}) == 5
