"""Provider omission of optional focus must not block image generation."""

from copy import deepcopy

import pytest

from image_agent.config import AgentConfig
from image_agent.models import CreationRequest
from image_agent.pipeline import Dependencies, run_pipeline
from image_agent.vision import VisionClient
from tests.fakes import FakeGenerator, analysis_data
from tests.test_images import picture
from tests.test_vision import ScriptTransport


@pytest.mark.parametrize("output_type,count", [("main_image", 1), ("detail_page", 3)])
@pytest.mark.parametrize("focus", ["omitted", "empty", "selected"])
async def test_optional_focus_allows_generation(request_data, output_type, count, focus):
    data = analysis_data()
    expected = ["e1"] if focus == "selected" else []
    if focus == "omitted":
        del data["intent"]["focus_element_ids"]
    else:
        data["intent"]["focus_element_ids"] = expected
    config = AgentConfig()
    transport = ScriptTransport([deepcopy(data) for _ in range(config.model_completion_attempts)])
    generator = FakeGenerator()

    async def load(source):
        return picture()

    result = await run_pipeline(
        CreationRequest(**(request_data | {"output_types": [output_type]})),
        config,
        dependencies=Dependencies(
            vision=VisionClient(transport, config), generator=generator, load=load,
        ),
    )
    assert result.status == "succeeded", result.error_info
    assert result.analysis.intent.focus_element_ids == expected
    assert len(transport.calls) == 1
    assert len(generator.calls) == len(result.assets) == count
    assert all(asset.status == "succeeded" and asset.image for asset in result.assets)
