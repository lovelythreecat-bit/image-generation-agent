"""Small native desktop harness for the public image-agent API."""

import io
import json
import os
import re
import time
import tkinter as tk
from pathlib import Path
from queue import Empty
from tkinter import filedialog, ttk
from tkinter.scrolledtext import ScrolledText

from PIL import Image, ImageOps, ImageTk
from pydantic import ValidationError

from . import AgentConfig, accept_candidate, analyze_materials, create_images, resume_images
from .desktop_runtime import (
    BackgroundJob,
    build_request,
    load_saved_result,
    saved_image_path,
    with_credentials,
)
from .errors import AgentError, make_error_info
from .models import OUTPUTS, PLATFORMS

PLATFORM_LABELS = dict(
    zip(
        PLATFORMS, ("Amazon", "淘宝", "天猫", "京东", "拼多多", "Shopee", "Lazada", "SHEIN", "Temu")
    )
)
OUTPUT_LABELS = dict(zip(OUTPUTS, ("主图", "详情图（3 张）", "拼多多白底")))
STATUS_LABELS = {
    "succeeded": "全部成功",
    "partial": "部分成功",
    "failed": "失败",
    "needs_input": "需要补充选择",
    "ready": "可生成",
    "pending_audit": "待审核",
    "audit_error": "审核异常，可恢复",
    "quality_failed": "质量未通过",
    "budget_exhausted": "调用预算耗尽",
    "accepted": "人工接受",
    "generation_uncertain": "生成提交状态不确定",
}
STAGE_LABELS = {
    "prepare": "准备任务",
    "prepare_asset": "准备图片",
    "generate": "生成图片",
    "save_candidate": "保存候选",
    "audit": "审核图片",
    "repair": "质量修复",
    "finish_asset": "图片处理完成",
    "group_audit": "详情组审",
    "finish": "任务完成",
    "generation_uncertain": "生成提交状态不确定",
}


class DesktopApp:
    def __init__(self, root: tk.Tk, *, project_dir: Path | None = None):
        self.root = root
        self.project_dir = Path(project_dir or Path.cwd()).resolve()
        self.job = BackgroundJob()
        self.busy = False
        self.closing = False
        self.materials: list[Path] = []
        self.analysis = None
        self.analysis_signature = None
        self.needs_input = False
        self.config = None
        self.loaded_config_path = None
        self.key_fields = {}
        self.secrets = set()
        self.assets = {}
        self.run_dir = None
        self.candidate_options = []
        self.progress_message = ""
        self.selection_options = []
        self.output_folder = self.project_dir / "out"
        self.preview_image = None
        self.preview_photo = None
        self.disabled_widgets = []
        self.operation = ""
        self.started = 0
        self.status = tk.StringVar(value="就绪 · 添加素材，填写商品信息，然后分析或生成。")
        self.config_path = tk.StringVar(value=str(self.project_dir / "examples/config.openai.json"))
        self.output_path = tk.StringVar(value=str(self.output_folder))
        self.accept_reason = tk.StringVar()
        self.fields = {
            key: tk.StringVar(value=value)
            for key, value in {
                "product_name": "",
                "category": "",
                "image_model": "fast",
                "image_size": "2K",
                "aspect_ratio": "auto",
            }.items()
        }
        self.platforms = {name: tk.BooleanVar(value=name == "taobao") for name in PLATFORMS}
        self.outputs = {name: tk.BooleanVar(value=name == "main_image") for name in OUTPUTS}
        self.outputs["pdd_white_background"].trace_add("write", self._white_background_changed)
        self.platforms["pinduoduo"].trace_add("write", self._pinduoduo_changed)
        self._build()
        for key in ("product_name", "category"):
            self.fields[key].trace_add("write", self.invalidate_analysis)
        self.brief.bind("<<Modified>>", self._brief_changed)
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        if Path(self.config_path.get()).is_file():
            self.load_config()
        self.poll_timer = self.root.after(80, self.poll)

    def _build(self):
        root = self.root
        root.title("Image Agent · 桌面测试工具")
        width = min(1240, root.winfo_screenwidth() - 80)
        height = min(860, root.winfo_screenheight() - 100)
        root.geometry(f"{width}x{height}")
        root.minsize(940, 620)
        root.configure(background="#f4f6f8")
        style = ttk.Style(root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("TLabel", font=("Microsoft YaHei UI", 10))
        style.configure("TButton", padding=(10, 5))
        style.configure("Heading.TLabel", font=("Microsoft YaHei UI", 18, "bold"))
        style.configure("Muted.TLabel", foreground="#536477")
        style.configure("TLabelframe.Label", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Treeview", rowheight=28)
        root.columnconfigure(0, weight=1)
        root.rowconfigure(1, weight=1)
        header = ttk.Frame(root, padding=(20, 14))
        header.grid(row=0, column=0, sticky="ew")
        ttk.Label(header, text="Image Agent", style="Heading.TLabel").pack(side="left")
        ttk.Label(header, text="素材分析 / 图片生成 / 审核结果", style="Muted.TLabel").pack(
            side="left", padx=20
        )
        panes = ttk.Panedwindow(root, orient="horizontal")
        panes.grid(row=1, column=0, sticky="nsew", padx=16)
        left = ttk.Frame(panes, width=410)
        panes.add(left, weight=0)
        self.canvas = tk.Canvas(left, width=410, highlightthickness=0, background="#f4f6f8")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.form = ttk.Frame(self.canvas, padding=(0, 0, 12, 10))
        window = self.canvas.create_window((0, 0), window=self.form, anchor="nw")
        self.form.bind(
            "<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        )
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        self.root.bind("<MouseWheel>", self._wheel, add="+")

        materials = self._section("1  素材与商品")
        actions = ttk.Frame(materials)
        actions.pack(fill="x")
        ttk.Button(actions, text="添加图片", command=self.choose_materials).pack(side="left")
        ttk.Button(actions, text="移除选中", command=self.remove_materials).pack(
            side="left", padx=6
        )
        ttk.Label(actions, text="1–8 张", style="Muted.TLabel").pack(side="right")
        self.material_list = tk.Listbox(
            materials, height=4, selectmode="extended", exportselection=False
        )
        self.material_list.pack(fill="x", pady=8)
        self.material_list.bind("<<ListboxSelect>>", self.preview_material)
        self._entry(materials, "商品名称 *", self.fields["product_name"])
        self._entry(materials, "商品分类 *", self.fields["category"])
        ttk.Label(materials, text="创作要求 / 提示词").pack(anchor="w", pady=(7, 3))
        self.brief = ScrolledText(materials, height=4, wrap="word", font=("Microsoft YaHei UI", 10))
        self.brief.pack(fill="x")

        targets = self._section("2  生成设置")
        grid = ttk.Frame(targets)
        grid.pack(fill="x")
        for index, (name, var) in enumerate(self.platforms.items()):
            ttk.Checkbutton(grid, text=PLATFORM_LABELS[name], variable=var).grid(
                row=index // 3, column=index % 3, sticky="w", padx=(0, 8), pady=2
            )
        ttk.Separator(targets).pack(fill="x", pady=8)
        for name, var in self.outputs.items():
            ttk.Checkbutton(targets, text=OUTPUT_LABELS[name], variable=var).pack(anchor="w")
        ttk.Label(
            targets,
            text="勾选拼多多白底会同时选中拼多多平台。",
            style="Muted.TLabel",
            wraplength=345,
        ).pack(anchor="w", pady=(3, 0))
        self.model_combo = self._combo(targets, "模型别名", "image_model", ("fast", "pro", "base"))
        self._combo(targets, "图片尺寸", "image_size", ("1K", "2K", "4K"))
        self._combo(
            targets,
            "画面比例",
            "aspect_ratio",
            ("auto", "1:1", "3:4", "4:5", "9:16", "16:9", "4:1", "1:4"),
        )

        api = self._section("3  API 配置与输出")
        self._entry(api, "配置文件（JSON）", self.config_path)
        buttons = ttk.Frame(api)
        buttons.pack(fill="x", pady=4)
        ttk.Button(buttons, text="选择配置", command=self.choose_config).pack(side="left")
        ttk.Button(buttons, text="加载配置", command=self.load_config).pack(side="left", padx=6)
        self.key_frame = ttk.Frame(api)
        self.key_frame.pack(fill="x")
        ttk.Label(
            api,
            text="密钥留空时使用配置或环境变量；输入仅本次有效。",
            style="Muted.TLabel",
            wraplength=345,
        ).pack(anchor="w", pady=6)
        self._entry(api, "输出目录", self.output_path)
        ttk.Button(api, text="选择输出目录", command=self.choose_output).pack(anchor="w", pady=5)
        task_actions = ttk.Frame(api)
        task_actions.pack(fill="x", pady=5)
        ttk.Button(task_actions, text="打开已有任务", command=self.choose_task).pack(side="left")
        ttk.Button(task_actions, text="恢复任务", command=lambda: self.start("resume")).pack(
            side="left", padx=6
        )

        right = ttk.Frame(panes, padding=(12, 0, 0, 0))
        panes.add(right, weight=1)
        self.notebook = ttk.Notebook(right)
        self.notebook.pack(fill="both", expand=True)
        result_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(result_tab, text="图片预览")
        self.result_tree = ttk.Treeview(
            result_tab,
            columns=("platform", "type", "status"),
            show="headings",
            height=5,
            selectmode="browse",
        )
        for key, title, size in (
            ("platform", "平台", 90),
            ("type", "图片类型", 180),
            ("status", "结果", 90),
        ):
            self.result_tree.heading(key, text=title)
            self.result_tree.column(key, width=size, minwidth=70)
        tree_scroll = ttk.Scrollbar(result_tab, command=self.result_tree.yview)
        self.result_tree.configure(yscrollcommand=tree_scroll.set)
        self.result_tree.grid(row=0, column=0, sticky="ew")
        tree_scroll.grid(row=0, column=1, sticky="ns")
        self.asset_notice = ttk.Label(result_tab, text="", wraplength=400, foreground="#915600")
        self.asset_notice.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        candidate_controls = ttk.Frame(result_tab)
        candidate_controls.grid(row=2, column=0, columnspan=2, sticky="ew", pady=6)
        ttk.Label(candidate_controls, text="候选历史（选择后预览）").pack(anchor="w")
        self.candidate_choice = ttk.Combobox(candidate_controls, state="readonly")
        self.candidate_choice.pack(fill="x")
        self.candidate_choice.bind("<<ComboboxSelected>>", self.preview_candidate)
        ttk.Label(candidate_controls, text="人工接受原因（必填；不会改为自动审核通过）").pack(
            anchor="w", pady=(5, 0)
        )
        self.reason_entry = ttk.Entry(candidate_controls, textvariable=self.accept_reason)
        self.reason_entry.pack(fill="x")
        self.accept_button = ttk.Button(
            candidate_controls,
            text="人工接受所选候选",
            command=self.accept_selected,
            state="disabled",
        )
        self.accept_button.pack(anchor="w", pady=(5, 0))
        self.preview = tk.Label(
            result_tab,
            text="添加素材后可预览\n生成图片将在这里展示",
            background="#eef2f6",
            foreground="#536477",
            font=("Microsoft YaHei UI", 12),
            compound="center",
            wraplength=400,
        )
        self.preview.grid(row=3, column=0, columnspan=2, sticky="nsew", pady=(10, 0))
        result_tab.rowconfigure(3, weight=1)
        result_tab.columnconfigure(0, weight=1)
        self.preview.bind("<Configure>", self._resize_preview)
        self.result_tree.bind("<<TreeviewSelect>>", self.preview_asset)

        analysis_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(analysis_tab, text="素材分析 / 澄清")
        ttk.Label(
            analysis_tab, text="需要澄清时选择方案，再点击「生成图片」。", style="Muted.TLabel"
        ).pack(anchor="w", pady=(0, 6))
        self.choice = ttk.Combobox(analysis_tab, state="readonly", values=("自动选择",))
        self.choice.current(0)
        self.choice.pack(fill="x", pady=(0, 8))
        self.analysis_text = ScrolledText(
            analysis_tab, wrap="word", font=("Consolas", 10), state="disabled"
        )
        self.analysis_text.pack(fill="both", expand=True)
        self.json_text = ScrolledText(
            self.notebook, wrap="word", font=("Consolas", 10), state="disabled"
        )
        self.notebook.add(self.json_text, text="结果 / 审核 JSON")
        creative_tab = ttk.Frame(self.notebook, padding=10)
        self.notebook.add(creative_tab, text="创作策划")
        ttk.Label(
            creative_tab,
            text="模型补充未指定的画面细节；用户要求、商品外观和平台规则优先。",
            style="Muted.TLabel",
            wraplength=650,
        ).pack(anchor="w", pady=(0, 8))
        self.creative_text = ScrolledText(
            creative_tab, wrap="word", font=("Microsoft YaHei UI", 10), state="disabled"
        )
        self.creative_text.pack(fill="both", expand=True)
        self.show_creative_plan(None)

        footer = ttk.Frame(root, padding=(20, 12))
        footer.grid(row=2, column=0, sticky="ew")
        self.analyze_button = ttk.Button(
            footer, text="分析素材", command=lambda: self.start("analyze")
        )
        self.analyze_button.pack(side="left")
        self.generate_button = ttk.Button(
            footer, text="生成图片", command=lambda: self.start("create")
        )
        self.generate_button.pack(side="left", padx=8)
        self.cancel_button = ttk.Button(
            footer, text="取消任务", command=self.cancel, state="disabled"
        )
        self.cancel_button.pack(side="left")
        ttk.Button(footer, text="打开输出目录", command=self.open_output).pack(side="right")
        ttk.Button(footer, text="导出当前 JSON", command=self.export_json).pack(
            side="right", padx=8
        )
        self.progress = ttk.Progressbar(root, mode="indeterminate")
        self.progress.grid(row=3, column=0, sticky="ew", padx=20)
        ttk.Label(root, textvariable=self.status, wraplength=900, padding=(20, 8)).grid(
            row=4, column=0, sticky="ew"
        )

    def _section(self, title):
        frame = ttk.LabelFrame(self.form, text=title, padding=12)
        frame.pack(fill="x", pady=(0, 12))
        return frame

    @staticmethod
    def _entry(parent, label, variable, *, password=False):
        ttk.Label(parent, text=label, wraplength=345).pack(anchor="w", pady=(5, 3))
        entry = ttk.Entry(parent, textvariable=variable, show="*" if password else "")
        entry.pack(fill="x")
        return entry

    def _combo(self, parent, label, key, values):
        row = ttk.Frame(parent)
        row.pack(fill="x", pady=(8, 0))
        ttk.Label(row, text=label).pack(side="left")
        combo = ttk.Combobox(
            row, textvariable=self.fields[key], values=values, state="readonly", width=16
        )
        combo.pack(side="right")
        return combo

    def _wheel(self, event):
        widget = event.widget
        while widget is not None:
            if widget == self.form:
                if not isinstance(event.widget, (tk.Text, tk.Listbox)):
                    self.canvas.yview_scroll(-int(event.delta / 120), "units")
                break
            widget = getattr(widget, "master", None)

    def choose_materials(self):
        files = filedialog.askopenfilenames(
            parent=self.root,
            title="选择商品素材（最多 8 张）",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.bmp"), ("所有文件", "*.*")],
        )
        self.add_materials(files)

    def add_materials(self, files):
        if self.busy:
            return
        paths = list(dict.fromkeys([*self.materials, *(Path(p).resolve() for p in files)]))
        if len(paths) > 8:
            self.status.set("最多选择 8 张素材；请减少所选图片。")
            return
        self.materials = paths
        self.material_list.delete(0, "end")
        for i, path in enumerate(paths, 1):
            self.material_list.insert("end", f"{i}. {path.name}")
        self.invalidate_analysis()
        if paths:
            self.material_list.selection_set(0)
            self.preview_material()
        else:
            self.preview_image = self.preview_photo = None
            self.preview.configure(image="", text="添加素材后可预览\n生成图片将在这里展示")

    def remove_materials(self):
        if self.busy:
            return
        for index in reversed(self.material_list.curselection()):
            self.materials.pop(index)
        self.add_materials([])

    def choose_config(self):
        path = filedialog.askopenfilename(
            parent=self.root, title="选择 API 配置", filetypes=[("JSON", "*.json")]
        )
        if path:
            self.config_path.set(path)
            self.load_config()

    def load_config(self):
        if self.busy:
            return False
        try:
            path = Path(self.config_path.get()).expanduser().resolve()
            config = AgentConfig.from_file(path)
        except Exception as error:
            self.show_error(error)
            return False
        # A newly loaded file starts a new credential form; never carry keys across providers.
        self.config = config
        self.loaded_config_path = path
        for widget in self.key_frame.winfo_children():
            widget.destroy()
        self.key_fields = {}
        services = config.services or {"legacy": None}
        for name, service in services.items():
            var = tk.StringVar()
            self.key_fields[name] = var
            label = name if service else "OpenRouter"
            self._entry(self.key_frame, f"{label} · API Key", var, password=True)
            if service:
                ttk.Label(
                    self.key_frame, text=service.base_url, style="Muted.TLabel", wraplength=345
                ).pack(anchor="w", pady=(2, 3))
        aliases = tuple(config.image_models) or ("fast", "pro", "base")
        self.model_combo.configure(values=aliases)
        if self.fields["image_model"].get() not in aliases:
            self.fields["image_model"].set(aliases[0])
        self.status.set(f"配置已加载：{path.name} · 可选模型：{', '.join(aliases)}")
        return True

    def choose_output(self):
        path = filedialog.askdirectory(parent=self.root, title="选择输出目录")
        if path:
            self.output_path.set(path)

    def choose_task(self):
        if self.busy:
            return
        path = filedialog.askdirectory(
            parent=self.root, title="选择已有任务目录（含 result.json 或检查点）"
        )
        if path:
            self.load_task(Path(path))

    def load_task(self, directory):
        if self.busy:
            return
        self.analysis = self.analysis_signature = None
        self.needs_input = False
        self.selection_options = []
        self.choice.configure(values=("自动选择",))
        self.choice.current(0)
        self._text(self.analysis_text, "当前任务暂无素材分析。")
        self.show_creative_plan(None)
        self.run_dir = Path(directory).resolve()
        self.output_folder = self.run_dir
        try:
            if (self.run_dir / "result.json").is_file():
                self.show_result(load_saved_result(self.run_dir))
                self.status.set("已打开任务 · 可查看候选或点击「恢复任务」。旧版任务仅支持查看。")
            else:
                self.assets = {}
                self.result_tree.delete(*self.result_tree.get_children())
                self.candidate_options = []
                self.candidate_choice.set("")
                self.candidate_choice.configure(values=())
                self.accept_button.configure(state="disabled")
                self.preview_image = self.preview_photo = None
                self.preview.configure(
                    image="", text="任务尚无结果清单。点击「恢复任务」检查并继续。"
                )
                self.asset_notice.configure(text="")
                self.status.set("已选择任务目录 · 点击「恢复任务」检查并继续。")
        except Exception as error:
            self.run_dir = None
            self.candidate_options = []
            self.candidate_choice.configure(values=())
            self.candidate_choice.set("")
            self.accept_button.configure(state="disabled")
            self.show_error(error)

    def session_config(self):
        path = Path(self.config_path.get()).expanduser().resolve()
        if self.config is None or path != self.loaded_config_path:
            if not self.load_config():
                return None
        config = with_credentials(self.config, {k: v.get() for k, v in self.key_fields.items()})
        for service in config.services.values():
            try:
                self.secrets.add(service.credential())
            except AgentError:
                pass
        if config.openrouter_api_key:
            self.secrets.add(config.openrouter_api_key.get_secret_value())
        return config

    def _signature(self):
        return (
            tuple(self.materials),
            self.fields["product_name"].get(),
            self.fields["category"].get(),
            self.brief.get("1.0", "end-1c"),
        )

    def _brief_changed(self, _event=None):
        if self.brief.edit_modified():
            self.brief.edit_modified(False)
            self.invalidate_analysis()

    def invalidate_analysis(self, *_args):
        if self.analysis is not None and self.analysis_signature != self._signature():
            self.analysis = self.analysis_signature = None
            self.needs_input = False
            self.selection_options = []
            self.choice.configure(values=("自动选择",))
            self.choice.current(0)
            self._text(self.analysis_text, "素材或创作要求已修改，请重新分析。")
            self._text(self.creative_text, "素材或创作要求已修改，请重新分析以更新创作策划。")
            self.status.set("输入已修改，旧分析与选择已清除。")

    def _white_background_changed(self, *_args):
        if self.outputs["pdd_white_background"].get() and not self.platforms["pinduoduo"].get():
            self.platforms["pinduoduo"].set(True)
            self.status.set("已选中拼多多平台，用于生成拼多多白底图。")

    def _pinduoduo_changed(self, *_args):
        if not self.platforms["pinduoduo"].get() and self.outputs["pdd_white_background"].get():
            self.outputs["pdd_white_background"].set(False)
            self.status.set("已取消拼多多白底图；生成白底图需要选中拼多多平台。")

    def make_request(self):
        if not self.output_path.get().strip():
            raise ValueError("请选择输出目录。")
        return build_request(
            self.materials,
            **{key: var.get() for key, var in self.fields.items()},
            platforms=[k for k, v in self.platforms.items() if v.get()],
            output_types=[k for k, v in self.outputs.items() if v.get()],
            creative_brief=self.brief.get("1.0", "end-1c").strip() or None,
            output_dir=Path(self.output_path.get()).expanduser().resolve(),
        )

    def start(self, operation):
        if self.busy or self.job.running:
            return
        try:
            config = self.session_config()
            if config is None:
                return
            if operation == "resume":
                if self.run_dir is None:
                    raise ValueError("请先打开已有任务目录。")
                run_dir = self.run_dir
                self._start_job(
                    operation,
                    lambda: resume_images(
                        run_dir, config=config, on_progress=self.job.report_progress
                    ),
                )
                return
            self.invalidate_analysis()
            request = self.make_request()
            for material in self.materials:
                if not material.is_file():
                    raise ValueError(f"素材文件不存在：{material.name}")
            config.validate_credentials(
                config.service_names(request.image_model if operation == "create" else None)
            )
            analysis = self.analysis if operation == "create" else None
            if analysis is not None:
                if self.choice.current() > 0:
                    request.selection = self.selection_options[self.choice.current() - 1]
                elif self.needs_input:
                    raise ValueError(
                        "请在「素材分析 / 澄清」中选择方案，或修改素材与要求后重新分析。"
                    )
                elif analysis.status == "failed":
                    analysis = None
            self.run_dir = None
            if operation == "create":
                self.assets = {}
                self.result_tree.delete(*self.result_tree.get_children())
                self.candidate_options = []
                self.candidate_choice.configure(values=())
                self.candidate_choice.set("")
                self.accept_reason.set("")
            self.output_folder = request.output_dir
            self._start_job(
                operation,
                lambda: (
                    analyze_materials(request, config=config)
                    if operation == "analyze"
                    else create_images(
                        request,
                        config=config,
                        analysis=analysis,
                        on_progress=self.job.report_progress,
                    )
                ),
            )
        except Exception as error:
            self._set_busy(False)
            self.show_error(error)

    def _start_job(self, operation, factory):
        self.operation = operation
        self.started = time.monotonic()
        self.progress_message = ""
        self.status.set(
            {
                "create": "正在生成…",
                "analyze": "正在分析…",
                "resume": "正在恢复任务…",
                "accept": "正在记录人工接受…",
            }[operation]
        )
        self._set_busy(True)
        self.job.start(factory)

    def accept_selected(self):
        if self.busy or self.job.running:
            return
        selected = self.result_tree.selection()
        index = self.candidate_choice.current()
        if (
            self.run_dir is None
            or not selected
            or index < 0
            or index >= len(self.candidate_options)
        ):
            self.status.set("请先打开任务并选择候选图片。")
            return
        candidate = self.candidate_options[index]
        if candidate.get("status") == "stage":
            self.status.set("中间场景不能作为最终图片接受。")
            return
        reason = self.accept_reason.get().strip()
        if not reason:
            self.status.set("人工接受必须填写原因；请先检查所选候选图片。")
            self.reason_entry.focus_set()
            return
        run_dir, asset_id, candidate_id = self.run_dir, selected[0], candidate["candidate_id"]
        try:
            self._start_job(
                "accept", lambda: accept_candidate(run_dir, asset_id, candidate_id, reason=reason)
            )
        except Exception as error:
            self._set_busy(False)
            self.show_error(error)

    def _set_busy(self, value):
        self.busy = value
        if value:

            def disable(parent):
                for child in parent.winfo_children():
                    if "state" in child.keys():
                        self.disabled_widgets.append((child, child.cget("state")))
                        child.configure(state="disabled")
                    disable(child)

            disable(self.form)
            self.choice.configure(state="disabled")
            self.progress.start(12)
        else:
            for widget, state in self.disabled_widgets:
                widget.configure(state=state)
            self.disabled_widgets.clear()
            self.choice.configure(state="readonly")
            self.progress.stop()
        self.analyze_button.configure(state="disabled" if value else "normal")
        self.generate_button.configure(state="disabled" if value else "normal")
        self.cancel_button.configure(state="normal" if value else "disabled")
        self.candidate_choice.configure(state="disabled" if value else "readonly")
        self.reason_entry.configure(state="disabled" if value else "normal")
        self.accept_button.configure(
            state="normal" if not value and self.run_dir and self.candidate_options else "disabled"
        )

    def cancel(self):
        if self.busy:
            self.job.cancel()
            self.cancel_button.configure(state="disabled")
            self.status.set("正在取消，等待任务清理…")

    def poll(self):
        try:
            while True:
                kind, value = self.job.events.get_nowait()
                if kind == "progress":
                    self.show_progress(value)
                    continue
                # Terminal events are handled only after the thread has fully exited.
                if self.job.running:
                    self.job.events.put((kind, value))
                    break
                self._set_busy(False)
                if kind == "result":
                    if self.operation == "analyze":
                        self.show_analysis(value)
                        self.notebook.select(1)
                        self.status.set(f"分析完成 · {STATUS_LABELS[value.status]}")
                    else:
                        self.show_result(value)
                elif kind == "cancelled":
                    self.status.set(
                        "已取消 · 已保存候选保留；打开任务目录后可恢复。提交状态不确定时需人工处理。"
                    )
                else:
                    self.show_error(value)
        except Empty:
            pass
        if self.closing and not self.job.running:
            self._destroy()
            return
        self.poll_timer = self.root.after(80, self.poll)

    def show_progress(self, event):
        if event.get("run_dir"):
            self.run_dir = Path(event["run_dir"]).resolve()
            self.output_folder = self.run_dir
        stage = STAGE_LABELS.get(event.get("stage"), event.get("stage", "处理中"))
        detail = f" · {event['asset_id']}" if event.get("asset_id") else ""
        attempt = f" · 第 {event['attempt']} 次" if event.get("attempt") else ""
        message = f" · {event['message']}" if event.get("message") else ""
        self.progress_message = self.safe_text(stage + detail + attempt + message)
        self.status.set(self.progress_message)

    def show_analysis(self, analysis, *, issues=(), needs_input=None):
        self.analysis = analysis
        self.show_creative_plan(analysis.creative_plan)
        self.analysis_signature = self._signature()
        self.needs_input = analysis.status == "needs_input" if needs_input is None else needs_input
        self.selection_options = []
        labels = ["自动选择"]
        all_issues = {
            issue.issue_id: issue
            for issue in [
                *analysis.issues,
                *(analysis.intent.unmet_requirements if analysis.intent else []),
                *issues,
            ]
        }
        seen_selections = set()
        for issue in all_issues.values():
            for option in issue.options:
                if option.selection is not None:
                    signature = option.selection.model_dump_json()
                    if signature in seen_selections:
                        continue
                    seen_selections.add(signature)
                    self.selection_options.append(option.selection)
                    labels.append(self.safe_text(f"{issue.message} → {option.label}"))
        self.choice.configure(values=labels)
        self.choice.current(0)
        data = analysis.model_dump(mode="json")
        if issues:
            data = {"analysis": data, "current_issues": [i.model_dump(mode="json") for i in issues]}
        self._text(self.analysis_text, self.to_json(data))

    def show_creative_plan(self, plan):
        if plan is None:
            self._text(self.creative_text, "暂无创作策划。点击「分析素材」生成；旧分析需重新分析。")
            return
        labels = {
            "scene": "场景 / 背景",
            "composition": "构图 / 机位",
            "lighting": "光线",
            "palette": "配色",
            "product_presentation": "商品展示重点",
            "mood": "氛围",
        }
        parts = ["用户明确要求（原文摘录）"]
        parts.extend(f"• {quote}" for quote in plan.user_requirements)
        if not plan.user_requirements:
            parts.append("未填写创作文字要求，按商品素材规划电商画面。")
        parts.extend(["", "模型补充建议（仅补充未指定的细节）"])
        for suggestion in plan.suggestions:
            parts.extend(
                [
                    f"{labels[suggestion.aspect]}：{suggestion.instruction}",
                    f"理由：{suggestion.reason}",
                    "",
                ]
            )
        if not plan.suggestions:
            parts.append("没有额外补充建议，沿用用户要求和默认拍摄规则。")
        self._text(self.creative_text, self.safe_text("\n".join(parts)))

    def show_result(self, result):
        self.run_dir = Path(result.run_dir).resolve() if result.run_dir else None
        self.assets = {a.asset_id: a for a in result.assets}
        self.candidate_options = []
        self.candidate_choice.configure(values=())
        self.candidate_choice.set("")
        self.accept_reason.set("")
        self.accept_button.configure(state="disabled")
        self.result_tree.delete(*self.result_tree.get_children())
        self.preview_image = self.preview_photo = None
        self.preview.configure(image="", text="本次任务没有可预览图片，请查看结果 JSON。")
        self.asset_notice.configure(text="")
        for asset in result.assets:
            self.result_tree.insert(
                "",
                "end",
                iid=asset.asset_id,
                values=(
                    PLATFORM_LABELS.get(asset.platform, asset.platform),
                    OUTPUT_LABELS.get(asset.output_type, asset.output_type)
                    + (f" / {asset.variant}" if asset.variant else ""),
                    "成功"
                    if asset.status == "succeeded"
                    else STATUS_LABELS.get(asset.status, asset.status),
                ),
            )
        output_root = Path(self.output_path.get()).expanduser().resolve()
        self.output_folder = self.run_dir or output_root
        for asset in result.assets:
            if asset.file_path and self.run_dir is None:
                path = Path(asset.file_path).resolve()
                if path.is_relative_to(output_root):
                    relative = path.relative_to(output_root)
                    if len(relative.parts) >= 3:
                        self.output_folder = output_root / relative.parts[0]
                        break
        self._text(self.json_text, self.to_json(result))
        if result.analysis is not None:
            self.show_analysis(
                result.analysis, issues=result.issues, needs_input=result.status == "needs_input"
            )
        else:
            self.show_creative_plan(None)
        self.status.set(
            f"任务结果 · {STATUS_LABELS.get(result.status, result.status)} · "
            f"成功 {sum(a.status == 'succeeded' for a in result.assets)}/{len(result.assets)} 张"
            + (" · 保存有错误，请查看 JSON" if result.output_errors else "")
            + (
                f" · 候选图 {sum(a.status not in ('succeeded', 'accepted') and a.image is not None for a in result.assets)} 张（未通过审核）"
                if any(
                    a.status not in ("succeeded", "accepted") and a.image is not None
                    for a in result.assets
                )
                else ""
            )
        )
        first = next((a for a in result.assets if a.image is not None), None)
        if first:
            self.result_tree.selection_set(first.asset_id)
            self.preview_asset()
            self.notebook.select(0)
        elif result.assets and result.status != "needs_input":
            self.result_tree.selection_set(result.assets[0].asset_id)
            self.preview_asset()
            self.notebook.select(0)
        else:
            self.notebook.select(1 if result.status == "needs_input" else 2)

    def safe_text(self, text):
        # Replace exact session credentials, including providers with non-sk key formats.
        for secret in sorted((s for s in self.secrets if s), key=len, reverse=True):
            text = text.replace(secret, "[redacted]")
        return re.sub(r"sk-[\w-]+", "[redacted]", text)

    def to_json(self, value):
        data = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        return self.safe_text(json.dumps(data, ensure_ascii=False, indent=2))

    @staticmethod
    def _text(widget, value):
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", value)
        widget.configure(state="disabled")

    def show_error(self, error):
        result = getattr(error, "result", None)
        if result is not None:
            self.show_result(result)
        if isinstance(error, (AgentError, ValidationError)):
            data = make_error_info(error).model_dump(mode="json")
        else:
            data = {"error": type(error).__name__, "message": str(error)}
        if result is not None:
            data = {"error": data, "result": result.model_dump(mode="json")}
        self._text(self.json_text, self.to_json(data))
        self.status.set("操作失败 · 请查看结果 / 审核 JSON；输入已保留。")
        self.notebook.select(2)

    def preview_material(self, _event=None):
        self.asset_notice.configure(text="")
        selected = self.material_list.curselection()
        if selected:
            self._load_preview(self.materials[selected[0]])

    def preview_asset(self, _event=None):
        selected = self.result_tree.selection()
        if selected:
            asset = self.assets[selected[0]]
            self.candidate_options = list(asset.candidates)
            self.candidate_choice.configure(
                values=[
                    f"第 {c.get('index', i + 1)} 次 · {c['candidate_id']} · {STATUS_LABELS.get(c.get('status'), c.get('status', ''))}"
                    for i, c in enumerate(self.candidate_options)
                ]
            )
            self.candidate_choice.set("")
            self.accept_reason.set("")
            self.accept_button.configure(
                state="normal"
                if self.run_dir and self.candidate_options and not self.busy
                else "disabled"
            )
            if self.candidate_options:
                accepted = [
                    (i, c)
                    for i, c in enumerate(self.candidate_options)
                    if c.get("status") == "accepted"
                ]
                selected_index = (
                    max(accepted, key=lambda item: item[1].get("acceptance", {}).get("at", ""))[0]
                    if asset.status == "accepted" and accepted
                    else len(self.candidate_options) - 1
                )
                selected_index = next(
                    (
                        i
                        for i, candidate in enumerate(self.candidate_options)
                        if candidate.get("selected")
                    ),
                    selected_index,
                )
                self.candidate_choice.current(selected_index)
            self.asset_notice.configure(text="")
            notice = []
            if asset.status == "accepted":
                notice.append("人工接受 · 未计为自动审核通过")
            elif asset.status != "succeeded" and (asset.image is not None or asset.candidates):
                notice.append("候选图 · 未通过审核")
                notice.append(asset.error or STATUS_LABELS.get(asset.status, "审核未完成"))
            if asset.stop_reason:
                notice.append("停止原因：" + asset.stop_reason)
            if asset.attempts:
                notice.append(f"生成记录：{len(asset.attempts)} 次")
            self.asset_notice.configure(text=self.safe_text("\n".join(notice)))
            if asset.image is not None:
                self._load_preview(io.BytesIO(asset.image))
            elif self.candidate_options and self.run_dir:
                self.preview_candidate()
            else:
                self.preview_image = self.preview_photo = None
                self.preview.configure(
                    image="",
                    text="生成失败\n\n"
                    + self.safe_text(asset.error or "此资产生成失败")
                    + "\n\n诊断信息也已包含在结果 JSON 中，可直接复制或导出。",
                )

    def preview_candidate(self, _event=None):
        index = self.candidate_choice.current()
        if index < 0 or index >= len(self.candidate_options) or self.run_dir is None:
            return
        candidate = self.candidate_options[index]
        asset = self.assets[self.result_tree.selection()[0]]
        label = STATUS_LABELS.get(candidate.get("status"), candidate.get("status", "未通过审核"))
        self.asset_notice.configure(
            text=self.safe_text(
                f"候选 {candidate['candidate_id']} · 第 {candidate.get('index', index + 1)} 次 · {label}"
                + (
                    "\n人工接受不代表自动审核通过"
                    if asset.status == "accepted"
                    else "\n候选历史；自动交付以资产审核状态为准"
                )
                + (f"\n停止原因：{asset.stop_reason}" if asset.stop_reason else "")
            )
        )
        self.accept_button.configure(
            state="normal" if not self.busy and candidate.get("status") != "stage" else "disabled"
        )
        try:
            if not candidate.get("file_path"):
                raise ValueError("此候选没有可用的本地图片。")
            self._load_preview(saved_image_path(self.run_dir, candidate["file_path"]))
        except (OSError, ValueError) as error:
            self.preview_image = self.preview_photo = None
            self.preview.configure(image="", text=self.safe_text(str(error)))

    def _load_preview(self, source):
        try:
            with Image.open(source) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail((1600, 1600))
                self.preview_image = image.convert("RGB")
            self._resize_preview()
        except (OSError, ValueError, Image.DecompressionBombError) as error:
            self.preview_image = self.preview_photo = None
            self.preview.configure(image="", text=f"无法预览：{self.safe_text(str(error))}")

    def _resize_preview(self, _event=None):
        self.preview.configure(wraplength=max(180, self.preview.winfo_width() - 32))
        self.asset_notice.configure(wraplength=max(180, self.preview.winfo_width() - 32))
        if self.preview_image is None:
            return
        image = self.preview_image.copy()
        image.thumbnail(
            (max(1, self.preview.winfo_width() - 24), max(1, self.preview.winfo_height() - 24))
        )
        self.preview_photo = ImageTk.PhotoImage(image, master=self.root)
        self.preview.configure(image=self.preview_photo, text="")

    def open_output(self):
        try:
            folder = self.output_folder
            if not folder.is_dir():
                folder = Path(self.output_path.get()).expanduser().resolve()
            if not folder.is_dir():
                self.status.set("输出目录尚未创建；生成后可打开。")
                return
            os.startfile(str(folder))
        except (OSError, AttributeError) as error:
            self.show_error(error)

    def export_json(self):
        widget = self.analysis_text if self.notebook.index("current") == 1 else self.json_text
        content = widget.get("1.0", "end-1c")
        try:
            json.loads(content)
        except ValueError:
            self.status.set("尚无可导出的 JSON；请先分析或生成。")
            return
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出当前 JSON",
            defaultextension=".json",
            filetypes=[("JSON", "*.json")],
        )
        if path:
            try:
                Path(path).write_text(content, encoding="utf-8")
                self.status.set("JSON 已导出。")
            except OSError as error:
                self.show_error(error)

    def close(self):
        if self.busy or self.job.running:
            self.closing = True
            self.cancel()
            self.status.set("正在结束任务并等待清理，完成后窗口会自动关闭…")
        else:
            self._destroy()

    def _destroy(self):
        self.root.after_cancel(self.poll_timer)
        self.progress.stop()
        self.root.destroy()


def main():
    root = tk.Tk()
    DesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
