import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from image_agent.cache import SemanticCache
from image_agent.config import AgentConfig
from image_agent.run_store import config_fingerprint
from image_agent.transport import HttpTransport
from image_agent.vision import StyleResponse, VisionClient


def structured_config(model="qwen3.6-plus", **overrides):
    return AgentConfig(
        services={
            "analysis": {
                "base_url": "https://analysis.example/v1",
                "protocol": "chat_completions",
                "api_key": "test-key",
                "chat_path": "/custom/chat/completions",
            },
            "images": {
                "base_url": "https://images.example/v1",
                "protocol": "openai_images",
                "api_key": "test-key",
            },
        },
        image_models={"fast": {"service": "images", "model": "gpt-image-2.5"}},
        stages={"vision": {"service": "analysis", "model": model}},
        **overrides,
    )


def test_thinking_config_file_defaults_and_opt_in(tmp_path, monkeypatch):
    path = tmp_path / "api.json"
    path.write_text("{}", encoding="utf-8")
    assert AgentConfig.from_file(path).enable_thinking is False
    path.write_text('{"enable_thinking": true}', encoding="utf-8")
    assert AgentConfig.from_file(path).enable_thinking is True
    monkeypatch.delenv("IMAGE_AGENT_ENABLE_THINKING", raising=False)
    assert AgentConfig.from_env().enable_thinking is False
    monkeypatch.setenv("IMAGE_AGENT_ENABLE_THINKING", "true")
    assert AgentConfig.from_env().enable_thinking is True


@pytest.mark.parametrize("model", ["qwen3.6-plus", "qwen/qwen3-vl-plus", "vendor/Qwen3.6-plus"])
@pytest.mark.parametrize("enabled", [None, True])
@pytest.mark.parametrize("stage", ["vision", "analyze_materials"])
async def test_qwen_wire_payload_on_initial_and_correction(model, enabled, stage):
    config = structured_config(model, **({} if enabled is None else {"enable_thinking": enabled}))
    requests = []

    def respond(request):
        requests.append(request)
        # Force a schema correction (and the completion path for analyze_materials).
        content = {} if len(requests) == 1 else {"style_prompt": "valid"}
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(content)}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        async with HttpTransport(config, client=client) as transport:
            result = await VisionClient(transport, config)._call(
                StyleResponse, "describe", [], stage=stage
            )
    assert result.style_prompt == "valid"
    assert len(requests) == 2
    for request in requests:
        assert str(request.url) == "https://analysis.example/v1/custom/chat/completions"
        payload = json.loads(request.content)
        assert payload["model"] == model
        assert payload["enable_thinking"] is (enabled is True)
        assert payload["response_format"] == {"type": "json_object"}
    assert (
        "previous" in json.loads(requests[1].content)["messages"][0]["content"][0]["text"].lower()
    )


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize(
    "model", ["gpt-4o-mini", "google/gemini-3-flash-preview", "vendor/not-qwen"]
)
async def test_non_qwen_wire_payload_unchanged(enabled, model):
    config = structured_config(model, enable_thinking=enabled)
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"style_prompt":"ok"}'}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        async with HttpTransport(config, client=client) as transport:
            await VisionClient(transport, config)._call(StyleResponse, "describe", [])
    assert len(payloads) == 1
    assert set(payloads[0]) == {"model", "messages", "response_format"}


async def test_legacy_openrouter_qwen_payload_unchanged():
    config = AgentConfig(
        openrouter_api_key="test-key", quality_model="qwen/qwen3-vl-plus", enable_thinking=True
    )
    payloads = []

    def respond(request):
        payloads.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"style_prompt":"ok"}'}}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        async with HttpTransport(config, client=client) as transport:
            await VisionClient(transport, config)._call(StyleResponse, "describe", [])
    assert "enable_thinking" not in payloads[0]


async def test_thinking_modes_have_isolated_semantic_cache(tmp_path):
    transport = SimpleNamespace()
    off = SemanticCache(structured_config(cache_directory=str(tmp_path)), transport)
    on = SemanticCache(
        structured_config(cache_directory=str(tmp_path), enable_thinking=True), transport
    )
    calls = []

    async def calculate(value):
        calls.append(value)
        return value

    for cache, value in [(off, "off"), (on, "on"), (off, "off"), (on, "on")]:
        assert (
            await cache.run("describe_style", {"input": "same"}, lambda: calculate(value)) == value
        )
    assert calls == ["off", "on"]
    assert off._key("describe_style", {"input": "same"}, None) != on._key(
        "describe_style", {"input": "same"}, None
    )


def test_thinking_policy_preserves_pre_setting_run_fingerprint():
    config = structured_config()
    # Reproduce the old fingerprint's field set, before enable_thinking existed.
    payload = config.model_dump(
        mode="json",
        exclude={
            "openrouter_api_key",
            "http_proxy",
            "request_timeout_seconds",
            "model_concurrency",
            "model_completion_attempts",
            "services",
            "cache_enabled",
            "cache_directory",
            "cache_max_bytes",
            "cache_ttl_seconds",
            "jpeg_cache_max_bytes",
            "enable_thinking",
        },
    )
    payload["services"] = {
        name: service.model_dump(mode="json", exclude={"api_key", "api_key_env"})
        for name, service in config.services.items()
    }
    old_fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    assert config_fingerprint(config) == old_fingerprint
    assert (
        config_fingerprint(config.model_copy(update={"enable_thinking": True})) == old_fingerprint
    )
    changed = config.model_copy(deep=True)
    changed.stages["vision"].model = "different-model"
    assert config_fingerprint(changed) != old_fingerprint
