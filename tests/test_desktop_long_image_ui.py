import asyncio
import gc
import io
import time
import tkinter as tk

import pytest
from PIL import Image

from image_agent.desktop import DesktopApp
from image_agent.long_image_models import (
    LongImageArtifact,
    LongImagePlanningResult,
    LongImageResult,
    LongSlotAsset,
)
from image_agent.models import MaterialAnalysis
from tests.fakes import analysis_data
from tests.test_long_image_pipeline import request_plan


@pytest.fixture(scope="module")
def tk_root():
    root = tk.Tk()
    root.withdraw()
    yield root
    root.destroy()


@pytest.fixture
def app(tk_root, tmp_path):
    root = tk.Toplevel(tk_root)
    window = DesktopApp(root, project_dir=tmp_path)
    window.set_mode("long_image")
    root.update()
    yield window
    window.job.cancel()
    if window.job.thread:
        window.job.thread.join(3)
    window.close()
    del window
    gc.collect()


def fill(panel):
    panel.fields["title"].set("泡茶教程")
    panel.brief.insert("1.0", "先温杯，再冲泡。")
    panel.app.root.update()


def wait_idle(app):
    deadline = time.monotonic() + 8
    while app.busy and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert not app.busy, app.status.get()


def preview_result(tmp_path, status="pending_audit"):
    data = io.BytesIO()
    Image.new("RGB", (750, 6000), "white").save(data, format="PNG")
    return LongImageResult(
        status=status,
        run_dir=str(tmp_path),
        page=LongImageArtifact(
            artifact_id="page",
            kind="page",
            status=status,
            availability="current",
            width=750,
            height=6000,
            image=data.getvalue(),
        ),
    )


def test_modes_preserve_fields_but_clear_stale_plan(app, tmp_path):
    panel = app.long_panel
    fill(panel)
    _, plan = request_plan(tmp_path, slots=False)
    panel.show_planning(LongImagePlanningResult(planning_id="p1", status="ready", plan=plan))
    app.fields["product_name"].set("原商品")
    app.set_mode("product")
    app.set_mode("long_image")
    assert panel.fields["title"].get() == "泡茶教程"
    assert app.fields["product_name"].get() == "原商品"
    assert panel.planning is None
    assert str(app.generate_button.cget("state")) == "disabled"


def test_nonproduct_hides_product_requirements(app):
    panel = app.long_panel
    fill(panel)
    assert panel.make_request().product_context is None
    assert panel.make_request().materials == []
    assert not panel.product_frame.winfo_ismapped()
    panel.scene.set("商品详情")
    app.root.update()
    assert panel.product_frame.winfo_ismapped()
    with pytest.raises(ValueError, match="素材"):
        panel.make_request()


def test_unready_plan_blocks_generation_without_config(app):
    fill(app.long_panel)
    app.start("create")
    assert not app.busy and "规划" in app.status.get()


def test_planning_history_deduplicates_missing_usage(app):
    panel = app.long_panel
    p = LongImagePlanningResult(planning_id="p1", status="needs_input", usage={"vision_calls": 2})
    panel.show_planning(p)
    panel.show_planning(p)
    panel.show_planning(LongImagePlanningResult(planning_id="p2", status="failed"))
    text = panel.planning_text.get("1.0", "end")
    assert len(panel.planning_history) == 2
    assert "2" in text and "不可用" in text


def test_selection_submits_same_analysis(app, tmp_path, monkeypatch):
    panel = app.long_panel
    fill(panel)
    path = tmp_path / "tea.png"
    Image.new("RGB", (64, 64)).save(path)
    panel.add_materials([path])
    panel.scene.set("商品详情")
    panel.fields["product_name"].set("茶杯")
    panel.fields["category"].set("茶具")
    snapshot = MaterialAnalysis(**analysis_data())
    from image_agent.long_image_models import LongImageIssue

    panel.show_planning(
        LongImagePlanningResult(
            planning_id="p1",
            status="needs_input",
            analysis=snapshot,
            issues=[
                LongImageIssue(
                    issue_id="pick",
                    code="pick",
                    message="请选择主体",
                    resolution="selection",
                    options=[
                        {"label": "杯子", "selection": {"mode": "explicit", "subject_ids": ["s1"]}}
                    ],
                )
            ],
        )
    )
    panel.choice.current(1)
    monkeypatch.setattr(app, "session_config", lambda: object())

    async def analyze(request, config=None, *, analysis=None, on_progress=None):
        assert analysis is snapshot and request.selection.subject_ids == ["s1"]
        _, plan = request_plan(tmp_path, slots=False)
        return LongImagePlanningResult(
            planning_id="p2", status="ready", plan=plan, analysis=analysis
        )

    monkeypatch.setattr("image_agent.desktop_long_image.analyze_long_image", analyze)
    app.start("analyze")
    wait_idle(app)
    assert len(panel.planning_history) == 2
    assert panel.make_request().selection.subject_ids == ["s1"]


def test_preview_scroll_warning_and_zoom_are_local(app, tmp_path):
    panel = app.long_panel
    panel.show_result(preview_result(tmp_path))
    app.root.update()
    canvas = panel.preview_canvas
    assert canvas.yview()[1] < 1
    before = app.canvas.yview()
    canvas.event_generate("<MouseWheel>", delta=-120)
    app.root.update()
    assert canvas.yview()[0] > 0 and app.canvas.yview() == before
    canvas.focus_force()
    canvas.event_generate("<End>")
    app.root.update()
    assert canvas.yview()[1] == 1
    panel.zoom.set("50%")
    panel.refresh_preview()
    assert "待审核" in panel.notice.cget("text")
    panel.zoom.set("150%")
    panel.refresh_preview()
    assert panel.preview_size == (1125, 9000)
    assert len(panel.thumbnail_cache) <= 4


def test_cancel_single_worker_keeps_busy_until_cleanup(app, monkeypatch):
    fill(app.long_panel)
    monkeypatch.setattr(app, "session_config", lambda: object())
    settled = []

    async def analyze(*args, **kwargs):
        try:
            await asyncio.sleep(60)
        finally:
            await asyncio.sleep(0.05)
            settled.append(True)

    monkeypatch.setattr("image_agent.desktop_long_image.analyze_long_image", analyze)
    app.start("analyze")
    time.sleep(0.03)
    thread = app.job.thread
    app.start("analyze")
    app.cancel()
    assert app.busy and app.job.thread is thread
    wait_idle(app)
    assert settled == [True] and "已取消" in app.status.get()


def test_accept_visible_reason_no_config_and_failed_load_unbinds(app, tmp_path, monkeypatch):
    panel = app.long_panel
    result = preview_result(tmp_path, "quality_failed")
    data = result.page.image
    (tmp_path / "candidate.png").write_bytes(data)
    result.image_assets = [
        LongSlotAsset(
            slot_id="slot",
            source="generate",
            status="quality_failed",
            image=data,
            candidates=[
                {"candidate_id": "c1", "status": "quality_failed", "file_path": "candidate.png"}
            ],
        )
    ]
    panel.show_result(result)
    panel.view.set("原始配图")
    panel.refresh_items()
    assert panel.reason_entry.winfo_ismapped()
    panel.accept_selected()
    assert "原因" in app.status.get() and not app.busy
    panel.accept_reason.set("人工核对可用")
    monkeypatch.setattr(
        app, "session_config", lambda: pytest.fail("local acceptance requested config")
    )

    async def accept(run_dir, slot_id, candidate_id, *, reason):
        assert (run_dir, slot_id, candidate_id, reason) == (tmp_path, "slot", "c1", "人工核对可用")
        result.status = "accepted"
        return result

    monkeypatch.setattr("image_agent.desktop_long_image.accept_long_image_candidate", accept)
    panel.accept_selected()
    wait_idle(app)
    assert "人工接受" in app.status.get()
    bad = tmp_path / "bad"
    bad.mkdir()
    app.load_task(bad)
    assert panel.run_dir is None and str(panel.accept_button.cget("state")) == "disabled"


def test_rebuild_uses_background_and_preserves_render_overrides(app, tmp_path, monkeypatch):
    panel = app.long_panel
    panel.show_result(preview_result(tmp_path))
    panel.fields["width"].set("900")
    panel.fields["slice_height"].set("1800")
    panel.fields["font_path"].set("C:/Windows/Fonts/msyh.ttc")
    panel.theme.set("温暖")
    monkeypatch.setattr(
        app, "session_config", lambda: pytest.fail("local rebuild requested config")
    )

    async def render(run_dir, *, render_options, on_progress):
        assert render_options == {
            "width": 900,
            "theme": "warm",
            "slice_height": 1800,
            "font_path": "C:/Windows/Fonts/msyh.ttc",
        }
        return preview_result(tmp_path)

    monkeypatch.setattr("image_agent.desktop_long_image.render_long_image", render)
    panel.rebuild_saved()
    assert app.busy
    wait_idle(app)
    assert panel.fields["width"].get() == "900"


def test_saved_local_zero_slot_create_and_load_need_no_config(app, tmp_path, monkeypatch):
    from image_agent.long_image_runtime import create_long_image, render_long_image

    request, plan = request_plan(tmp_path / "saved", slots=False)
    saved = asyncio.run(create_long_image(request, plan=plan))
    saved = asyncio.run(
        render_long_image(
            saved.run_dir,
            render_options={
                "width": 850,
                "theme": "warm",
                "slice_height": 1700,
                "font_path": "C:/Windows/Fonts/msyh.ttc",
            },
        )
    )
    monkeypatch.setattr(
        app, "session_config", lambda: pytest.fail("local operation requested config")
    )
    app.load_task(saved.run_dir)
    panel = app.long_panel
    assert panel.fields["width"].get() == "850" and panel.theme.get() == "温暖"
    assert panel.fields["slice_height"].get() == "1700"
    assert str(panel.brief.cget("state")) == "normal"
    panel.rebuild_saved()
    wait_idle(app)
    assert panel.result.page.width == 850 and panel.result.usage == saved.usage
    panel.show_planning(LongImagePlanningResult(planning_id="new-plan", status="ready", plan=plan))
    app.start("create")
    wait_idle(app)
    assert panel.result.status == "succeeded"


def test_preview_switches_semantic_sections_slices_and_source_without_fake_audit(app, tmp_path):
    panel = app.long_panel
    result = preview_result(tmp_path, "accepted")
    result.sections = [
        LongImageArtifact(
            artifact_id="section-one",
            kind="section",
            status="accepted",
            availability="stale",
            width=750,
            height=6000,
            image=result.page.image,
        )
    ]
    result.slices = [
        LongImageArtifact(
            artifact_id="slice-one",
            kind="slice",
            status="accepted",
            availability="current",
            width=750,
            height=6000,
            image=result.page.image,
        )
    ]
    result.image_assets = [
        LongSlotAsset(
            slot_id="source", source="source", source_status="available", image=result.page.image
        )
    ]
    panel.show_result(result)
    panel.view.set("语义段落")
    panel.refresh_items()
    assert panel.item_choice.get() == "section-one"
    assert "旧版产物" in panel.notice.cget("text") and "人工接受" in panel.notice.cget("text")
    panel.view.set("切片")
    panel.refresh_items()
    assert panel.item_choice.get() == "slice-one"
    panel.view.set("原始配图")
    panel.refresh_items()
    assert panel.item_choice.get() == "source" and not panel.candidates
    assert str(panel.accept_button.cget("state")) == "disabled"


def test_required_copy_preview_has_source_ids_and_counts(app, tmp_path):
    from image_agent.long_image_models import LongImageRequirement, LongTextSource

    _, plan = request_plan(tmp_path, slots=False)
    plan.requirements = [
        *plan.requirements,
        LongImageRequirement(
            requirement_id="r1",
            kind="page_text",
            text="2026年10月9日",
            source=LongTextSource(pointer="/required_copy/0/text"),
        ),
    ]
    app.long_panel.show_planning(
        LongImagePlanningResult(
            planning_id="p1", status="ready", plan=plan, usage={"vision_calls": 1}
        )
    )
    text = app.long_panel.planning_text.get("1.0", "end")
    assert "r1" in text and "/required_copy/0/text" in text and "2026年10月9日" in text
    assert "生成配图 0" in text and str(app.generate_button.cget("state")) == "normal"
    app.long_panel.fields["title"].set("修改标题")
    assert app.long_panel.planning is None


def test_tk_form_wheel_and_keyboard_focus_do_not_scroll_preview(app, tmp_path):
    panel = app.long_panel
    panel.show_result(preview_result(tmp_path))
    app.root.update()
    before = panel.preview_canvas.yview()
    panel.entries["title"].event_generate("<MouseWheel>", delta=-120)
    app.root.update()
    assert app.canvas.yview()[0] > 0 and panel.preview_canvas.yview() == before
    panel.preview_canvas.focus_force()
    app.root.update()
    assert app.root.focus_get() == panel.preview_canvas
    assert panel.preview_canvas.cget("takefocus") == "1"


def test_independent_planning_progress_never_rebinds_saved_candidate_actions(app, tmp_path):
    panel = app.long_panel
    panel.show_result(preview_result(tmp_path / "saved"))
    app.operation = "analyze"
    app.show_progress(
        {"stage": "plan", "run_dir": str(tmp_path / "planning"), "message": "正在规划"}
    )
    assert panel.run_dir == tmp_path / "saved"
    assert app.run_dir == tmp_path / "saved"


def test_actual_saved_history_preview_accept_and_render_preserve_bytes_and_options(
    app, tmp_path, monkeypatch
):
    from image_agent.config import AgentConfig
    from image_agent.long_image_runtime import execute_long_image, render_long_image
    from tests.test_long_image_pipeline import Transport, deps_for

    class ColorTransport(Transport):
        async def post_json(self, path, payload):
            if path == "/images":
                out = io.BytesIO()
                color = "red" if not any(p == "/images" for p, _ in self.posts) else "blue"
                Image.new("RGB", (768, 768), color).save(out, "PNG")
                self.image = out.getvalue()
            return await super().post_json(path, payload)

    request, plan = request_plan(tmp_path / "run")
    transport = ColorTransport(audits=[False, True])
    result = asyncio.run(
        execute_long_image(request, AgentConfig(), plan=plan, dependencies=deps_for(transport))
    )
    result = asyncio.run(
        render_long_image(result.run_dir, render_options={"width": 880, "theme": "warm"})
    )
    app.load_task(result.run_dir)
    panel = app.long_panel
    panel.view.set("原始配图")
    panel.refresh_items()
    assert len(panel.candidates) == 2
    assert Image.open(io.BytesIO(panel.preview_data)).getpixel((0, 0)) == (0, 0, 255)
    panel.candidate_choice.current(0)
    panel.refresh_preview()
    assert Image.open(io.BytesIO(panel.preview_data)).getpixel((0, 0)) == (255, 0, 0)
    panel.accept_reason.set("人工确认第一张可用")
    monkeypatch.setattr(app, "session_config", lambda: pytest.fail("local accept requested config"))
    panel.accept_selected()
    wait_idle(app)
    assert panel.result.status == "accepted"
    assert panel.result.page.width == 880 and panel.theme.get() == "温暖"
    assert panel.result.usage == result.usage
    assert panel.candidate_choice.current() == 0
    assert "人工接受" in panel.notice.cget("text")


def test_invalid_preview_cannot_redisplay_previous_candidate_on_scroll(app, tmp_path):
    panel = app.long_panel
    panel.show_result(preview_result(tmp_path))
    bad = preview_result(tmp_path)
    bad.page.image = b"invalid png"
    panel.show_result(bad)
    panel.preview_canvas.yview_moveto(0.5)
    app.root.update()
    assert panel.preview_data is None and panel.photo is None
    assert not any(
        panel.preview_canvas.type(item) == "image" for item in panel.preview_canvas.find_all()
    )
