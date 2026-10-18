import json

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import event

from image_agent.server.app import create_app
from image_agent.server.config_service import ConfigService
from image_agent.server.database import Database
from image_agent.server.db_models import Base
from image_agent.server.secrets import SecretCipher
from image_agent.server.settings import ServerSettings


@pytest.fixture
async def config_client(tmp_path):
    settings = ServerSettings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'api.db'}",
        token="test-token",
        secret_key=Fernet.generate_key().decode(),
        data_dir=tmp_path / "data",
    )
    db = Database(settings.database_url)

    @event.listens_for(db.engine.sync_engine, "connect")
    def foreign_keys(connection, _):
        connection.execute("PRAGMA foreign_keys=ON")

    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app = create_app(settings)
    app.state.database = db
    app.state.config_service = ConfigService(db.session_factory, SecretCipher(settings.secret_key))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"Authorization": "Bearer test-token"},
    ) as client:
        yield client, app
    await db.close()


async def create_graph(client):
    async def create(path, body):
        response = await client.post(path, json=body)
        assert response.status_code == 201, response.text
        return response.json()

    image = await create(
        "/api/v1/services",
        {
            "name": "images",
            "base_url": "https://images.example",
            "protocol": "openai_images",
            "api_key": "never-public-key",
        },
    )
    vision = await create(
        "/api/v1/services",
        {
            "name": "vision",
            "base_url": "https://vision.example",
            "protocol": "chat_completions",
            "api_key": "never-public-vision-key",
        },
    )
    im = await create("/api/v1/model-configs", {"service_id": image["id"], "model": "image-one"})
    vm = await create("/api/v1/model-configs", {"service_id": vision["id"], "model": "vision-one"})
    agent = await create(
        "/api/v1/agent-configs",
        {
            "name": "product",
            "parameters": {"vision_image_limit": 5},
            "image_models": {"fast": im["id"]},
            "stages": {"vision": vm["id"]},
        },
    )
    return image, vision, im, vm, agent


async def test_full_http_crud_pagination_versions_and_references(config_client):
    client, _ = config_client
    image, vision, im, vm, agent = await create_graph(client)
    for path, row in (("services", image), ("model-configs", im), ("agent-configs", agent)):
        response = await client.get(f"/api/v1/{path}/{row['id']}")
        assert response.status_code == 200
        assert response.json()["id"] == row["id"]
        assert (await client.get(f"/api/v1/{path}?limit=1&offset=0")).status_code == 200
        assert (await client.get(f"/api/v1/{path}?limit=101")).status_code == 422
    assert len((await client.get("/api/v1/services?limit=1&offset=1")).json()) == 1
    assert (await client.delete(f"/api/v1/services/{image['id']}")).status_code == 409
    assert (
        await client.patch(
            f"/api/v1/model-configs/{im['id']}", json={"version": 1, "enabled": False}
        )
    ).status_code == 409
    response = await client.patch(
        f"/api/v1/agent-configs/{agent['id']}", json={"version": 1, "enabled": False}
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.json()["enabled"] is False
    assert (
        await client.patch(
            f"/api/v1/agent-configs/{agent['id']}", json={"version": 1, "name": "stale"}
        )
    ).status_code == 409
    assert (await client.delete(f"/api/v1/agent-configs/{agent['id']}")).status_code == 204
    for row in (im, vm):
        assert (await client.delete(f"/api/v1/model-configs/{row['id']}")).status_code == 204
    for row in (image, vision):
        assert (await client.delete(f"/api/v1/services/{row['id']}")).status_code == 204
    assert (await client.get("/api/v1/services")).json() == []
    assert (await client.get(f"/api/v1/services/{image['id']}")).status_code == 404


async def test_secret_write_only_patch_and_unknown_fields(config_client, caplog):
    client, app = config_client
    image, _, _, _, agent = await create_graph(client)
    service_url = f"/api/v1/services/{image['id']}"
    response = await client.patch(service_url, json={"version": 1, "name": "new"})
    assert response.status_code == 200
    resolved, _ = await app.state.config_service.resolve_agent_config(agent["id"])
    assert resolved.services[image["id"]].credential() == "never-public-key"
    response = await client.patch(service_url, json={"version": 2, "api_key": "new-private-key"})
    assert response.status_code == 200
    assert "new-private-key" not in response.text
    resolved, _ = await app.state.config_service.resolve_agent_config(agent["id"])
    assert resolved.services[image["id"]].credential() == "new-private-key"
    for fields in (
        {"api_key": None},
        {"api_key": ""},
        {"api_key": "   "},
        {"secret_typo": "do-not-echo"},
        {"base_url": "https://user:do-not-echo@example.com"},
    ):
        response = await client.patch(service_url, json={"version": 3, **fields})
        assert response.status_code == 422
        assert "do-not-echo" not in response.text
        assert set(response.json()) == {"code", "message", "request_id"}
    public = (await client.get(service_url)).json()
    assert public["has_api_key"] is True
    assert "api_key" not in public and "api_key_encrypted" not in public
    assert "new-private-key" not in caplog.text


async def test_agent_route_patch_is_atomic_and_validates_protocol(config_client):
    client, _ = config_client
    _, _, im, vm, agent = await create_graph(client)
    url = f"/api/v1/agent-configs/{agent['id']}"
    response = await client.patch(
        url, json={"version": 1, "name": "should-roll-back", "stages": {"vision": im["id"]}}
    )
    assert response.status_code == 422
    unchanged = (await client.get(url)).json()
    assert unchanged["name"] == "product"
    assert unchanged["version"] == 1
    assert unchanged["stages"] == {"vision": vm["id"]}
    response = await client.patch(
        url, json={"version": 1, "stages": {"vision": vm["id"], "audit_image": vm["id"]}}
    )
    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.json()["stages"]["audit_image"] == vm["id"]


async def test_openapi_read_schemas_have_no_credentials(config_client):
    client, _ = config_client
    schema = (await client.get("/openapi.json")).json()
    write = schema["components"]["schemas"]["ServiceCreate"]["properties"]["api_key"]
    assert write["writeOnly"] is True
    read = schema["components"]["schemas"]["ServiceRead"]["properties"]
    assert "has_api_key" in read
    assert "api_key" not in read and "api_key_encrypted" not in read
    assert "never-public-key" not in json.dumps(schema)
    for prefix in ("services", "model-configs", "agent-configs"):
        assert schema["paths"][f"/api/v1/{prefix}"]["post"]["responses"]["201"]["content"][
            "application/json"
        ]["schema"]
        assert schema["paths"][f"/api/v1/{prefix}/{{identifier}}"]["patch"]["security"]
