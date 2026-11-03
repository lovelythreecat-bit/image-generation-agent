"""Thinking is current deployment policy, independent of stored agent/task inputs."""

import json
import os

import pytest
from pydantic import ValidationError

from image_agent.errors import ProviderError
from image_agent.server.schemas import AgentParameters
from image_agent.server.settings import CACHE_FIELDS, DEPLOYMENT_FIELDS, ServerSettings
from tests import test_server_config_service as config_fixtures
from tests import test_server_file_config as file_fixtures
from tests import test_server_tasks as task_fixtures
from tests.test_server_config_service import graph
from tests.test_server_docs import make_app
from tests.test_server_tasks import config, submit

config_service = config_fixtures.config_service
server_config = file_fixtures.server_config
service = task_fixtures.service


def test_thinking_is_deployment_only(tmp_path):
    assert DEPLOYMENT_FIELDS == CACHE_FIELDS | {"enable_thinking"}
    assert "enable_thinking" not in CACHE_FIELDS
    assert "enable_thinking" not in AgentParameters.model_fields
    for value in (False, True):
        with pytest.raises(ValidationError):
            AgentParameters(enable_thinking=value)
    for definition in make_app(tmp_path).openapi()["components"]["schemas"].values():
        assert "enable_thinking" not in definition.get("properties", {})


@pytest.mark.parametrize("enabled", [None, False, True])
def test_file_thinking_defaults_and_explicit_boolean(server_config, monkeypatch, enabled):
    values = json.loads(server_config.read_text("utf-8-sig"))
    if enabled is not None:
        values["enable_thinking"] = enabled
    server_config.write_text(json.dumps(values), "utf-8")
    # File loading ignores deployment environment even when it contradicts JSON.
    monkeypatch.setenv("IMAGE_AGENT_SERVER_ENABLE_THINKING", "false" if enabled else "true")
    assert ServerSettings.from_file(server_config).enable_thinking is (enabled is True)


@pytest.mark.parametrize("invalid", ["true", "false", 0, 1, None, [], {}])
def test_file_thinking_rejects_non_booleans(server_config, invalid):
    values = json.loads(server_config.read_text("utf-8-sig"))
    values["enable_thinking"] = invalid
    server_config.write_text(json.dumps(values), "utf-8")
    with pytest.raises(ValueError):
        ServerSettings.from_file(server_config)


@pytest.fixture
def server_environment(server_config, monkeypatch):
    for name in tuple(os.environ):
        if name.startswith("IMAGE_AGENT_SERVER_"):
            monkeypatch.delenv(name)
    values = json.loads(server_config.read_text("utf-8-sig"))
    for name in ("database_url", "token", "secret_key", "data_dir"):
        monkeypatch.setenv("IMAGE_AGENT_SERVER_" + name.upper(), values[name])


@pytest.mark.parametrize(
    "value, expected",
    [(None, False), ("true", True), ("false", False), (" TRUE ", True), ("False", False)],
)
def test_environment_thinking(server_environment, monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("IMAGE_AGENT_SERVER_ENABLE_THINKING", value)
    assert ServerSettings.from_env().enable_thinking is expected


@pytest.mark.parametrize("value", ["", "1", "0", "yes", "null", "private-invalid"])
def test_environment_thinking_rejects_invalid_values(server_environment, monkeypatch, value):
    monkeypatch.setenv("IMAGE_AGENT_SERVER_ENABLE_THINKING", value)
    with pytest.raises(ValueError) as error:
        ServerSettings.from_env()
    assert "private-invalid" not in str(error.value)


@pytest.mark.parametrize("legacy", [False, True])
async def test_imported_thinking_is_not_stored_as_agent_policy(config_service, legacy):
    from image_agent.server.db_models import AgentConfigRecord
    from image_agent.server.task_service import config_mapping

    mapping = (
        {"openrouter_api_key": "legacy-private-credential"} if legacy else config_mapping(config())
    )
    mapping["enable_thinking"] = True
    imported = await config_service.import_config(mapping, "thinking import")
    assert not DEPLOYMENT_FIELDS.intersection(imported["parameters"])
    async with config_service.session_factory() as session:
        row = await session.get(AgentConfigRecord, imported["id"])
        assert not DEPLOYMENT_FIELDS.intersection(row.parameters)
    resolved, _ = await config_service.resolve_agent_config(imported["id"])
    assert resolved.enable_thinking is False


async def test_historical_agent_policy_is_ignored_and_not_public(config_service):
    from image_agent.server.db_models import AgentConfigRecord
    from image_agent.server.schemas import AgentConfigRead

    *_, agent = await graph(config_service)
    original = agent["parameters"].copy()
    async with config_service.session_factory() as session:
        row = await session.get(AgentConfigRecord, agent["id"])
        row.parameters = original | {"enable_thinking": True}
        await session.commit()
    for public in [
        await config_service.get_agent_config(agent["id"]),
        *(await config_service.list_agent_configs()),
    ]:
        assert public["parameters"] == original
        AgentConfigRead.model_validate(public)
    resolved, _ = await config_service.resolve_agent_config(agent["id"])
    assert resolved.enable_thinking is False


@pytest.mark.parametrize("operation", ["analyze_materials", "create_images", "resume"])
@pytest.mark.parametrize("historical", [None, False, True])
@pytest.mark.parametrize("enabled", [False, True])
async def test_execution_uses_current_thinking_policy(
    service,
    monkeypatch,
    operation,
    historical,
    enabled,
):
    from image_agent import pipeline

    # Imported/per-agent runtime values cannot enter the immutable task snapshot.
    service.config_service.current = config().model_copy(update={"enable_thinking": True})
    kind = "analyze_materials" if operation == "analyze_materials" else "create_images"
    await submit(service, kind=kind)
    task = await service.repository.claim()
    snapshot = service.cipher.decrypt_mapping(task.config_snapshot_encrypted)
    assert not DEPLOYMENT_FIELDS.intersection(snapshot)
    if historical is not None:
        snapshot["enable_thinking"] = historical
        task.config_snapshot_encrypted = service.cipher.encrypt_mapping(snapshot)
    if operation == "resume":
        task.operation = "resume"
    service.settings.enable_thinking = enabled
    service.config_service.current = config("model-edited-after-submission")
    received = []

    async def capture(request_or_run, runtime, **kwargs):
        received.append(runtime)
        raise ProviderError("stop before model I/O", code="offline_thinking_probe")

    monkeypatch.setattr(pipeline, "resume_images" if operation == "resume" else operation, capture)
    await service.execute(task)
    assert len(received) == 1
    assert received[0].enable_thinking is enabled
    assert received[0].model_name("pro") == "image-old"
    assert (await service.get_task(task.id))["error"]["code"] == "offline_thinking_probe"
