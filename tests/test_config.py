import pytest
from pydantic import ValidationError


def test_env_defaults_and_isolation(monkeypatch):
    from image_agent.config import AgentConfig

    monkeypatch.setenv("OPENROUTER_API_KEY", "should-not-read")
    monkeypatch.setenv("IMAGE_AGENT_QUALITY_MODEL", "test-quality")
    cfg = AgentConfig.from_env()
    assert cfg.openrouter_api_key is None
    assert cfg.style_model == "test-quality"
    assert cfg.model_name("fast") == "openai/gpt-image-2"
    assert cfg.request_timeout_seconds == 180
    assert cfg.generation_reference_limit == 4
    assert cfg.vision_image_limit == 12


@pytest.mark.parametrize(
    "field",
    [
        "request_timeout_seconds",
        "model_concurrency",
        "generation_reference_limit",
        "vision_image_limit",
    ],
)
def test_positive_capacity(field):
    from image_agent.config import AgentConfig

    with pytest.raises(ValidationError):
        AgentConfig(**{field: 0})


def test_secrets_and_error_mapping():
    from image_agent.config import AgentConfig
    from image_agent.errors import ProviderError, make_error_info

    assert "secret-token" not in repr(AgentConfig(openrouter_api_key="secret-token"))
    e = make_error_info(ProviderError("timeout", kind="transport"))
    assert (e.code, e.retryable) == ("provider_transport", True)
    e = make_error_info(ProviderError("bad", kind="http", status_code=400))
    assert not e.retryable
