"""Deployment cache policy must not become part of the HTTP configuration contract."""

import pytest

from image_agent.errors import ProviderError
from image_agent.server.schemas import AgentParameters
from image_agent.server.settings import CACHE_FIELDS, ServerSettings
from tests import test_server_tasks as task_fixtures
from tests.test_server_docs import make_app
from tests.test_server_tasks import submit

service = task_fixtures.service

HTTP_PARAMETERS = {
    "http_proxy",
    "request_timeout_seconds",
    "model_concurrency",
    "generation_reference_limit",
    "vision_image_limit",
    "model_completion_attempts",
}


def test_http_parameter_and_openapi_contract_remain_unchanged(tmp_path):
    from pydantic import ValidationError

    assert set(AgentParameters.model_fields) == HTTP_PARAMETERS
    schema = make_app(tmp_path).openapi()
    assert set(schema["components"]["schemas"]["AgentParameters"]["properties"]) == HTTP_PARAMETERS
    for definition in schema["components"]["schemas"].values():
        assert not CACHE_FIELDS.intersection(definition.get("properties", {}))
    for name in CACHE_FIELDS:
        with pytest.raises(ValidationError):
            AgentParameters(**{name: None})


@pytest.mark.parametrize("operation", ["analyze_materials", "create_images", "resume"])
@pytest.mark.parametrize("historical_cache", [False, True])
@pytest.mark.parametrize("deployment_defaults", [False, True])
async def test_execution_uses_deployment_cache_policy_over_snapshot(
    service,
    monkeypatch,
    tmp_path,
    operation,
    historical_cache,
    deployment_defaults,
):
    from image_agent import pipeline

    kind = "analyze_materials" if operation == "analyze_materials" else "create_images"
    reference, _ = await submit(service, kind=kind)
    task = await service.repository.claim()
    assert task.id == reference["task_id"]
    snapshot = service.cipher.decrypt_mapping(task.config_snapshot_encrypted)
    assert not CACHE_FIELDS.intersection(snapshot)
    if historical_cache:
        snapshot.update(
            cache_enabled=True,
            cache_directory="old-deployment/cache",
            cache_ttl_seconds=999,
            cache_max_bytes=999,
            jpeg_cache_max_bytes=999,
        )
        task.config_snapshot_encrypted = service.cipher.encrypt_mapping(snapshot)
    if operation == "resume":
        task.operation = "resume"
    expected = dict(
        cache_enabled=False,
        cache_directory=str(tmp_path / "new-cache"),
        cache_ttl_seconds=30,
        cache_max_bytes=4096,
        jpeg_cache_max_bytes=0,
    )
    if deployment_defaults:
        expected = {name: getattr(ServerSettings, name) for name in CACHE_FIELDS}
    for name, value in expected.items():
        setattr(service.settings, name, value)
    if not deployment_defaults:
        service.settings.cache_directory = tmp_path / "new-cache"
    # Model/config edits after submission must not change the immutable snapshot.
    from tests.test_server_tasks import config

    service.config_service.current = config("new-model-must-not-be-used")
    received = []

    async def capture(request_or_run, runtime, **kwargs):
        received.append(runtime)
        raise ProviderError("stop before model I/O", code="offline_probe")

    monkeypatch.setattr(pipeline, "resume_images" if operation == "resume" else operation, capture)
    await service.execute(task)
    assert len(received) == 1
    runtime = received[0]
    assert {name: getattr(runtime, name) for name in CACHE_FIELDS} == expected
    assert runtime.model_name("pro") == "image-old"
    assert runtime.credential_services()["i"].credential() == "private-credential"
    assert (await service.get_task(task.id))["error"]["code"] == "offline_probe"
