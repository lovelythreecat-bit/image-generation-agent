import base64

import pytest

from image_agent.config import AgentConfig
from image_agent.errors import ProviderError
from image_agent.models import GenerationReference


@pytest.mark.parametrize(
    "payload",
    [
        {"data": [{"b64_json": "aW1hZ2U="}]},
        {
            "choices": [
                {
                    "message": {
                        "images": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "message": {
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "data:image/png;base64,aW1hZ2U="},
                            }
                        ]
                    }
                }
            ]
        },
    ],
)
def test_three_response_shapes(payload):
    from image_agent.generate import extract_image_bytes

    assert extract_image_bytes(payload) == b"image"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"data": [{"b64_json": "%%%"}]},
        {"choices": [{"message": {"content": "hello"}}]},
        {"choices": [{"message": {"content": []}}]},
    ],
)
def test_bad_response_is_protocol(payload):
    from image_agent.generate import extract_image_bytes

    with pytest.raises(ProviderError) as error:
        extract_image_bytes(payload)
    assert error.value.kind == "protocol"


async def test_payload_order_and_capacity():
    from image_agent.generate import ImageGenerator

    calls = []

    class Transport:
        async def post_json(self, path, payload):
            calls.append((path, payload))
            return {"data": [{"b64_json": base64.b64encode(b"image").decode()}]}

    gen = ImageGenerator(Transport(), AgentConfig(), encoder=lambda data, limit: data)
    refs = tuple(
        GenerationReference(material_id=f"m{i}", data=str(i).encode(), role="identity")
        for i in range(4)
    )
    assert (
        await gen.generate(
            prompt="hello",
            model="openai/gpt-image-2",
            size="4K",
            aspect_ratio="3:4",
            references=refs,
        )
        == b"image"
    )
    path, p = calls[0]
    assert path == "/images" and p["quality"] == "high" and p["n"] == 1
    assert [x["image_url"]["url"].split(",")[1] for x in p["input_references"]] == [
        "MA==",
        "MQ==",
        "Mg==",
        "Mw==",
    ]
    assert "Reference Image 1" in p["prompt"] and "m0" in p["prompt"]
    with pytest.raises(ProviderError) as error:
        await gen.generate(
            prompt="hello", model="other", size="2K", aspect_ratio="1:1", references=refs + refs[:1]
        )
    assert error.value.code == "reference_capacity_exceeded"
    assert len(calls) == 1
