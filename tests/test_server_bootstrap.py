import os
import subprocess
import sys

import pytest


def test_required_environment_is_explicit_and_safe(monkeypatch):
    from image_agent.server.settings import ServerSettings
    for name in tuple(os.environ):
        if name.startswith("IMAGE_AGENT_SERVER_"):
            monkeypatch.delenv(name)
    monkeypatch.setenv("IMAGE_AGENT_OPENROUTER_API_KEY", "legacy-secret")
    with pytest.raises(ValueError) as caught:
        ServerSettings.from_env()
    assert "DATABASE_URL" in str(caught.value)
    assert "legacy-secret" not in str(caught.value)


def test_settings_validate_mysql_and_hide_secrets(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    from image_agent.server.settings import ServerSettings
    values = {"DATABASE_URL": "mysql+asyncmy://u:db-secret@localhost/a", "TOKEN": "token-secret",
              "SECRET_KEY": Fernet.generate_key().decode(), "DATA_DIR": str(tmp_path)}
    for k, v in values.items():
        monkeypatch.setenv("IMAGE_AGENT_SERVER_" + k, v)
    settings = ServerSettings.from_env()
    assert settings.worker_concurrency == 1 and settings.queue_capacity == 100
    assert settings.host == "127.0.0.1"
    assert "db-secret" not in repr(settings) and "token-secret" not in repr(settings)
    monkeypatch.setenv("IMAGE_AGENT_SERVER_DATABASE_URL", "sqlite+aiosqlite:///test.db")
    with pytest.raises(ValueError):
        ServerSettings.from_env()


def test_core_and_server_package_import_without_service_dependencies(tmp_path):
    script = """
import sys, importlib.abc
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, *args):
        if fullname.split('.')[0] in {'fastapi','uvicorn','sqlalchemy','asyncmy','alembic','cryptography','multipart'}:
            raise ImportError('optional dependency blocked')
sys.meta_path.insert(0, Block())
import image_agent
import image_agent.server
assert callable(image_agent.create_images)
"""
    result = subprocess.run([sys.executable, "-c", script], cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_token_and_safe_error_envelopes(tmp_path):
    from fastapi import Depends
    from fastapi.testclient import TestClient
    from pydantic import BaseModel

    from image_agent.server.app import create_app
    from image_agent.server.auth import require_token
    from image_agent.server.settings import ServerError, ServerSettings
    settings = ServerSettings("sqlite+aiosqlite://", "server-token", "unused", tmp_path)
    app = create_app(settings)

    class Input(BaseModel):
        value: int

    @app.post("/probe", dependencies=[Depends(require_token)])
    async def probe(body: Input):
        return {"ok": True}

    @app.get("/failure")
    async def failure():
        raise ServerError(409, "conflict", "版本冲突")

    client = TestClient(app)
    for token in (None, "wrong"):
        headers = {} if token is None else {"Authorization": "Bearer " + token}
        response = client.post("/probe", json={"value": 1}, headers=headers)
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"
        assert set(response.json()) == {"code", "message", "request_id"}
    assert client.post("/probe", json={"value": 1}, headers={"Authorization": "Bearer server-token"}).status_code == 200
    invalid = client.post("/probe", json={"value": "secret-validation-input"},
                          headers={"Authorization": "Bearer server-token"})
    assert invalid.status_code == 422
    assert "secret-validation-input" not in invalid.text
    failed = client.get("/failure")
    assert failed.status_code == 409 and failed.json()["code"] == "conflict"
    assert failed.headers["X-Request-ID"] == failed.json()["request_id"]


def test_readiness_never_reports_uninitialized_resources_ready(tmp_path):
    from fastapi.testclient import TestClient

    from image_agent.server.app import create_app
    from image_agent.server.settings import ServerSettings
    app = create_app(ServerSettings("sqlite+aiosqlite://", "server-token", "unused", tmp_path))
    client = TestClient(app)
    assert client.get("/health/live").json() == {"status": "ok"}
    response = client.get("/health/ready")
    assert response.status_code == 503
    assert "sqlite" not in response.text and str(tmp_path) not in response.text



def test_unexpected_failure_is_sanitized_without_propagating_to_server_logs(tmp_path):
    from fastapi.testclient import TestClient

    from image_agent.server.app import create_app
    from image_agent.server.settings import ServerSettings
    app = create_app(ServerSettings("sqlite+aiosqlite://", "server-token", "unused", tmp_path))

    @app.get("/unexpected")
    async def unexpected():
        raise RuntimeError("raw-key-that-must-never-reach-logs")

    # The default client re-raises unhandled exceptions, just as Uvicorn logs them.
    response = TestClient(app).get("/unexpected")
    assert response.status_code == 500
    assert "raw-key" not in response.text
    assert response.headers["X-Request-ID"] == response.json()["request_id"]


def test_lifespan_database_outage_keeps_docs_public_and_never_falls_back(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from image_agent.server.app import create_app
    from image_agent.server.settings import ServerSettings
    monkeypatch.setenv("IMAGE_AGENT_OPENROUTER_API_KEY", "legacy-private-key")
    # A read-only missing SQLite DB gives a real connection failure, with no provider call.
    settings = ServerSettings("sqlite+aiosqlite:///file:does-not-exist?mode=ro&uri=true",
                              "server-token", Fernet.generate_key().decode(), tmp_path)
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/health/ready").status_code == 503
        assert client.get("/docs").status_code == 200
        response = client.get("/api/v1/services", headers={"Authorization": "Bearer server-token"})
        assert response.status_code == 503
        assert "legacy-private-key" not in response.text



@pytest.mark.asyncio
async def test_lifespan_initialized_database_and_storage_become_ready(tmp_path):
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from image_agent.server.app import create_app
    from image_agent.server.database import Database
    from image_agent.server.db_models import Base
    from image_agent.server.settings import ServerSettings
    settings = ServerSettings(f"sqlite+aiosqlite:///{tmp_path / 'ready.db'}", "server-token",
                              Fernet.generate_key().decode(), tmp_path / "data")
    database = Database(settings.database_url)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await database.close()
    with TestClient(create_app(settings)) as client:
        assert client.get("/health/ready").json() == {"status": "ok"}
        response = client.get("/api/v1/services", headers={"Authorization": "Bearer server-token"})
        assert response.status_code == 200 and response.json() == []
        assert (tmp_path / "data" / "media").is_dir()
    # Readiness never claims success outside an active lifespan.
    assert client.get("/health/ready").status_code == 503



@pytest.mark.asyncio
async def test_failed_startup_rejects_submissions_even_if_database_reconnects(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet
    from fastapi.testclient import TestClient

    from image_agent.server.app import create_app
    from image_agent.server.database import Database
    from image_agent.server.db_models import Base
    from image_agent.server.settings import ServerError, ServerSettings
    settings = ServerSettings(f"sqlite+aiosqlite:///{tmp_path / 'reconnect.db'}", "server-token",
                              Fernet.generate_key().decode(), tmp_path / "data")
    db = Database(settings.database_url)
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await db.close()

    async def fail_probe(self):
        raise ServerError(503, "database_unavailable", "数据库暂时不可用")

    monkeypatch.setattr(Database, "initialize", fail_probe)
    with TestClient(create_app(settings)) as client:
        # DB queries now work, but no worker started; callers must see unavailable.
        response = client.get("/api/v1/services", headers={"Authorization": "Bearer server-token"})
        assert response.status_code == 503
        assert client.get("/docs").status_code == 200
