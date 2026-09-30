import base64
import json

import httpx
import pytest

from image_agent import AgentConfig
from image_agent.errors import ProviderError
from image_agent.generate import ImageGenerator
from image_agent.models import GenerationReference
from image_agent.transport import HttpTransport
from tests.test_service_config import deployment


def configured(monkeypatch):
    monkeypatch.setenv("TEST_IMAGE_KEY", "image-only")
    monkeypatch.setenv("TEST_VISION_KEY", "vision-only")
    return AgentConfig.from_mapping(deployment())


async def test_decode_diagnostics_retain_request_context_and_are_serializable(monkeypatch):
    from image_agent.errors import make_error_info

    config = configured(monkeypatch)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": ""}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        generator = ImageGenerator(
            HttpTransport(config, client=client), config, encoder=lambda data, limit: data
        )
        with pytest.raises(ProviderError) as error:
            await generator.generate(
                prompt="private prompt",
                model="gpt-image-2",
                size="1K",
                aspect_ratio="1:1",
                references=(
                    GenerationReference(material_id="m1", data=b"private image", role="identity"),
                ),
            )
    info = make_error_info(error.value).model_dump(mode="json")
    message = info["message"]
    for expected in (
        "image.example",
        "v1 > images > edits",
        "openai_images",
        "gpt-image-2",
        "refs=1",
        "multipart",
        "data[0].b64_json",
        "empty",
    ):
        assert expected in message
    for secret in ("image-only", "vision-only", "private prompt", "private image"):
        assert secret not in json.dumps(info)
    assert info["retryable"] is False
    assert len(calls) == 1


@pytest.mark.parametrize("references", [(), (b"first-image", b"second-image")])
async def test_official_image_wire_format(monkeypatch, references):
    config = configured(monkeypatch)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={"data": [{"b64_json": base64.b64encode(b"result").decode()}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        generator = ImageGenerator(
            HttpTransport(config, client=client), config, encoder=lambda data, limit: data
        )
        image = await generator.generate(
            prompt="Create cup",
            model="gpt-image-2",
            size="4K",
            aspect_ratio="3:4",
            references=tuple(
                GenerationReference(material_id=f"m{i}", data=data, role="identity")
                for i, data in enumerate(references)
            ),
        )
    assert image == b"result" and len(calls) == 1
    request = calls[0]
    assert request.headers["authorization"] == "Bearer image-only"
    assert request.url.host == "image.example"
    assert b"input_fidelity" not in request.content and b"input_references" not in request.content
    if references:
        assert request.url.path == "/v1/images/edits"
        assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
        assert request.content.count(b'name="image[]"') == 2
        assert request.content.index(b"first-image") < request.content.index(b"second-image")
        assert b"\r\n\r\nhigh\r\n" in request.content
        assert b"\r\n\r\n768x1024\r\n" in request.content
        assert b"Reference Image 1: m0" in request.content
    else:
        assert request.url.path == "/v1/images/generations"
        assert json.loads(request.content) == dict(
            model="gpt-image-2", prompt="Create cup", n=1, quality="high", size="768x1024"
        )


@pytest.mark.parametrize("aspect", ["4:1", "1:4"])
async def test_official_unsupported_aspect_fails_before_http(monkeypatch, aspect):
    config = configured(monkeypatch)
    calls = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: calls.append(r))
    ) as client:
        generator = ImageGenerator(
            HttpTransport(config, client=client), config, encoder=lambda data, limit: data
        )
        with pytest.raises(ProviderError) as error:
            await generator.generate(
                prompt="x", model="gpt-image-2", size="2K", aspect_ratio=aspect, references=()
            )
    assert error.value.kind == "capability" and not calls


async def test_bound_transport_multipart_retry_and_redaction(monkeypatch):
    config = configured(monkeypatch)
    calls, delays = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            503 if len(calls) == 1 else 403, json={"error": {"message": "denied image-only"}}
        )

    async def sleep(delay):
        delays.append(delay)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        transport = HttpTransport(config, client=client, sleep=sleep).for_service("images")
        with pytest.raises(ProviderError) as error:
            await transport.post_multipart(
                "/images/edits",
                {"model": "gpt-image-2"},
                [("image[]", ("one.jpg", b"image", "image/jpeg"))],
            )
    assert "denied" in error.value.message and "image-only" not in error.value.message
    assert delays == [2.0] and len(calls) == 2
    assert all(b"image" in request.content for request in calls)


@pytest.mark.parametrize("aspect", ["1:1", "3:4", "4:5", "9:16", "16:9"])
def test_supported_canvas_obeys_official_limits(aspect):
    from image_agent.image_protocols import openai_canvas

    width, height = map(int, openai_canvas(aspect).split("x"))
    a, b = map(int, aspect.split(":"))
    assert width % 16 == height % 16 == 0
    assert width * b == height * a
    assert max(width, height) <= 3840
    assert 655360 <= width * height <= 8294400


async def test_compatible_router_custom_endpoint_uses_configured_model(monkeypatch):
    data = deployment()
    data["services"]["images"].update(
        protocol="openrouter_images", generation_path="/custom/generate"
    )
    data["image_models"]["fast"]["model"] = "compatible-image-model"
    monkeypatch.setenv("TEST_IMAGE_KEY", "image-only")
    config = AgentConfig.from_mapping(data)
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"b64_json": "aW1hZ2U="}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        generator = ImageGenerator(
            HttpTransport(config, client=client),
            config,
            alias="fast",
            encoder=lambda data, limit: data,
        )
        assert (
            await generator.generate(
                prompt="cup",
                model=config.model_name("fast"),
                size="2K",
                aspect_ratio="1:1",
                references=(),
            )
            == b"image"
        )
    assert len(calls) == 1
    assert str(calls[0].url) == "https://image.example/v1/custom/generate"
    assert calls[0].headers["authorization"] == "Bearer image-only"
    assert json.loads(calls[0].content) == dict(
        model="compatible-image-model", prompt="cup", n=1, aspect_ratio="1:1", resolution="2K"
    )
