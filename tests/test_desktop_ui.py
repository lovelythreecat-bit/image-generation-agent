import asyncio
import gc
import json
import time
import tkinter as tk

import pytest
from PIL import Image

from image_agent.desktop import DesktopApp
from image_agent.errors import OutputError
from image_agent.models import MaterialAnalysis
from tests.fakes import analysis_data
from tests.test_output import result


@pytest.fixture(scope="module")
def tk_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tmp_path, tk_root):
    root = tk.Toplevel(tk_root)
    root.withdraw()
    window = DesktopApp(root, project_dir=tmp_path)
    window.config_path.set("examples/config.openai.json")
    window.load_config()
    yield window
    window.job.cancel()
    if window.job.thread:
        window.job.thread.join(3)
    if root.winfo_exists():
        window.close()
    # Collect Tk-owned objects on their owning thread before the next worker starts.
    del window
    gc.collect()


def fill(app, tmp_path):
    path = tmp_path / "商品.png"
    Image.new("RGB", (64, 64), "white").save(path)
    app.add_materials([str(path)])
    app.fields["product_name"].set("陶瓷杯")
    app.fields["category"].set("家居")
    return path


def wait_idle(app):
    deadline = time.monotonic() + 4
    while app.busy and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert not app.busy


def test_form_uses_configured_models_and_invalidates_analysis(app, tmp_path):
    path = fill(app, tmp_path)
    assert app.fields["image_model"].get() == "fast"
    request = app.make_request()
    assert request.materials[0].source.path == path
    assert request.platforms == ["taobao"]
    assert request.output_types == ["main_image"]
    app.show_analysis(MaterialAnalysis(**analysis_data()))
    assert app.analysis is not None
    app.fields["product_name"].set("另一个商品")
    assert app.analysis is None


def test_creative_plan_is_readable_and_cleared_when_brief_changes(app, tmp_path):
    from tests.test_creative_planning import creative_plan_data

    fill(app, tmp_path)
    app.brief.insert("1.0", "高级感，适合电商")
    app.show_analysis(
        MaterialAnalysis(**(analysis_data() | {"creative_plan": creative_plan_data()}))
    )
    rendered = app.creative_text.get("1.0", "end")
    assert "用户明确要求" in rendered
    assert "模型补充建议" in rendered
    assert "高级感" in rendered and "柔和侧光" in rendered
    app.brief.insert("end", "，户外场景")
    app.invalidate_analysis()
    assert "柔和侧光" not in app.creative_text.get("1.0", "end")
    assert "重新分析" in app.creative_text.get("1.0", "end")


@pytest.mark.parametrize("saved_result", [False, True])
def test_open_task_without_analysis_clears_previous_creative_plan(app, tmp_path, saved_result):
    from tests.test_creative_planning import creative_plan_data

    fill(app, tmp_path)
    app.show_analysis(
        MaterialAnalysis(**(analysis_data() | {"creative_plan": creative_plan_data()}))
    )
    directory = tmp_path / "other-task"
    directory.mkdir()
    if saved_result:
        (directory / "result.json").write_text(result().model_dump_json(), encoding="utf-8")
    app.load_task(directory)
    assert "柔和侧光" not in app.creative_text.get("1.0", "end")
    assert app.analysis is None and app.analysis_signature is None
    assert not app.needs_input and not app.selection_options


def test_analyze_runs_in_worker_and_displays_result(app, tmp_path, monkeypatch):
    fill(app, tmp_path)
    app.key_fields["official_images"].set("test-image")
    app.key_fields["official_vision"].set("test-vision")

    async def analyze(request, config):
        assert config.services["official_vision"].credential() == "test-vision"
        await asyncio.sleep(0.02)
        return MaterialAnalysis(**analysis_data())

    monkeypatch.setattr("image_agent.desktop.analyze_materials", analyze)
    app.start("analyze")
    assert app.busy
    wait_idle(app)
    assert app.analysis.status == "ready"
    assert "分析完成" in app.status.get()
    assert "ceramic cup" in app.analysis_text.get("1.0", "end")


@pytest.mark.parametrize("detail", [False, True])
def test_white_background_selects_required_platform_and_analyzes(
    app, tmp_path, monkeypatch, detail
):
    fill(app, tmp_path)
    for field in app.key_fields.values():
        field.set("test-key")
    app.outputs["detail_page"].set(detail)
    app.outputs["pdd_white_background"].set(True)
    assert app.platforms["pinduoduo"].get()
    request = app.make_request()
    assert request.platforms == ["taobao", "pinduoduo"]
    assert "pdd_white_background" in request.output_types
    assert ("detail_page" in request.output_types) == detail

    async def analyze(request, config):
        return MaterialAnalysis(**analysis_data())

    monkeypatch.setattr("image_agent.desktop.analyze_materials", analyze)
    app.start("analyze")
    wait_idle(app)
    assert app.analysis is not None and app.analysis.status == "ready"


def test_deselecting_pinduoduo_clears_only_white_background(app, tmp_path):
    fill(app, tmp_path)
    app.platforms["pinduoduo"].set(True)
    app.outputs["detail_page"].set(True)
    app.outputs["pdd_white_background"].set(True)
    app.platforms["pinduoduo"].set(False)
    assert not app.outputs["pdd_white_background"].get()
    request = app.make_request()
    assert request.platforms == ["taobao"]
    assert request.output_types == ["main_image", "detail_page"]


def test_deselecting_white_background_keeps_selected_platforms(app, tmp_path):
    fill(app, tmp_path)
    app.outputs["pdd_white_background"].set(True)
    app.outputs["pdd_white_background"].set(False)
    assert app.make_request().platforms == ["taobao", "pinduoduo"]


def test_worker_error_masks_session_key_and_preserves_form(app, tmp_path, monkeypatch):
    fill(app, tmp_path)
    for field in app.key_fields.values():
        field.set("session-secret-value")

    async def fail(request, config):
        raise RuntimeError("problem session-secret-value")

    monkeypatch.setattr("image_agent.desktop.analyze_materials", fail)
    app.start("analyze")
    wait_idle(app)
    rendered = app.json_text.get("1.0", "end")
    assert "session-secret-value" not in rendered
    assert "[redacted]" in rendered
    assert app.fields["product_name"].get() == "陶瓷杯"


def test_cancel_keeps_ui_responsive_until_cleanup(app, tmp_path, monkeypatch):
    fill(app, tmp_path)
    for field in app.key_fields.values():
        field.set("test-key")

    async def pending(request, config):
        await asyncio.sleep(60)

    monkeypatch.setattr("image_agent.desktop.analyze_materials", pending)
    app.start("analyze")
    app.cancel()
    wait_idle(app)
    assert "已取消" in app.status.get()


def test_material_count_is_limited_without_erasing_existing_inputs(app, tmp_path):
    fill(app, tmp_path)
    app.add_materials([str(tmp_path / f"{i}.png") for i in range(8)])
    assert len(app.materials) == 1
    assert "8" in app.status.get()


def test_saved_result_opens_its_actual_run_directory(app, tmp_path):
    saved = result()
    run = tmp_path / "out" / "automatic-run"
    saved.assets[0].file_path = str(run / "taobao" / "main_image.jpg")
    app.show_result(saved)
    assert app.output_folder == run
    assert app.preview_image is not None
    assert "全部成功" in app.status.get()
    assert json.loads(app.json_text.get("1.0", "end"))["assets"][0]["status"] == "succeeded"


def test_manifest_failure_keeps_result_diagnostics(app):
    app.show_error(OutputError("manifest failed", result=result()))
    payload = json.loads(app.json_text.get("1.0", "end"))
    assert payload["error"]["code"] == "output"
    assert payload["result"]["assets"][0]["status"] == "succeeded"
    assert app.preview_image is not None


def test_failed_asset_shows_diagnostics_without_opening_json(app):
    failed = result()
    failed.status = "failed"
    asset = failed.assets[0]
    asset.status = "failed"
    asset.image = None
    asset.error = "invalid image base64; data[0].b64_json: empty; chars=0"
    app.show_result(failed)
    assert app.notebook.index(app.notebook.select()) == 0
    assert app.result_tree.selection() == (asset.asset_id,)
    assert asset.error in app.preview.cget("text")
    assert "生成失败" in app.preview.cget("text")
    assert app.preview_image is None


def test_failed_audit_candidate_is_previewed_with_a_persistent_warning(app):
    candidate = result()
    candidate.status = candidate.assets[0].status = "failed"
    candidate.assets[0].error = "audit_image: fact_checks missing"
    app.show_result(candidate)
    assert app.preview_image is not None
    assert "未通过审核" in app.asset_notice.cget("text")
    assert "fact_checks" in app.asset_notice.cget("text")
    app._resize_preview()
    assert "未通过审核" in app.asset_notice.cget("text")
    assert "候选图 1" in app.status.get()
    app.show_result(result())
    assert app.asset_notice.cget("text") == ""


def test_clarification_resumes_with_selected_snapshot(app, tmp_path, monkeypatch):
    fill(app, tmp_path)
    for field in app.key_fields.values():
        field.set("test-key")
    data = analysis_data()
    data["status"] = "needs_input"
    data["issues"] = [
        {
            "issue_id": "pick",
            "code": "pick_subject",
            "resolution": "selection",
            "message": "请选择主体",
            "options": [
                {
                    "id": "cup",
                    "label": "使用杯子",
                    "selection": {"mode": "explicit", "subject_ids": ["s1"]},
                }
            ],
        }
    ]
    snapshot = MaterialAnalysis(**data)
    app.show_analysis(snapshot)
    app.choice.current(1)

    async def create(request, config, *, analysis, on_progress=None):
        assert analysis is snapshot
        assert request.selection.subject_ids == ["s1"]
        assert request.selection.mode == "explicit"
        return result()

    monkeypatch.setattr("image_agent.desktop.create_images", create)
    app.start("create")
    wait_idle(app)
    assert "全部成功" in app.status.get()
    assert app.preview_image is not None


def test_clearing_materials_clears_stale_preview(app, tmp_path):
    fill(app, tmp_path)
    assert app.preview_image is not None
    app.material_list.selection_set(0)
    app.remove_materials()
    assert not app.materials
    assert app.preview_image is None


def test_close_removes_poll_callback(app):
    timer_ids = app.root.tk.call("after", "info")
    assert timer_ids
    app.close()
    assert not app.root.tk.call("after", "info")


def test_desktop_generates_and_saves_through_real_pipeline(app, tmp_path, monkeypatch):
    from image_agent.images import load_image
    from image_agent.pipeline import Dependencies
    from tests.fakes import FakeGenerator, FakeVision

    fill(app, tmp_path)
    for field in app.key_fields.values():
        field.set("test-key")
    dependencies = Dependencies(vision=FakeVision(), generator=FakeGenerator(), load=load_image)
    monkeypatch.setattr("image_agent.pipeline._dependencies", lambda *args: dependencies)
    app.start("create")
    wait_idle(app)
    assert "全部成功" in app.status.get(), app.json_text.get("1.0", "end")
    assert (app.output_folder / "result.json").is_file()
    assert (app.output_folder / "approved" / "taobao" / "main_image" / "default.png").is_file()
    assert app.preview_image is not None
    assert len(app.result_tree.get_children()) == 1


@pytest.mark.parametrize("location", ["intent", "result"])
def test_clarification_includes_all_core_issue_sources(app, location):
    issue = {
        "issue_id": "pick",
        "code": "pick_subject",
        "resolution": "selection",
        "message": "请选择主体",
        "options": [
            {
                "id": "cup",
                "label": "使用杯子",
                "selection": {"mode": "explicit", "subject_ids": ["s1"]},
            }
        ],
    }
    data = analysis_data()
    if location == "intent":
        data["status"] = "needs_input"
        data["intent"]["unmet_requirements"] = [issue]
        app.show_analysis(MaterialAnalysis(**data))
    else:
        from image_agent.models import Issue

        pending = result()
        pending.status = "needs_input"
        pending.analysis = MaterialAnalysis(**data)
        pending.issues = [Issue(**issue)]
        app.show_result(pending)
    assert len(app.selection_options) == 1
    assert app.selection_options[0].subject_ids == ["s1"]
    assert "请选择主体" in app.analysis_text.get("1.0", "end")


def test_progress_updates_on_main_thread_and_retains_stage(app):
    import threading

    calls = []
    original = app.status.set
    app.status.set = lambda value: (calls.append(threading.get_ident()), original(value))[-1]
    app.job.report_progress(
        {"stage": "audit", "asset_id": "a1", "attempt": 2, "message": "checking", "run_dir": None}
    )
    app.root.after_cancel(app.poll_timer)
    app.poll()
    assert calls == [threading.get_ident()]
    assert "审核" in app.status.get() and "2" in app.status.get()


def test_saved_candidates_can_be_reviewed_and_explicitly_accepted(app, tmp_path, monkeypatch):
    r = result()
    r.status = r.assets[0].status = "quality_failed"
    r.run_dir = str(tmp_path)
    r.assets[0].stop_reason = "no_improvement"
    path = tmp_path / "candidate.jpg"
    path.write_bytes(r.assets[0].image)
    r.assets[0].candidates = [
        {
            "candidate_id": "c0001",
            "index": 1,
            "status": "quality_failed",
            "file_path": "candidate.jpg",
        }
    ]
    app.show_result(r)
    assert "no_improvement" in app.asset_notice.cget("text")
    assert app.candidate_choice.current() == 0
    app.accept_selected()
    assert "原因" in app.status.get() and not app.busy
    app.accept_reason.set("商品外观经人工检查可用")

    async def accept(run_dir, asset_id, candidate_id, *, reason):
        assert run_dir == tmp_path
        assert candidate_id == "c0001" and reason == "商品外观经人工检查可用"
        r.status = r.assets[0].status = "accepted"
        return r

    monkeypatch.setattr("image_agent.desktop.accept_candidate", accept)
    app.accept_selected()
    wait_idle(app)
    assert "人工接受" in app.status.get()
    assert "自动审核" in app.asset_notice.cget("text")
    assert "成功 0/" in app.status.get()


def test_resume_uses_current_config_without_requiring_current_materials(app, tmp_path, monkeypatch):
    app.run_dir = tmp_path
    app.key_fields["official_vision"].set("current-key")

    async def resume(run_dir, config=None, *, on_progress=None):
        assert (
            run_dir == tmp_path and config.services["official_vision"].credential() == "current-key"
        )
        on_progress(
            {
                "stage": "audit",
                "asset_id": "a",
                "attempt": 1,
                "message": "recheck",
                "run_dir": str(tmp_path),
            }
        )
        r = result()
        r.run_dir = str(tmp_path)
        return r

    monkeypatch.setattr("image_agent.desktop.resume_images", resume)
    app.start("resume")
    wait_idle(app)
    assert "全部成功" in app.status.get()
    assert not app.materials


def test_opening_bad_task_does_not_rebind_old_candidates(app, tmp_path):
    r = result()
    r.run_dir = str(tmp_path / "old")
    r.assets[0].candidates = [
        {
            "candidate_id": "c0001",
            "index": 1,
            "status": "quality_failed",
            "file_path": "candidate.jpg",
        }
    ]
    app.show_result(r)
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "result.json").write_text("{bad json", encoding="utf-8")
    app.load_task(bad)
    assert app.run_dir != bad
    assert str(app.accept_button.cget("state")) == "disabled"


def test_open_v1_task_shows_saved_image_without_generation(app, tmp_path):
    r = result()
    (tmp_path / "image.jpg").write_bytes(r.assets[0].image)
    payload = r.model_dump(mode="json")
    payload["schema_version"] = "1.0"
    payload["assets"][0].update(image="image.jpg", file_path="image.jpg")
    (tmp_path / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    app.load_task(tmp_path)
    assert app.preview_image is not None and not app.job.running
    assert "旧版任务仅支持查看" in app.status.get()


def test_start_new_task_drops_previous_candidate_binding(app, tmp_path, monkeypatch):
    fill(app, tmp_path)
    for field in app.key_fields.values():
        field.set("test-key")
    old = result()
    old.run_dir = str(tmp_path / "old")
    old.assets[0].candidates = [
        {
            "candidate_id": "c0001",
            "index": 1,
            "status": "quality_failed",
            "file_path": "candidate.jpg",
        }
    ]
    app.show_result(old)

    async def pending(request, config, *, analysis, on_progress):
        on_progress({"stage": "prepare", "run_dir": str(tmp_path / "new")})
        await asyncio.sleep(60)

    monkeypatch.setattr("image_agent.desktop.create_images", pending)
    app.start("create")
    app.cancel()
    wait_idle(app)
    assert not app.candidate_options and not app.assets
    assert str(app.accept_button.cget("state")) == "disabled"


def test_accepted_older_candidate_stays_selected_and_history_can_be_previewed(app, tmp_path):
    from io import BytesIO

    r = result()
    r.status = r.assets[0].status = "accepted"
    r.run_dir = str(tmp_path)
    images = []
    for name, color in [("old.png", "red"), ("new.png", "blue")]:
        data = BytesIO()
        Image.new("RGB", (32, 32), color).save(data, format="PNG")
        (tmp_path / name).write_bytes(data.getvalue())
        images.append(data.getvalue())
    r.assets[0].image = images[0]
    r.assets[0].candidates = [
        {
            "candidate_id": "c0001",
            "index": 1,
            "status": "accepted",
            "file_path": "old.png",
            "selected": True,
        },
        {
            "candidate_id": "c0002",
            "index": 2,
            "status": "accepted",
            "file_path": "new.png",
            "selected": False,
            "acceptance": {"at": "2026-09-30T00:00:00+00:00"},
        },
    ]
    app.show_result(r)
    assert app.candidate_choice.current() == 0
    assert app.preview_image.getpixel((0, 0)) == (255, 0, 0)
    app.candidate_choice.current(1)
    app.preview_candidate()
    assert app.preview_image.getpixel((0, 0)) == (0, 0, 255)
    assert "c0002" in app.asset_notice.cget("text")
