import asyncio
import json
from pathlib import Path

import pytest

from image_agent.config import AgentConfig
from image_agent.errors import AgentError, OutputError, ProviderError
from image_agent.execution import ExecutionPolicy
from image_agent.graph_runtime import resume_pipeline
from image_agent.pipeline import run_pipeline
from image_agent.run_store import RunStore
from tests.test_product_long_image_pipeline import long_setup


async def test_saved_operation_before_checkpoint_is_not_reposted(
    request_data, tmp_path, monkeypatch
):
    request, deps, _, gen = long_setup(request_data | {"output_dir": tmp_path})
    original = RunStore.json
    interrupted = False

    def write(self, name, value=None):
        nonlocal interrupted
        if (
            name == "state.json"
            and value is not None
            and value["phase"] == "long_save_candidate"
            and not interrupted
        ):
            interrupted = True
            raise asyncio.CancelledError()
        return original(self, name, value)

    monkeypatch.setattr(RunStore, "json", write)
    with pytest.raises(asyncio.CancelledError):
        await run_pipeline(request, AgentConfig(), dependencies=deps)
    result = await resume_pipeline(next(tmp_path.iterdir()), AgentConfig(), dependencies=deps)
    assert result.status == "succeeded" and len(gen.calls) == 6
    assert result.usage["image_calls"] == 6


async def test_segment_saved_before_audit_resumes_audit_only(request_data, tmp_path):
    request, deps, _, gen = long_setup(request_data | {"output_dir": tmp_path})

    def stop(event):
        if event["stage"] == "long_audit":
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_pipeline(request, AgentConfig(), dependencies=deps, on_progress=stop)
    assert len(gen.calls) == 1
    result = await resume_pipeline(next(tmp_path.iterdir()), AgentConfig(), dependencies=deps)
    assert result.status == "succeeded" and len(gen.calls) == 6


async def test_submitted_response_missing_stays_uncertain(request_data, tmp_path):
    request, deps, _, gen = long_setup(
        request_data | {"output_dir": tmp_path}, error=asyncio.CancelledError()
    )
    with pytest.raises(asyncio.CancelledError):
        await run_pipeline(request, AgentConfig(), dependencies=deps)
    gen.error = None
    result = await resume_pipeline(next(tmp_path.iterdir()), AgentConfig(), dependencies=deps)
    assert result.status == "generation_uncertain" and len(gen.calls) == 1
    again = await resume_pipeline(result.run_dir, AgentConfig(), dependencies=deps)
    assert again.status == "generation_uncertain" and len(gen.calls) == 1


async def test_pending_copy_audit_does_not_repeat_physical_or_generation(request_data, tmp_path):
    request, deps, vision, gen = long_setup(request_data | {"output_dir": tmp_path})
    original = vision.audit_product_long_image_copy

    async def stop(*args):
        raise asyncio.CancelledError()

    vision.audit_product_long_image_copy = stop
    with pytest.raises(asyncio.CancelledError):
        await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert len(gen.calls) == 1
    before = vision.calls.count("audit:taobao.long_image.default")
    vision.audit_product_long_image_copy = original
    result = await resume_pipeline(next(tmp_path.iterdir()), AgentConfig(), dependencies=deps)
    assert result.status == "succeeded" and len(gen.calls) == 6
    assert vision.calls.count("audit:taobao.long_image.default") == before + 5


async def test_audit_error_final_resumes_only_errored_sections(request_data, tmp_path):
    request, deps, vision, gen = long_setup(request_data | {"output_dir": tmp_path})
    original = vision.audit_product_long_image_copy

    async def fail(request, section, plan, image):
        if section.section_id == "s2":
            raise ProviderError("copy audit unavailable")
        return await original(request, section, plan, image)

    vision.audit_product_long_image_copy = fail
    result = await run_pipeline(
        request, AgentConfig(), dependencies=deps, policy=ExecutionPolicy(max_audit_retries=0)
    )
    assert result.status == "succeeded" and result.assets[0].image and len(gen.calls) == 6
    assert result.assets[0].delivery and result.warnings
    audits_before = len(vision.calls)
    vision.audit_product_long_image_copy = original
    resumed = await resume_pipeline(result.run_dir, AgentConfig(), dependencies=deps)
    assert resumed.status == "succeeded" and len(gen.calls) == 6
    assert resumed.usage["image_calls"] == result.usage["image_calls"]
    assert len(vision.calls) == audits_before


async def test_local_composition_failure_retries_without_generation(
    request_data, tmp_path, monkeypatch
):
    import image_agent.product_long_image_render as render

    request, deps, _, gen = long_setup(request_data | {"output_dir": tmp_path})
    original = render.compose_product_long_image

    def fail(*args, **kwargs):
        raise OutputError("local composition/write unavailable")

    monkeypatch.setattr(render, "compose_product_long_image", fail)
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "failed" and len(gen.calls) == 6
    monkeypatch.setattr(render, "compose_product_long_image", original)
    resumed = await resume_pipeline(result.run_dir, AgentConfig(), dependencies=deps)
    assert resumed.status == "succeeded" and len(gen.calls) == 6
    assert resumed.usage["image_calls"] == result.usage["image_calls"]


async def test_local_atomic_write_oserror_is_reviewable_and_retryable(
    request_data, tmp_path, monkeypatch
):
    request, deps, _, gen = long_setup(request_data | {"output_dir": tmp_path})
    original = RunStore.write

    def fail(self, name, data):
        if name.startswith("slices/"):
            raise OSError("disk unavailable")
        return original(self, name, data)

    monkeypatch.setattr(RunStore, "write", fail)
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == "failed" and result.assets[0].error_info.kind == "output"
    monkeypatch.setattr(RunStore, "write", original)
    resumed = await resume_pipeline(result.run_dir, AgentConfig(), dependencies=deps)
    assert resumed.status == "succeeded" and len(gen.calls) == 6


async def test_cancel_during_generation_saves_returned_response_and_waits(request_data, tmp_path):
    request, deps, _, gen = long_setup(request_data | {"output_dir": tmp_path})
    original = gen.generate
    entered, release = asyncio.Event(), asyncio.Event()

    async def delayed(**kwargs):
        entered.set()
        await release.wait()
        return await original(**kwargs)

    gen.generate = delayed
    task = asyncio.create_task(run_pipeline(request, AgentConfig(), dependencies=deps))
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    run = next(tmp_path.iterdir())
    journal = json.loads((run / "operations.json").read_text(encoding="utf-8"))
    assert list(journal.values())[0]["status"] == "saved"
    gen.generate = original
    result = await resume_pipeline(run, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded" and len(gen.calls) == 6


@pytest.mark.parametrize("damage", ["segment", "raw", "slice", "plan", "plan_state"])
async def test_tampered_refs_block_before_paid_work(request_data, tmp_path, damage):
    request, deps, vision, gen = long_setup(request_data | {"output_dir": tmp_path})
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    root = Path(result.run_dir)
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    runtime = state["product_long_images"][result.assets[0].asset_id]
    if damage == "segment":
        ref = runtime["segments"][0]["candidates"][0]["ref"]
    elif damage == "raw":
        ref = runtime["segments"][0]["candidates"][0]["raw"]
    elif damage == "slice":
        ref = state["result"]["assets"][0]["candidates"][-1]["metadata"]["slices"][0]
    elif damage == "plan":
        ref = runtime["plan_ref"]
    else:
        runtime["plan"]["style_direction"] = "tampered"
        (root / "state.json").write_text(json.dumps(state), encoding="utf-8")
    if damage != "plan_state":
        (root / ref["path"]).write_bytes(b"tampered")
    before = len(vision.calls)
    with pytest.raises(AgentError, match="integrity"):
        await resume_pipeline(root, AgentConfig(), dependencies=deps)
    assert len(vision.calls) == before and len(gen.calls) == 6


@pytest.mark.parametrize("pause_phase", ["long_generate", "long_audit"])
async def test_resume_recompiles_old_contaminated_long_plan(request_data, tmp_path, pause_phase):
    from image_agent.models import PreparedContext
    from image_agent.quality import check_pixels, evaluate_quality
    from image_agent.selection import compile_element_plan
    from tests.fakes import analysis_data

    data = analysis_data()
    data["materials"].append(
        dict(material_id="m2", sha256="hash2", observed_role="style", summary="red cup")
    )
    data["subjects"].append(
        dict(
            subject_id="s2",
            material_ids=["m2"],
            identity_fact_ids=["f2"],
            representative_material_id="m2",
            matches_product=True,
        )
    )
    data["facts"].append(
        dict(
            fact_id="f2",
            subject_id="s2",
            description="red ceramic cup",
            verifiability="visible_appearance",
            evidence=[dict(material_id="m2", observation="red cup visible", source_type="visual")],
            confidence=0.95,
        )
    )
    data["elements"].append(
        dict(
            element_id="shared",
            subject_id=None,
            kind="style",
            description="red ceramic cup inspiration",
            fact_ids=["f1", "f2"],
        )
    )
    data["intent"]["proposal"]["preferred_element_ids"].append("shared")
    inputs = request_data | {
        "output_dir": tmp_path,
        "materials": [
            request_data["materials"][0],
            dict(material_id="m2", source={"data": b"red"}, role_hint="style"),
        ],
    }
    request, deps, vision, gen = long_setup(inputs, discovery=data)
    request.long_image.segment_count = 3

    def stop(event):
        if event["stage"] == pause_phase:
            raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await run_pipeline(request, AgentConfig(), dependencies=deps, on_progress=stop)
    store = RunStore(next(tmp_path.iterdir()))
    state = store.json("state.json")
    context = PreparedContext.model_validate(state["context"] | {"materials": store.materials()})
    from image_agent.models import AssetTarget

    target = AssetTarget.model_validate(state["targets"][0])
    old_plan = compile_element_plan(request, target, context, AgentConfig())
    assert "f2" in [r.target_id for r in old_plan.requirements]
    state["result"]["assets"][0]["element_plan"] = old_plan.model_dump(mode="json")
    if pause_phase == "long_audit":
        segment = state["product_long_images"][target.target_key]["segments"][0]
        candidate = segment["candidates"][-1]
        candidate["element_plan"] = old_plan.model_dump(mode="json")
        image = store.read(candidate["ref"])
        audit = await vision.audit_image(request, target, context, old_plan, image)
        candidate["quality"] = segment["quality"] = evaluate_quality(
            audit,
            check_pixels(
                image, target, request.image_size, AgentConfig().model_name(request.image_model)
            ),
            target,
            old_plan,
        ).model_dump(mode="json")
    store.json("state.json", state)
    original_audit = vision.audit_image
    audited = []

    async def capture(request, target, context, element_plan, image):
        audited.append(element_plan.audit_material_ids)
        assert [f.fact_id for f in context.analysis.facts] == ["f1"]
        assert "f2" not in [r.target_id for r in element_plan.requirements]
        return await original_audit(request, target, context, element_plan, image)

    vision.audit_image = capture
    result = await resume_pipeline(store.directory, AgentConfig(), dependencies=deps)
    assert result.status == "succeeded" and len(gen.calls) == 3
    assert audited == [["m1"]] * 3
    assert all(
        "f2" not in call["prompt"] and "red ceramic cup" not in call["prompt"] for call in gen.calls
    )
