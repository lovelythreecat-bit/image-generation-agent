"""Acceptance checks use the public runtime and only replace external HTTP responses."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from image_agent import (
    accept_long_image_candidate,
    analyze_long_image,
    create_long_image,
    long_result_to_bundle,
    render_long_image,
    resume_long_image,
)
from image_agent.config import AgentConfig
from image_agent.errors import AgentError, OutputError
from image_agent.execution import ExecutionPolicy
from image_agent.long_image_store import LongImageRunStore
from scripts.long_image_offline_demo import (
    SCENES,
    OfflineProvider,
    make_request,
    offline_config,
    offline_http,
    verify_delivery,
)


def test_offline_demo_delivers_three_real_runtime_pages(tmp_path):
    script = Path(__file__).parents[1] / "scripts" / "long_image_offline_demo.py"
    completed = subprocess.run(
        [sys.executable, "-X", "utf8", str(script), "--out", str(tmp_path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    summary = json.loads((tmp_path / "demo-summary.json").read_text(encoding="utf-8"))
    assert set(summary) == {"product_detail", "promotion", "tutorial"}
    for scene in summary:
        result = json.loads(Path(summary[scene]["result_json"]).read_text(encoding="utf-8"))
        assert result["task_type"] == "long_image"
        assert result["status"] == "succeeded"
        assert result["page"]["availability"] == "current"
        assert len(result["sections"]) == 6
        assert len(result["slices"]) >= 2


@pytest.mark.asyncio
@pytest.mark.parametrize("scene", SCENES)
async def test_all_scenes_preserve_exact_copy_table_pixels_and_charge_shared_slot_once(
    tmp_path, scene
):
    # A regression that generates per section, loses copy, or publishes bad pixels fails here.
    request = make_request(scene, tmp_path)
    if scene != "product_detail":
        assert request.product_specs == [] and request.service_notes == []
    provider = OfflineProvider(request)
    with offline_http(provider):
        result = await create_long_image(request, offline_config())
    bundle = verify_delivery(result)
    assert provider.image_posts == 1
    assert len(result.image_assets) == 1
    assert result.sections[0].dependencies == result.sections[1].dependencies == ["shared"]
    assert result.usage.execution == {
        "image_calls": 1,
        "vision_calls": 3 if scene == "product_detail" else 2,
    }
    assert len(provider.posts) == (4 if scene == "product_detail" else 3)
    assert result.usage.display_total == result.usage.execution
    assert [section.role for section in result.plan.sections] == [
        "hero",
        "image_text",
        "bullets",
        "steps",
        "table",
        "closing",
    ]
    drawn = result.page.layout["text_units"]
    cells = [
        unit["text"]
        for unit in drawn
        if unit["section_id"] == "table" and unit["unit_id"] != "table-heading"
    ]
    expected_cells = {
        "product_detail": ["容量", "３５０ mL", "活动日期", "2026年10月12日"],
        "brand_intro": ["理念", "清晰表达", "形式", "文字与插图"],
        "promotion": ["活动日期", "2026年10月12日", "参与方式", "现场交流"],
        "tutorial": ["准备", "清单与收纳盒", "检查", "３ 项逐一核对"],
        "comparison": ["方式", "说明", "顺序整理", "３ 步逐项完成"],
        "custom": ["阅读方式", "先整体后细节", "检查项目", "３ 项内容"],
    }
    assert cells == expected_cells[scene]
    assert [unit["text"] for unit in drawn if "exact-copy" in unit["requirement_ids"]] == [
        "保持原文\n第二行：ＡＢＣ 123"
    ]
    assert sum(unit["text"] == request.title for unit in drawn) == 2
    assert "原样保留：１０月１２日，09:30" in [unit["text"] for unit in drawn]
    required_records = [unit for unit in drawn if unit["requirement_ids"]]
    table_ids = (
        ["spec-0-name", "spec-0-value", "spec-1-name", "spec-1-value", "service-0"]
        if scene == "product_detail"
        else ["table-0", "table-1", "table-2", "table-3"]
    )
    assert [unit["requirement_ids"] for unit in required_records[:-1]] == [
        [rid] for rid in ["title", *table_ids, "exact-copy", "same-title"]
    ]
    assert required_records[0]["source"] == {
        "pointer": "/title",
        "start": None,
        "end": None,
        "model_authored": False,
    }
    exact = next(unit for unit in required_records if unit["requirement_ids"] == ["exact-copy"])
    assert (
        exact["source"]["pointer"] == f"/required_copy/{0 if scene == 'product_detail' else 4}/text"
    )
    brief = required_records[-1]
    assert brief["source"]["pointer"] == "/content_brief"
    assert (
        request.content_brief[brief["source"]["start"] : brief["source"]["end"]]
        == "原样保留：１０月１２日，09:30"
    )
    for unit in drawn:
        assert "".join(line["text"] for line in unit["lines"]) == unit["text"]
    assert all(
        unit["font_size"] >= 26
        for unit in drawn
        if unit["unit_id"] in {"intro-body", "step-1", "step-2", "step-3"}
    )
    assert bundle.dto.image_assets[0].candidates[0].selected is True
    assert bundle.blobs[bundle.dto.page.blob_id] == result.page.image
    if scene == "product_detail":
        assert request.product_context.platform is None
        assert len({hashlib.sha256(m.source.data).hexdigest() for m in request.materials}) == 2
        store = LongImageRunStore(result.run_dir)
        with store.lock():
            materials = store.materials()
            assert [m.sha256 for m in materials] == [
                hashlib.sha256(m.source.data).hexdigest() for m in request.materials
            ]
        store.close()
        assert result.plan.resolved_selection.primary_subject_id == "cup"
        assert result.plan.analysis_ref is not None
    else:
        assert request.product_context is None and request.materials == []
        assert result.image_assets[0].audit.checks[0].kind == "purpose"


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["source", "text"])
async def test_source_and_text_only_use_real_pixels_without_image_or_audit_post(tmp_path, mode):
    request = make_request("tutorial", tmp_path)
    if mode == "source":
        from image_agent.models import MaterialInput
        from scripts.long_image_offline_demo import illustration

        request.materials = [
            MaterialInput(material_id="original", source={"data": illustration("tutorial")})
        ]
    provider = OfflineProvider(request, mode=mode)
    with offline_http(provider):
        result = await create_long_image(request, offline_config())
    verify_delivery(result)
    assert provider.posts == [("/chat/completions", "offline-plan")]
    assert result.usage.execution == {"image_calls": 0, "vision_calls": 1}
    if mode == "source":
        asset = result.image_assets[0]
        assert asset.source_status == "available" and asset.status is None
        assert asset.audit is None and asset.candidates == []
        assert asset.image == request.materials[0].source.data
    else:
        assert result.image_assets == [] and result.page.layout["images"] == []


@pytest.mark.asyncio
async def test_missing_date_and_invalid_plan_never_submit_an_image(tmp_path):
    request = make_request("promotion", tmp_path / "missing")
    request.content_brief = "活动宣传，必须展示活动日期，尚未确定"
    provider = OfflineProvider(request)
    with offline_http(provider):
        missing = await create_long_image(request, offline_config())
    assert missing.status == "needs_input" and missing.page is None
    assert missing.issues[0].field == "content_brief"
    assert provider.posts == [] and missing.usage.execution == {"image_calls": 0, "vision_calls": 0}
    request = make_request("tutorial", tmp_path / "invalid")
    provider = OfflineProvider(request, malformed_plan=True)
    with offline_http(provider):
        invalid = await create_long_image(request, offline_config())
    assert invalid.status == "failed" and invalid.page is None
    assert invalid.error_info.code == "plan_invalid"
    assert provider.image_posts == 0
    assert len(provider.posts) == invalid.usage.execution["vision_calls"] == 4


@pytest.mark.asyncio
async def test_independent_retry_correction_usage_and_trusted_reuse_survive_local_changes(
    tmp_path, monkeypatch
):
    request = make_request("tutorial", tmp_path / "planning")

    class RetryProvider(OfflineProvider):
        def respond(self, http_request):
            if not self.posts:
                self.posts.append((http_request.url.path, "offline-plan"))
                return httpx.Response(503, json={"error": "temporary offline fixture"})
            self.malformed_plan = len(self.posts) == 1
            return super().respond(http_request)

    provider = RetryProvider(request)
    with offline_http(provider):
        planning = await analyze_long_image(request, offline_config())
        assert planning.status == "ready"
        assert len(provider.posts) == 3
        assert planning.usage == {"image_calls": 0, "vision_calls": 3}
        changed = request.model_copy(
            update={
                "output_dir": tmp_path / "delivery",
                "options": request.options.model_copy(
                    update={"width": 600, "theme": "premium", "slice_height": 500}
                ),
            }
        )
        result = await create_long_image(
            changed, offline_config(), plan=planning.plan, planning_record=planning.record
        )
    verify_delivery(result)
    assert len(provider.posts) == 5 and provider.image_posts == 1
    assert result.page.width == 600 and all(slice.height <= 500 for slice in result.slices)
    assert result.usage.execution == {"image_calls": 1, "vision_calls": 1}
    assert result.usage.linked_planning == {
        planning.planning_id: {"image_calls": 0, "vision_calls": 3}
    }
    assert result.usage.display_total == {"image_calls": 1, "vision_calls": 4}

    def forbidden(*args, **kwargs):
        raise AssertionError("local rerender/recovery must not access configuration or HTTP")

    monkeypatch.setattr(AgentConfig, "from_env", forbidden)
    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    rerendered = await render_long_image(
        result.run_dir, render_options={"width": 800, "theme": "warm"}
    )
    recovered = await resume_long_image(result.run_dir)
    verify_delivery(recovered)
    assert recovered.page.width == 800
    assert recovered.page.sha256 == rerendered.page.sha256 != result.page.sha256
    assert recovered.usage == result.usage
    assert long_result_to_bundle(recovered).dto.usage.display_total == {
        "image_calls": 1,
        "vision_calls": 4,
    }


@pytest.mark.asyncio
async def test_same_bytes_failed_audit_acceptance_refreshes_every_derived_artifact(
    tmp_path, monkeypatch
):
    request = make_request("tutorial", tmp_path)
    provider = OfflineProvider(request, audit="fail")
    with offline_http(provider):
        failed = await create_long_image(
            request, offline_config(), policy=ExecutionPolicy(max_quality_repairs=0)
        )
    assert failed.status == "quality_failed" and failed.page.availability == "current"
    with pytest.raises(AgentError):
        await accept_long_image_candidate(failed.run_dir, "shared", "c0001", reason="  ")

    def forbidden(*args, **kwargs):
        raise AssertionError("acceptance is local")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    accepted = await accept_long_image_candidate(
        failed.run_dir, "shared", "c0001", reason="已人工核对离线配图"
    )
    assert accepted.status == "accepted" and accepted.page.sha256 == failed.page.sha256
    assert accepted.page.image == failed.page.image
    assert accepted.delivery_revision == failed.delivery_revision + 1
    assert accepted.image_assets[0].asset_revision == failed.image_assets[0].asset_revision + 1
    assert accepted.image_assets[0].audit.passed is False
    assert accepted.image_assets[0].candidates[0]["acceptance"]["reason"] == "已人工核对离线配图"
    assert accepted.image_assets[0].candidates[0]["acceptance"]["at"]
    assert [a.status for a in accepted.sections] == [
        "accepted",
        "accepted",
        "succeeded",
        "succeeded",
        "succeeded",
        "succeeded",
    ]
    assert all(
        a.status == "accepted" and a.availability == "current"
        for a in [accepted.page, *accepted.slices]
    )
    assert [a.sha256 for a in accepted.sections] == [a.sha256 for a in failed.sections]
    bundle = long_result_to_bundle(accepted)
    assert (
        bundle.dto.page.status == "accepted"
        and bundle.blobs[bundle.dto.page.blob_id] == failed.page.image
    )
    assert bundle.dto.image_assets[0].candidates[0].selected
    assert accepted.usage == failed.usage and len(provider.posts) == 3


@pytest.mark.asyncio
async def test_same_bytes_reaudit_changes_current_metadata_without_second_image_post(tmp_path):
    request = make_request("tutorial", tmp_path)
    provider = OfflineProvider(request, audit="error")
    with offline_http(provider):
        failed = await create_long_image(
            request, offline_config(), policy=ExecutionPolicy(max_audit_retries=0)
        )
        assert failed.status == "audit_error" and failed.page.availability == "current"
        provider.audit = "pass"
        recovered = await resume_long_image(failed.run_dir, offline_config())
    verify_delivery(recovered)
    assert recovered.page.image == failed.page.image and recovered.page.sha256 == failed.page.sha256
    assert recovered.delivery_revision == failed.delivery_revision + 1
    assert recovered.image_assets[0].asset_revision == failed.image_assets[0].asset_revision + 1
    assert recovered.image_assets[0].error_info is None
    assert recovered.image_assets[0].candidates[0].get("error_info") is None
    assert provider.image_posts == 1 and len(provider.posts) == 4
    assert recovered.usage.execution == {"image_calls": 1, "vision_calls": 3}


class Interrupted(BaseException):
    pass


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ["submitted", "response", "saved"])
async def test_raw_and_uncertain_recovery_never_repeat_image_post(tmp_path, monkeypatch, boundary):
    request = make_request("tutorial", tmp_path)
    provider = OfflineProvider(request)
    original = LongImageRunStore.mark_operation
    original_response = provider.respond

    def lost_response(http_request):
        response = original_response(http_request)
        if "/images/" in http_request.url.path:
            raise Interrupted()
        return response

    if boundary == "response":
        provider.respond = lost_response

    def interrupt(store, operation, status, **extra):
        original(store, operation, status, **extra)
        if status == boundary:
            raise Interrupted()

    monkeypatch.setattr(LongImageRunStore, "mark_operation", interrupt)
    with offline_http(provider):
        with pytest.raises(Interrupted):
            await create_long_image(request, offline_config())
        monkeypatch.setattr(LongImageRunStore, "mark_operation", original)
        provider.respond = original_response
        image_posts = provider.image_posts
        recovered = await resume_long_image(next(tmp_path.iterdir()), offline_config())
    assert provider.image_posts == image_posts
    assert recovered.usage.execution["image_calls"] == 1
    if boundary == "saved":
        verify_delivery(recovered)
        assert image_posts == 1 and len(provider.posts) == 3
    else:
        assert recovered.status == "generation_uncertain" and recovered.page is None
        assert image_posts == (1 if boundary == "response" else 0)
        assert len(provider.posts) == (2 if boundary == "response" else 1)


@pytest.mark.asyncio
async def test_product_trusted_snapshot_reuse_skips_discovery_and_keeps_material_hashes(tmp_path):
    request = make_request("product_detail", tmp_path / "planning")
    provider = OfflineProvider(request)
    with offline_http(provider):
        planning = await analyze_long_image(request, offline_config())
        assert planning.status == "ready" and len(provider.posts) == 2
        assert planning.usage == {"image_calls": 0, "vision_calls": 2}
        hashes = [hashlib.sha256(m.source.data).hexdigest() for m in request.materials]
        assert [m.sha256 for m in planning.analysis.materials] == hashes
        changed = request.model_copy(
            update={
                "output_dir": tmp_path / "create",
                "options": request.options.model_copy(
                    update={"width": 820, "theme": "warm", "slice_height": 800}
                ),
            }
        )
        result = await create_long_image(
            changed,
            offline_config(),
            plan=planning.plan,
            analysis=planning.analysis,
            planning_record=planning.record,
        )
        assert provider.posts[-2:] == [
            ("/images/edits", "image"),
            ("/chat/completions", "offline-audit"),
        ]
    verify_delivery(result)
    assert len(provider.posts) == 4 and provider.image_posts == 1
    assert result.plan.analysis_ref == planning.plan.analysis_ref
    assert result.plan.resolved_selection == planning.plan.resolved_selection
    assert result.page.width == 820 and all(slice.height <= 800 for slice in result.slices)
    assert result.usage.execution == {"image_calls": 1, "vision_calls": 1}
    assert result.usage.linked_planning == {
        planning.planning_id: {"image_calls": 0, "vision_calls": 2}
    }
    assert result.usage.display_total == {"image_calls": 1, "vision_calls": 3}


@pytest.mark.asyncio
async def test_failed_slice_save_never_reports_current_success_and_recovers_locally(
    tmp_path, monkeypatch
):
    request = make_request("tutorial", tmp_path)
    provider = OfflineProvider(request, mode="text")
    original = LongImageRunStore.write

    def fail_slice(store, relative, data):
        if relative.startswith("deliveries/") and "/slice/" in relative:
            raise OutputError("offline injected slice write failure")
        return original(store, relative, data)

    monkeypatch.setattr(LongImageRunStore, "write", fail_slice)
    with offline_http(provider):
        failed = await create_long_image(request, offline_config())
    assert failed.status == "failed" and failed.output_errors
    assert failed.page.availability != "current"
    assert long_result_to_bundle(failed).dto.page.blob_id is None
    monkeypatch.setattr(LongImageRunStore, "write", original)

    def forbidden(*args, **kwargs):
        raise AssertionError("saved output recovery must be local")

    monkeypatch.setattr(httpx, "AsyncClient", forbidden)
    monkeypatch.setattr(AgentConfig, "from_env", forbidden)
    recovered = await resume_long_image(failed.run_dir)
    verify_delivery(recovered)
    assert len(provider.posts) == 1 and recovered.usage == failed.usage


@pytest.mark.asyncio
async def test_corrupt_selected_image_invalidates_page_and_withholds_public_blobs(tmp_path):
    request = make_request("tutorial", tmp_path)
    provider = OfflineProvider(request)
    with offline_http(provider):
        result = await create_long_image(request, offline_config())
    (Path(result.run_dir) / result.image_assets[0].file_path).write_bytes(b"corrupt")
    recovered = await resume_long_image(result.run_dir)
    assert recovered.status == "failed" and recovered.output_errors
    assert recovered.page.availability == "stale" and recovered.page.image is None
    bundle = long_result_to_bundle(recovered)
    assert (
        bundle.dto.page.blob_id is None and bundle.dto.image_assets[0].candidates[0].blob_id is None
    )
    assert len(provider.posts) == 3


@pytest.mark.asyncio
async def test_trusted_record_rejects_changed_copy_before_any_generation(tmp_path):
    request = make_request("tutorial", tmp_path / "planning")
    provider = OfflineProvider(request)
    with offline_http(provider):
        planning = await analyze_long_image(request, offline_config())
        request = request.model_copy(
            update={"title": "另一主题", "output_dir": tmp_path / "create"}
        )
        failed = await create_long_image(
            request, offline_config(), plan=planning.plan, planning_record=planning.record
        )
    assert failed.status == "failed" and failed.page is None
    assert failed.error_info.code == "plan_invalid"
    assert len(provider.posts) == 1 and provider.image_posts == 0
