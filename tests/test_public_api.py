import asyncio
import base64
import json

import httpx
import pytest

from image_agent import AgentConfig, CreationRequest, analyze_materials, create_images
from image_agent.errors import OutputError
from tests.fakes import analysis_data
from tests.test_images import picture


def mock_provider(monkeypatch, *, gate=None):
    original = httpx.AsyncClient
    clients = []
    calls = []

    async def handler(req):
        payload = json.loads(req.content)
        calls.append((req.url.path, payload))
        if req.url.path.endswith("/images"):
            if gate:
                gate[0].set()
                await gate[1].wait()
            return httpx.Response(
                200, json={"data": [{"b64_json": base64.b64encode(picture((1600, 1600))).decode()}]}
            )
        text = payload["messages"][0]["content"][0]["text"]
        if text.startswith("Discover"):
            result = analysis_data()
        elif text.startswith("Recheck the candidate"):
            result = dict(
                outcome="verified",
                subject_checks=[dict(subject_id="s1", score=95, same_product=True, reason="same")],
                fact_checks=[
                    dict(fact_id="f1", presence="present", fidelity_score=95, reason="same")
                ],
                intent_valid=True,
                issue_resolutions=[],
            )
        else:
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
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        client = original(transport=httpx.MockTransport(handler), **kwargs)
        clients.append(client)
        return client

    monkeypatch.setattr(httpx, "AsyncClient", factory)
    return calls, clients


async def test_public_api_real_vision_generator_and_snapshot(request_data, monkeypatch, tmp_path):
    calls, clients = mock_provider(monkeypatch)
    request_data["materials"][0]["source"] = {"data": picture()}
    r = CreationRequest(**request_data)
    cfg = AgentConfig(openrouter_api_key="mock-only")
    analysis = await analyze_materials(r, cfg)
    assert len(calls) == 1 and analysis.status == "ready"
    r.output_dir = tmp_path
    result = await create_images(r, cfg, analysis=analysis)
    assert result.status == "succeeded" and len(calls) == 4
    assert result.assets[0].image == picture((1600, 1600))
    assert (tmp_path / "create/result.json").exists()
    assert all(c.is_closed for c in clients)
    with pytest.raises(OutputError):
        await create_images(r, cfg, analysis=analysis)
    assert len(calls) == 4


async def test_public_cancel_generation_leaves_no_files(request_data, monkeypatch, tmp_path):
    entered, release = asyncio.Event(), asyncio.Event()
    calls, clients = mock_provider(monkeypatch, gate=(entered, release))
    request_data["materials"][0]["source"] = {"data": picture()}
    r = CreationRequest(**(request_data | {"output_dir": tmp_path}))
    task = asyncio.create_task(create_images(r, AgentConfig(openrouter_api_key="mock-only")))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert (tmp_path / "create").is_dir() and list((tmp_path / "create").iterdir()) == []
    assert all(c.is_closed for c in clients)


async def test_legacy_all_style_analysis_first_reference_generation(request_data):
    from image_agent.pipeline import run_pipeline
    from tests.test_pipeline import setup

    data = analysis_data()
    # Rename material references consistently for the legacy product input.
    data = json.loads(json.dumps(data).replace('"m1"', '"product"'))
    data["materials"] += [
        dict(material_id=f"ref-{i}", sha256="hash", observed_role="style", summary="style")
        for i in range(1, 5)
    ]
    request_data.pop("materials")
    request_data.update(
        product_image={"data": picture()}, reference_images=[{"data": picture()}] * 4
    )
    r, deps, vision, gen = setup(request_data, discovery=data)
    counts = []

    async def style(request, references):
        counts.append(len(references))
        return "soft scene"

    vision.describe_style = style
    result = await run_pipeline(r, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded" and counts == [4]
    assert [ref.material_id for ref in gen.calls[0]["references"]] == ["product", "ref-1"]
    assert result.assets[0].element_plan.audit_material_ids == ["product", "ref-1"]
