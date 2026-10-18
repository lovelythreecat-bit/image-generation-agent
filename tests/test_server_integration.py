"""Acceptance probe: real HTTP+DB+core, scripted external provider responses only."""

import asyncio
import base64
import json
import os
import socket
import sys

import httpx
import pytest
from cryptography.fernet import Fernet
from sqlalchemy import make_url

from image_agent.server.app import create_app
from image_agent.server.database import Database
from image_agent.server.db_models import Base
from image_agent.server.settings import ServerSettings
from tests.fakes import analysis_data
from tests.test_images import picture

_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_CONNECT_EX = socket.socket.connect_ex


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Allow only an explicitly dedicated DB; all providers stay MockTransport."""
    raw = os.environ.get("IMAGE_AGENT_ACCEPTANCE_DATABASE_URL")
    addresses, port = set(), None
    if raw:
        url = make_url(raw)
        if url.drivername != "mysql+asyncmy" or not (url.database or "").startswith(
            "image_agent_http_test_"
        ):
            raise RuntimeError(
                "HTTP integration requires a dedicated image_agent_http_test_* database"
            )
        port = url.port or 3306
        addresses = {
            row[4][0] for row in socket.getaddrinfo(url.host, port, type=socket.SOCK_STREAM)
        }

    def guard(original):
        def connect(sock, address):
            caller = sys._getframe(1)
            if (
                caller.f_globals.get("__name__") == "socket"
                and "socketpair" in caller.f_code.co_name
            ):
                return original(sock, address)
            if isinstance(address, tuple) and address[0] in addresses and address[1] == port:
                return original(sock, address)
            raise AssertionError("HTTP integration must not connect to a real model provider")

        return connect

    monkeypatch.setattr(socket.socket, "connect", guard(_ORIGINAL_CONNECT))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(_ORIGINAL_CONNECT_EX))


def mock_provider(monkeypatch):
    original = httpx.AsyncClient
    calls, clients = [], []

    async def handler(req):
        calls.append((req.url.path, req.headers.get("content-type")))
        if req.url.path.endswith("/images"):
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(picture((1600, 1600))).decode()}]}
            )
        payload = json.loads(req.content)
        text = payload["messages"][0]["content"][0]["text"]
        if text.startswith("Discover"):
            result = analysis_data()
        elif text.startswith("Recheck the candidate"):
            result = {
                "outcome": "verified",
                "subject_checks": [
                    {"subject_id": "s1", "score": 95, "same_product": True, "reason": "same"}
                ],
                "fact_checks": [
                    {"fact_id": "f1", "presence": "present", "fidelity_score": 95, "reason": "same"}
                ],
                "intent_valid": True,
                "issue_resolutions": [],
            }
        else:
            result = {
                "subject_checks": [
                    {"subject_id": "s1", "score": 95, "same_product": True, "reason": "same"}
                ],
                "fact_checks": [
                    {"fact_id": "f1", "presence": "present", "fidelity_score": 95, "reason": "same"}
                ],
                "element_checks": [
                    {
                        "element_id": "e1",
                        "presence": "present",
                        "fidelity_score": 95,
                        "reason": "same",
                    }
                ],
                "constraint_checks": [],
                "visual_quality": 90,
                "platform_compliance": 90,
                "garment_fusion": 90,
                "model_preference": 90,
                "output_intent": 90,
                "passed": True,
                "reason": "ok",
            }
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        client = original(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls, clients


@pytest.mark.asyncio
@pytest.mark.parametrize("image_protocol", ["openrouter_images", "openai_images"])
async def test_http_upload_analyze_create_download_restart(tmp_path, monkeypatch, image_protocol):
    settings = ServerSettings(
        database_url=os.environ.get(
            "IMAGE_AGENT_ACCEPTANCE_DATABASE_URL",
            f"sqlite+aiosqlite:///{tmp_path / 'acceptance.db'}",
        ),
        token="offline-test-token",
        secret_key=Fernet.generate_key().decode(),
        data_dir=tmp_path / "data",
    )
    db = Database(settings.database_url)
    async with db.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    await db.close()
    original_client = httpx.AsyncClient
    calls, clients = mock_provider(monkeypatch)
    app = create_app(settings)
    headers = {"Authorization": "Bearer offline-test-token"}
    async with app.router.lifespan_context(app):
        async with original_client(
            transport=httpx.ASGITransport(app=app), base_url="http://test", headers=headers
        ) as client:
            assert (await client.get("/health/ready")).status_code == 200
            no_auth = await client.get("/api/v1/services", headers={"Authorization": ""})
            assert no_auth.status_code == 401
            raw = {
                "services": {
                    "images": {
                        "base_url": "https://image.example/v1",
                        "protocol": image_protocol,
                        "generation_path": "/images",
                        "edit_path": "/images",
                        "api_key": "offline-image-private-key",
                    },
                    "vision": {
                        "base_url": "https://vision.example/v1",
                        "protocol": "chat_completions",
                        "api_key": "offline-vision-private-key",
                    },
                },
                "image_models": {"fast": {"service": "images", "model": "gpt-image-2"}},
                "stages": {"vision": {"service": "vision", "model": "gpt-4o-mini"}},
            }
            agent = await app.state.config_service.import_config(raw, "integration")
            uploaded = await client.post(
                "/api/v1/media", files={"file": ("cup.png", picture(), "image/png")}
            )
            assert uploaded.status_code == 201, uploaded.text
            request = {
                "product_name": "Cup",
                "category": "kitchen",
                "platforms": ["taobao"],
                "output_types": ["main_image"],
                "image_model": "fast",
                "materials": [{"material_id": "m1", "media_id": uploaded.json()["media_id"]}],
            }
            analysis_submission = {
                "kind": "analyze_materials",
                "agent_config_id": agent["id"],
                "request": request,
            }
            analyzed = await client.post(
                "/api/v1/tasks",
                json=analysis_submission,
                headers={"Idempotency-Key": "integration-analysis-" + agent["id"]},
            )
            assert analyzed.status_code == 202, analyzed.text
            analysis_id = analyzed.json()["task_id"]

            async def wait(identifier):
                for _ in range(200):
                    response = await client.get(f"/api/v1/tasks/{identifier}")
                    assert response.status_code == 200, response.text
                    if response.json()["status"] in ("completed", "failed", "interrupted"):
                        assert response.json()["status"] == "completed", response.text
                        return response.json()
                    await asyncio.sleep(0.05)
                pytest.fail("Worker did not finish the offline task")

            await wait(analysis_id)
            generated_submission = {
                "kind": "create_images",
                "agent_config_id": agent["id"],
                "request": request,
                "analysis_task_id": analysis_id,
            }
            generated = await client.post(
                "/api/v1/tasks",
                json=generated_submission,
                headers={"Idempotency-Key": "integration-create-" + agent["id"]},
            )
            assert generated.status_code == 202, generated.text
            task_id = generated.json()["task_id"]
            replay = await client.post(
                "/api/v1/tasks",
                json=generated_submission,
                headers={"Idempotency-Key": "integration-create-" + agent["id"]},
            )
            assert replay.status_code == 202 and replay.json()["task_id"] == task_id
            status = await wait(task_id)
            assert status["core_status"] == "succeeded", status
            result = await client.get(f"/api/v1/tasks/{task_id}/result")
            assert result.status_code == 200, result.text
            asset = result.json()["assets"][0]
            assert asset["status"] == "succeeded" and asset["blob_id"]
            blob = await client.get(f"/api/v1/tasks/{task_id}/blobs/{asset['blob_id']}")
            assert blob.status_code == 200 and blob.content == picture((1600, 1600))
            assert blob.headers["content-type"].startswith("image/")
            assert any(
                path.endswith("/images")
                and (
                    "multipart/form-data" in content
                    if image_protocol == "openai_images"
                    else "application/json" in content
                )
                for path, content in calls
            )
            assert len(calls) == 3, "Idempotent requests or reuse must not duplicate provider calls"
            public_json = json.dumps(result.json())
            assert "offline-image-private-key" not in public_json
            assert "offline-vision-private-key" not in public_json
            assert str(tmp_path) not in public_json
    assert all(client.is_closed for client in clients)
    second = create_app(settings)
    async with second.router.lifespan_context(second):
        async with original_client(
            transport=httpx.ASGITransport(app=second), base_url="http://test", headers=headers
        ) as client:
            persisted = await client.get(f"/api/v1/tasks/{task_id}/result")
            assert persisted.status_code == 200 and persisted.json() == result.json()
            assert (await client.get(f"/api/v1/agent-configs/{agent['id']}")).status_code == 200
            assert len(calls) == 3, "Restart must not rerun completed paid generations"
