import json

import pytest

from image_agent.config import AgentConfig
from image_agent.execution import reserve_post
from image_agent.models import CreationRequest, LoadedMaterial
from image_agent.pipeline import Dependencies, prepare_analysis, run_pipeline
from image_agent.run_store import RunStore
from image_agent.vision import VisionClient
from tests.fakes import FakeGenerator, analysis_data
from tests.test_images import picture
from tests.test_product_long_image_planning import authored_plan, planning_context
from tests.test_vision import ScriptTransport


class CountedTransport(ScriptTransport):
    async def post_json(self, path, payload):
        reserve_post()
        return await super().post_json(path, payload)


def dependencies(transport, config):
    async def load(source):
        return picture()

    return Dependencies(VisionClient(transport, config), FakeGenerator(), load)


async def test_main_detail_share_completed_analysis_but_always_generate_new_images(
    request_data, tmp_path
):
    config = AgentConfig(cache_directory=str(tmp_path / "cache"))
    incomplete = analysis_data() | {"creative_plan": None}
    transport = CountedTransport([incomplete, analysis_data()])
    deps = dependencies(transport, config)
    first_store, second_store = RunStore(), RunStore()
    first_request = CreationRequest(**request_data)
    first = await run_pipeline(first_request, config, dependencies=deps, store=first_store)
    second_request = CreationRequest(
        **(
            request_data
            | {
                "request_id": "another-request",
                "output_types": ["detail_page"],
                "platforms": ["jd"],
                "image_model": "base",
            }
        )
    )
    second = await run_pipeline(second_request, config, dependencies=deps, store=second_store)
    assert first.status == second.status == "succeeded"
    assert len(transport.calls) == 2  # First analysis plus its structured correction.
    assert first.usage["vision_calls"] == 2
    assert second.usage["vision_calls"] == 0
    assert len(deps.generator.calls) == 4  # 1 main + 3 new detail images.
    assert first.analysis.analysis_id == second.analysis.analysis_id
    second.analysis.subjects[0].matches_product = False
    assert first.analysis.subjects[0].matches_product is True
    # Snapshots retain the entire internal result after the disposable cache is gone.
    for path in (tmp_path / "cache").glob("*.json"):
        path.unlink()
    state = second_store.json("state.json")
    assert state["context"]["analysis"]["creative_plan"] is not None
    assert state["context"]["analysis"]["subjects"][0]["matches_product"] is True
    events = [
        json.loads(raw)
        for name, raw in second_store.memory.items()
        if name.startswith("diagnostics/cache/")
    ]
    assert [event["outcome"] for event in events] == ["hit"]


@pytest.mark.parametrize("change", ["brief", "style", "hint", "bytes", "id", "role"])
async def test_analysis_semantic_input_changes_do_not_reuse(request_data, tmp_path, change):
    config = AgentConfig(cache_directory=str(tmp_path / "cache"))
    transport = ScriptTransport([analysis_data(), analysis_data()])
    deps = dependencies(transport, config)
    request = CreationRequest(**request_data)
    material = LoadedMaterial(
        material_id="m1", data=picture(), sha256="old", order=0, role_hint="auto"
    )
    await prepare_analysis(request, config, deps, loaded_materials=(material,))
    changed = request.model_copy(deep=True)
    altered = material.model_copy(deep=True)
    if change == "brief":
        changed.creative_brief = "new background"
    elif change == "style":
        changed.style_hint = "new palette"
    elif change == "hint":
        changed.materials[0].subject_hint = "use this subject"
    elif change == "bytes":
        altered.data = picture(color="blue")
    elif change == "role":
        changed.materials[0].role_hint = "identity"
    else:
        changed.materials[0].material_id = altered.material_id = "m2"
        data = analysis_data()
        data["materials"][0]["material_id"] = "m2"
        data["subjects"][0]["material_ids"] = ["m2"]
        data["subjects"][0]["representative_material_id"] = "m2"
        data["facts"][0]["evidence"][0]["material_id"] = "m2"
        transport.responses[0] = data
    result = await prepare_analysis(changed, config, deps, loaded_materials=(altered,))
    assert result.analysis.status == "ready"
    assert len(transport.calls) == 2


async def test_failed_analysis_is_never_cached(request_data, tmp_path):
    from image_agent.errors import ProviderError

    config = AgentConfig(cache_directory=str(tmp_path / "cache"))
    transport = ScriptTransport([ProviderError("down", kind="transport"), analysis_data()])
    deps = dependencies(transport, config)
    request = CreationRequest(**request_data)
    assert (await prepare_analysis(request, config, deps)).analysis.status == "failed"
    assert (await prepare_analysis(request, config, deps)).analysis.status == "ready"
    assert len(transport.calls) == 2


async def test_plan_reuses_validated_result_preserving_context_ids_and_rebinds_snapshot(
    request_data, tmp_path
):
    from image_agent.product_long_image import ProductLongImageWorkflow

    request, target, context = await planning_context(request_data)
    plan = authored_plan(request, target, context)
    config = AgentConfig(cache_directory=str(tmp_path / "cache"))
    transport = ScriptTransport([plan.model_dump(mode="json")] * 2)
    vision = VisionClient(transport, config)
    original = await vision.plan_product_long_image(request, target, context)
    original.sections[0].visual_prompt = "consumer-only modification"

    class Nodes:
        dependencies = type("Deps", (), {"vision": vision})()
        store = RunStore()

        def asset(self, state):
            return type("Asset", (), {"asset_id": "current-long"})()

        def target(self, state):
            return target

        def context(self, state):
            return context

    nodes = Nodes()
    nodes.request = request
    state = {}
    await ProductLongImageWorkflow(nodes).plan(state)
    assert len(transport.calls) == 1
    saved = nodes.store.json("plans/current-long.json")
    assert saved["sections"][0]["visual_prompt"] == plan.sections[0].visual_prompt
    assert saved["input_fingerprint"] == plan.input_fingerprint
    assert state["product_long_images"]["current-long"]["plan"] == saved
    changed_context = context.model_copy(deep=True)
    changed_context.analysis.analysis_id = "another-analysis"
    await vision.plan_product_long_image(request, target, changed_context)
    assert len(transport.calls) == 2


async def test_garment_key_hashes_actual_bytes_and_subject_not_excluded_public_bytes(
    request_data, tmp_path, monkeypatch
):
    from image_agent.models import GarmentStructureAudit

    config = AgentConfig(cache_directory=str(tmp_path / "cache"))
    vision = VisionClient(ScriptTransport([]), config)
    calls = []

    async def call(schema, instruction, images, **kwargs):
        calls.append(images)
        return GarmentStructureAudit(items=[])

    monkeypatch.setattr(vision, "_call", call)
    request = CreationRequest(**request_data)
    material = LoadedMaterial(
        material_id="m1", data=picture(), sha256="same-claimed-hash", order=0, role_hint="auto"
    )
    await vision.extract_garments(request, (material,), "s1")
    await vision.extract_garments(request, (material,), "s1")
    await vision.extract_garments(request, (material,), "s2")
    altered = material.model_copy(update={"data": picture(color="blue")})
    await vision.extract_garments(request, (altered,), "s1")
    assert len(calls) == 3
