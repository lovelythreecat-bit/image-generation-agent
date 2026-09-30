"""Offline public-API check suitable for a runtime-only wheel environment."""

import asyncio
import base64
import io
import json

import httpx
from PIL import Image

import image_agent
from image_agent import AgentConfig, CreationRequest, ImageSource, MaterialInput, create_images


async def smoke():
    stream = io.BytesIO()
    Image.new("RGB", (1600, 1600), "white").save(stream, "PNG")
    image = stream.getvalue()
    discovery = {
        "analysis_id": "a1",
        "fingerprint": "placeholder",
        "status": "ready",
        "materials": [
            {
                "material_id": "m1",
                "sha256": "placeholder",
                "observed_role": "identity",
                "summary": "white cup",
            }
        ],
        "subjects": [
            {
                "subject_id": "s1",
                "material_ids": ["m1"],
                "identity_fact_ids": ["f1"],
                "representative_material_id": "m1",
                "matches_product": True,
            }
        ],
        "facts": [
            {
                "fact_id": "f1",
                "subject_id": "s1",
                "description": "white cup",
                "verifiability": "visible_appearance",
                "evidence": [
                    {"material_id": "m1", "observation": "white cup", "source_type": "visual"}
                ],
                "confidence": 1,
            }
        ],
        "elements": [
            {
                "element_id": "e1",
                "kind": "detail",
                "subject_id": "s1",
                "description": "ceramic",
                "fact_ids": ["f1"],
            }
        ],
        "intent": {
            "proposal": {
                "primary_subject_id": "s1",
                "subject_ids": ["s1"],
                "required_element_ids": [],
                "preferred_element_ids": ["e1"],
                "excluded_element_ids": [],
            },
            "constraints": [],
            "focus_element_ids": ["e1"],
            "unmet_requirements": [],
        },
    }
    audit = {
        "subject_checks": [
            {"subject_id": "s1", "score": 95, "same_product": True, "reason": "same"}
        ],
        "fact_checks": [
            {"fact_id": "f1", "presence": "present", "fidelity_score": 95, "reason": "same"}
        ],
        "element_checks": [
            {"element_id": "e1", "presence": "present", "fidelity_score": 95, "reason": "same"}
        ],
        "constraint_checks": [],
        "visual_quality": 90,
        "platform_compliance": 90,
        "garment_fusion": 90,
        "model_preference": 90,
        "output_intent": 90,
        "passed": True,
        "reason": "ok",
    }
    calls = []

    def handler(request):
        payload = json.loads(request.content)
        calls.append(request.url.path)
        if request.url.path.endswith("/images"):
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(image).decode()}]}
            )
        text = payload["messages"][0]["content"][0]["text"]
        result = discovery if text.startswith("Discover") else audit
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})

    original = httpx.AsyncClient
    clients = []

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        client = original(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    httpx.AsyncClient = factory
    try:
        request = CreationRequest(
            product_name="cup",
            category="kitchen",
            materials=[MaterialInput(material_id="m1", source=ImageSource(data=image))],
            platforms=["taobao"],
            output_types=["main_image"],
        )
        result = await create_images(request, AgentConfig(openrouter_api_key="offline-only"))
        assert result.status == "succeeded", result.model_dump_json()
        assert result.assets[0].image == image and len(calls) == 3
        assert all(c.is_closed for c in clients)
        print("Offline public API smoke passed; package:", image_agent.__file__)
    finally:
        httpx.AsyncClient = original


if __name__ == "__main__":
    asyncio.run(smoke())
