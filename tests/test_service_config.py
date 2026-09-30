import json

import pytest

from image_agent import AgentConfig
from image_agent.errors import ConfigurationError


def deployment():
    return {
        "services": {
            "images": {
                "base_url": "https://image.example/v1",
                "protocol": "openai_images",
                "api_key_env": "TEST_IMAGE_KEY",
            },
            "vision": {
                "base_url": "https://vision.example/v1",
                "protocol": "chat_completions",
                "api_key_env": "TEST_VISION_KEY",
            },
        },
        "image_models": {
            alias: {"service": "images", "model": "gpt-image-2"}
            for alias in ("pro", "fast", "base")
        },
        "stages": {"vision": {"service": "vision", "model": "gpt-4o-mini"}},
    }


def test_mapping_file_precedence_and_routes(tmp_path, monkeypatch):
    monkeypatch.setenv("IMAGE_AGENT_OPENROUTER_API_KEY", "legacy-never-used")
    monkeypatch.setenv("IMAGE_AGENT_MODEL_FAST", "legacy-model")
    monkeypatch.setenv("TEST_IMAGE_KEY", "image-secret")
    monkeypatch.setenv("TEST_VISION_KEY", "vision-secret")
    data = deployment()
    data["stages"]["audit_image"] = {"service": "vision", "model": "audit-model"}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    cfg = AgentConfig.from_file(path)
    assert cfg == AgentConfig.from_mapping(data)
    assert cfg.model_name("fast") == "gpt-image-2"
    assert cfg.vision_route("audit_image").model == "audit-model"
    assert cfg.vision_route("analyze_materials").model == "gpt-4o-mini"
    assert cfg.services["images"].credential() == "image-secret"
    assert cfg.services["vision"].credential() == "vision-secret"
    assert "image-secret" not in repr(cfg)


@pytest.mark.parametrize(
    "change",
    ["unknown_stage", "unknown_service", "wrong_protocol", "missing_alias", "mixed_legacy"],
)
def test_invalid_routes_are_configuration_errors(change):
    data = deployment()
    if change == "unknown_stage":
        data["stages"]["typo"] = data["stages"]["vision"]
    elif change == "unknown_service":
        data["stages"]["vision"]["service"] = "missing"
    elif change == "wrong_protocol":
        data["stages"]["vision"]["service"] = "images"
    elif change == "missing_alias":
        data["image_models"] = {}
    else:
        data["openrouter_api_key"] = "do-not-inherit"
    with pytest.raises(ConfigurationError):
        AgentConfig.from_mapping(data)


def test_missing_selected_key_does_not_use_legacy_key(monkeypatch):
    monkeypatch.setenv("IMAGE_AGENT_OPENROUTER_API_KEY", "legacy-key")
    monkeypatch.delenv("TEST_IMAGE_KEY", raising=False)
    with pytest.raises(ConfigurationError, match="TEST_IMAGE_KEY"):
        AgentConfig.from_mapping(deployment()).validate_credentials()


@pytest.mark.parametrize("content", ["{", "[]", '{"secret_typo":"never-echo-this"}'])
def test_invalid_file_has_safe_configuration_error(tmp_path, content):
    path = tmp_path / "bad.json"
    path.write_text(content, encoding="utf-8")
    with pytest.raises(ConfigurationError) as error:
        AgentConfig.from_file(path)
    assert "never-echo-this" not in str(error.value)


def test_single_alias_and_unselected_key_are_supported(monkeypatch):
    data = deployment()
    data["image_models"] = {"fast": data["image_models"]["fast"]}
    monkeypatch.setenv("TEST_IMAGE_KEY", "image-key")
    monkeypatch.setenv("TEST_VISION_KEY", "vision-key")
    data["services"]["unused"] = {
        "base_url": "https://unused.example",
        "protocol": "openai_images",
        "api_key_env": "UNSET_KEY",
    }
    data["image_models"]["base"] = {"service": "unused", "model": "other"}
    cfg = AgentConfig.from_mapping(data)
    cfg.validate_credentials(cfg.service_names("fast"))
    with pytest.raises(ConfigurationError, match="not configured"):
        cfg.model_name("pro")


@pytest.mark.parametrize(
    "fields",
    [
        {"base_url": "https://user:secret@service.example/v1"},
        {"base_url": "file:///tmp/service"},
        {"edit_path": "https://other.example/images"},
        {"chat_path": "//other.example/images"},
        {"api_key": "secret-must-not-appear"},
    ],
)
def test_invalid_service_definitions_fail_safely(fields):
    data = deployment()
    data["services"]["images"].update(fields)
    with pytest.raises(ConfigurationError) as error:
        AgentConfig.from_mapping(data)
    assert "secret-must-not-appear" not in str(error.value)


@pytest.mark.parametrize("name", ["openai", "openrouter"])
def test_shipped_configuration_loads_without_credentials(name):
    from pathlib import Path

    config = AgentConfig.from_file(Path(__file__).parents[1] / "examples" / f"config.{name}.json")
    assert config.model_name("fast") == (
        "gpt-image-2" if name == "openai" else "openai/gpt-image-2"
    )
