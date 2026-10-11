# Product Generated Long Image Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 用户已选择自主多 agent 实施；无需重复审批，不做 Git 操作或 worktree。

**Goal:** 在现有商品图片任务中生成完整设计区段组成的电商商品长图，复用真实商品证据和持久化审核管线。

**Architecture:** `CreationRequest` 添加 long_image 输出和有界参数，`build_targets` 每平台建立一个长图 Asset。商品 graph 为该 Asset 路由到结构化策划、逐段完整生图、逐段外观/文案审核与纯像素拼接阶段；重逻辑放新 product_long_image 模块，继续使用原 RunStore、BudgetLedger 和 transport。桌面在商品类型复选区新增参数和长图预览，保留历史通用长图内部加载兼容。

**Tech Stack:** Python >=3.12、Pydantic 2、LangGraph、Pillow、httpx、Tkinter、pytest；无新依赖。

**Spec:** `docs/superpowers/specs/2026-10-10-product-generated-long-image.md`

## Global Constraints

- 当前工作区实施；禁止所有 Git 操作、分支/worktree、提交及远端操作。
- 实现 agents 不修改 `.agent-runs/.../state.json`，仅主调度更新运行账本。
- 图像生成模型必须生成每个区段的完整画面，包含商品、背景、短文案、场景和版式。
- 程序仅等比标准化宽度、纵向拼接、连续像素切片和保存。
- 禁止将模型产物降为局部配图，再由程序绘制标题、正文、参数表、装饰版式或水印到商品长图。
- 每平台一个 `Asset(output_type="long_image", variant=None)`，属于同一商品任务；未选长图不增加模型调用。
- 默认 width=750、segment_count=6、segment_aspect_ratio="3:4"；实际高度按完整段像素计算，约6000。
- 成品宽400–2400、高不超过60000、总像素不超过60,000,000；切片1500高且像素可完整还原。
- 旧 create_long_image/DTO/CLI/保存任务兼容；保持用户营销原文，模型不得自行增加未知功效或参数。
- Python 命令使用 `.venv/Scripts/python.exe` 且 `PYTHONDONTWRITEBYTECODE=1`；实现者只运行各自定向测试，主调度统一运行全量测试。

## Review Focus

1. 混选长图与主图/详情图/白底：共用一次商品准备，长图文案不污染其他输出，固定三图审核不包含长图。
2. 多商品/显式 selection/过期快照：策划只用当前选中主体事实和原始参考，无法选择时零长图生图。
3. required_copy/known_info 含中文、换行、数字与用户营销语：输入精确保留且进提示词；新增模型事实不得成为可通过审核的默认文案。
4. 提交后取消/崩溃、审核错误、本地写入失败：已保存段不重生图，uncertain 不重提，预算不重置。
5. 大图、损坏文件、快速预览切换/窗口关闭：尺寸有界、哈希校验、后台解码、迟到结果拒绝且不碰 Tk 工作线程。

---

## 文件所有权与执行顺序

两个实现 agent：BACKEND 和 UI；一个独立 REVIEW agent。UI 可按固定 options 契约并行实现；完整联调等待 BACKEND。不得递归派发 agents。主调度负责账本和全量验证。

| 所有者 | 文件 |
|---|---|
| BACKEND | `src/image_agent/models.py`, `contracts.py`, `__init__.py`, `prompt.py`, `config.py`, `vision.py`, `graph_nodes.py`, `graph_runtime.py`, `run_store.py`, `output.py`（必要处）；新 `product_long_image_models.py`, `product_long_image.py`, `product_long_image_render.py`；新 backend tests；`README.md` 的新 API/差异说明 |
| UI | `src/image_agent/desktop.py`, `desktop_long_image.py`, `desktop_runtime.py`（必要处）；`tests/test_desktop*.py` 与新 `tests/test_desktop_product_long_image.py` |
| REVIEW | 只读审查，写自己的 run/reports 和 evidence；发现问题交回对应所有者修复 |

`generate.py`/`transport.py`/`image_protocols.py` 现有接口足够，原则上无需修改；若发现必要变更仅 BACKEND 修改并报告原因。旧 `long_image_models/planning/product/render/runtime/nodes` 不做商品新路径迁移，继续旧 API。UI 不改 `models.py`/`contracts.py`，BACKEND 不改 desktop 文件/测试。不修改过去规格文档。

### Task 1 (BACKEND): 商品请求和目标契约

**Files:** Modify `models.py`, `contracts.py`, `__init__.py`, `prompt.py`; Test new `tests/test_product_long_image_models.py` and existing `test_models.py/test_contracts.py/test_prompt.py/test_public_api.py`.

**Interfaces:**
- Produces `ProductLongImageOptions(width: int=750, segment_count: int=6, segment_aspect_ratio: Literal["3:4","4:5","9:16"]="3:4", known_info: str="", required_copy: list[str]=[])` in `models.py`.
- Produces `CreationRequest.long_image: ProductLongImageOptions | None = None`, same optional DTO field; `OutputType`/`OUTPUTS` include `long_image`.
- Existing `build_targets(request, presentation_mode)` emits one `<platform>.long_image.default` target per platform; ratio for generation is options.segment_aspect_ratio and independent of request.aspect_ratio used by ordinary images.
- UI consumes exact fields above; final result remains `CreationResult/Asset` without a new required result type.

- [ ] Add `test_long_output_defaults_and_mixed_targets`: long-only defaults equal750/6/3:4; with main/detail/white on taobao+pinduoduo target counts and variants preserve current rules plus two long assets.
- [ ] Add parametrized `test_long_options_reject_invalid` for width399/2401, count2/13, booleans, ratio1:1, known_info12001 chars,31 copy entries, blank copy and501 chars; accept exact endpoints. `test_long_copy_preserves_codepoints` preserves spaces/newlines/digits in nonblank copy.
- [ ] Add `test_long_dto_binding_and_legacy_requests`: options survive request_from_dto; old DTO/request defaults still read; final candidate blob exported without source paths.
- [ ] Run tests above with `.venv/Scripts/python.exe -m pytest tests/test_product_long_image_models.py -q`, observe new behavior fails before implementation.
- [ ] Implement contract and target changes; no default long target for old requests. Export options publicly. Do not change `analysis_fingerprint` because new layout/content options do not alter discovery inputs.
- [ ] Run focused contract/prompt/public tests; record output in backend evidence. If enumeration-length assertions exist, migrate only generic target count assertions to include long when selected, retaining old selection-specific counts.

### Task 2 (BACKEND): 商品专属策划和完整区段提示词

**Files:** Create `product_long_image_models.py`, `product_long_image.py`; Modify `config.py`, `vision.py`; Test new `tests/test_product_long_image_planning.py`.

**Interfaces:**
- Consumes request/target and ready `PreparedContext` from existing prepare_context, unchanged `compile_element_plan/select_references/bind_generation_references`.
- Produces schemas `ProductLongImageCopy/Section/Plan` and `ProductLongImageCopyAudit` defined by spec.
- `VisionClient.plan_product_long_image(self, request: CreationRequest, target: AssetTarget, context: PreparedContext) -> ProductLongImagePlan` uses `_call` and new stage `plan_product_long_image` falling back to configured vision route.
- `VisionClient.audit_product_long_image_copy(self, request, section, plan, image: bytes) -> ProductLongImageCopyAudit` uses `_call`, stage `audit_product_long_image_copy`, fallback vision. Existing audit_image handles physical identity; text audit handles plan/input fidelity.
- `product_long_image_fingerprint(request, target, context) -> str`; `validate_product_long_image_plan(request, target, context, plan) -> ProductLongImagePlan`; `build_product_long_image_prompt(request, target, context, element_plan, plan, section, *, feedback: str="") -> str`.

- [ ] Add `test_planning_receives_selected_product_and_known_inputs`: Fake transport records selected blue cup subject/facts, original image, known_info and multiline required_copy; excludes unselected red cup facts; returns6 authored, nonidentical sections/style and correct provenance.
- [ ] Add `test_plan_rejects_unknown_fact_or_missing_exact_copy`: unknown/unselected fact IDs, nonexistent quote, missing required_copy, duplicate IDs, count mismatch reject before any image call. `test_missing_material_or_efficacy_is_not_a_default_module` plans supported visual detail without fabricated composition/effect.
- [ ] Add `test_full_section_prompt_contains_copy_and_identity`: each prompt contains exact section copy, style, specific product facts/selected reference mapping; explicitly asks to design entire section; no legacy local-text drawing instruction. Use required user claim verbatim without new proof requirement.
- [ ] Add fingerprint tests changing selection/known_info/copy/platform/options makes long plan stale, same unchanged analysis may still be used to prepare a new plan.
- [ ] Run failing planning tests, then implement schemas, bounded response/source validation, vision methods/routes and compiler. Planning receives selected raw images and provenance, not just category name. Persist a reviewable structured plan at graph boundary in Task3.
- [ ] Run planning, vision/config/prompt regressions; record output. Preserve existing marketing policy for other outputs.

### Task 3 (BACKEND): 持久化逐段生图、审核、拼接和恢复

**Files:** Modify `graph_nodes.py`, `graph_runtime.py`, `run_store.py`, optionally `output.py`; extend `product_long_image.py`; Create `product_long_image_render.py`; Test new `tests/test_product_long_image_pipeline.py`, `test_product_long_image_recovery.py`, `test_product_long_image_render.py`.

**Interfaces:**
- Existing `create_images/run_pipeline/resume_images/accept_candidate` signatures unchanged.
- `ProductLongImageWorkflow(nodes)` uses existing GraphNodes references; async methods `plan(state)`, `generate(state)`, `save_candidate(state)`, `audit(state)`, `compose(state)` route phases `long_plan`, `long_generate`, `long_save_candidate`, `long_audit`, `long_compose`. GraphNodes stage adapters delegate, node wrapper checkpoints once per operation. Ordinary target path unchanged.
- `compose_product_long_image(images: Sequence[bytes], *, width: int, slice_height: int=1500) -> tuple[bytes, list[bytes], list[dict[str,int]]]` returns full PNG, contiguous slice PNGs and ordered segment y/width/height boundaries. This pure module has no font, ImageDraw or layout-render dependency.
- Runtime state keyed by stable asset ID holds immutable plan, section index, per-segment refs/attempts/quality/text audits. Operation IDs include asset ID, section ID and attempt so journaling and ledger distinguish calls. Final candidate metadata records plan, source segment hashes/audits, dimensions/boundaries/slice refs. Any intermediate Asset.candidates rows use status=stage.

- [ ] Add fake-image pipeline `test_mixed_outputs_share_discovery_and_complete_long_sections`: one preparation; 6 complete design image calls for long; model refs correspond to selected product; one final long Asset; no long asset in audit_detail_set. Unselected long causes zero new planning/generation calls.
- [ ] Add `test_product_path_never_draws_text_or_uses_old_renderer`: spy/raise on `ImageDraw.text/multiline_text`, old render_long_layout/render runtime; new pipeline succeeds using predesigned segment fixture pixels only. Assert model prompts contain complete section copy (fixed-six role placeholders alone fail test).
- [ ] Add `test_long_quality_is_aggregate_and_repairs_only_failed_segment`: one segment identity/copy fail triggers bounded strict repair for that segment; all original references preserved; exhausted text audit cannot mark full image succeeded. Complete failed segments produce reviewable full candidate, missing segment produces no complete candidate. Accept final keeps accepted; reject stage acceptance.
- [ ] Add renderer pixel tests:6x(750,1000) -> (750,6000); off-width fixtures preserve entire borders after proportional normalization; last slice shorter; rejoin slices pixel-identical to full image; >60000 height or >60m pixels fails without crop/partial success.
- [ ] Add recovery tests for: saved operation before node checkpoint; interrupted between segment save/audit; submitted response missing; pending copy audit; local composition/write exception; tampered segment/raw/slice hash; cancelled task. Assert generated operation IDs are never reposted, image usage stable for audit/local retries, uncertain blocked, all refs verified before paid work.
- [ ] Run failing new tests. Extend STAGES and prepare_asset route to long_plan only for long targets. Use current node call_scope for vision and nested image scope at each actual generation. Do not loop multiple paid segment submissions inside one uncheckpointed stage.
- [ ] Implement workflow with raw/journal saved before audit, full section strict repair, copy audit plus existing physical audit, first approved continuity reference when capacity allows. Raw/segment bytes and provenance persist; no cleanup removes source sections. Preserve graph exception statuses.
- [ ] Implement pure pixel assembly and atomic file writes; final succeeded candidate iff all segment checks pass. Segments awaiting/error audit must route long_audit on resume; failed local phase resumes long_compose. Add runtime/candidate reference validation in RunStore as necessary and keep historical state defaults valid.
- [ ] Run focused new backend tests plus pipeline/process/graph/entrypoint recovery, appearance/marketing/transport regressions. Update README with complete new request example and distinction from legacy local-text API. Hand off backend contract verification to UI/main scheduler.

### Task 4 (UI): 商品复选项、参数与当前结果预览

**Files:** Modify `desktop.py`, optionally `desktop_long_image.py/desktop_runtime.py`; Test new `tests/test_desktop_product_long_image.py` plus existing desktop tests. No writes to models/contracts/backend files.

**Interfaces:**
- Consume `OUTPUTS`, `ProductLongImageOptions` and `CreationRequest.long_image` from Task1. Consume unchanged create_images/CreationResult/Asset with output_type long_image; preview full candidate through asset.image/file_path and existing candidate selection.
- Add `OUTPUT_LABELS['long_image']='长图'` explicitly; avoid positional zip silently missing label.
- UI-only `ProductLongImageControls` may live desktop_long_image.py and expose `options() -> ProductLongImageOptions`, `set_visible(selected: bool) -> None`; fixture-accessible `app.product_long_controls` and `.frame/.fields/.known_info/.copy_text` for Tk tests. Reuse current app.fields style_hint/brief inputs.

- [ ] Add `test_long_checkbox_shows_parameters_without_mode_switch`: default hidden, selection shows frame, mode stays product, material paths/analysis same; long can mix with each output; no visible mode_choice offering 图文长图. Retain internal set_mode/legacy saved load behavior.
- [ ] Add `test_long_request_serializes_exact_options`: fields default750/6/3:4, supplied known_info and required_copy sent exactly; unselected requests long_image=None; cancelling selection keeps form values. Invalid fields block job with useful message and do not load/restart a worker. Editing long params does not clear unrelated product discovery.
- [ ] Add `test_create_passes_analysis_selection_for_mixed_long`: monkeypatch desktop.create_images; receives same analysis snapshot/selected subject IDs in single request; one job starts; ordinary output fields remain unchanged.
- [ ] Add preview tests `test_long_asset_scroll_zoom_and_candidate_notice`, `test_late_decode_cannot_replace_new_asset`, `test_close_joins_preview_worker`: full long image fit width/scroll, zoom local, candidate status/accept reason preserved, work decoded off Tk thread, last-current image visible after switching and close.
- [ ] Run new Tk tests before implementation (if options unavailable, wait for Task1 rather than patch backend). Add controls beneath output checkbox group. Remove visible top mode selector, keep internal/historical task compatibility methods. Use default6 segments as visual count with hint approximately6000 high; no promise exact6153.
- [ ] Reuse/extract existing `_PreviewWorker` for product full-long scrolling in current preview tab; ordinary images keep current presentation, worker results are token/epoch-guarded. Avoid invoking old local rebuild/render actions for product long images.
- [ ] Run all desktop-focused tests; preserve internal old mode, source decode/cancel/history tests. Update assertions only if tied to now-removed visible mode selection, not to old API behavior. Report files and test evidence.

### Task 5 (REVIEW/main scheduler): 独立审查和完整验收

**Files:** Read only product code/tests/spec/plan and implementer evidence. Write own review report/evidence; no code ownership conflicts.

- [ ] Verify exact source fact flow (MaterialAnalysis -> ResolvedSelection -> plan -> prompt -> reference binding) with two-product and required-copy fixtures; inspect both prompts and model payloads.
- [ ] Trace every new output phase to same ledger/journal/store; check recovery chooses long audit/compose and cannot repost uncertain image call. Check all new persisted refs are validated including slices and source segments.
- [ ] Confirm no new render path calls local text renderer, fonts, draw.text, caption overlays or default six universal prompts; fixture image generation is acceptable only as test evidence, never visual-quality evidence.
- [ ] Confirm UI long selection is a checkbox in product mode, no visible separate switch; actual call is create_images and options/selection pass through. Verify live Tk layout/scroll where environment permits; screenshots are evidence, not generated output-quality proof.
- [ ] Main scheduler runs `$env:PYTHONDONTWRITEBYTECODE='1'; .venv/Scripts/python.exe -m pytest -q` and `.venv/Scripts/python.exe -m ruff check src tests scripts examples`, records actual output; no Git diff/status commands. Use content/baseline comparisons only if needed.
- [ ] Review legacy tests: keep local-copy isolation assertions for create_long_image; add opposite model-copy coverage only for new product path; old DTO/history without options read unchanged. Review source claim provenance and model-copy verification limitations against spec.
- [ ] Report P0/P1/P2 findings with concrete file/line and triggering example. Hand required fixes back to responsible owner, run affected checks again; main scheduler final full run only if changes justify it. If no real provider output inspected, state functional pipeline verified but real-model visual acceptance unverified.

## Self-review / handoff

Spec coverage maps request to Task1, source-grounded design to Task2, full model section generation/recovery/pixel delivery to Task3, checkbox/preview to Task4, all five Review Focus cases to tests in Tasks1–4 and independent Task5. Interface names/options/defaults agree across both agents. Existing generic tests are retained with explicit API scope; no old assertion forces new text isolation. No repeated confirmation, commit or worktree steps are part of execution because user authorization and no-Git constraint override those skill defaults.
