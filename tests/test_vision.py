import json

import pytest

from image_agent.config import AgentConfig
from image_agent.errors import ProviderError
from image_agent.models import CreationRequest, LoadedMaterial
from tests.fakes import analysis_data
from tests.test_images import picture


class ScriptTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def post_json(self, path, payload):
        self.calls.append((path, payload))
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return {"choices": [{"message": {"content": json.dumps(value)}}]}


def materials():
    return (
        LoadedMaterial(material_id="m1", data=picture(), sha256="hash", order=0, role_hint="auto"),
    )


async def test_discovery_complete_response_and_source_quote(request_data):
    from image_agent.vision import VisionClient

    transport = ScriptTransport([analysis_data()])
    vision = VisionClient(transport, AgentConfig())
    result = await vision.analyze_materials(CreationRequest(**request_data), materials())
    assert result.subjects[0].representative_material_id == "m1"
    assert len(transport.calls) == 1
    assert transport.calls[0][0] == "/chat/completions"
    data = analysis_data()
    data["intent"]["constraints"] = [
        dict(
            constraint_id="c1",
            kind="placement",
            subject_ids=["s1"],
            element_ids=[],
            instruction="place left",
            source="brief",
            source_quote="not in brief",
            source_material_id=None,
            priority="required",
        )
    ]
    normalized = await VisionClient(ScriptTransport([data]), AgentConfig()).analyze_materials(
        CreationRequest(**request_data), materials()
    )
    assert not normalized.intent.constraints and normalized.warnings


@pytest.mark.parametrize("value", ["a " * 19 + ".", "a " * 20 + "with.", "a " * 21])
async def test_style_incomplete_single_fallback(request_data, value):
    from image_agent.vision import VisionClient

    valid = "A softly lit room shows natural colors and balanced composition with clear space around the central subject and gentle shadows."
    transport = ScriptTransport([{"style_prompt": value}, {"style_prompt": valid}])
    result = await VisionClient(
        transport, AgentConfig(style_model="style", quality_model="quality")
    ).describe_style(CreationRequest(**request_data), (picture(),) * 4)
    assert result == valid
    assert [p["model"] for _, p in transport.calls] == ["style", "quality"]
    assert (
        sum(b["type"] == "image_url" for b in transport.calls[0][1]["messages"][0]["content"]) == 4
    )


async def test_style_transport_error_no_fallback_and_capacity(request_data):
    from image_agent.vision import VisionClient

    transport = ScriptTransport([ProviderError("timeout", kind="transport")])
    with pytest.raises(ProviderError):
        await VisionClient(transport, AgentConfig(style_model="style")).describe_style(
            CreationRequest(**request_data), (picture(),)
        )
    assert len(transport.calls) == 1
    transport = ScriptTransport([])
    with pytest.raises(ProviderError) as error:
        await VisionClient(transport, AgentConfig(vision_image_limit=1)).describe_style(
            CreationRequest(**request_data), (picture(), picture())
        )
    assert error.value.code == "vision_capacity_exceeded" and not transport.calls


async def test_discovery_low_confidence_preserves_warning_without_blocking(request_data):
    from image_agent.vision import VisionClient

    data = analysis_data()
    data["facts"][0]["confidence"] = 0.6
    result = await VisionClient(ScriptTransport([data]), AgentConfig()).analyze_materials(
        CreationRequest(**request_data), materials()
    )
    assert result.status == "ready" and not result.issues and result.warnings
