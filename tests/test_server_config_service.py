import json

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event

from image_agent.server.config_service import ConfigService
from image_agent.server.database import Database
from image_agent.server.db_models import Base, ProviderService
from image_agent.server.secrets import SecretCipher
from image_agent.server.settings import ServerError


@pytest.fixture
async def config_service(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'config.db'}")

    @event.listens_for(db.engine.sync_engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    service = ConfigService(db.session_factory, SecretCipher(Fernet.generate_key().decode()))
    yield service
    await db.close()


async def graph(service):
    image = await service.create_service(
        {
            "name": "images",
            "base_url": "https://image.example/v1",
            "protocol": "openai_images",
            "api_key": "image-secret",
        }
    )
    vision = await service.create_service(
        {
            "name": "vision",
            "base_url": "https://vision.example/v1",
            "protocol": "chat_completions",
            "api_key": "vision-secret",
        }
    )
    im = await service.create_model_config({"service_id": image["id"], "model": "gpt-image-2"})
    vm = await service.create_model_config({"service_id": vision["id"], "model": "gpt-4o-mini"})
    agent = await service.create_agent_config(
        {
            "name": "shop",
            "image_models": {"fast": im["id"]},
            "stages": {"vision": vm["id"], "audit_image": vm["id"]},
            "parameters": {"model_concurrency": 3, "request_timeout_seconds": 70},
        }
    )
    return image, vision, im, vm, agent


async def test_roundtrip_config_and_encrypted_snapshot(config_service, monkeypatch):
    monkeypatch.setenv("IMAGE_AGENT_MODEL_FAST", "must-not-inherit")
    image, vision, im, vm, agent = await graph(config_service)
    config, versions = await config_service.resolve_agent_config(agent["id"])
    assert config.model_name("fast") == "gpt-image-2"
    assert config.vision_route("audit_image").model == "gpt-4o-mini"
    assert config.model_concurrency == 3
    assert config.request_timeout_seconds == 70
    assert config.services[image["id"]].credential() == "image-secret"
    assert versions == {
        "agent_config": {agent["id"]: 1},
        "model_configs": {im["id"]: 1, vm["id"]: 1},
        "services": {image["id"]: 1, vision["id"]: 1},
    }
    async with config_service.session_factory() as session:
        stored = await session.get(ProviderService, image["id"])
        assert "image-secret" not in stored.api_key_encrypted
    assert "image-secret" not in json.dumps(image)
    assert image["has_api_key"] is True


async def test_reference_version_and_secret_patch(config_service):
    image, _, im, _, agent = await graph(config_service)
    for action in (
        lambda: config_service.delete_service(image["id"]),
        lambda: config_service.update_service(image["id"], {"version": 1, "enabled": False}),
        lambda: config_service.delete_model_config(im["id"]),
    ):
        with pytest.raises(ServerError) as error:
            await action()
        assert error.value.status_code == 409
    result = await config_service.update_service(image["id"], {"version": 1, "name": "new name"})
    assert result["version"] == 2
    config, _ = await config_service.resolve_agent_config(agent["id"])
    assert config.services[image["id"]].credential() == "image-secret"
    await config_service.update_service(image["id"], {"version": 2, "api_key": "replacement"})
    config, _ = await config_service.resolve_agent_config(agent["id"])
    assert config.services[image["id"]].credential() == "replacement"
    with pytest.raises(ServerError) as error:
        await config_service.update_service(image["id"], {"version": 1, "name": "stale"})
    assert error.value.status_code == 409
    for value in (None, "", "   "):
        with pytest.raises(ServerError) as error:
            await config_service.update_service(image["id"], {"version": 3, "api_key": value})
        assert error.value.status_code == 422


async def test_invalid_routes_and_parameters_roll_back(config_service):
    image, _, im, vm, agent = await graph(config_service)
    for fields in (
        {"stages": {}},
        {"stages": {"vision": im["id"]}},
        {"image_models": {"typo": im["id"]}},
        {"parameters": {"model_concurrency": 0}},
        {"stages": {"vision": vm["id"], "bad": vm["id"]}},
    ):
        with pytest.raises(ServerError) as error:
            await config_service.update_agent_config(agent["id"], {"version": 1, **fields})
        assert error.value.status_code == 422
        unchanged = await config_service.get_agent_config(agent["id"])
        assert unchanged["version"] == 1
        assert unchanged["stages"] == {"vision": vm["id"], "audit_image": vm["id"]}
    with pytest.raises(ServerError) as error:
        await config_service.update_service(
            image["id"], {"version": 1, "protocol": "chat_completions"}
        )
    assert error.value.status_code == 409
    assert (await config_service.get_service(image["id"]))["protocol"] == "openai_images"


async def test_disabled_and_missing_configs_do_not_resolve(config_service):
    _, _, _, _, agent = await graph(config_service)
    await config_service.update_agent_config(agent["id"], {"version": 1, "enabled": False})
    with pytest.raises(ServerError) as error:
        await config_service.resolve_agent_config(agent["id"])
    assert error.value.status_code == 409
    with pytest.raises(ServerError) as error:
        await config_service.get_service("missing")
    assert error.value.status_code == 404


async def test_resolve_in_submission_transaction_freezes_old_config(config_service):
    image, _, im, _, agent = await graph(config_service)
    async with config_service.session_factory() as session, session.begin():
        config, versions = await config_service.resolve_agent_config_in_session(
            session, agent["id"]
        )
        payload = config.model_dump(
            mode="json",
            exclude={
                "openrouter_api_key",
                "openrouter_base_url",
                "model_pro",
                "model_fast",
                "model_base",
                "quality_model",
                "style_model",
            },
        )
        for sid, item in payload["services"].items():
            item["api_key"] = config.services[sid].credential()
        encrypted = config_service.cipher.encrypt_mapping(payload)
    await config_service.update_model_config(im["id"], {"version": 1, "model": "new-model"})
    frozen = config_service.cipher.decrypt_mapping(encrypted)
    assert frozen["image_models"]["fast"]["model"] == "gpt-image-2"
    assert frozen["services"][image["id"]]["api_key"] == "image-secret"
    assert versions["model_configs"][im["id"]] == 1
    new, _ = await config_service.resolve_agent_config(agent["id"])
    assert new.model_name("fast") == "new-model"


def test_cipher_rejects_invalid_keys_and_payloads():
    cipher = SecretCipher(Fernet.generate_key().decode())
    token = cipher.encrypt_mapping({"api_key": "do-not-echo"})
    assert cipher.decrypt_mapping(token) == {"api_key": "do-not-echo"}
    other = SecretCipher(Fernet.generate_key().decode())
    with pytest.raises(ServerError) as error:
        other.decrypt_mapping(token)
    assert "do-not-echo" not in str(error.value)
    with pytest.raises(ServerError):
        SecretCipher("invalid-do-not-echo")


async def test_import_normalizes_atomically(config_service, monkeypatch):
    monkeypatch.setenv("DEPLOY_IMAGE_KEY", "env-key")
    raw = {
        "services": {
            "images": {
                "base_url": "https://image.example",
                "protocol": "openai_images",
                "api_key_env": "DEPLOY_IMAGE_KEY",
            },
            "vision": {
                "base_url": "https://vision.example",
                "protocol": "chat_completions",
                "api_key_env": "MISSING_DEPLOY_KEY",
            },
        },
        "image_models": {"fast": {"service": "images", "model": "img"}},
        "stages": {"vision": {"service": "vision", "model": "vis"}},
    }
    monkeypatch.delenv("MISSING_DEPLOY_KEY", raising=False)
    with pytest.raises(ServerError):
        await config_service.import_config(raw, "broken")
    assert await config_service.list_services() == []
    monkeypatch.setenv("MISSING_DEPLOY_KEY", "vision-key")
    imported = await config_service.import_config(raw, "imported")
    resolved, _ = await config_service.resolve_agent_config(imported["id"])
    assert resolved.services[resolved.image_models["fast"].service].credential() == "env-key"
    legacy = await config_service.import_config(
        {
            "openrouter_api_key": "legacy-key",
            "model_fast": "legacy-image",
            "quality_model": "legacy-vision",
        },
        "legacy",
    )
    resolved, _ = await config_service.resolve_agent_config(legacy["id"])
    assert resolved.model_name("fast") == "legacy-image"
    assert resolved.vision_route("vision").model == "legacy-vision"


async def test_delete_full_graph_and_pagination(config_service):
    image, vision, im, vm, agent = await graph(config_service)
    assert len(await config_service.list_services(limit=1, offset=1)) == 1
    await config_service.delete_agent_config(agent["id"])
    await config_service.delete_model_config(im["id"])
    await config_service.delete_model_config(vm["id"])
    await config_service.delete_service(image["id"])
    await config_service.delete_service(vision["id"])
    assert await config_service.list_services() == []
    assert await config_service.list_model_configs() == []
    assert await config_service.list_agent_configs() == []


async def test_database_probe_never_creates_schema(tmp_path):
    db = Database(f"sqlite+aiosqlite:///{tmp_path / 'probe.db'}")
    try:
        await db.initialize()
        assert await db.ready()
        async with db.engine.connect() as connection:
            from sqlalchemy import text

            names = (
                (
                    await connection.execute(
                        text("SELECT name FROM sqlite_master WHERE type='table'")
                    )
                )
                .scalars()
                .all()
            )
        assert names == []
    finally:
        await db.close()
