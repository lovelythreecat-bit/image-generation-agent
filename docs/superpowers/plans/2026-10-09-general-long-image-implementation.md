# 通用图文长图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 实现六种通用场景的内容规划、配图、中文排版、长图与切片导出，以及桌面、CLI、Python API 和可恢复交付。

**Architecture:** 新增独立长图请求、结果与 LangGraph 编排，复用图像客户端、预算和存储原语。内容要求分为页面文字和图槽视觉要求；本地渲染与交付状态分离，可无密钥重排版。既有商品主图、三张详情图、白底图及旧任务行为保持兼容。

**Tech Stack:** Python ≥3.12、Pydantic 2、Pillow 12、LangGraph、SQLite、httpx、Tkinter、pytest、Ruff；新增 `fonttools>=4.60,<5` 用于 cmap 检查，禁止依赖网络下载字体。

**Spec:** [通用图文长图设计](../specs/2026-10-09-general-long-image-design.md)。用户已认可评审结果及多 agent 执行方式；本文件是待审阅的实施计划。

## Global Constraints

- 场景：`product_detail/brand_intro/promotion/tutorial/comparison/custom`；非商品不强制平台或素材，商品详情要求真实素材。
- 标题 1–120 字，介绍最多 20000 字，素材 0–8 张，参数最多 30 项，售后最多 10 项；ID 使用现有安全 Id 类型。
- 默认规划 4–8 段，明确用户结构允许 2–12 段；最多 6 个生成槽；无图块用空 slot_ids。
- 宽 750（400–2400），切片高 1500（500–4000），最高 16000 像素，最多 38400000 像素；超限报错，不截正文。
- 主题 `auto/clean/warm/premium`；整图、段落、切片 PNG，连续切片可逐像素复原整图；750 宽正文默认至少 26 像素。
- 页内原文由程序绘制，数字/日期逐码点保留；用户营销声明沿用 MARKETING_POLICY，不要求证明、不弱化、不编造额外专属事实。
- 原生标签/图案归图像证据；page_text/section_text 不传入图槽 must_show 和修复。
- 分别记录规划与生成台账，关联用量按 planning_id 去重；全部视觉及图像 POST 在所属 call_scope 内，不重置恢复预算。
- started/submitted 无持久响应为 generation_uncertain，禁止自动重复生图；本地重排版不创建网络客户端或要求密钥。
- 长图独立 Schema 1.0、task_type=long_image；旧商品结果 v1/v2 可查看，恢复继续只支持既有 v2；不扩大旧恢复支持。
- 人工接受必须有非空 reason 和时间，混合 accepted/succeeded 的完整成品为 accepted；保存/布局失败不能被配图成功掩盖。
- 外部 DTO 不含本地路径/台账路径；本地原子修订先 intent/stale 后产物再 current 指针，像素键与交付版本分开。
- 不做平台上传、Web 服务、拖拽编辑器、PSD、任意 HTML 或批量套版；真实付费 API 不属于默认开发验收。

## Review Focus

- 同一像素的 pending_audit→succeeded、quality_failed→accepted：全部衍生状态更新，布局失败仍 failed（任务 5）。
- 独立规划的格式纠正、HTTP 重试及复用：真实 POST 计数、无重复规划、关联用量去重（任务 4/5）。
- 标题与指定文案相同、emoji/中英混排、原文换行：按要求 ID 校验次数，字体缺字早报错（任务 1/2）。
- 无素材、零生成槽、未知类型的历史 JSON：通用页面可成功，未知类型明确报错，旧商品结果不误派（任务 3/6/7）。
- 接受、保存、切换 current 指针之间中断或文件损坏：旧产物 stale，纯本地恢复不收费、不泄漏路径（任务 3/5/6）。

## 文件与协作边界

新文件按职责分为 `long_image_models.py`（本地类型）、`long_image_contracts.py`（DTO）、`long_image_requirements.py`（来源与校验）、`long_image_fonts.py`（字体）、`long_image_render.py`（布局）、`long_image_store.py`（存储）、`long_image_product.py`（商品适配）、`long_image_vision.py`（规划/审核）、`long_image_planning.py`（规划入口）、`long_image_nodes.py`（图节点）、`long_image_runtime.py`（公开执行入口）、`long_image_cli.py`（CLI）和 `desktop_long_image.py`（桌面长图视图/行为）。

最多三个 worker 同时运行。任务 1 固定公共接口后，任务 2 和任务 3 可并行；任务 4 依赖 1/3；任务 5 依赖 2/3/4；任务 6 与任务 7 可并行；任务 8 在集成后执行。每个文件同时只有一个写入者，公共接口变更由主调度协调并更新计划/记录。

按用户指定方式：开发 agent 各自验证负责范围，完成后由一个独立代码评审 agent 检查整体；其发现统一交给独立修复 agent，随后复测与复审闭环。不会为每个小任务重复开全量评审。工作区选择在执行前核对，记录于实施账本；不替换或删除用户现有 checkout。

所有 worker 读取本计划、spec 和其依赖的 Interfaces；不创建下级 agent，不改其它任务范围，不写主调度状态，不自行推送或发布。主调度拥有 `.agent-runs/<实施运行>/`、计划进度、全套验证和集成提交。

### Task 1: 公共模型、要求来源与外部契约

**Files:** 新增 `src/image_agent/long_image_models.py`、`long_image_requirements.py`、`long_image_contracts.py`；修改 `scripts/export_schema.py`；新增 `tests/test_long_image_models.py`、`test_long_image_requirements.py`、`test_long_image_contracts.py`；导出四个新 Schema，保留旧 Schema。

**Interfaces:**
- 产生 `LongImageRequest`、`LongImageOptions`、`LongProductContext`、`LongImageRequirement`、`LongImageTextUnit`、`LongImageSection`、`LongImageSlot`、`LongImagePlan`、`LongImageIssue`、`LongImagePlanningResult`、`LongPlanningRecord`、`LongSlotAsset`、`LongImageArtifact`、`LongImageUsage`、`LongImageResult`、`RenderedLongImage`。
- `LongImagePlan` 固定字段：schema_version、planner_version、input_fingerprint、scene、theme、visual_direction、requirements、image_slots、sections、analysis_ref、resolved_selection。
- `LongSlotAsset` 固定字段：slot_id、source、source_status（available/missing/invalid）、status（生成槽使用 AssetStatus）、selected_candidate_id、candidates、image（bytes，排除序列化）、file_path、sha256、asset_revision、audit、error_info。source 不伪造模型状态/报告。
- `LongImageArtifact`：artifact_id、kind（section/page/slice）、status、availability（current/stale/absent）、image、file_path、sha256、width、height、delivery_revision、dependencies、layout；image 排除序列化。
- `LongImageResult`：schema_version、task_type、run_id、run_dir、status、plan、issues、error_info、image_assets、sections、page（可空）、slices、page_checks、usage、warnings、output_errors、delivery_revision。
- `LongPlanningRecord` 保存不透明 record_id/planning_id、请求/plan/analysis 内容哈希、选择摘要、真实用量及本地记录引用；可信绑定由存储层建立，客户端不能自报 trusted。
- `RenderedLongImage`：sections、page、slices、layout、checks、render_fingerprint；只表示本地像素与检查，交付状态由任务 5 聚合。
- `collect_text_requirements(request: LongImageRequest) -> list[LongImageRequirement]`；`validate_long_image_plan(request, plan, *, material_hashes: dict[str,str], analysis=None) -> None`；`planning_fingerprint(request, material_hashes) -> str`，失败抛 AgentError(code=plan_invalid)。
- 四个 DTO 同名加 DTO 后缀；`long_request_from_dto(dto, sources, *, font_bindings=None) -> LongImageRequest`；`long_result_to_bundle(result) -> LongImageResultBundle`（dto/blobs）；`long_planning_to_dto(result) -> LongImagePlanningResultDTO`。

- [ ] Step 1：写失败用例 `test_nonproduct_needs_no_platform_or_materials`，断言 tutorial 请求合法、product_detail 无素材拒绝；`test_slot_references_and_six_slot_limit` 拒绝悬空/重复/未引用/第七生成槽。
- [ ] Step 2：运行 `.venv\Scripts\python.exe -m pytest tests/test_long_image_models.py tests/test_long_image_requirements.py tests/test_long_image_contracts.py -q`，确认失败来自新功能未实现。
- [ ] Step 3：实现上述类型、JSON Pointer/码点原文来源、必需文案次数/顺序与反向槽引用校验；明确引号原文先由本地提取，模型不得删除。表格单元格同样为带来源的文本单元。
- [ ] Step 4：增加 `test_same_text_distinct_ids`（相同文字两个要求不能靠子串计数通过）、`test_missing_copy_fails_before_generation`、`test_bundle_contains_no_local_paths`（嵌套错误与记录也不泄漏）、`test_stale_page_is_not_current_blob`；跑上述测试及旧 `test_contracts.py`。
- [ ] Step 5：运行 Schema 导出并逐项验证旧四个导出与历史 result-v1 保持原有契约，新四个可由 JSON Schema 载入；提交本任务范围。

### Task 2: 字体、纯本地布局与像素一致切片

**Files:** 新增 `long_image_fonts.py`、`long_image_render.py`、`tests/test_long_image_render.py`、`tests/test_long_image_fonts.py`；修改 `pyproject.toml` 和 `uv.lock`（只增加 fonttools）。

**Interfaces:** 消费任务 1 类型；产生 `resolve_fonts(texts: list[str], font_path: Path|None) -> FontSet`（实际字体哈希、cmap、回退顺序）；`preflight_long_image(request) -> None`；`render_long_layout(plan: LongImagePlan, selected_assets: dict[str,LongSlotAsset], options: LongImageOptions) -> RenderedLongImage`、`render_fingerprint(plan, assets, options, fonts) -> str`。全部同步本地函数，由异步入口使用现有 compute 包装；不访问 API 或写任务目录。公开内存结果入口由任务 5 包装，最终状态不在渲染器内聚合。

- [ ] Step 1：写 `test_slices_reconstruct_page_pixels`：解码 PNG 切片后纵向拼接，断言与整图像素相等；`test_table_and_step_text_not_clipped`：核对每个文本 ID 已绘制、区域不重叠。
- [ ] Step 2：运行新渲染/字体测试确认失败；使用已安装真实字体测试中文，缺中文环境用明确 skip 原因，最终 Windows 验收必须实际执行中文用例。
- [ ] Step 3：实现六种通用版式、两列表格与多列对比表、文字实际测量、码点换行与按字符字体回退。FontTools 读取 TTF/OTF/TTC cmap；优先用户字体，Windows 微软雅黑/黑体/宋体，POSIX 常见 Noto/DejaVu，缺字返回 font_missing_glyph；不下载字体。
- [ ] Step 4：增加 `test_missing_glyph_before_paid_call`、`test_font_file_replacement_changes_fingerprint`、`test_height_limit_does_not_truncate`、`test_zero_generated_slots_render_successfully`、`test_multilingual_and_newlines_preserve_source`，验证尺寸/字号/完整配图、主题对比度及超限错误；运行新测试和 `test_images.py`。
- [ ] Step 5：添加最大宽高输入的内存检查，逐段释放中间图，禁止同时构造多份整图；依赖安装仅更新声明及锁文件，不修改系统字体。提交本任务范围。

### Task 3: 长图任务存储、可信规划绑定与原子衍生修订

**Files:** 新增 `long_image_store.py`、`tests/test_long_image_store.py`；必要修改 `run_store.py` 仅提取可共用存储原语，旧请求/恢复行为不变。

**Interfaces:** 消费任务 1 类型；`LongImageRunStore(directory=None)` 复用 RunStore 的 lock/read/write/json/operation 方法；`initialize(request, materials, config, policy, *, planning_result=None) -> None`；`request() -> LongImageRequest`；`load_planning_record(record_id, *, request, plan, analysis=None) -> LongPlanningRecord`；`save_planning_result(result, request, materials, policy) -> LongPlanningRecord`；`hydrate_result() -> LongImageResult`；`validate_inputs_and_candidates() -> None`。
- 长图 run.json：version=2、task_type=long_image、独立请求、inputs（可空）、policy、config_fingerprint、规划记录。旧无类型 v2 仍为商品任务，不给 v1 新续跑能力。
- `begin_delivery_revision(*, choices, statuses, options) -> int`、`commit_delivery_revision(result, rendered=None) -> None`、`pending_delivery_revision() -> dict|None` 固定 intent→stale→新 revision 文件→原子 current 指针→检查点提交顺序。

- [ ] Step 1：写 `test_empty_materials_persist_without_legacy_source_access`、`test_external_record_id_cannot_forge_verified_plan`、`test_relative_paths_and_hashes_are_checked`；运行新测试确认失败。
- [ ] Step 2：实现类型化长图存储、已验证规划哈希绑定和空输入；台账使用现有 BudgetLedger，不复制计数实现。
- [ ] Step 3：写 `test_delivery_intent_makes_old_page_stale`、`test_recover_each_revision_commit_boundary`、`test_corrupt_selected_candidate_is_not_current`，分别在 intent、槽元数据、段图、指针切换、清理处注入中断并核对一致性。
- [ ] Step 4：实现衍生版本保存与加载；同字节改变状态可复用像素，但元数据和 current 指针必须更新。跑新测试及 `test_process_recovery.py/test_graph_recovery.py/test_output.py`。
- [ ] Step 5：核对所有路径在任务根目录内、写入原子、锁和台账不被重置；提交本任务范围。

### Task 4: 规划、澄清、商品适配与槽审核

**Files:** 新增 `long_image_product.py`、`long_image_vision.py`、`long_image_planning.py`、`tests/test_long_image_planning.py`、`tests/test_long_image_product.py`；修改 `config.py` 增加两种视觉阶段；必要修改 `pipeline.py/selection.py/prompt.py/vision.py` 只提取商品适配所需 helper，不改变旧默认行为。

**Interfaces:** 消费任务 1/3；`LongImageVisionClient.plan(request, materials, requirements, *, analysis=None) -> LongImagePlanningResult`；`audit(slot, plan, image, *, candidate_id, materials) -> LongImageAudit`，LongImageAudit 固定 checks/passed/repairable/repair_prompt 和槽/候选 ID。
- `prepare_long_product(request, materials, *, analysis, vision) -> PreparedLongProduct`；`compile_long_product_slot(request, slot, plan, product) -> PreparedLongSlot`，返回证据、引用、提示及本槽视觉要求；不得构造虚假商品或平台来通过 CreationRequest 校验。
- 公开 async `analyze_long_image(request, config=None, *, analysis=None, policy=None, on_progress=None) -> LongImagePlanningResult`；内部 `prepare_long_plan(request, config, dependencies, store, ledger, *, plan=None, analysis=None, planning_record=None) -> LongImagePlanningResult` 供任务 5 在其台账内调用。
- PreparedLongProduct/PreparedLongSlot 为本模块类型；前者含真实分析/选择/证据快照，后者含 references、prompt、target、element_plan（商品时）及通用槽要求。

- [ ] Step 1：写六场景参数化离线规划用例、`test_required_date_missing_yields_field_issue`、`test_two_products_selection_uses_same_snapshot`；使用真实材料 hash 的 FakeVision/FakeGenerator，无网络。运行确认失败。
- [ ] Step 2：实现独立规划任务与 Ledger、planning_id/记录；追加 `plan_long_image/audit_long_image_asset` 路由，默认已有 vision。统一 `_call` 的 JSON 纠正和验证，在所属 vision scope 内执行发现、验证、规划、审核。
- [ ] Step 3：实现缺输入 issues/来源校验、同快照选择、ready 记录绑定；商品编译只包含槽视觉要求，平台为空使用通用详情规则；tutorial/brand 等不填商品审核字段。
- [ ] Step 4：增加 `test_corrective_post_is_counted`、`test_shared_budget_limits_repeated_planning`、`test_exact_copy_does_not_enter_slot_must_show`、`test_native_label_still_audited`、`test_unknown_or_missing_audit_checks_raise_audit_error`、`test_forged_or_stale_plan_rejected`；跑新测试及旧 `test_marketing_claims.py/test_creative_planning.py/test_selection.py/test_config.py/test_platforms.py`。
- [ ] Step 5：核对源码引用不是外部计划自报可信，source/none 不付费生成或审核，缺具体事实不虚构；提交本任务范围。

### Task 5: 可恢复生成、状态聚合、接受与本地重排版

**Files:** 新增 `long_image_nodes.py`、`long_image_runtime.py`、`tests/test_long_image_pipeline.py`、`tests/test_long_image_recovery.py`；修改 `__init__.py` 公开模型、DTO 和新 API，旧公开符号保留。

**Interfaces:** 消费任务 1–4；产生 spec 中 create/resume/accept/render 入口。`accept_long_image_candidate(run_dir, slot_id, candidate_id, *, reason: str) -> LongImageResult` 强制非空原因和时间；async `create_long_image(request, config=None, *, plan=None, analysis=None, planning_record=None, policy=None, on_progress=None) -> LongImageResult`；async `resume_long_image(run_dir, config=None, *, on_progress=None) -> LongImageResult`；async `render_long_image(run_dir, *, render_options=None, on_progress=None) -> LongImageResult`；同步 `render_long_image_memory(plan: LongImagePlan, selected_assets: dict[str,LongSlotAsset], *, render_options=None) -> LongImageResult` 包装任务 2 的 render_long_layout，并执行同一状态聚合。
- `aggregate_long_image(plan, assets, *, checks, output_errors) -> AssetStatus` 使用 spec 聚合表；`LongImageNodes` 节点：prepare/plan/prepare_slot/generate/save_candidate/audit/repair/finish_slot/render/compose/finish。LongGraphState 包含 phase、slot_index、result、runtime、planning_record、delivery intent。
- 进度事件沿用 stage/message/run_dir，加 slot_id、section_id、attempt；取消沿用当前线程 settle 及任务 cleanup 规则。

- [ ] Step 1：写 `test_zero_slots_and_source_only_success`、`test_inline_and_saved_plan_generation_counts`、`test_product_and_generic_audits_are_separate`；运行确认失败。
- [ ] Step 2：实现单套真正 LangGraph 编排；每槽串行，适用首图作为风格参考，商品证据优先。调用发生在稳定 operation_id 的 image scope 内，raw/saved 先于候选/检查点；不在节点里复制 HTTP 协议层。
- [ ] Step 3：写恢复注入测试 `test_submitted_without_raw_never_regenerates`、`test_saved_raw_recovers_without_post`、`test_same_bytes_audit_and_accept_update_all_outputs`、`test_accept_then_layout_failure_is_failed`、`test_local_render_resume_requires_no_credentials`；使用 FakeTransport 统计实际 POST，不只统计 wrapper 调用。
- [ ] Step 4：实现状态表、usage execution/linked_planning/display_total 去重、接受记录及原子修订；render 白名单只有 width/theme/slice_height/font_path，拒绝内容覆盖；audit_error 恢复只重审。补 `test_cancel_preserves_saved_candidates`、`test_usage_not_duplicated_on_resume_or_export`、`test_save_failure_blocks_page_success`。
- [ ] Step 5：运行本任务 tests 及旧 `test_entrypoint_recovery.py/test_process_recovery.py/test_public_api.py/test_pipeline.py`；检查旧符号与 task type 报错；提交本任务范围。

### Task 6: CLI、JSON 媒体绑定与文档示例

**Files:** 新增 `long_image_cli.py`、`tests/test_long_image_cli.py`；修改 `__main__.py`、`README.md`；新增 `examples/long-image/{tutorial,promotion,product-detail}.json`，不含密钥或本机路径。

**Interfaces:** `add_long_image_parser(subparsers) -> None`；`run_long_image_cli(args) -> int`；子命令 long-image analyze/create/resume/accept/render。本地 --font-path/--out/任务目录参数不进入请求 JSON；--plan-record 绑定可信本地记录，显式商品选择可提供同一分析文件。

- [ ] Step 1：写无素材 tutorial 的 JSON 创建、规划复用、无密钥 render、带 reason 接受的 CLI 测试；写未知 task_type/version/绝对媒体路径拒绝用例，确认失败。
- [ ] Step 2：接入新子命令、request DTO 和 media_id 映射；打印各阶段用量、整图/段落/切片及状态，不打印密钥、数据前缀或长提示词。
- [ ] Step 3：按 spec 退出码表验证全部状态，尤其 accepted=4、保存失败=1、audit_error=5、generation_uncertain=7，零生成槽成功=0；普通 CLI 及旧历史查看保持兼容。
- [ ] Step 4：运行 `test_long_image_cli.py/test_cli.py/test_contracts.py/test_live_smoke.py`；更新 README 的 API/CLI/桌面入口、字体要求、生成调用与规划预算、旧结果查看和恢复边界。
- [ ] Step 5：验收示例 JSON 可绑定实际测试素材执行且不硬编码商品平台；提交本任务范围。

### Task 7: 中文桌面长图模式、澄清与预览

**Files:** 新增 `desktop_long_image.py`、`tests/test_desktop_long_image.py`、`tests/test_desktop_long_image_ui.py`；修改 `desktop.py/desktop_runtime.py`。

**Interfaces:** `LongImagePanel(app)` 维护长图输入/规划/结果，`make_request() -> LongImageRequest`、`show_planning(result) -> None`、`show_result(result) -> None`、`rebuild_saved() -> None`；`load_saved_task(run_dir) -> CreationResult|LongImageResult` 先按类型/版本分派。既有 load_saved_result 保留商品用途及签名，旧测试行为不变。

- [ ] Step 1：写 `test_modes_preserve_fields_but_clear_stale_plan`、`test_nonproduct_hides_product_requirements`、`test_selection_submits_same_analysis`，确认失败。
- [ ] Step 2：沿用现有 ttk 样式、中文字体、BackgroundJob、取消/错误处理及候选原因输入；长图模式支持六场景、主题/内容、可选素材、折叠布局参数，商品详情才显示商品及平台。
- [ ] Step 3：规划视图显示区块、必需原文、issues、调用数量及用量去重；未 ready 不能生成。历史长图支持修改四项布局设置并“重新排版”，无密钥可执行。
- [ ] Step 4：实现整图适应宽度、缩放/纵向滚动、段落/切片/配图切换，使用缓存缩略图避免整图多份内存；键盘可达、错误定位字段、忙时按钮禁用，鼠标滚轮按焦点作用于参数或预览区域。
- [ ] Step 5：运行新 UI tests 与 `test_desktop.py/test_desktop_ui.py`；实际 Tkinter 离线样例检查切换、滚动、取消、接受及本地排版。按现有 UI 世界使用 product-design-consistency/ui-ux-pro-max/impeccable，不重建整套视觉规范；提交本任务范围。

### Task 8: 集成验收、独立代码评审与修复闭环

**Files:** 新增 `scripts/long_image_offline_demo.py`、`tests/test_long_image_acceptance.py`；更新本计划进度和 `docs/superpowers/reviews/2026-10-09-general-long-image-code-review.md`。产物放 `out/long-image-offline-demo/`；角色报告和证据归实施 `.agent-runs`。

**Interfaces:** demo 只使用确定性规划/配图替身和真实本地字体，生成商品详情、活动宣传、教程三个场景的 result.json、整图、语义段落和切片；不得默认调用真实 API。

- [ ] Step 1：验收六场景完整入口、共享槽只生成一次、原文/表格完整、缺输入及失败状态、独立规划用量与恢复；运行 `python -m pytest tests/test_long_image_acceptance.py -q`。
- [ ] Step 2：运行全量 `python -m pytest -q`、`python -m ruff check src tests scripts`、`python scripts/export_schema.py`，核对 Schema 差异；每项保存退出码和当前代码快照。构建 wheel 并在已有隔离 venv 验证新增公开符号及旧入口。
- [ ] Step 3：生成三份离线样例，实际打开整图及各段检查字体、层级、表格、完整性；再用真实 Tkinter 查看长图模式。将视觉缺陷集中修复并确认一次，保留结果及局限。
- [ ] Step 4：派独立 reviewer 阅读完整 diff、spec、实施计划和验证证据，检查恢复计费、状态、媒体绑定、原文、兼容、UI；不得把自述测试通过当评审依据。
- [ ] Step 5：将完整 findings 交给另一修复 agent，写有意义的失败回归用例、修复、运行受影响 tests，再由 reviewer 针对修复范围复审。仍有问题按用户要求继续闭环，不静默丢弃；所有 agent 的修改范围和 attempt 可追踪。
- [ ] Step 6：最终代码冻结后重跑受影响回归及必要全量验证，记录真实结果，提交所有必要改动；报告成品路径、使用方式、测试与评审结果。保留本地任务记录，不推送/发布，不宣称离线验证代表真实模型出图质量。

## 计划自查与启动顺序

实施前基线：在 `F:\Work\电商\imageAgent`、HEAD `1c1950a` 运行 `.venv\Scripts\python.exe -m pytest -q`，退出 0，`452 passed in 60.50s`。当前 venv 无 fontTools，安装及锁文件更新归任务 2；尚未改动产品代码。

覆盖映射：输入/DTO/原文→1；字体/布局/切片→2；可信绑定/持久恢复→3；六场景/澄清/商品适配/审核/规划预算→4；生成/修复/接受/状态/用量/恢复→5；CLI/JSON/文档→6；桌面模式/预览→7；最终回归、视觉验收和独立评审修复→8。Review Focus 各项已有对应失败用例。

审批后先核对执行工作区和基线、初始化实施账本。任务 1 完成后启动 2/3；3 完成后 4 可与 2 并行；5 集成后并行 6/7；8 统一验证与评审。若公共类型必须调整，先通知依赖 worker，暂停该接口的消费者再提交兼容更新，禁止同时改同一文件。

**执行方式已确认：多 agent 开发，最终独立代码评审，另一 agent 修复，再复测复审。** 计划审阅只确认实施内容，不重新选择执行方式。
