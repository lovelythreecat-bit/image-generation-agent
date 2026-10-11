import gc
import io
import threading
import time
import tkinter as tk

import pytest
from PIL import Image, ImageFile

from image_agent.desktop import DesktopApp
from image_agent.models import (
    Asset,
    CreationResult,
    MaterialAnalysis,
    ProductInputAudit,
    SelectionSpec,
)
from tests.fakes import analysis_data
from tests.test_desktop_ui import fill, wait_idle


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
    window.config_path.set("examples/config.openai.json")
    window.load_config()
    root.update()
    yield window
    window.job.cancel()
    if window.job.thread:
        window.job.thread.join(3)
    if root.winfo_exists():
        window.close()
    if hasattr(window, "product_long_preview"):
        worker = window.product_long_preview._preview_worker.thread
        if worker:
            worker.join(3)
            assert not worker.is_alive()
    del window
    gc.collect()


def picture(color="white"):
    data = io.BytesIO()
    Image.new("RGB", (750, 6000), color).save(data, "PNG")
    return data.getvalue()


def long_result(tmp_path, color="white", status="quality_failed"):
    return CreationResult(
        status="partial",
        input_check=ProductInputAudit(matches=True, observed_product="cup", reason="ok"),
        run_dir=str(tmp_path),
        assets=[
            Asset(
                asset_id="taobao.long_image.default",
                platform="taobao",
                output_type="long_image",
                model="test",
                status=status,
                image=picture(color),
                error="文案待核对",
                candidates=[
                    {
                        "candidate_id": "c1",
                        "status": status,
                        "file_path": "candidate.png",
                        "acceptance": {"reason": "人工核对可用"},
                    }
                ],
            )
        ],
    )


def wait_preview(app):
    deadline = time.monotonic() + 8
    while app.product_long_preview.photo is None and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert app.product_long_preview.photo is not None, app.asset_notice.cget("text")


@pytest.mark.parametrize("other", ["main_image", "detail_page", "pdd_white_background"])
def test_long_checkbox_shows_parameters_without_mode_switch(app, tmp_path, other):
    path = fill(app, tmp_path)
    app.root.update()
    analysis = MaterialAnalysis(**analysis_data())
    app.show_analysis(analysis)
    controls = app.product_long_controls
    assert not controls.frame.winfo_ismapped()
    for var in app.outputs.values():
        var.set(False)
    app.outputs[other].set(True)
    app.outputs["long_image"].set(True)
    app.root.update()
    assert controls.frame.winfo_ismapped()
    assert app.mode == "product" and app.materials == [path] and app.analysis is analysis
    assert set(app.make_request().output_types) == {other, "long_image"}
    assert not app.mode_choice.winfo_ismapped()


def test_long_request_serializes_exact_options_and_preserves_hidden_inputs(app, tmp_path):
    fill(app, tmp_path)
    app.outputs["long_image"].set(True)
    controls = app.product_long_controls
    assert app.make_request().long_image.model_dump() == {
        "width": 750,
        "segment_count": 6,
        "segment_aspect_ratio": "3:4",
        "known_info": "",
        "required_copy": [],
    }
    controls.fields["width"].set("900")
    controls.fields["segment_count"].set("5")
    controls.fields["segment_aspect_ratio"].set("4:5")
    controls.known_info.insert("1.0", "  陶瓷\n容量 500ml  ")
    controls.copy_text.insert("1.0", "  2026 年限定  \n买 2 送 1！")
    app.root.update()
    analysis = MaterialAnalysis(**analysis_data())
    app.show_analysis(analysis)
    controls.fields["width"].set("1000")
    assert app.analysis is analysis
    options = app.make_request().long_image
    assert (options.width, options.segment_count, options.segment_aspect_ratio) == (1000, 5, "4:5")
    assert options.known_info == "  陶瓷\n容量 500ml  "
    assert options.required_copy == ["  2026 年限定  ", "买 2 送 1！"]
    app.outputs["long_image"].set(False)
    assert app.make_request().long_image is None
    app.outputs["long_image"].set(True)
    assert app.make_request().long_image == options


@pytest.mark.parametrize(
    "field,value", [("width", "bad"), ("width", "399"), ("segment_count", "13")]
)
def test_invalid_long_parameters_block_before_loading_config_or_worker(
    app, tmp_path, monkeypatch, field, value
):
    fill(app, tmp_path)
    app.outputs["long_image"].set(True)
    app.product_long_controls.fields[field].set(value)
    monkeypatch.setattr(
        app, "session_config", lambda: pytest.fail("invalid parameters loaded config")
    )
    app.start("create")
    assert not app.busy and not app.job.running
    assert app.product_long_controls.error.get()
    assert value == app.product_long_controls.fields[field].get()


def test_create_passes_analysis_selection_for_mixed_long(app, tmp_path, monkeypatch):
    fill(app, tmp_path)
    app.root.update()
    for field in app.key_fields.values():
        field.set("test-key")
    app.outputs["long_image"].set(True)
    analysis = MaterialAnalysis(**analysis_data())
    app.show_analysis(analysis)
    app.selection_options = [SelectionSpec(mode="explicit", subject_ids=["s1"])]
    app.choice.configure(values=("自动选择", "陶瓷杯"))
    app.choice.current(1)
    seen = []

    async def create(request, *, config, analysis, on_progress):
        seen.append((request, analysis))
        return CreationResult(
            status="failed",
            input_check=ProductInputAudit(matches=True, observed_product="cup", reason="ok"),
        )

    monkeypatch.setattr("image_agent.desktop.create_images", create)
    app.start("create")
    wait_idle(app)
    assert len(seen) == 1 and seen[0][1] is analysis
    request = seen[0][0]
    assert request.selection.subject_ids == ["s1"]
    assert request.output_types == ["main_image", "long_image"]
    assert request.aspect_ratio == "auto" and request.long_image.width == 750


def test_long_asset_scroll_zoom_and_candidate_notice(app, tmp_path, monkeypatch):
    result = long_result(tmp_path)
    (tmp_path / "candidate.png").write_bytes(result.assets[0].image)
    threads = []
    original = Image.Image.load

    def tracked(image, *args, **kwargs):
        if image.size == (750, 6000):
            threads.append(threading.get_ident())
        return original(image, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", tracked)
    app.show_result(result)
    wait_preview(app)
    preview = app.product_long_preview
    assert threads and threading.get_ident() not in threads
    assert preview.preview_size[1] > preview.preview_canvas.winfo_height()
    assert preview.photo.width() == preview.preview_size[0]
    assert "候选图" in app.asset_notice.cget("text") and "未通过审核" in app.asset_notice.cget(
        "text"
    )
    preview.preview_canvas.focus_force()
    preview.preview_canvas.event_generate("<End>")
    app.root.update()
    assert preview.preview_canvas.yview()[0] > 0
    assert app.root.focus_get() == preview.preview_canvas
    preview.zoom.set("100%")
    preview.refresh_preview()
    wait_preview(app)
    assert preview.preview_size == (750, 6000)
    app.preview_candidate()
    wait_preview(app)
    assert "接受原因：人工核对可用" in app.asset_notice.cget("text")


@pytest.mark.parametrize("switch", ["asset", "candidate", "invalid", "ordinary"])
def test_late_decode_cannot_replace_new_asset(app, tmp_path, monkeypatch, switch):
    entered, release = threading.Event(), threading.Event()
    original = ImageFile.ImageFile.load

    def blocked(image, *args, **kwargs):
        if image.size == (750, 6000) and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(image, *args, **kwargs)

    monkeypatch.setattr(ImageFile.ImageFile, "load", blocked)
    app.show_result(long_result(tmp_path))
    # Drain the programmatic Treeview selection before simulating the next user action.
    app.root.update()
    deadline = time.monotonic() + 3
    while not entered.is_set() and time.monotonic() < deadline:
        app.root.update()
        time.sleep(0.01)
    assert entered.is_set()
    try:
        if switch == "asset":
            app.show_result(long_result(tmp_path, "blue"))
        elif switch == "candidate":
            (tmp_path / "candidate.png").write_bytes(picture("blue"))
            app.preview_candidate()
        elif switch == "invalid":
            app._load_preview(io.BytesIO(b"bad png"), long_image=True)
        else:
            from tests.test_output import result

            app.show_result(result())
    finally:
        release.set()
    preview = app.product_long_preview
    if switch in ("asset", "candidate"):
        wait_preview(app)
        assert preview.photo.tk.call(str(preview.photo), "get", 0, 0) == (0, 0, 255)
    else:
        preview._preview_worker.thread.join(0.2)
        app.root.update()
        assert preview.photo is None
        assert not any(
            preview.preview_canvas.type(i) == "image" for i in preview.preview_canvas.find_all()
        )


def test_close_joins_preview_worker(app, tmp_path):
    app.show_result(long_result(tmp_path))
    wait_preview(app)
    worker = app.product_long_preview._preview_worker.thread
    app.close()
    worker.join(3)
    assert not worker.is_alive() and app.product_long_preview._preview_worker.closed


def test_close_during_decode_discards_pixels_without_touching_destroyed_tk(
    app, tmp_path, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    original = ImageFile.ImageFile.load

    def blocked(image, *args, **kwargs):
        if image.size == (750, 6000) and not entered.is_set():
            entered.set()
            assert release.wait(5)
        return original(image, *args, **kwargs)

    monkeypatch.setattr(ImageFile.ImageFile, "load", blocked)
    app.show_result(long_result(tmp_path))
    app.root.update()
    assert entered.wait(2)
    worker = app.product_long_preview._preview_worker.thread
    try:
        app.close()
    finally:
        release.set()
    worker.join(3)
    assert not worker.is_alive()
    assert app.product_long_preview._preview_worker.take() is None


def test_oversized_long_preview_is_rejected_before_decode(app, tmp_path, monkeypatch):
    data = io.BytesIO()
    Image.new("RGB", (2401, 10), "blue").save(data, "PNG")
    threads = []
    original = Image.Image.load

    def tracked(image, *args, **kwargs):
        if image.size == (2401, 10):
            threads.append(threading.get_ident())
        return original(image, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "load", tracked)
    app._load_preview(data, long_image=True)
    app.root.update()
    assert not threads and app.product_long_preview.photo is None
    assert "尺寸" in app.asset_notice.cget("text")


@pytest.mark.parametrize(
    "stage", ["long_plan", "long_generate", "long_save_candidate", "long_audit", "long_compose"]
)
def test_product_long_progress_uses_localized_stage(app, stage):
    app.show_progress({"stage": stage})
    assert stage not in app.status.get()
    assert "长图" in app.status.get()


def test_long_image_failure_notice_locates_section_and_retains_specific_copy_error(app, tmp_path):
    saved = long_result(tmp_path, status="quality_failed")
    saved.assets[0].candidates[0]["metadata"] = {"segments": [
        {"status": "succeeded"},
        {"status": "quality_failed", "copy_audit": {
            "passed": False, "text_matches": False, "issues": ["缺少指定文字：无线连接"]
        }},
    ]}
    app.show_result(saved)
    assert "第 2 段" in app.asset_notice.cget("text")
    assert "无线连接" in app.asset_notice.cget("text")


@pytest.mark.parametrize("field", ["known_info", "copy_text"])
def test_changing_long_text_invalidates_analysis_without_requiring_valid_dimensions(
    app, tmp_path, field
):
    fill(app, tmp_path)
    app.outputs["long_image"].set(True)
    app.show_analysis(MaterialAnalysis(**analysis_data()))
    controls = app.product_long_controls
    controls.fields["width"].set("暂未填完")
    getattr(controls, field).insert("1.0", "用户新增的商品要求")
    app.root.update()
    assert app.analysis is None
    assert "用户新增的商品要求" in getattr(controls, field).get("1.0", "end")
