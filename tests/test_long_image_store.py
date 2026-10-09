"""Real disk, budget and interruption tests for long-image persistence."""

import importlib
from copy import deepcopy

import pytest

from image_agent.config import AgentConfig
from image_agent.errors import AgentError, OutputError
from image_agent.execution import ExecutionPolicy
from image_agent.long_image_models import (
    LongImageArtifact,
    LongImageOptions,
    LongImagePlanningResult,
    LongImageResult,
    LongImageSlot,
    LongSlotAsset,
    RenderedLongImage,
)
from image_agent.models import LoadedMaterial
from image_agent.run_store import digest
from tests.test_images import picture
from tests.test_long_image_requirements import request_and_plan


def store_type():
    try:
        return importlib.import_module("image_agent.long_image_store").LongImageRunStore
    except ModuleNotFoundError:
        pytest.fail("LongImageRunStore is not implemented")


def initialized(tmp_path=None):
    request, plan = request_and_plan()
    store = store_type()(tmp_path)
    store.initialize(request, (), AgentConfig(), ExecutionPolicy())
    return store, request, plan


@pytest.mark.parametrize("persisted", [False, True], ids=["memory", "disk"])
@pytest.mark.parametrize("metadata", ["null", "[]", '"invalid"', "42", "true"])
def test_non_object_run_metadata_is_a_controlled_output_error(tmp_path, persisted, metadata):
    store = store_type()(tmp_path if persisted else None)
    store.write("run.json", metadata.encode("utf-8"))

    with pytest.raises(OutputError, match="metadata.*JSON object") as caught:
        store.load_metadata()

    assert caught.value.code == "output"
    assert caught.value.kind == "output"


@pytest.mark.parametrize("metadata", ["null", "[]", '"invalid"', "42", "true"])
@pytest.mark.parametrize(
    "entry", ["render_long_image", "resume_long_image", "accept_long_image_candidate"]
)
@pytest.mark.asyncio
async def test_public_local_paths_reject_non_object_metadata_without_clients(
    tmp_path, monkeypatch, metadata, entry
):
    import image_agent
    import image_agent.long_image_runtime as runtime

    def denied(*args, **kwargs):
        raise AssertionError("invalid metadata must not read configuration or create clients")

    monkeypatch.setattr(AgentConfig, "from_env", denied)
    monkeypatch.setattr(runtime, "HttpTransport", denied)
    monkeypatch.setattr(runtime, "_lazy_dependencies", denied)
    (tmp_path / "run.json").write_text(metadata, encoding="utf-8")
    operation = getattr(image_agent, entry)

    if entry == "accept_long_image_candidate":
        with pytest.raises(OutputError, match="metadata.*JSON object") as caught:
            await operation(tmp_path, "slot", "candidate", reason="已确认")
        assert caught.value.code == "output"
        assert caught.value.kind == "output"
    else:
        result = await operation(tmp_path)
        assert result.status == "failed"
        assert result.error_info.code == "output"
        assert result.error_info.kind == "output"
        assert "metadata" in result.error_info.message
        assert "JSON object" in result.error_info.message
        assert result.output_errors == [result.error_info]
        assert result.page is None
        assert result.image_assets == []

    assert (tmp_path / "run.json").read_text(encoding="utf-8") == metadata
    assert not (tmp_path / "state.json").exists()
    assert not (tmp_path / "budget.sqlite").exists()


@pytest.mark.parametrize(
    "metadata",
    [
        {"version": 1, "task_type": "long_image"},
        {"version": 3, "task_type": "long_image"},
        {"version": 2, "task_type": "unknown"},
        {"version": 2},
    ],
)
def test_unsupported_object_run_metadata_remains_a_clear_validation_error(tmp_path, metadata):
    store = store_type()(tmp_path)
    store.json("run.json", metadata)

    with pytest.raises(AgentError, match="only version 2 long_image runs") as caught:
        store.load_metadata()

    assert caught.value.code == "validation"


def planning_record(store, request, plan):
    ledger = store.open_ledger()
    ledger.reserve("vision", "planning/test")
    if store.directory:
        ledger.close()
    return store.save_planning_result(
        LongImagePlanningResult(
            planning_id="planning-test",
            status="ready",
            plan=plan,
            usage={"vision_calls": 999},
        ),
        request,
        (),
        ExecutionPolicy(),
    )


def test_empty_materials_persist_without_legacy_source_access(tmp_path):
    store, request, _ = initialized(tmp_path)
    loaded = store_type()(tmp_path)
    assert loaded.request() == request
    assert loaded.materials() == ()
    assert loaded.load_metadata()["task_type"] == "long_image"
    assert loaded.load_metadata()["version"] == 2
    assert loaded.open_ledger().usage() == {"image_calls": 0, "vision_calls": 0}
    assert store.load_metadata()["inputs"] == []


def test_external_record_id_cannot_forge_verified_plan(tmp_path):
    store, request, plan = initialized(tmp_path)
    record = planning_record(store, request, plan)
    assert record.usage == {"image_calls": 0, "vision_calls": 1}
    loaded = store.load_planning_record(record.record_id, request=request, plan=plan)
    assert loaded == record
    changed = plan.model_copy(update={"visual_direction": "forged"})
    with pytest.raises(AgentError, match="record|plan"):
        store.load_planning_record(record.record_id, request=request, plan=changed)
    forged = record.model_copy(update={"usage": {"image_calls": 0, "vision_calls": 999}})
    with pytest.raises(AgentError):
        store_type().resolve_planning_record(forged, request=request, plan=plan)
    record_data = store.json(f"records/{record.record_id}.json")
    record_data["plan"]["visual_direction"] = "changed on disk"
    store.json(f"records/{record.record_id}.json", record_data)
    with pytest.raises(AgentError):
        store.load_planning_record(record.record_id, request=request, plan=plan)


def test_memory_record_can_be_resolved_only_through_original_binding():
    store, request, plan = initialized()
    record = planning_record(store, request, plan)
    resolved = store_type().resolve_planning_record(record, request=request, plan=plan)
    assert resolved.usage == {"image_calls": 0, "vision_calls": 1}
    forged = record.model_copy(update={"record_id": "unregistered"})
    with pytest.raises(AgentError):
        store_type().resolve_planning_record(forged, request=request, plan=plan)


@pytest.mark.parametrize("persisted", [False, True], ids=["memory", "disk"])
@pytest.mark.parametrize(
    "options",
    [
        {"width": 1000},
        {"theme": "warm"},
        {"slice_height": 2000},
        {"font_path": "local-font.ttf"},
        {"width": 1000, "theme": "premium", "slice_height": 2000, "font_path": "font.ttf"},
    ],
)
def test_layout_changes_reuse_verified_plan_without_reserving_budget(tmp_path, persisted, options):
    planning_store, request, plan = initialized(tmp_path / "planning" if persisted else None)
    record = planning_record(planning_store, request, plan)
    changed = request.model_copy(update={"options": LongImageOptions(**options)})
    generation_store = store_type()(tmp_path / "generation" if persisted else None)

    for _ in range(2):
        loaded = planning_store.load_planning_record(record.record_id, request=changed, plan=plan)
        resolved = generation_store.resolve_planning_record(record, request=changed, plan=plan)
        assert loaded == resolved == record
        assert resolved.usage == {"image_calls": 0, "vision_calls": 1}

    generation_store.initialize(
        changed,
        (),
        AgentConfig(),
        ExecutionPolicy(),
        planning_result=LongImagePlanningResult(
            planning_id=record.planning_id, status="ready", plan=plan, record=resolved
        ),
    )
    meta = generation_store.load_metadata()
    assert meta["planning_record"] == record.model_dump(mode="json")
    assert meta["planning_result"]["plan"] == plan.model_dump(mode="json")
    assert generation_store.request().options == changed.options
    assert planning_store.request().options == request.options
    with planning_store.budget() as ledger:
        assert ledger.usage() == {"image_calls": 0, "vision_calls": 1}
    with generation_store.budget() as ledger:
        assert ledger.usage() == {"image_calls": 0, "vision_calls": 0}


@pytest.mark.parametrize(
    "path", ["../escape.bin", "/absolute.bin", "C:/absolute.bin", "x/../a", "x\\..\\a"]
)
def test_relative_paths_and_hashes_are_checked(tmp_path, path):
    store, _, _ = initialized(tmp_path)
    with pytest.raises(OutputError):
        store.write(path, b"unsafe")
    ref = store.write("inputs/safe.bin", b"safe")
    assert store.read(ref) == b"safe"
    store.path(ref["path"]).write_bytes(b"corrupt")
    with pytest.raises(OutputError, match="integrity"):
        store.read(ref)


def test_reinitialize_keeps_budget_and_journal_and_rejects_changed_policy(tmp_path):
    store, request, _ = initialized(tmp_path)
    ledger = store.open_ledger()
    ledger.reserve("image", "run/slot/1")
    ledger.close()
    store.mark_operation("run/slot/1", "submitted")
    store.initialize(request, (), AgentConfig(), ExecutionPolicy())
    ledger = store.open_ledger()
    assert ledger.usage()["image_calls"] == 1
    ledger.close()
    assert store.operation("run/slot/1")["submissions"] == 1
    with pytest.raises(AgentError):
        store.initialize(request, (), AgentConfig(), ExecutionPolicy(max_image_calls=2))
    store.path("budget.sqlite").unlink()
    with pytest.raises(AgentError, match="missing"):
        store.open_ledger()


def test_persisted_usage_is_verified_against_ledger(tmp_path):
    store, request, plan = initialized(tmp_path)
    record = planning_record(store, request, plan)
    data = deepcopy(store.json(f"records/{record.record_id}.json"))
    data["record"]["usage"] = {"image_calls": 0, "vision_calls": 42}
    store.json(f"records/{record.record_id}.json", data)
    with pytest.raises(AgentError):
        store.load_planning_record(record.record_id, request=request, plan=plan)


def test_material_bytes_bind_to_persisted_input(tmp_path):
    data = picture((20, 10))
    request, _ = request_and_plan(materials=[{"material_id": "m1", "source": {"data": data}}])
    material = LoadedMaterial(
        material_id="m1", data=data, sha256=digest(data), order=0, role_hint="auto"
    )
    store = store_type()(tmp_path)
    store.initialize(request, (material,), AgentConfig(), ExecutionPolicy())
    assert store.request().materials[0].source.path.read_bytes() == data
    store.path("inputs/00-m1.bin").write_bytes(b"corrupt")
    with pytest.raises(OutputError):
        store.validate_inputs_and_candidates()


def delivery(store, plan, status="succeeded"):
    plan = plan.model_copy(deep=True)
    plan.sections[0].slot_ids = ["slot"]
    plan.image_slots = [
        LongImageSlot(
            slot_id="slot",
            source="generate",
            role="illustration",
            prompt="A simple diagram",
            aspect_ratio="1:1",
            section_ids=["first"],
        )
    ]
    data = picture((20, 10), "navy")
    ref = store.write("candidates/slot/c1.png", data)
    asset = LongSlotAsset(
        slot_id="slot",
        source="generate",
        status=status,
        selected_candidate_id="c1",
        image=data,
        file_path=ref["path"],
        sha256=ref["sha256"],
        candidates=[{"candidate_id": "c1", "file_path": ref["path"], "sha256": ref["sha256"]}],
    )

    def artifact(aid, kind, dependencies):
        return LongImageArtifact(
            artifact_id=aid,
            kind=kind,
            image=data,
            width=20,
            height=10,
            dependencies=dependencies,
        )

    rendered = RenderedLongImage(
        sections=[artifact("first", "section", ["slot"]), artifact("last", "section", [])],
        page=artifact("page", "page", ["slot"]),
        slices=[artifact("slice-0", "slice", ["slot"])],
        render_fingerprint="pixels-v1",
        checks=[{"code": "layout", "passed": True}],
    )
    result = LongImageResult(
        status=status,
        plan=plan,
        image_assets=[asset],
        sections=[
            rendered.sections[0].model_copy(update={"status": status}),
            rendered.sections[1].model_copy(update={"status": "succeeded"}),
        ],
    )
    return result, rendered


def saved_delivery(tmp_path, status="succeeded"):
    store, request, plan = initialized(tmp_path)
    result, rendered = delivery(store, plan, status)
    store.begin_delivery_revision(
        choices={"slot": "c1"}, statuses={"slot": status}, options=request.options
    )
    store.commit_delivery_revision(result, rendered)
    return store, request, result, rendered


@pytest.mark.parametrize("persisted", [False, True], ids=["memory", "disk"])
@pytest.mark.parametrize("damage", ["bytes", "path", "hash", "duplicate"])
def test_candidate_hydration_clears_cached_bytes_on_failure(tmp_path, persisted, damage):
    from image_agent.long_image_contracts import long_result_to_bundle

    store, _, plan = initialized(tmp_path if persisted else None)
    result, _ = delivery(store, plan)
    asset = result.image_assets[0]
    ref = store.write("candidates/slot/c2.png", picture((20, 10), "red"))
    asset.candidates.append(
        {"candidate_id": "c2", "file_path": ref["path"], "sha256": ref["sha256"]}
    )
    store._hydrate_asset(asset)
    # Old local inline adapters must never bypass failed store verification.
    asset.candidates[1]["image"] = picture((20, 10), "red")
    if damage == "bytes":
        store.write(ref["path"], b"damaged")
    elif damage == "path":
        asset.candidates[1]["file_path"] = "../outside.png"
    elif damage == "hash":
        asset.candidates[1]["sha256"] = "0" * 64
    else:
        asset.candidates[1]["candidate_id"] = "c1"
    with pytest.raises(OutputError):
        store._hydrate_asset(asset)
    assert asset.image is None
    if damage != "duplicate":
        bundle = long_result_to_bundle(result)
        assert not bundle.blobs
        assert all(c.blob_id is None for c in bundle.dto.image_assets[0].candidates)


@pytest.mark.parametrize("change", ["path", "hash", "id"])
def test_candidate_export_rejects_changed_cached_binding(tmp_path, change):
    from image_agent.long_image_contracts import long_result_to_bundle

    store, _, plan = initialized(tmp_path)
    result, _ = delivery(store, plan)
    asset = result.image_assets[0]
    store._hydrate_asset(asset)
    assert long_result_to_bundle(result).dto.image_assets[0].candidates[0].blob_id
    asset.candidates[0][{"path": "file_path", "hash": "sha256", "id": "candidate_id"}[change]] = {
        "path": "../outside.png",
        "hash": "0" * 64,
        "id": "other",
    }[change]
    candidate = long_result_to_bundle(result).dto.image_assets[0].candidates[0]
    assert candidate.blob_id is None


def test_delivery_intent_makes_old_page_stale(tmp_path):
    store, request, _, _ = saved_delivery(tmp_path)
    assert store.hydrate_result().page.availability == "current"
    revision = store.begin_delivery_revision(
        choices={"slot": "c1"},
        statuses={"slot": "accepted"},
        options=request.options,
    )
    loaded = store_type()(tmp_path).hydrate_result()
    assert revision == 2
    assert loaded.page.availability == "stale"
    assert loaded.status == "failed"
    assert loaded.image_assets[0].status == "accepted"
    assert store.pending_delivery_revision()["revision"] == 2


@pytest.mark.parametrize(
    "boundary",
    ["intent", "stale", "slots", "section", "manifest", "pointer", "checkpoint", "cleanup"],
)
def test_recover_each_revision_commit_boundary(tmp_path, monkeypatch, boundary):
    store, request, _, rendered = saved_delivery(tmp_path, "pending_audit")
    result = store.hydrate_result()
    result.status = "accepted"
    result.image_assets[0].status = "accepted"
    result.sections[0].status = "accepted"
    before = store.open_ledger()
    usage = before.usage()
    before.close()
    original_write, original_remove = store.write, store.remove
    fired = False

    def crash_write(name, data):
        nonlocal fired
        ref = original_write(name, data)
        targets = {
            "intent": name == "delivery-intent.json",
            "stale": name == "result.json",
            "slots": name == "slots.json",
            "section": name == "deliveries/2/section/first.png",
            "manifest": name == "deliveries/2/manifest.json",
            "pointer": name == "delivery-current.json",
            "checkpoint": name == "state.json",
        }
        if targets.get(boundary) and not fired:
            fired = True
            raise OSError("power loss")
        return ref

    def crash_remove(name):
        nonlocal fired
        original_remove(name)
        if boundary == "cleanup" and name == "delivery-intent.json" and not fired:
            fired = True
            raise OSError("power loss")

    monkeypatch.setattr(store, "write", crash_write)
    monkeypatch.setattr(store, "remove", crash_remove)
    with pytest.raises(OSError, match="power loss"):
        store.begin_delivery_revision(
            choices={"slot": "c1"}, statuses={"slot": "accepted"}, options=request.options
        )
        store.commit_delivery_revision(result, rendered)
    fresh = store_type()(tmp_path)
    intermediate = fresh.hydrate_result()
    if fresh.pending_delivery_revision():
        assert intermediate.page.availability != "current"
        # The runtime supplies the same locally reconstructed pixels if the
        # interruption occurred before the manifest was complete.
        fresh.commit_delivery_revision(result, rendered)
    recovered = fresh.hydrate_result()
    assert recovered.status == "accepted"
    assert recovered.delivery_revision == 2
    assert recovered.page.availability == "current"
    assert recovered.page.image == rendered.page.image
    assert [a.status for a in recovered.sections] == ["accepted", "succeeded"]
    assert [a.dependencies for a in recovered.sections] == [["slot"], []]
    assert all(a.status == "accepted" for a in [recovered.page, *recovered.slices])
    assert fresh.pending_delivery_revision() is None
    after = fresh.open_ledger()
    assert after.usage() == usage
    after.close()


def test_same_pixels_new_status_updates_every_manifest_without_renderer(tmp_path):
    store, request, _, _ = saved_delivery(tmp_path, "pending_audit")
    result = store.hydrate_result()
    old_hash = result.page.sha256
    result.status = "succeeded"
    result.image_assets[0].status = "succeeded"
    result.sections[0].status = "succeeded"
    store.begin_delivery_revision(
        choices={"slot": "c1"}, statuses={"slot": "succeeded"}, options=request.options
    )
    store.commit_delivery_revision(result)
    result = store.hydrate_result()
    assert result.page.sha256 == old_hash
    assert result.image_assets[0].asset_revision == 2
    assert all(
        a.status == "succeeded" and a.delivery_revision == 2
        for a in [*result.sections, result.page, *result.slices]
    )
    assert store.json("result.json")["status"] == "succeeded"
    assert store.json("state.json")["result"]["status"] == "succeeded"


@pytest.mark.parametrize(
    ("initial", "updated"), [("pending_audit", "succeeded"), ("quality_failed", "accepted")]
)
@pytest.mark.parametrize("reuse_pixels", [False, True], ids=["rendered", "reuse"])
@pytest.mark.parametrize("interrupted", [False, True], ids=["commit", "recover"])
def test_mixed_sections_keep_own_status_through_same_pixel_revision(
    tmp_path, monkeypatch, initial, updated, reuse_pixels, interrupted
):
    store, request, plan = initialized(tmp_path)
    result, rendered = delivery(store, plan)
    result.plan.sections.insert(
        1,
        result.plan.sections[1].model_copy(update={"section_id": "middle", "slot_ids": ["other"]}),
    )
    result.plan.image_slots.append(
        result.plan.image_slots[0].model_copy(
            update={"slot_id": "other", "section_ids": ["middle"]}
        )
    )
    result.image_assets.append(
        result.image_assets[0].model_copy(deep=True, update={"slot_id": "other", "status": initial})
    )
    rendered.sections.insert(
        1,
        rendered.sections[0].model_copy(
            update={"artifact_id": "middle", "dependencies": ["other"]}
        ),
    )
    result.sections.insert(1, rendered.sections[1].model_copy(update={"status": initial}))
    rendered.page.dependencies = ["slot", "other"]
    rendered.slices[0].dependencies = ["slot", "other"]
    result.status = initial
    choices = {"slot": "c1", "other": "c1"}
    store.begin_delivery_revision(
        choices=choices, statuses={"slot": "succeeded", "other": initial}, options=request.options
    )
    store.commit_delivery_revision(result, rendered)
    result = store.hydrate_result()
    assert [a.status for a in result.sections] == ["succeeded", initial, "succeeded"]
    before_hashes = [a.sha256 for a in [*result.sections, result.page, *result.slices]]
    result.status = updated
    result.sections[1].status = updated
    candidates = deepcopy(result.image_assets[1].candidates)
    if updated == "accepted":
        candidates[0].update(accepted_reason="用户确认", accepted_at="2026-10-09T12:00:00Z")
    store.begin_delivery_revision(
        choices=choices,
        statuses={"slot": "succeeded", "other": {"status": updated, "candidates": candidates}},
        options=request.options,
    )
    original_write = store.write

    def crash_after_manifest(name, data):
        ref = original_write(name, data)
        if name == "deliveries/2/manifest.json":
            raise OSError("crash after manifest")
        return ref

    if interrupted:
        monkeypatch.setattr(store, "write", crash_after_manifest)
        with pytest.raises(OSError, match="crash after manifest"):
            store.commit_delivery_revision(result, None if reuse_pixels else rendered)
        assert store_type()(tmp_path).hydrate_result().page.availability == "stale"
        store = store_type()(tmp_path)
        assert store.recover_delivery_revision()
    else:
        store.commit_delivery_revision(result, None if reuse_pixels else rendered)
    restored = store_type()(tmp_path).hydrate_result()
    artifacts = [*restored.sections, restored.page, *restored.slices]
    assert [a.status for a in restored.sections] == ["succeeded", updated, "succeeded"]
    assert [a.dependencies for a in restored.sections] == [["slot"], ["other"], []]
    assert [a.status for a in restored.image_assets] == ["succeeded", updated]
    assert [a.asset_revision for a in restored.image_assets] == [1, 2]
    assert restored.page.status == updated
    assert all(a.status == updated for a in restored.slices)
    assert all(a.availability == "current" and a.delivery_revision == 2 for a in artifacts)
    assert [a.sha256 for a in artifacts] == before_hashes
    for name in ["result.json", "deliveries/2/manifest.json", "state.json"]:
        saved = store.json(name)
        saved = saved.get("result", saved)
        assert [a["status"] for a in saved["sections"]] == ["succeeded", updated, "succeeded"]
    if updated == "accepted":
        assert restored.image_assets[1].candidates[0]["accepted_reason"] == "用户确认"
    with store.budget() as ledger:
        assert ledger.usage() == {"image_calls": 0, "vision_calls": 0}


@pytest.mark.parametrize("target", ["selected", "unselected", "page", "section", "slice"])
def test_corrupt_selected_candidate_is_not_current(tmp_path, target):
    store, _, _, _ = saved_delivery(tmp_path)
    result = store.hydrate_result()
    if target == "unselected":
        ref = store.write("candidates/slot/c2.png", picture((20, 10), "red"))
        result.image_assets[0].candidates.append(
            {"candidate_id": "c2", "file_path": ref["path"], "sha256": ref["sha256"]}
        )
        store.save_checkpoint(result)
        path = ref["path"]
    else:
        path = {
            "selected": result.image_assets[0].file_path,
            "page": result.page.file_path,
            "section": result.sections[0].file_path,
            "slice": result.slices[0].file_path,
        }[target]
    store.path(path).write_bytes(b"corrupt")
    with pytest.raises(OutputError):
        store.validate_inputs_and_candidates()
    loaded = store.hydrate_result()
    assert loaded.status == "failed"
    assert loaded.page.availability != "current"
    assert loaded.output_errors


def test_render_override_does_not_mutate_original_request(tmp_path):
    store, request, _, _ = saved_delivery(tmp_path)
    original = store.json("run.json")["request"]
    options = request.options.model_copy(update={"width": 1000, "theme": "warm"})
    store.begin_delivery_revision(
        choices={"slot": "c1"}, statuses={"slot": "succeeded"}, options=options
    )
    assert store.json("run.json")["request"] == original
    assert store.pending_delivery_revision()["options"]["width"] == 1000


@pytest.mark.parametrize("failure", ["layout", "save"])
@pytest.mark.parametrize("rerender", [False, True])
def test_failed_delivery_retains_only_stale_old_pixels(tmp_path, failure, rerender):
    store, request, _, rendered = saved_delivery(tmp_path, "accepted")
    result = store.hydrate_result()
    if failure == "layout":
        result.status = "failed"
    else:
        result.output_errors = [{"kind": "output", "code": "output", "message": "save failed"}]
    store.begin_delivery_revision(
        choices={"slot": "c1"}, statuses={"slot": "accepted"}, options=request.options
    )
    store.commit_delivery_revision(result, rendered if rerender else None)
    result = store.hydrate_result()
    assert result.status == "failed"
    expected_availability = "absent" if rerender else "stale"
    assert all(
        a.status == "failed" and a.availability == expected_availability
        for a in [*result.sections, result.page, *result.slices]
    )
    assert result.image_assets[0].status == "accepted"


def test_uniform_budget_context_and_close_preserve_memory_record():
    store, request, plan = initialized()
    with store.budget() as ledger:
        ledger.reserve("vision", "planning/context")
    record = store.save_planning_result(
        LongImagePlanningResult(planning_id="context-plan", status="ready", plan=plan),
        request,
        (),
        ExecutionPolicy(),
    )
    store.close()
    assert store_type().resolve_planning_record(record, request=request, plan=plan).usage == {
        "image_calls": 0,
        "vision_calls": 1,
    }


def test_inline_planning_usage_is_immutable_when_execution_ledger_grows(tmp_path):
    store, request, plan = initialized(tmp_path)
    record = planning_record(store, request, plan)
    with store.budget() as ledger:
        ledger.reserve("image", "run/slot/1")
    loaded = store.load_planning_record(record.record_id, request=request, plan=plan)
    assert loaded.usage == {"image_calls": 0, "vision_calls": 1}


def test_record_locator_cannot_substitute_a_different_record(tmp_path):
    first, request, plan = initialized(tmp_path / "first")
    second, _, _ = initialized(tmp_path / "second")
    record1 = planning_record(first, request, plan)
    record2 = planning_record(second, request, plan)
    forged = record1.model_copy(update={"local_record_ref": record2.local_record_ref})
    with pytest.raises(AgentError):
        store_type().resolve_planning_record(forged, request=request, plan=plan)


def test_persisted_record_request_must_match_original_run(tmp_path):
    store, request, plan = initialized(tmp_path)
    record = planning_record(store, request, plan)
    other_request, other_plan = request_and_plan(style_hint="different original intent")
    other = store_type()(tmp_path / "other")
    other.initialize(other_request, (), AgentConfig(), ExecutionPolicy())
    other_record = planning_record(other, other_request, other_plan)
    data = other.json(f"records/{other_record.record_id}.json")
    data["record"].update(
        record_id=record.record_id,
        local_record_ref=record.local_record_ref,
        ledger_ref=record.ledger_ref,
    )
    store.json(f"records/{record.record_id}.json", data)
    with pytest.raises(AgentError):
        store.load_planning_record(record.record_id, request=other_request, plan=other_plan)


def test_successful_asset_requires_selected_candidate_even_with_file_path(tmp_path):
    store, _, plan = initialized(tmp_path)
    result, _ = delivery(store, plan)
    result.image_assets[0].selected_candidate_id = None
    with pytest.raises(OutputError):
        store.save_checkpoint(result)


def test_source_asset_cannot_point_to_unrelated_generated_file(tmp_path):
    store, _, plan = initialized(tmp_path)
    ref = store.write("candidates/unrelated.png", picture((20, 10)))
    source = LongSlotAsset(
        slot_id="source",
        source="source",
        source_status="available",
        file_path=ref["path"],
        sha256=ref["sha256"],
    )
    with pytest.raises(OutputError):
        store.save_checkpoint(LongImageResult(status="succeeded", plan=plan, image_assets=[source]))


def test_corrupt_previous_pixels_can_be_replaced_by_local_rerender(tmp_path):
    store, request, _, rendered = saved_delivery(tmp_path)
    result = store.hydrate_result()
    store.path(result.page.file_path).write_bytes(b"corrupt")
    store.begin_delivery_revision(
        choices={"slot": "c1"}, statuses={"slot": "succeeded"}, options=request.options
    )
    store.commit_delivery_revision(result, rendered)
    assert store.hydrate_result().page.availability == "current"


def test_pointer_checkpoint_interruption_can_finish_from_saved_manifest(tmp_path, monkeypatch):
    store, request, _, rendered = saved_delivery(tmp_path)
    result = store.hydrate_result()
    result.status = "accepted"
    store.begin_delivery_revision(
        choices={"slot": "c1"}, statuses={"slot": "accepted"}, options=request.options
    )
    original = store.write

    def crash(name, data):
        ref = original(name, data)
        if name == "delivery-current.json":
            raise OSError("crash after pointer")
        return ref

    monkeypatch.setattr(store, "write", crash)
    with pytest.raises(OSError):
        store.commit_delivery_revision(result, rendered)
    fresh = store_type()(tmp_path)
    fresh.recover_delivery_revision()
    assert fresh.hydrate_result().status == "accepted"
    assert fresh.hydrate_result().page.availability == "current"


def test_changed_options_cannot_reuse_pixels_from_old_layout(tmp_path):
    store, request, _, _ = saved_delivery(tmp_path)
    result = store.hydrate_result()
    store.begin_delivery_revision(
        choices={"slot": "c1"},
        statuses={"slot": "succeeded"},
        options=request.options.model_copy(update={"width": 1000}),
    )
    with pytest.raises(OutputError):
        store.commit_delivery_revision(result)


def test_committed_pixel_reuse_retains_render_fingerprint(tmp_path):
    store, request, _, _ = saved_delivery(tmp_path)
    result = store.hydrate_result()
    store.begin_delivery_revision(
        choices={"slot": "c1"}, statuses={"slot": "accepted"}, options=request.options
    )
    result.status = "accepted"
    store.commit_delivery_revision(result)
    manifest = store.json("deliveries/2/manifest.json")
    assert manifest["render_fingerprint"] == "pixels-v1"


@pytest.mark.parametrize("missing", ["section", "slice", "slot"])
def test_incomplete_success_cannot_replace_current_delivery(tmp_path, missing):
    store, request, _, rendered = saved_delivery(tmp_path)
    result = store.hydrate_result()
    store.begin_delivery_revision(
        choices={"slot": "c1"}, statuses={"slot": "succeeded"}, options=request.options
    )
    if missing == "section":
        rendered.sections = rendered.sections[:1]
    elif missing == "slice":
        rendered.slices = []
    else:
        result.image_assets = []
    with pytest.raises(OutputError):
        store.commit_delivery_revision(result, rendered)
    assert store.hydrate_result().page.availability == "stale"


def test_source_only_and_zero_slot_deliveries(tmp_path):
    store, request, plan = initialized(tmp_path / "zero")
    _, rendered = delivery(store, plan)
    for artifact in [*rendered.sections, rendered.page, *rendered.slices]:
        artifact.dependencies = []
    sections = [a.model_copy(update={"status": "succeeded"}) for a in rendered.sections]
    result = LongImageResult(status="succeeded", plan=plan, sections=sections)
    store.begin_delivery_revision(choices={}, statuses={}, options=request.options)
    store.commit_delivery_revision(result, rendered)
    assert store.hydrate_result().status == "succeeded"
    assert store.hydrate_result().image_assets == []
    assert [a.status for a in store.hydrate_result().sections] == ["succeeded", "succeeded"]

    data = picture((20, 10))
    request, plan = request_and_plan(materials=[{"material_id": "m1", "source": {"data": data}}])
    material = LoadedMaterial(
        material_id="m1", data=data, sha256=digest(data), order=0, role_hint="auto"
    )
    store = store_type()(tmp_path / "source")
    store.initialize(request, [material], AgentConfig(), ExecutionPolicy())
    plan.sections[0].slot_ids = ["source"]
    plan.image_slots = [
        LongImageSlot(
            slot_id="source",
            source="source",
            role="illustration",
            material_ids=["m1"],
            section_ids=["first"],
        )
    ]
    source = LongSlotAsset(
        slot_id="source",
        source="source",
        source_status="available",
        file_path="inputs/00-m1.bin",
        sha256=digest(data),
    )
    rendered.sections[0].dependencies = ["source"]
    rendered.page.dependencies = ["source"]
    rendered.slices[0].dependencies = ["source"]
    sections = [a.model_copy(update={"status": "succeeded"}) for a in rendered.sections]
    result = LongImageResult(
        status="succeeded", plan=plan, image_assets=[source], sections=sections
    )
    store.begin_delivery_revision(
        choices={"source": None}, statuses={"source": "available"}, options=request.options
    )
    store.commit_delivery_revision(result, rendered)
    restored = store.hydrate_result()
    assert restored.image_assets[0].image == data
    assert restored.image_assets[0].status is None
    assert [a.status for a in restored.sections] == ["succeeded", "succeeded"]
    assert [a.dependencies for a in restored.sections] == [["source"], []]
    with store.budget() as ledger:
        assert ledger.usage() == {"vision_calls": 0, "image_calls": 0}


def test_acceptance_metadata_survives_intent_before_slot_write(tmp_path):
    store, request, _, _ = saved_delivery(tmp_path, "quality_failed")
    result = store.hydrate_result()
    accepted = deepcopy(result.image_assets[0].candidates)
    accepted[0].update(accepted_reason="用户确认视觉效果", accepted_at="2026-10-09T12:00:00Z")
    store.begin_delivery_revision(
        choices={"slot": "c1"},
        statuses={"slot": {"status": "accepted", "candidates": accepted}},
        options=request.options,
    )
    restored = store_type()(tmp_path).hydrate_result()
    assert restored.image_assets[0].candidates[0]["accepted_reason"] == "用户确认视觉效果"
    assert restored.image_assets[0].asset_revision == 2
    assert restored.page.availability == "stale"


def test_changed_selected_bytes_require_rerender(tmp_path):
    store, request, _, _ = saved_delivery(tmp_path)
    result = store.hydrate_result()
    ref = store.write("candidates/slot/c2.png", picture((20, 10), "red"))
    result.image_assets[0].candidates.append(
        {"candidate_id": "c2", "file_path": ref["path"], "sha256": ref["sha256"]}
    )
    store.save_checkpoint(result)
    store.begin_delivery_revision(
        choices={"slot": "c2"}, statuses={"slot": "succeeded"}, options=request.options
    )
    with pytest.raises(OutputError, match="rendering"):
        store.commit_delivery_revision(result)


def test_rendered_png_hash_and_dimensions_verified(tmp_path):
    store, request, plan = initialized(tmp_path)
    result, rendered = delivery(store, plan)
    rendered.page.width = 21
    store.begin_delivery_revision(
        choices={"slot": "c1"}, statuses={"slot": "succeeded"}, options=request.options
    )
    with pytest.raises(OutputError, match="dimensions"):
        store.commit_delivery_revision(result, rendered)


def test_corrupt_checks_file_invalidates_current(tmp_path):
    store, _, _, _ = saved_delivery(tmp_path)
    store.json("deliveries/1/checks.json", [{"code": "layout", "passed": False}])
    with pytest.raises(OutputError):
        store.validate_inputs_and_candidates()
    assert store.hydrate_result().page.availability == "stale"


def test_audit_checkpoint_before_intent_still_increments_asset_revision(tmp_path):
    store, request, _, _ = saved_delivery(tmp_path, "pending_audit")
    result = store.hydrate_result()
    result.image_assets[0].status = "succeeded"
    store.save_checkpoint(result, phase="render")
    store.begin_delivery_revision(
        choices={"slot": "c1"}, statuses={"slot": "succeeded"}, options=request.options
    )
    result.status = "succeeded"
    result.sections[0].status = "succeeded"
    store.commit_delivery_revision(result)
    restored = store.hydrate_result()
    assert restored.image_assets[0].asset_revision == 2
    assert restored.page.status == "succeeded"
    assert store.json("state.json")["phase"] == "render"


def test_first_delivery_after_generation_checkpoint_has_asset_revision(tmp_path):
    store, request, plan = initialized(tmp_path)
    result, rendered = delivery(store, plan)
    store.save_checkpoint(result, phase="render")
    store.begin_delivery_revision(
        choices={"slot": "c1"}, statuses={"slot": "succeeded"}, options=request.options
    )
    store.commit_delivery_revision(result, rendered)
    assert store.hydrate_result().image_assets[0].asset_revision == 1


def test_source_slot_cannot_select_another_valid_input(tmp_path):
    materials = [
        LoadedMaterial(
            material_id=f"m{i}",
            data=picture((20, 10), color),
            sha256=digest(picture((20, 10), color)),
            order=i,
            role_hint="auto",
        )
        for i, color in enumerate(["navy", "red"])
    ]
    request, plan = request_and_plan(
        materials=[{"material_id": m.material_id, "source": {"data": m.data}} for m in materials]
    )
    plan.sections[0].slot_ids = ["source"]
    plan.image_slots = [
        LongImageSlot(
            slot_id="source",
            source="source",
            role="reference",
            material_ids=["m0"],
            section_ids=["first"],
        )
    ]
    store = store_type()(tmp_path)
    store.initialize(request, materials, AgentConfig(), ExecutionPolicy())
    _, rendered = delivery(store, plan)
    wrong = LongSlotAsset(
        slot_id="source",
        source="source",
        source_status="available",
        file_path="inputs/01-m1.bin",
        sha256=materials[1].sha256,
    )
    result = LongImageResult(status="succeeded", plan=plan, image_assets=[wrong])
    store.begin_delivery_revision(
        choices={"source": None}, statuses={"source": "available"}, options=request.options
    )
    with pytest.raises(OutputError, match="source"):
        store.commit_delivery_revision(result, rendered)
