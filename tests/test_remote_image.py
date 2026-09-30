import base64

import httpx
import pytest

from image_agent import AgentConfig
from image_agent.errors import ProviderError
from image_agent.generate import ImageGenerator
from image_agent.transport import HttpTransport
from tests.test_images import picture


async def generate_with(client, *, sleep=None):
    config = AgentConfig(openrouter_api_key="api-secret")
    transport = HttpTransport(config, client=client, **({"sleep": sleep} if sleep else {}))
    return await ImageGenerator(transport, config, encoder=lambda data, limit: data).generate(
        prompt="cup", model="gpt-image-2", size="1K", aspect_ratio="1:1", references=()
    )


@pytest.mark.parametrize("inline", [None, "", " \n"])
async def test_url_image_with_empty_base64_is_downloaded_without_api_credentials(inline):
    image = picture((32, 32))
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "b64_json": inline,
                            "url": "https://cdn.example/image.png?signature=private",
                        }
                    ]
                },
                headers={"set-cookie": "api_session=secret; Domain=.example; Path=/"},
            )
        assert str(request.url) == "https://cdn.example/image.png?signature=private"
        assert "authorization" not in request.headers
        assert "cookie" not in request.headers
        return httpx.Response(200, content=image)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer inherited-secret", "Cookie": "session=secret"},
    ) as client:
        assert await generate_with(client) == image
    assert [r.method for r in calls] == ["POST", "GET"]


@pytest.mark.parametrize("inline, succeeds", [("aW1hZ2U=", True), ("%%%", False)])
async def test_nonempty_inline_image_keeps_precedence_over_remote_url(inline, succeeds):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(
            200, json={"data": [{"b64_json": inline, "url": "https://cdn.example/image.png"}]}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        if succeeds:
            assert await generate_with(client) == base64.b64decode(inline)
        else:
            with pytest.raises(ProviderError, match="invalid image base64"):
                await generate_with(client)
    assert len(calls) == 1


async def test_image_redirect_and_download_retry_never_repeat_generation_or_send_auth():
    calls, slept = [], []
    image = picture((32, 32))
    downloads = 0

    async def sleep(delay):
        slept.append(delay)

    def handler(request):
        nonlocal downloads
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                200, json={"data": [{"b64_json": "", "url": "https://cdn.example/start"}]}
            )
        assert "authorization" not in request.headers and "cookie" not in request.headers
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={
                    "location": "https://assets.example/final",
                    "set-cookie": "tracking=secret; Path=/",
                },
            )
        downloads += 1
        return httpx.Response(503 if downloads == 1 else 200, content=image)

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), auth=("user", "secret")
    ) as client:
        assert await generate_with(client, sleep=sleep) == image
    assert sum(r.method == "POST" for r in calls) == 1
    assert downloads == 2 and slept == [2.0]


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/secret",
        "https://user:password@cdn.example/image",
        "http://127.0.0.1/image",
        "http://localhost/image",
    ],
)
async def test_invalid_image_url_fails_without_a_download(url):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json={"data": [{"url": url}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as error:
            await generate_with(client)
    assert len(calls) == 1
    assert "password" not in str(error.value)


@pytest.mark.parametrize(
    "status, content", [(404, b"private error"), (200, b"<html>not an image</html>")]
)
async def test_download_failure_is_explicit_without_leaking_signed_url(status, content):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(
                200, json={"data": [{"url": "https://cdn.example/image?signature=private-value"}]}
            )
        return httpx.Response(status, content=content)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as error:
            await generate_with(client)
    assert len(calls) == 2
    assert "private-value" not in str(error.value) and "private error" not in str(error.value)
    if status == 404:
        assert error.value.status_code == 404


async def test_download_rejects_oversized_content_length():
    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example/image"}]})
        return httpx.Response(200, content=b"x", headers={"content-length": str(100 * 1024 * 1024)})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="size limit"):
            await generate_with(client)


async def test_download_enforces_stream_limit_without_content_length_and_closes_response():
    class LargeStream(httpx.AsyncByteStream):
        closed = False

        async def __aiter__(self):
            for _ in range(33):
                yield b"x" * (1024 * 1024)

        async def aclose(self):
            self.closed = True

    stream = LargeStream()

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example/image"}]})
        return httpx.Response(200, stream=stream)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="size limit"):
            await generate_with(client)
    assert stream.closed


@pytest.mark.parametrize("location, get_count", [("http://127.0.0.1/private", 1), ("/image", 6)])
async def test_download_checks_redirect_targets_and_bounds_redirect_loops(location, get_count):
    calls = []

    def handler(request):
        calls.append(request)
        if request.method == "POST":
            return httpx.Response(200, json={"data": [{"url": "https://cdn.example/image"}]})
        return httpx.Response(302, headers={"location": location})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError):
            await generate_with(client)
    assert sum(r.method == "GET" for r in calls) == get_count
    assert all(r.url.host != "127.0.0.1" for r in calls)
