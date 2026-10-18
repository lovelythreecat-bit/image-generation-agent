"""Explicit dedicated-DB integration checks; never fall back to the production URL."""

import asyncio
import os
import re
import socket
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from cryptography.fernet import Fernet
from sqlalchemy import make_url, select, text
from sqlalchemy.exc import IntegrityError

from image_agent.server.config_service import ConfigService
from image_agent.server.database import Database
from image_agent.server.db_models import (
    AgentModelRoute,
    Base,
    HttpTask,
    ModelConfig,
)
from image_agent.server.secrets import SecretCipher
from image_agent.server.settings import ServerError

TEST_URL = os.environ.get("IMAGE_AGENT_SERVER_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not TEST_URL,
    reason="Dedicated IMAGE_AGENT_SERVER_TEST_DATABASE_URL not supplied; MySQL not verified",
)
ORIGINAL_CONNECT = socket.socket.connect
ORIGINAL_CONNECT_EX = socket.socket.connect_ex


def dedicated_url():
    try:
        url = make_url(TEST_URL)
        if url.drivername != "mysql+asyncmy" or not re.fullmatch(
            r"image_agent_http_test_[A-Za-z0-9_]+", url.database or ""
        ):
            raise ValueError
        return url
    except (TypeError, ValueError):
        raise RuntimeError(
            "MySQL tests require an explicitly named image_agent_http_test_* dedicated database"
        ) from None


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    """Override global offline fixture solely for this explicitly dedicated host."""
    if not TEST_URL:
        return
    url = dedicated_url()
    allowed_addresses = {
        item[4][0]
        for item in socket.getaddrinfo(url.host, url.port or 3306, type=socket.SOCK_STREAM)
    }

    def guard(original):
        def connect(sock, address):
            caller = sys._getframe(1)
            if (
                caller.f_globals.get("__name__") == "socket"
                and "socketpair" in caller.f_code.co_name
            ):
                return original(sock, address)
            if (
                isinstance(address, tuple)
                and address[0] in allowed_addresses
                and address[1] == (url.port or 3306)
            ):
                return original(sock, address)
            raise AssertionError(
                "MySQL integration tests may only connect to their dedicated DB host"
            )

        return connect

    monkeypatch.setattr(socket.socket, "connect", guard(ORIGINAL_CONNECT))
    monkeypatch.setattr(socket.socket, "connect_ex", guard(ORIGINAL_CONNECT_EX))


@pytest.fixture
async def mysql_service():
    dedicated_url()
    config = Config(str(Path(__file__).parents[1] / "server-alembic.ini"))
    config.attributes["database_url"] = TEST_URL
    await asyncio.to_thread(command.upgrade, config, "head")
    db = Database(TEST_URL)
    cipher = SecretCipher(Fernet.generate_key().decode())
    await db.initialize()

    async def clean():
        async with db.engine.begin() as connection:
            for table in reversed(Base.metadata.sorted_tables):
                await connection.execute(table.delete())

    await clean()
    yield ConfigService(db.session_factory, cipher), db
    await clean()
    await db.close()


async def graph(service):
    image = await service.create_service(
        {
            "name": "images",
            "base_url": "https://image.example/v1",
            "protocol": "openai_images",
            "api_key": "private-image",
        }
    )
    vision = await service.create_service(
        {
            "name": "vision",
            "base_url": "https://vision.example/v1",
            "protocol": "chat_completions",
            "api_key": "private-vision",
        }
    )
    im = await service.create_model_config({"service_id": image["id"], "model": "image-model"})
    vm = await service.create_model_config({"service_id": vision["id"], "model": "vision-model"})
    agent = await service.create_agent_config(
        {"name": "mysql-agent", "image_models": {"fast": im["id"]}, "stages": {"vision": vm["id"]}}
    )
    return image, im, agent


async def test_mysql_migration_constraints_and_persistence(mysql_service):
    service, db = mysql_service
    image, im, agent = await graph(service)
    async with db.session_factory() as session:
        assert (
            await session.execute(text("SELECT COUNT(*) FROM alembic_version"))
        ).scalar_one() == 1
        tables = (
            await session.execute(
                text(
                    "SELECT TABLE_NAME, ENGINE, TABLE_COLLATION FROM information_schema.TABLES WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME != 'alembic_version'"
                )
            )
        ).all()
        assert {row[0] for row in tables} == {
            "provider_services",
            "model_configs",
            "agent_configs",
            "agent_model_routes",
            "http_media",
            "http_tasks",
        }
        assert all(row[1] == "InnoDB" and row[2].startswith("utf8mb4") for row in tables)
    async with db.session_factory() as session:
        session.add(ModelConfig(service_id="00000000-0000-0000-0000-000000000000", model="missing"))
        with pytest.raises(IntegrityError):
            await session.commit()
    async with db.session_factory() as session:
        session.add(
            AgentModelRoute(
                agent_config_id=agent["id"],
                model_config_id=im["id"],
                route_kind="image_models",
                route_name="fast",
            )
        )
        with pytest.raises(IntegrityError):
            await session.commit()
    with pytest.raises(ServerError) as error:
        await service.delete_service(image["id"])
    assert error.value.status_code == 409
    # A new engine/session can decrypt the persisted configuration after restart.
    reopened = Database(TEST_URL)
    try:
        new_service = ConfigService(reopened.session_factory, service.cipher)
        cfg, versions = await new_service.resolve_agent_config(agent["id"])
        assert cfg.model_name("fast") == "image-model"
        assert cfg.services[image["id"]].credential() == "private-image"
        assert versions["services"][image["id"]] == 1
    finally:
        await reopened.close()


async def test_mysql_concurrent_version_patch_and_atomic_snapshot(mysql_service):
    service, db = mysql_service
    image, _, agent = await graph(service)
    pending = None
    async with db.session_factory() as session, session.begin():
        config, versions = await service.resolve_agent_config_in_session(session, agent["id"])
        # The writer must wait for the transaction storing the accepted snapshot.
        pending = asyncio.create_task(
            service.update_service(image["id"], {"version": 1, "api_key": "new-secret"})
        )
        await asyncio.sleep(0.05)
        assert not pending.done()
        snapshot = {
            "credential": config.services[image["id"]].credential(),
            "model": config.model_name("fast"),
        }
        session.add(
            HttpTask(
                kind="analyze_materials",
                agent_config_id=agent["id"],
                normalized_request={},
                idempotency_key="snapshot-key",
                request_hash="0" * 64,
                config_versions=versions,
                config_snapshot_encrypted=service.cipher.encrypt_mapping(snapshot),
                run_ref="tasks/snapshot",
            )
        )
    await pending
    async with db.session_factory() as session:
        task = (
            await session.execute(
                select(HttpTask).where(HttpTask.idempotency_key == "snapshot-key")
            )
        ).scalar_one()
        assert (
            service.cipher.decrypt_mapping(task.config_snapshot_encrypted)["credential"]
            == "private-image"
        )
        assert task.config_versions["services"][image["id"]] == 1
    outcomes = await asyncio.gather(
        service.update_service(image["id"], {"version": 2, "name": "writer-a"}),
        service.update_service(image["id"], {"version": 2, "name": "writer-b"}),
        return_exceptions=True,
    )
    assert sum(isinstance(item, dict) for item in outcomes) == 1
    failures = [item for item in outcomes if isinstance(item, ServerError)]
    assert len(failures) == 1 and failures[0].status_code == 409
    assert (await service.get_service(image["id"]))["version"] == 3


async def test_mysql_idempotency_key_exact_case_unicode_and_trailing_spaces(mysql_service):
    service, db = mysql_service
    _, _, agent = await graph(service)
    keys = ["A", "a", "a ", "图片"]
    async with db.session_factory() as session, session.begin():
        for key in keys:
            session.add(
                HttpTask(
                    kind="analyze_materials",
                    agent_config_id=agent["id"],
                    normalized_request={},
                    idempotency_key=key,
                    request_hash="0" * 64,
                    config_versions={},
                    config_snapshot_encrypted="encrypted",
                    run_ref="tasks/test",
                )
            )
    async with db.session_factory() as session:
        for key in keys:
            task = (
                await session.execute(select(HttpTask).where(HttpTask.idempotency_key == key))
            ).scalar_one()
            assert task.idempotency_key == key
