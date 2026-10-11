"""Native Chinese long-image workspace with one task worker and a bounded preview decoder."""

import io
import math
import threading
import tkinter as tk
from collections import OrderedDict
from pathlib import Path
from tkinter import filedialog, ttk

from PIL import Image, ImageTk

from . import (
    accept_long_image_candidate,
    analyze_long_image,
    create_long_image,
    render_long_image,
    resume_long_image,
)
from .desktop_runtime import with_credentials
from .long_image_models import LongImageOptions, LongImageRequest, LongProductContext
from .models import ImageSource, MaterialInput, SelectionSpec
from .user_errors import format_user_error

SCENES = {
    "商品详情": "product_detail",
    "品牌介绍": "brand_intro",
    "活动宣传": "promotion",
    "步骤教程": "tutorial",
    "对比说明": "comparison",
    "自定义": "custom",
}
THEMES = {"自动": "auto", "简洁": "clean", "温暖": "warm", "质感": "premium"}
VIEWS = ("整张长图", "语义段落", "切片", "原始配图")


class ProductLongImageControls:
    """Optional parameters inside the existing product output selection."""

    def __init__(self, app, parent):
        self.app = app
        self.frame = ttk.Frame(parent)
        self.fields = {
            key: tk.StringVar(value=value)
            for key, value in {
                "width": "750",
                "segment_count": "6",
                "segment_aspect_ratio": "3:4",
            }.items()
        }
        self.error = tk.StringVar()
        dimensions = ttk.Frame(self.frame)
        dimensions.pack(fill="x")
        self.entries = {}
        for column, (key, label) in enumerate(
            (
                ("width", "宽度（像素）"),
                ("segment_count", "区段数"),
                ("segment_aspect_ratio", "区段比例"),
            )
        ):
            dimensions.columnconfigure(column, weight=1, uniform="long-dimensions")
            field = ttk.Frame(dimensions)
            field.grid(row=0, column=column, sticky="ew", padx=(0, 8) if column < 2 else 0)
            ttk.Label(field, text=label, style="Field.TLabel").pack(anchor="w", pady=(0, 3))
            if key == "segment_aspect_ratio":
                entry = ttk.Combobox(
                    field,
                    textvariable=self.fields[key],
                    values=("3:4", "4:5", "9:16"),
                    state="readonly",
                    width=7,
                )
            else:
                entry = ttk.Entry(field, textvariable=self.fields[key], width=7)
            entry.pack(fill="x")
            self.entries[key] = entry
        ttk.Label(
            self.frame,
            text="默认 6 段，750 宽时约 6000 高；按实际画面保存。",
            style="Muted.TLabel",
            wraplength=335,
        ).pack(anchor="w", pady=(4, 6))
        ttk.Label(self.frame, text="已知商品信息（可选）", style="Field.TLabel").pack(
            anchor="w", pady=(4, 3)
        )
        self.known_info = app._text_area(self.frame, height=3)
        self.known_info.pack(fill="x")
        ttk.Label(
            self.frame,
            text="必须出现的文案（每行一条，保留原文）",
            style="Field.TLabel",
            wraplength=335,
        ).pack(anchor="w", pady=(8, 3))
        self.copy_text = app._text_area(self.frame, height=3)
        self.copy_text.pack(fill="x")
        for widget in (self.known_info, self.copy_text):
            widget.bind("<<Modified>>", self._text_changed)
        app.outputs["long_image"].trace_add("write", app.invalidate_analysis)
        ttk.Label(self.frame, textvariable=self.error, style="Notice.TLabel", wraplength=335).pack(
            fill="x", pady=(4, 0)
        )

    def _text_changed(self, event):
        if event.widget.edit_modified():
            event.widget.edit_modified(False)
            self.app.invalidate_analysis()

    def set_visible(self, selected):
        if selected:
            self.frame.pack(fill="x", pady=(8, 4))
        else:
            self.frame.pack_forget()

    def options(self):
        from pydantic import ValidationError

        from .models import ProductLongImageOptions

        self.error.set("")
        values = {}
        for key, label, low, high in (
            ("width", "宽度", 400, 2400),
            ("segment_count", "区段数", 3, 12),
        ):
            try:
                values[key] = int(self.fields[key].get())
                if not low <= values[key] <= high:
                    raise ValueError()
            except ValueError:
                message = f"{label}请输入 {low}–{high} 的整数。"
                self.error.set(message)
                self.entries[key].focus_set()
                raise ValueError(message) from None
        try:
            return ProductLongImageOptions(
                **values,
                segment_aspect_ratio=self.fields["segment_aspect_ratio"].get(),
                known_info=self.known_info.get("1.0", "end-1c"),
                required_copy=[
                    line for line in self.copy_text.get("1.0", "end-1c").split("\n") if line.strip()
                ],
            )
        except ValidationError as error:
            field = error.errors()[0]["loc"][0]
            message = {
                "segment_aspect_ratio": "区段比例请选择 3:4、4:5 或 9:16。",
                "known_info": "已知商品信息最多 12000 字，请缩短内容。",
                "required_copy": "文案最多 30 条，每条最多 500 字，请减少或缩短文案。",
            }.get(field, "长图参数有误，请检查输入。")
            self.error.set(message)
            (
                self.known_info
                if field == "known_info"
                else self.copy_text
                if field == "required_copy"
                else self.entries[field]
            ).focus_set()
            raise ValueError(message) from error


class _PreviewWorker:
    """One decoded source and one latest request; never calls Tk."""

    def __init__(self):
        self.condition = threading.Condition()
        self.request = None
        self.result = None
        self.version = 0
        self.closed = False
        self.thread = None

    def _discard_result(self):
        if self.result and self.result[2] is not None:
            self.result[2].close()
        self.result = None

    def invalidate(self):
        with self.condition:
            self.version += 1
            self.request = None
            self._discard_result()
            self.condition.notify()

    def submit(self, token, key, data, scale):
        with self.condition:
            if self.closed:
                return
            self.version += 1
            self._discard_result()
            self.request = (token, key, data, scale)
            if self.thread is None:
                self.thread = threading.Thread(
                    target=self._run, name="long-image-preview", daemon=True
                )
                self.thread.start()
            self.condition.notify()

    def take(self):
        with self.condition:
            result, self.result = self.result, None
            return result

    def close(self):
        with self.condition:
            self.closed = True
            self.request = None
            self._discard_result()
            self.condition.notify()

    def _run(self):
        source = None
        source_data = None
        try:
            while True:
                with self.condition:
                    while self.request is None and not self.closed:
                        if source is not None:
                            source.close()
                            source = None
                            source_data = None
                        self.condition.wait()
                    if self.closed:
                        return
                    token, key, data, scale = self.request
                    version = self.version
                    self.request = None
                resized = None
                error = None
                try:
                    if source_data is not data:
                        if source is not None:
                            source.close()
                        source = Image.open(io.BytesIO(data))
                        source.load()
                        source_data = data
                    data = None
                    with self.condition:
                        if self.closed or version != self.version:
                            continue
                    width = key[1][0]
                    top, bottom = key[2:]
                    source_top = math.floor(top / scale)
                    source_bottom = min(source.height, math.ceil(bottom / scale))
                    with source.crop((0, source_top, source.width, source_bottom)) as cropped:
                        resized = cropped.resize((width, bottom - top), Image.Resampling.LANCZOS)
                except Exception as failure:
                    error = str(failure)
                    if source is not None:
                        source.close()
                        source = None
                        source_data = None
                data = None
                with self.condition:
                    if not self.closed and version == self.version:
                        self.result = (token, key, resized, error)
                        # Retain the sole decoded source between viewport requests.
                        while self.request is None and version == self.version and not self.closed:
                            self.condition.wait()
                    elif resized is not None:
                        resized.close()
        finally:
            if source is not None:
                source.close()


class _ScrollablePreview:
    """Viewport strips shared by product and historical long-image previews."""

    def _clear_preview(self):
        self._preview_epoch += 1
        self._preview_requested_key = None
        self._preview_worker.invalidate()
        if self._preview_timer is not None:
            self.app.root.after_cancel(self._preview_timer)
            self._preview_timer = None
        self.preview_data = self.preview_size = self._preview_source_id = self.photo = None
        self.thumbnail_cache.clear()
        self.preview_canvas.delete("all")

    def _preview_destroyed(self, _event=None):
        self._preview_epoch += 1
        self._preview_worker.close()
        if self._preview_timer is not None:
            self.app.root.after_cancel(self._preview_timer)
            self._preview_timer = None
        self.thumbnail_cache.clear()
        self.photo = None

    def _poll_preview(self):
        self._preview_timer = None
        result = self._preview_worker.take()
        if result is not None:
            token, key, resized, error = result
            try:
                if token == self._preview_epoch and key == self._preview_requested_key:
                    self._preview_requested_key = None
                    if error is not None:
                        self.notice.configure(
                            text=self.notice.cget("text") + " · "
                            + self.app.safe_text(format_user_error(error, stage="preview").text)
                        )
                        self._clear_preview()
                    else:
                        self.thumbnail_cache[key] = ImageTk.PhotoImage(
                            resized, master=self.app.root
                        )
                        while len(self.thumbnail_cache) > 4:
                            self.thumbnail_cache.popitem(last=False)
                        self._draw_visible_preview()
            except Exception as error:
                self.notice.configure(
                    text=self.notice.cget("text") + " · "
                    + self.app.safe_text(format_user_error(error, stage="preview").text)
                )
                self._clear_preview()
            finally:
                if resized is not None:
                    resized.close()
        if self._preview_requested_key is not None:
            self._preview_timer = self.app.root.after(20, self._poll_preview)

    def _preview_scrolled(self, start, end):
        self.preview_scrollbar.set(start, end)
        self._draw_visible_preview()

    def _draw_visible_preview(self):
        """Cache at most four viewport strips, never resized whole-page copies."""
        if self.drawing or not self.preview_data or not self.preview_size:
            return
        self.drawing = True
        try:
            canvas = self.preview_canvas
            width, height = self.preview_size
            top = max(0, int(canvas.canvasy(0) - 8))
            top = (top // 512) * 512
            bottom = min(height, top + max(512, canvas.winfo_height()) + 512)
            if bottom <= top:
                return
            key = (self._preview_source_id, self.preview_size, top, bottom)
            if key not in self.thumbnail_cache:
                if key != self._preview_requested_key:
                    self._preview_requested_key = key
                    self._preview_worker.submit(
                        self._preview_epoch, key, self.preview_data, self.preview_scale
                    )
                if self._preview_timer is None:
                    self._preview_timer = self.app.root.after(20, self._poll_preview)
                return
            # A cache hit also invalidates a pending strip for a viewport we left.
            if self._preview_requested_key is not None:
                self._preview_requested_key = None
                self._preview_worker.invalidate()
            self.photo = self.thumbnail_cache[key]
            self.thumbnail_cache.move_to_end(key)
            canvas.delete("all")
            canvas.create_image(8, top + 8, anchor="nw", image=self.photo)
        finally:
            self.drawing = False

    def _preview_resize(self, event):
        if (
            self.zoom.get() == "适应宽度"
            and self.preview_size
            and abs(event.width - self.preview_size[0] - 16) > 2
        ):
            self.refresh_preview()
        else:
            self._draw_visible_preview()

    def _wheel(self, event):
        self._scroll(-int(event.delta / 120), "units")
        return "break"

    def _scroll(self, direction, unit):
        self.preview_canvas.yview_scroll(direction, unit)
        return "break"

    def _edge(self, position):
        self.preview_canvas.yview_moveto(position)
        return "break"


class ProductLongImagePreview(_ScrollablePreview):
    """Full product long-image preview in the ordinary result tab."""

    def __init__(self, app, parent):
        from .desktop import UI_COLORS

        self.app = app
        self.notice = app.asset_notice
        self.source_data = None
        self.thumbnail_cache = OrderedDict()
        self.photo = self.preview_size = self.preview_data = self._preview_source_id = None
        self.preview_scale = 1
        self.drawing = False
        self._preview_worker = _PreviewWorker()
        self._preview_epoch = 0
        self._preview_requested_key = self._preview_timer = None
        self.zoom = tk.StringVar(value="适应宽度")
        self.frame = ttk.Frame(parent)
        self.frame.rowconfigure(1, weight=1)
        self.frame.columnconfigure(0, weight=1)
        tools = ttk.Frame(self.frame)
        tools.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(tools, text="长图预览", style="Field.TLabel").pack(side="left")
        ttk.Label(tools, text="缩放", style="Muted.TLabel").pack(side="left", padx=(16, 6))
        self.zoom_choice = ttk.Combobox(
            tools,
            textvariable=self.zoom,
            values=("适应宽度", "25%", "50%", "75%", "100%", "150%"),
            state="readonly",
            width=10,
        )
        self.zoom_choice.pack(side="left")
        self.zoom_choice.bind("<<ComboboxSelected>>", self.refresh_preview)
        self.preview_canvas = tk.Canvas(
            self.frame,
            background=UI_COLORS["preview"],
            highlightthickness=1,
            highlightbackground=UI_COLORS["border"],
            highlightcolor=UI_COLORS["accent"],
            takefocus=True,
        )
        self.preview_scrollbar = ttk.Scrollbar(self.frame, command=self.preview_canvas.yview)
        horizontal = ttk.Scrollbar(
            self.frame, orient="horizontal", command=self.preview_canvas.xview
        )
        self.preview_canvas.configure(
            yscrollcommand=self._preview_scrolled, xscrollcommand=horizontal.set
        )
        self.preview_canvas.grid(row=1, column=0, sticky="nsew")
        self.preview_scrollbar.grid(row=1, column=1, sticky="ns")
        horizontal.grid(row=2, column=0, sticky="ew")
        self.preview_canvas.bind("<Configure>", self._preview_resize)
        self.preview_canvas.bind("<Destroy>", self._preview_destroyed)
        self.preview_canvas.bind("<MouseWheel>", self._wheel)
        self.preview_canvas.bind("<Button-1>", lambda e: self.preview_canvas.focus_set())
        for key, direction, unit in (
            ("Down", 1, "units"),
            ("Up", -1, "units"),
            ("Next", 1, "pages"),
            ("Prior", -1, "pages"),
        ):
            self.preview_canvas.bind(f"<{key}>", lambda e, d=direction, u=unit: self._scroll(d, u))
        self.preview_canvas.bind("<Home>", lambda e: self._edge(0))
        self.preview_canvas.bind("<End>", lambda e: self._edge(1))

    def set_source(self, data):
        self.source_data = data
        self.preview_canvas.yview_moveto(0)
        self.preview_canvas.xview_moveto(0)
        self.refresh_preview()

    def clear(self):
        self.source_data = None
        self._clear_preview()

    def refresh_preview(self, _event=None):
        self._clear_preview()
        if not self.source_data:
            return
        try:
            # Read only the header on Tk; the shared worker decodes full pixels.
            with Image.open(io.BytesIO(self.source_data)) as image:
                width, height = image.size
                if width > 2400 or height > 60000 or width * height > 60000000:
                    raise ValueError("长图尺寸超出预览上限，请查看任务目录。")
            scale = (
                max(1, self.preview_canvas.winfo_width() - 16) / width
                if self.zoom.get() == "适应宽度"
                else int(self.zoom.get().rstrip("%")) / 100
            )
            self.preview_size = (max(1, round(width * scale)), max(1, round(height * scale)))
            self.preview_data = self.source_data
            self._preview_source_id = id(self.source_data)
            self.preview_scale = scale
            self.preview_canvas.configure(
                scrollregion=(0, 0, self.preview_size[0] + 16, self.preview_size[1] + 16)
            )
            self._draw_visible_preview()
        except Exception as error:
            self.notice.configure(
                text=self.notice.cget("text") + " · "
                + self.app.safe_text(format_user_error(error, stage="preview").text)
            )
            self.source_data = None
            self._clear_preview()


class LongImagePanel(_ScrollablePreview):
    def __init__(self, app):
        self.app = app
        self.materials = []
        self.planning = None
        self.analysis = None
        self.planning_history = {}
        self.result = None
        self.run_dir = None
        self.selection_options = []
        self.active_selection = SelectionSpec()
        self.items = []
        self.candidates = []
        self.thumbnail_cache = OrderedDict()
        self.photo = None
        self.preview_size = None
        self.preview_data = None
        self._preview_source_id = None
        self.preview_scale = 1
        self.drawing = False
        self._preview_worker = _PreviewWorker()
        self._preview_epoch = 0
        self._preview_requested_key = None
        self._preview_timer = None
        self.scene = tk.StringVar(value="步骤教程")
        self.theme = tk.StringVar(value="自动")
        self.view = tk.StringVar(value=VIEWS[0])
        self.zoom = tk.StringVar(value="适应宽度")
        self.accept_reason = tk.StringVar()
        self.field_error = tk.StringVar()
        self.layout_open = tk.BooleanVar(value=False)
        self.fields = {
            key: tk.StringVar(value=value)
            for key, value in {
                "title": "",
                "product_name": "",
                "category": "",
                "platform": "通用",
                "width": "750",
                "slice_height": "1500",
                "font_path": "",
                "style_hint": "",
                "image_model": "pro",
                "image_size": "2K",
            }.items()
        }
        self._build()
        self.scene.trace_add("write", self._scene_changed)
        for name in (
            "title",
            "product_name",
            "category",
            "platform",
            "style_hint",
            "image_model",
            "image_size",
        ):
            self.fields[name].trace_add("write", self.invalidate)
        self.brief.bind("<<Modified>>", self._brief_changed)
        self.copy_text.bind("<<Modified>>", self._brief_changed)
        self.spec_text.bind("<<Modified>>", self._brief_changed)
        self.service_text.bind("<<Modified>>", self._brief_changed)
        self._scene_changed()

    def _build(self):
        from .desktop import PLATFORM_LABELS, UI_COLORS

        app = self.app
        self.form = ttk.Frame(app.canvas, padding=(0, 0, 8, 12), style="Workspace.TFrame")
        self.form.bind(
            "<Configure>", lambda e: app.canvas.configure(scrollregion=app.canvas.bbox("all"))
        )
        content = ttk.Frame(self.form, padding=16)
        content.pack(fill="x", pady=(0, 12))
        ttk.Label(content, text="长图内容", style="Section.TLabel").pack(anchor="w")
        self._combo(content, "使用场景", self.scene, tuple(SCENES))
        self.entries = {}
        self.entries["title"] = app._entry(content, "标题 *", self.fields["title"])
        ttk.Label(content, text="内容要求 * · 保留原文、数字与顺序").pack(anchor="w", pady=(10, 4))
        self.brief = app._text_area(content, height=5)
        self.brief.pack(fill="x")
        self._combo(content, "主题", self.theme, tuple(THEMES))
        self.entries["style_hint"] = app._entry(
            content, "配图风格（可选）", self.fields["style_hint"]
        )
        ttk.Label(content, text="必须出现的原文（每行一条，可选）").pack(anchor="w", pady=(10, 4))
        self.copy_text = app._text_area(content, height=2)
        self.copy_text.pack(fill="x")
        self.error_label = ttk.Label(
            content, textvariable=self.field_error, style="Notice.TLabel", wraplength=335
        )
        self.error_label.pack(fill="x", pady=(6, 0))

        material = ttk.Frame(self.form, padding=16)
        material.pack(fill="x", pady=(0, 12))
        ttk.Label(material, text="参考素材", style="Section.TLabel").pack(anchor="w")
        ttk.Label(
            material,
            text="最多 8 张；商品详情必须添加，其余场景可留空。",
            style="Muted.TLabel",
            wraplength=335,
        ).pack(anchor="w", pady=(4, 8))
        actions = ttk.Frame(material)
        actions.pack(fill="x")
        ttk.Button(
            actions, text="+ 添加图片", command=self.choose_materials, style="Soft.TButton"
        ).pack(side="left")
        ttk.Button(actions, text="移除选中", command=self.remove_materials).pack(
            side="left", padx=8
        )
        self.material_list = tk.Listbox(
            material,
            height=3,
            selectmode="extended",
            exportselection=False,
            font=("Microsoft YaHei UI", 10),
            background=UI_COLORS["field"],
            foreground=UI_COLORS["text"],
            highlightcolor=UI_COLORS["accent"],
        )
        self.material_list.pack(fill="x", pady=(8, 0))
        self.product_frame = ttk.Frame(material)
        self.entries["product_name"] = app._entry(
            self.product_frame, "商品名称 *", self.fields["product_name"]
        )
        self.entries["category"] = app._entry(
            self.product_frame, "商品分类 *", self.fields["category"]
        )
        self._combo(
            self.product_frame,
            "平台（可选）",
            self.fields["platform"],
            ("通用", *PLATFORM_LABELS.values()),
        )
        ttk.Label(self.product_frame, text="商品规格（每行 名称=值）").pack(anchor="w", pady=(8, 4))
        self.spec_text = app._text_area(self.product_frame, height=2)
        self.spec_text.pack(fill="x")
        ttk.Label(self.product_frame, text="服务说明（每行一条）").pack(anchor="w", pady=(8, 4))
        self.service_text = app._text_area(self.product_frame, height=2)
        self.service_text.pack(fill="x")

        layout = ttk.Frame(self.form, padding=16)
        layout.pack(fill="x", pady=(0, 12))
        ttk.Checkbutton(
            layout,
            text="展开尺寸、字体与配图设置",
            variable=self.layout_open,
            command=self._toggle_layout,
        ).pack(anchor="w")
        self.layout_frame = ttk.Frame(layout)
        for key, label in (
            ("width", "长图宽度（400–2400 px）"),
            ("slice_height", "切片高度（500–4000 px）"),
            ("font_path", "本地字体路径（可选）"),
        ):
            self.entries[key] = app._entry(self.layout_frame, label, self.fields[key])
        ttk.Button(self.layout_frame, text="选择字体", command=self.choose_font).pack(
            anchor="w", pady=4
        )
        self._combo(
            self.layout_frame, "配图模型", self.fields["image_model"], ("pro", "fast", "base")
        )
        self._combo(self.layout_frame, "配图尺寸", self.fields["image_size"], ("1K", "2K", "4K"))
        self.rebuild_button = ttk.Button(
            layout,
            text="重新排版已保存长图",
            command=self.rebuild_saved,
            state="disabled",
            style="Soft.TButton",
        )
        self.rebuild_button.pack(fill="x", pady=(10, 0))
        ttk.Label(
            layout,
            text="只修改宽度、主题、切片高度与字体，无需密钥。",
            style="Muted.TLabel",
            wraplength=335,
        ).pack(anchor="w", pady=(4, 0))
        api = ttk.Frame(self.form, padding=16)
        api.pack(fill="x")
        ttk.Label(api, text="服务与任务", style="Section.TLabel").pack(anchor="w")
        app._entry(api, "网络配置文件", app.config_path)
        ttk.Button(api, text="选择配置 / 输入密钥", command=self.configure_services).pack(
            anchor="w", pady=4
        )
        app._entry(api, "输出目录", app.output_path)
        ttk.Button(api, text="选择输出目录", command=app.choose_output).pack(anchor="w", pady=4)
        actions = ttk.Frame(api)
        actions.pack(fill="x", pady=4)
        ttk.Button(actions, text="打开已有任务", command=app.choose_task).pack(side="left")
        ttk.Button(actions, text="恢复任务", command=lambda: self.start("resume")).pack(
            side="left", padx=8
        )

        self.preview_tab = ttk.Frame(app.notebook, padding=12)
        self.planning_tab = ttk.Frame(app.notebook, padding=16)
        app.notebook.add(self.preview_tab, text="长图预览")
        app.notebook.add(self.planning_tab, text="长图规划 / 澄清")
        toolbar = ttk.Frame(self.preview_tab)
        toolbar.pack(fill="x")
        self.view_combo = ttk.Combobox(
            toolbar, textvariable=self.view, values=VIEWS, state="readonly", width=10
        )
        self.view_combo.pack(side="left")
        self.view_combo.bind("<<ComboboxSelected>>", self.refresh_items)
        self.item_choice = ttk.Combobox(toolbar, state="readonly", width=24)
        self.item_choice.pack(side="left", padx=8, fill="x", expand=True)
        self.item_choice.bind("<<ComboboxSelected>>", self._item_changed)
        self.zoom_choice = ttk.Combobox(
            toolbar,
            textvariable=self.zoom,
            values=("适应宽度", "50%", "100%", "150%"),
            state="readonly",
            width=10,
        )
        self.zoom_choice.pack(side="right")
        self.zoom_choice.bind("<<ComboboxSelected>>", self.refresh_preview)
        self.notice = ttk.Label(
            self.preview_tab, text="先规划内容，再生成长图。", style="Notice.TLabel", wraplength=650
        )
        self.notice.pack(fill="x", pady=8)
        review = ttk.Frame(self.preview_tab)
        review.pack(fill="x", pady=(0, 8))
        ttk.Label(review, text="候选").grid(row=0, column=0, sticky="w")
        self.candidate_choice = ttk.Combobox(review, state="readonly", width=23)
        self.candidate_choice.grid(row=0, column=1, sticky="ew", padx=8)
        self.candidate_choice.bind("<<ComboboxSelected>>", self.refresh_preview)
        self.accept_button = ttk.Button(
            review, text="接受所选候选", command=self.accept_selected, state="disabled"
        )
        self.accept_button.grid(row=0, column=2)
        ttk.Label(review, text="接受原因 *").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.reason_entry = ttk.Entry(review, textvariable=self.accept_reason)
        self.reason_entry.grid(row=1, column=1, columnspan=2, sticky="ew", padx=8, pady=(6, 0))
        review.columnconfigure(1, weight=1)
        preview = ttk.Frame(self.preview_tab)
        preview.pack(fill="both", expand=True)
        self.preview_canvas = tk.Canvas(
            preview,
            background=UI_COLORS["preview"],
            highlightthickness=1,
            highlightbackground=UI_COLORS["border"],
            highlightcolor=UI_COLORS["accent"],
            takefocus=True,
        )
        vertical = ttk.Scrollbar(preview, command=self.preview_canvas.yview)
        self.preview_scrollbar = vertical
        horizontal = ttk.Scrollbar(preview, orient="horizontal", command=self.preview_canvas.xview)
        self.preview_canvas.configure(
            yscrollcommand=self._preview_scrolled, xscrollcommand=horizontal.set
        )
        self.preview_canvas.grid(row=0, column=0, sticky="nsew")
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        preview.rowconfigure(0, weight=1)
        preview.columnconfigure(0, weight=1)
        self.preview_canvas.bind("<Configure>", self._preview_resize)
        self.preview_canvas.bind("<Destroy>", self._preview_destroyed)
        self.preview_canvas.bind("<MouseWheel>", self._wheel)
        self.preview_canvas.bind("<Button-1>", lambda e: self.preview_canvas.focus_set())
        for key, direction, unit in (
            ("Down", 1, "units"),
            ("Up", -1, "units"),
            ("Next", 1, "pages"),
            ("Prior", -1, "pages"),
        ):
            self.preview_canvas.bind(f"<{key}>", lambda e, d=direction, u=unit: self._scroll(d, u))
        self.preview_canvas.bind("<Home>", lambda e: self._edge(0))
        self.preview_canvas.bind("<End>", lambda e: self._edge(1))
        self.choice = ttk.Combobox(self.planning_tab, state="readonly", values=("自动选择",))
        self.choice.current(0)
        self.choice.pack(fill="x")
        self.choice.bind("<<ComboboxSelected>>", self._selection_changed)
        ttk.Label(
            self.planning_tab,
            text="选择方案后点击「规划长图」，沿用当前素材分析快照。",
            style="Muted.TLabel",
        ).pack(anchor="w", pady=8)
        self.planning_text = app._text_area(self.planning_tab, state="disabled")
        self.planning_text.pack(fill="both", expand=True)
        app.notebook.hide(self.preview_tab)
        app.notebook.hide(self.planning_tab)

    @staticmethod
    def _combo(parent, label, variable, values):
        ttk.Label(parent, text=label).pack(anchor="w", pady=(8, 4))
        widget = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly")
        widget.pack(fill="x")
        return widget

    def configure_services(self):
        # Existing credentials form remains the single source of provider settings.
        self.app.set_mode("product")
        self.app.canvas.yview_moveto(1)
        self.app.status.set("在「API 配置与输出」输入本次密钥，再切回「图文长图」。")

    def _toggle_layout(self):
        if self.layout_open.get():
            self.layout_frame.pack(fill="x", before=self.rebuild_button)
        else:
            self.layout_frame.pack_forget()

    def _scene_changed(self, *_args):
        if self.scene.get() == "商品详情":
            self.product_frame.pack(fill="x", pady=(10, 0))
        else:
            self.product_frame.pack_forget()
        self.invalidate()

    def _brief_changed(self, event):
        if event.widget.edit_modified():
            event.widget.edit_modified(False)
            self.invalidate()

    def invalidate(self, *_args):
        self.planning = None
        self.analysis = None
        self.selection_options = []
        self.active_selection = SelectionSpec()
        self.choice.configure(values=("自动选择",))
        self.choice.current(0)
        self.app._text(self.planning_text, "输入已修改，请重新规划长图。")
        self.update_actions()

    def _selection_changed(self, _event=None):
        # Selection modifies the request but deliberately retains the analysis snapshot.
        self.planning = None
        index = self.choice.current()
        self.active_selection = self.selection_options[index - 1] if index > 0 else SelectionSpec()
        self.update_actions()
        self.app.status.set("选择已更新，请重新规划；会使用同一素材分析快照。")

    def choose_materials(self):
        self.add_materials(
            filedialog.askopenfilenames(
                parent=self.app.root,
                title="选择参考素材",
                filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp")],
            )
        )

    def add_materials(self, paths):
        if self.app.busy:
            return
        materials = list(dict.fromkeys([*self.materials, *(Path(p).resolve() for p in paths)]))
        if len(materials) > 8:
            self.app.status.set("最多添加 8 张素材。")
            return
        self.materials = materials
        self.material_list.delete(0, "end")
        for path in materials:
            self.material_list.insert("end", path.name)
        self.invalidate()

    def remove_materials(self):
        for index in reversed(self.material_list.curselection()):
            self.materials.pop(index)
        self.add_materials([])

    def choose_font(self):
        path = filedialog.askopenfilename(
            parent=self.app.root, title="选择本地字体", filetypes=[("字体", "*.ttf *.otf *.ttc")]
        )
        if path:
            self.fields["font_path"].set(path)

    def render_options(self):
        try:
            options = dict(
                width=int(self.fields["width"].get()),
                theme=THEMES[self.theme.get()],
                slice_height=int(self.fields["slice_height"].get()),
                font_path=self.fields["font_path"].get().strip() or None,
            )
            LongImageOptions.model_validate(options)
            return options
        except ValueError:
            self.layout_open.set(True)
            self._toggle_layout()
            raise ValueError("请检查长图宽度、切片高度和字体设置。") from None

    def make_request(self):
        from .desktop import PLATFORM_LABELS

        title = self.fields["title"].get()
        brief = self.brief.get("1.0", "end-1c")
        if not title.strip():
            self.entries["title"].focus_set()
            raise ValueError("请填写标题。")
        if not brief.strip():
            self.brief.focus_set()
            raise ValueError("请填写内容要求。")
        context = None
        specs = []
        service = []
        scene = SCENES[self.scene.get()]
        if scene == "product_detail":
            if not self.materials:
                raise ValueError("商品详情需要至少一张真实素材。")
            for field in ("product_name", "category"):
                if not self.fields[field].get().strip():
                    self.entries[field].focus_set()
                    raise ValueError("请填写商品名称与分类。")
            platform = {v: k for k, v in PLATFORM_LABELS.items()}.get(self.fields["platform"].get())
            context = LongProductContext(
                product_name=self.fields["product_name"].get(),
                category=self.fields["category"].get(),
                platform=platform,
            )
            for line in self.spec_text.get("1.0", "end-1c").splitlines():
                if line.strip():
                    if "=" not in line:
                        raise ValueError("商品规格请按「名称=值」逐行填写。")
                    name, value = line.split("=", 1)
                    specs.append({"name": name, "value": value})
            service = [
                line for line in self.service_text.get("1.0", "end-1c").splitlines() if line.strip()
            ]
        choice = self.choice.current()
        selection = (
            (self.selection_options[choice - 1] if choice > 0 else self.active_selection)
            if context
            else SelectionSpec()
        )
        return LongImageRequest(
            scene=scene,
            title=title,
            content_brief=brief,
            materials=[
                MaterialInput(material_id=f"m{i}", source=ImageSource(path=path))
                for i, path in enumerate(self.materials, 1)
            ],
            style_hint=self.fields["style_hint"].get() or None,
            product_context=context,
            product_specs=specs,
            service_notes=service,
            required_copy=[
                {"requirement_id": f"copy-{i}", "text": line}
                for i, line in enumerate(self.copy_text.get("1.0", "end-1c").splitlines(), 1)
                if line.strip()
            ],
            selection=selection,
            options=LongImageOptions(**self.render_options()),
            image_model=self.fields["image_model"].get(),
            image_size=self.fields["image_size"].get(),
            output_dir=Path(self.app.output_path.get()).expanduser().resolve(),
        )

    def start(self, operation):
        app = self.app
        if app.busy or app.job.running:
            return
        try:
            self.field_error.set("")
            if operation == "create" and (
                self.planning is None
                or self.planning.status != "ready"
                or self.planning.plan is None
            ):
                raise ValueError("请先完成规划；规划可生成后才能生成长图。")
            if operation == "resume":
                if self.run_dir is None:
                    raise ValueError("请先打开已有长图任务。")
                directory = self.run_dir
                # Runtime itself loads credentials only if unfinished network work exists.
                config = (
                    with_credentials(app.config, {k: v.get() for k, v in app.key_fields.items()})
                    if app.config is not None
                    else None
                )

                def factory():
                    return resume_long_image(
                        directory, config=config, on_progress=app.job.report_progress
                    )
            else:
                request = self.make_request()
                for path in self.materials:
                    if not path.is_file():
                        raise ValueError(f"素材文件不存在：{path.name}")
                planning = self.planning
                needs_network = (
                    operation == "analyze"
                    or any(slot.source == "generate" for slot in planning.plan.image_slots)
                    or (request.scene == "product_detail" and planning.record is None)
                )
                config = app.session_config() if needs_network else None
                if needs_network and config is None:
                    return
                analysis = self.analysis
                self.active_selection = request.selection
                if operation == "analyze":

                    def factory():
                        return analyze_long_image(
                            request,
                            config=config,
                            analysis=analysis,
                            on_progress=app.job.report_progress,
                        )
                else:
                    self.clear_binding()
                    app.run_dir = None

                    def factory():
                        return create_long_image(
                            request,
                            config=config,
                            plan=planning.plan,
                            analysis=analysis,
                            planning_record=planning.record,
                            on_progress=app.job.report_progress,
                        )

            app._start_job(operation, factory)
        except Exception as error:
            self.field_error.set(app.safe_text(str(error)))
            app.show_error(error)
            app.status.set(app.safe_text(format_user_error(error).text))

    def show_planning(self, result):
        self.planning = result
        self.analysis = result.analysis
        self.planning_history[result.planning_id] = result
        self.selection_options = []
        labels = ["自动选择"]
        seen = set()
        for issue in result.issues:
            for option in issue.options:
                if option.get("selection"):
                    selection = SelectionSpec.model_validate(option["selection"])
                    if selection.model_dump_json() not in seen:
                        seen.add(selection.model_dump_json())
                        self.selection_options.append(selection)
                        labels.append(option.get("label", option.get("id", "选择主体")))
        self.choice.configure(values=labels)
        self.choice.current(0)
        lines = [f"规划状态：{self._status(result.status)}", f"规划记录：{result.planning_id}"]
        if result.error_info:
            lines.append(format_user_error(result, stage="plan").text)
        for issue in result.issues:
            lines.append(
                f"问题：{issue.message}" + (f" · 字段 {issue.field}" if issue.field else "")
            )
            if issue.field in self.entries:
                self.entries[issue.field].focus_set()
            elif issue.field == "content_brief":
                self.brief.focus_set()
        if result.plan:
            plan = result.plan
            count = sum(s.source == "generate" for s in plan.image_slots)
            lines.extend(
                [
                    f"区块 {len(plan.sections)} · 生成配图 {count} · 原样素材 {sum(s.source == 'source' for s in plan.image_slots)}",
                    "配图逐槽生成；规划、审核和修复另计调用。",
                    "",
                    "必需原文 / 来源 / ID：",
                ]
            )
            for req in plan.requirements:
                lines.append(
                    f"{req.requirement_id} · {req.source.pointer or '模型补充'} · {req.text}"
                )
            for section in plan.sections:
                role = {
                    "hero": "首屏",
                    "image_text": "图文",
                    "bullets": "要点",
                    "steps": "步骤",
                    "table": "表格",
                    "closing": "结语",
                }[section.role]
                lines.append(
                    f"\n{section.section_id} · {role} · 素材槽 {', '.join(section.slot_ids) or '无'}"
                )
                lines.extend(unit.text for unit in section.text_units)
                lines.extend(" | ".join(cell.text for cell in row) for row in section.table_rows)
        lines.append("\n独立规划历史（按记录去重）：")
        totals = {}
        missing = False
        for planning in self.planning_history.values():
            lines.append(f"{planning.planning_id} · {self._usage(planning.usage)}")
            missing |= planning.usage is None
            for key, value in (planning.usage or {}).items():
                totals[key] = totals.get(key, 0) + value
        lines.append(
            "历史总用量：" + ("不可用（存在缺失记录）" if missing else self._usage(totals))
        )
        lines.extend(result.warnings)
        self.app._text(self.planning_text, "\n".join(lines))
        self.app._text(self.app.json_text, self.app.to_json(result))
        self.app.notebook.select(self.planning_tab)
        self.field_error.set("；".join(i.message for i in result.issues))
        self.app.status.set(
            format_user_error(result, stage="plan").text if result.error_info
            else f"规划完成 · {self._status(result.status)}"
        )
        self.update_actions()

    @staticmethod
    def _usage(value):
        if value is None:
            return "用量不可用"
        return f"视觉 {value.get('vision_calls', 0)} · 生图 {value.get('image_calls', 0)}"

    @staticmethod
    def _status(value):
        from .desktop import STATUS_LABELS

        return STATUS_LABELS.get(value, value or "未知")

    def clear_binding(self):
        self.run_dir = None
        self.result = None
        self.items = []
        self.candidates = []
        self._clear_preview()
        self.candidate_choice.set("")
        self.candidate_choice.configure(values=())
        self.accept_reason.set("")
        self.update_actions()

    def show_result(self, result):
        self.result = result
        self.run_dir = Path(result.run_dir).resolve() if result.run_dir else None
        self.app.run_dir = self.run_dir
        if self.run_dir:
            self.app.output_folder = self.run_dir
        self.thumbnail_cache.clear()
        self.app._text(self.app.json_text, self.app.to_json(result))
        self.refresh_items()
        self.app.notebook.select(self.preview_tab)
        self.app.status.set(
            f"长图 · {self._status(result.status)} · 当前任务 {self._usage(result.usage.execution)} · 去重总计 {self._usage(result.usage.display_total)}"
            + (" · " + format_user_error(result).text if result.error_info else "")
        )
        self.update_actions()

    def load_saved(self, result):
        from .long_image_runtime import current_options
        from .long_image_store import LongImageRunStore

        store = LongImageRunStore(Path(result.run_dir))
        with store.lock():
            request = store.request()
            options = current_options(store)
        self.fields["title"].set(request.title)
        self.scene.set(next(k for k, v in SCENES.items() if v == request.scene))
        self.app._text(self.brief, request.content_brief)
        self.materials = [m.source.path for m in request.materials]
        self.material_list.delete(0, "end")
        for path in self.materials:
            self.material_list.insert("end", path.name)
        for key in ("width", "slice_height", "font_path"):
            self.fields[key].set(str(getattr(options, key) or ""))
        self.theme.set(next(k for k, v in THEMES.items() if v == options.theme))
        self.fields["style_hint"].set(request.style_hint or "")
        if request.product_context:
            from .desktop import PLATFORM_LABELS

            context = request.product_context
            self.fields["product_name"].set(context.product_name)
            self.fields["category"].set(context.category)
            self.fields["platform"].set(PLATFORM_LABELS.get(context.platform, "通用"))
        self.app._text(self.copy_text, "\n".join(c.text for c in request.required_copy))
        self.app._text(
            self.spec_text, "\n".join(f"{s.name}={s.value}" for s in request.product_specs)
        )
        self.app._text(self.service_text, "\n".join(request.service_notes))
        for widget in (self.brief, self.copy_text, self.spec_text, self.service_text):
            widget.configure(state="normal")
            widget.edit_modified(False)
        self.fields["image_model"].set(request.image_model)
        self.fields["image_size"].set(request.image_size)
        self.show_result(result)

    def refresh_items(self, _event=None):
        result = self.result
        self.items = (
            []
            if result is None
            else (
                {
                    VIEWS[0]: [result.page] if result.page else [],
                    VIEWS[1]: result.sections,
                    VIEWS[2]: result.slices,
                    VIEWS[3]: result.image_assets,
                }[self.view.get()]
            )
        )
        labels = [getattr(item, "artifact_id", None) or item.slot_id for item in self.items]
        self.item_choice.configure(values=labels)
        if labels:
            self.item_choice.current(0)
        else:
            self.item_choice.set("")
        self._item_changed()

    def _item_changed(self, _event=None):
        index = self.item_choice.current()
        item = self.items[index] if 0 <= index < len(self.items) else None
        self.candidates = getattr(item, "candidates", []) if item else []
        self.candidate_choice.configure(
            values=[
                f"{c.get('candidate_id')} · {self._status(c.get('status'))}"
                for c in self.candidates
            ]
        )
        if self.candidates:
            selected = next(
                (
                    i
                    for i, c in enumerate(self.candidates)
                    if c.get("candidate_id") == item.selected_candidate_id or c.get("selected")
                ),
                len(self.candidates) - 1,
            )
            self.candidate_choice.current(selected)
        else:
            self.candidate_choice.set("")
        self.preview_canvas.yview_moveto(0)
        self.refresh_preview()
        self.update_actions()

    def refresh_preview(self, _event=None):
        self._clear_preview()
        if self.result is None:
            return
        item_index = self.item_choice.current()
        item = self.items[item_index] if 0 <= item_index < len(self.items) else None
        status = getattr(item, "status", None) or getattr(item, "source_status", "")
        availability = getattr(item, "availability", None)
        candidate_index = self.candidate_choice.current()
        candidate = (
            self.candidates[candidate_index]
            if 0 <= candidate_index < len(self.candidates)
            else None
        )
        notices = [f"任务：{self._status(self.result.status)}"]
        if item:
            notices.append(
                f"所选：{self._status(status)}"
                + (
                    f" · {'旧版产物，请重新排版' if availability == 'stale' else availability}"
                    if availability
                    else ""
                )
            )
        if candidate:
            notices.append(
                f"候选 {candidate.get('candidate_id')} · {self._status(candidate.get('status'))}"
            )
            acceptance = candidate.get("acceptance") or {}
            if acceptance.get("reason"):
                notices.append(f"接受原因：{acceptance['reason']}")
            if candidate.get("error_info"):
                notices.append(format_user_error(candidate).text)
        if item and getattr(item, "error_info", None):
            notices.append(format_user_error(item).text)
        if self.result.error_info:
            notices.append(format_user_error(self.result).text)
        if status not in ("succeeded", "available") or self.result.status != "succeeded":
            notices.append("请核对状态与审核记录；人工接受保留自动审核历史。")
        notices.extend(self.result.warnings)
        notices.extend(format_user_error(e, stage="output").text for e in self.result.output_errors)
        self.notice.configure(text=self.app.safe_text(" · ".join(notices)))
        data = getattr(item, "image", None)
        try:
            if candidate:
                from .long_image_contracts import long_result_to_bundle

                bundle = long_result_to_bundle(self.result)
                asset_dto = next(a for a in bundle.dto.image_assets if a.slot_id == item.slot_id)
                candidate_dto = next(
                    c for c in asset_dto.candidates if c.candidate_id == candidate["candidate_id"]
                )
                data = bundle.blobs.get(candidate_dto.blob_id) if candidate_dto.blob_id else None
            if not data:
                self.preview_canvas.delete("all")
                self.preview_canvas.create_text(
                    16,
                    16,
                    anchor="nw",
                    text=self.app.safe_text("当前没有可预览图片。\n" + "\n".join(notices)),
                    width=450,
                )
                self.preview_canvas.configure(scrollregion=(0, 0, 500, 100))
                return
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
                scale = (
                    max(1, self.preview_canvas.winfo_width() - 16) / width
                    if self.zoom.get() == "适应宽度"
                    else int(self.zoom.get().rstrip("%")) / 100
                )
                size = (max(1, round(width * scale)), max(1, round(height * scale)))
            self.preview_size = size
            self.preview_data = data
            # Immutable bytes identity is enough for this transient, selection-scoped
            # cache. The worker retains the original bytes while reusing their decode.
            self._preview_source_id = id(data)
            self.preview_scale = scale
            self.preview_canvas.configure(scrollregion=(0, 0, size[0] + 16, size[1] + 16))
            self._draw_visible_preview()
        except Exception as error:
            self.notice.configure(
                text=self.notice.cget("text") + " · "
                + self.app.safe_text(format_user_error(error, stage="preview").text)
            )
            self._clear_preview()

    def update_actions(self):
        app = self.app
        ready = self.planning and self.planning.status == "ready" and self.planning.plan is not None
        if getattr(app, "mode", "product") == "long_image":
            app.generate_button.configure(state="normal" if ready and not app.busy else "disabled")
        self.rebuild_button.configure(
            state="normal" if self.run_dir and not app.busy else "disabled"
        )
        self.accept_button.configure(
            state="normal" if self.run_dir and self.candidates and not app.busy else "disabled"
        )

    def accept_selected(self):
        app = self.app
        if app.busy or app.job.running:
            return
        index = self.item_choice.current()
        candidate_index = self.candidate_choice.current()
        if (
            not self.run_dir
            or not 0 <= index < len(self.items)
            or not 0 <= candidate_index < len(self.candidates)
        ):
            app.status.set("请先打开长图任务，切到原始配图并选择候选。")
            return
        reason = self.accept_reason.get().strip()
        if not reason:
            app.status.set("人工接受必须填写原因。")
            self.reason_entry.focus_set()
            return
        directory, slot_id = self.run_dir, self.items[index].slot_id
        candidate_id = self.candidates[candidate_index]["candidate_id"]
        app._start_job(
            "accept",
            lambda: accept_long_image_candidate(directory, slot_id, candidate_id, reason=reason),
        )

    def rebuild_saved(self):
        app = self.app
        if app.busy or app.job.running:
            return
        try:
            if self.run_dir is None:
                raise ValueError("请先打开已保存的图文长图任务。")
            options = self.render_options()
            directory = self.run_dir
            app._start_job(
                "render",
                lambda: render_long_image(
                    directory, render_options=options, on_progress=app.job.report_progress
                ),
            )
        except Exception as error:
            self.field_error.set(app.safe_text(str(error)))
            app.show_error(error)
