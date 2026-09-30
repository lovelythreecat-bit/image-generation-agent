import base64
import json

import httpx
import pytest

from image_agent import AgentConfig, CreationRequest, create_images
from image_agent.errors import ConfigurationError, ProviderError
from image_agent.transport import HttpTransport
from image_agent.vision import VisionClient
from tests.fakes import analysis_data
from tests.test_images import picture
from tests.test_service_config import deployment


@pytest.mark.parametrize("image_response", ["base64", "url"])
@pytest.mark.parametrize("audit_reply", ["valid", "repair", "fail"])
async def test_full_pipeline_routes_same_model_by_alias(
    monkeypatch, request_data, tmp_path, image_response, audit_reply
):
    data = deployment()
    data["services"]["alternate"] = {
        "base_url": "https://alternate.example/v1",
        "protocol": "openai_images",
        "api_key_env": "NOT_SET",
    }
    data["image_models"]["base"] = {"service": "alternate", "model": "gpt-image-2"}
    data["stages"]["audit_image"] = {"service": "vision", "model": "audit-custom"}
    monkeypatch.setenv("TEST_IMAGE_KEY", "image-only")
    monkeypatch.setenv("TEST_VISION_KEY", "vision-only")
    monkeypatch.delenv("NOT_SET", raising=False)
    config = AgentConfig.from_mapping(data)
    calls = []
    original = httpx.AsyncClient
    image_data = picture((1024, 1024))
    audit_calls = 0

    def handler(request):
        nonlocal audit_calls
        calls.append(request)
        if request.url.host == "cdn.example":
            assert request.method == "GET"
            assert "authorization" not in request.headers
            return httpx.Response(200, content=image_data)
        if request.url.host == "image.example":
            assert request.url.path == "/v1/images/edits"
            assert request.headers["authorization"] == "Bearer image-only"
            item = (
                {"b64_json": "", "url": "https://cdn.example/result.png"}
                if image_response == "url"
                else {"b64_json": base64.b64encode(image_data).decode()}
            )
            return httpx.Response(200, json={"data": [item]})
        assert request.url.host == "vision.example"
        assert request.headers["authorization"] == "Bearer vision-only"
        payload = json.loads(request.content)
        if len(calls) == 1:
            assert payload["model"] == "gpt-4o-mini"
            result = analysis_data()
        else:
            audit_calls += 1
            assert payload["model"] == "audit-custom"
            result = dict(
                subject_checks=[dict(subject_id="s1", score=95, same_product=True, reason="same")],
                fact_checks=[
                    dict(fact_id="f1", presence="present", fidelity_score=95, reason="same")
                ],
                element_checks=[
                    dict(element_id="e1", presence="present", fidelity_score=95, reason="same")
                ],
                constraint_checks=[],
                visual_quality=90,
                platform_compliance=90,
                garment_fusion=90,
                model_preference=90,
                output_intent=90,
                passed=True,
                reason="ok",
            )
            if audit_reply == "fail" or (audit_reply == "repair" and audit_calls == 1):
                del result["fact_checks"]
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    request_data["materials"][0]["source"] = {"data": picture()}
    request = CreationRequest(
        **(request_data | {"image_model": "fast", "output_dir": tmp_path / "out"})
    )
    result = await create_images(request, config)
    assert result.status == ("audit_error" if audit_reply == "fail" else "succeeded")
    assert len(calls) == (4 if image_response == "url" else 3) + (
        5 if audit_reply == "fail" else audit_reply != "valid"
    )
    assert sum(r.url.host == "image.example" for r in calls) == 1
    assert result.assets[0].model == "gpt-image-2"
    assert len(list((tmp_path / "out").glob("*/result.json"))) == 1
    pattern = (
        "*/candidates/taobao.main_image.default/c0001.png"
        if audit_reply == "fail"
        else "*/approved/taobao/main_image/default.png"
    )
    saved_images = list((tmp_path / "out").glob(pattern))
    assert len(saved_images) == 1 and saved_images[0].read_bytes() == image_data
    if audit_reply == "fail":
        assert result.assets[0].quality is None
        assert "audit_image" in result.assets[0].error
        assert "fact_checks" in result.assets[0].error


async def test_missing_configured_key_and_alias_fail_before_output(
    monkeypatch, request_data, tmp_path
):
    data = deployment()
    monkeypatch.delenv("TEST_IMAGE_KEY", raising=False)
    monkeypatch.setenv("TEST_VISION_KEY", "vision-only")
    request = CreationRequest(
        **(request_data | {"image_model": "fast", "output_dir": tmp_path / "out"})
    )
    with pytest.raises(ConfigurationError, match="TEST_IMAGE_KEY"):
        await create_images(request, AgentConfig.from_mapping(data))
    data["image_models"] = {"base": data["image_models"]["base"]}
    with pytest.raises(ConfigurationError, match="not configured"):
        await create_images(request, AgentConfig.from_mapping(data))
    assert not (tmp_path / "out").exists()


async def test_configured_style_failure_does_not_fallback(monkeypatch, request_data):
    data = deployment()
    data["stages"]["describe_style"] = {"service": "vision", "model": "style-custom"}
    monkeypatch.setenv("TEST_VISION_KEY", "vision-only")
    calls = []

    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200, json={"choices": [{"message": {"content": '{"style_prompt":"short"}'}}]}
        )

    config = AgentConfig.from_mapping(data)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="incomplete"):
            await VisionClient(HttpTransport(config, client=client), config).describe_style(
                CreationRequest(**request_data), (picture(),)
            )
    assert len(calls) == 1 and calls[0]["model"] == "style-custom"


@pytest.mark.parametrize("entry", ["cli", "smoke"])
def test_entrypoint_config_missing_key_before_output(tmp_path, monkeypatch, capsys, entry):
    from image_agent.__main__ import run_cli
    from scripts.live_smoke import main

    path = tmp_path / "api.json"
    path.write_text(json.dumps(deployment()), encoding="utf-8")
    monkeypatch.delenv("TEST_IMAGE_KEY", raising=False)
    monkeypatch.setenv("IMAGE_AGENT_OPENROUTER_API_KEY", "never-used")
    out = tmp_path / "out"
    args = [
        "create" if entry == "cli" else "--live",
        "--config",
        str(path),
        "--material",
        "cup.png",
        "--name",
        "cup",
        "--category",
        "kitchen",
        "--platform",
        "taobao",
        "--output",
        "main_image",
        "--out",
        str(out),
    ]
    assert (run_cli if entry == "cli" else main)(args) == 1
    assert "TEST_IMAGE_KEY" in capsys.readouterr().err
    assert not out.exists()


async def test_analyze_needs_only_vision_key_and_honors_custom_path(monkeypatch, request_data):
    from image_agent import analyze_materials

    data = deployment()
    data["services"]["vision"]["chat_path"] = "/custom/vision"
    monkeypatch.delenv("TEST_IMAGE_KEY", raising=False)
    monkeypatch.setenv("TEST_VISION_KEY", "vision-only")
    calls = []
    original = httpx.AsyncClient

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(analysis_data())}}]}
        )

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
    )
    request_data["materials"][0]["source"] = {"data": picture()}
    result = await analyze_materials(
        CreationRequest(**request_data), AgentConfig.from_mapping(data)
    )
    assert result.status == "ready" and len(calls) == 1
    assert str(calls[0].url) == "https://vision.example/v1/custom/vision"
    assert calls[0].headers["authorization"] == "Bearer vision-only"
