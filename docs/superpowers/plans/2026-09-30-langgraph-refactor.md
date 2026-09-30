# LangGraph Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans task-by-task. User explicitly selected multiple agents by role. Independent files may be implemented concurrently; shared interfaces require coordinator agreement.

**Goal:** 交付可恢复、候选不丢失、修复利用证据且预算受控的 LangGraph 图片工作流。

**Architecture:** 保留公共入口与业务模块；拆分准备、生成、候选保存、审核、修复、组审和交付为图节点。SQLite + 文件持久化；恢复及 UI 共用同一执行引擎。

**Tech Stack:** Python 3.12、LangGraph、SQLite checkpoint、Pydantic、httpx、Pillow、Tkinter、pytest。

**Spec:** docs/superpowers/specs/2026-09-30-langgraph-refactor-design.md

## Global Constraints

- 全量迁移平台/输出/API/CLI/桌面；旧编排最终不作为生产后备。
- candidates/、approved/、accepted/ 分离；历史候选不覆盖。
- 质量默认最多修复 2 轮（可配置）；审核异常单独恢复原图；预算持久化且 HTTP 重试计入。
- 本次 live 全程生图 ≤10、视觉 ≤30；只有主协调者可发起真实调用。
- 凭证不写文档、日志、图状态、仓库提交。不得读取或提交 examples/秘钥.md；live 凭证只读用户指定仓库外文件。
- 保护当前未提交改动和历史 out/；不全量 git add，不删除用户资料。
- 工作者仅修改分配文件，不自行派生子 Agent，不执行真实 API。

## Review Focus

- 已生成后审核 schema 错误：恢复不产生第二次生图，候选仍可查看。
- API 提交后响应未保存：不自动重复收费，必须报告不确定性。
- 恢复时预算或输入文件变化：持久化计数不可重置，校验损坏与任务互斥。
- 简单宣传图的功能声明：既不能误拒无文案主图，也不能豁免真正可见的外观细节。
- 多平台详情 + 白底：保留部分结果、真实反映组审状态、候选不得混入成功包。

## Task 0 — 基线和契约（协调者）

- [x] 保存当前源码/测试/文档的本地备份，排除凭证与 out；建立 codex/langgraph-refactor 分支。
- [x] 确认依赖版本可用、Python 3.12 可安装；将精确兼容区间加入 pyproject.toml。
- [x] 按用户授权执行后续任务；维护 docs/superpowers/plans/2026-09-30-langgraph-progress.md 记录接口与证据。

## Task 1 — 审核语义与定向修复（业务角色）

**Files:** models.py、selection.py、quality.py、prompt.py、vision.py、compliance.py；对应新/旧测试。

**Interfaces:** 新增 `build_repair_prompt(request, target, context, plan, quality, *, previous_prompt="") -> str`；原 build_prompt 保留。事实增加可验证性类别，缺省兼容旧分析；不能仅靠命中特定商品词判不可验证。新增资产/结果 v2 状态和候选元数据须与引擎约定。

- [x] 写回归测试：宣传文字功能不可强制呈现、可见细节仍必须呈现、数字保持不变、产品原生印花保留。
- [x] 先运行确认新测试失败，再实现分类与冻结要求编译、正确的审核指令。
- [x] 定向修复包括失败指标/理由/保持项/原始身份权威，测试不同失败产生不同修复指令。
- [x] 聚焦复核 ID 校验接入格式纠正循环，非适用指标不驱动无关修复。
- [x] 运行本任务测试，记录结果与对外接口，等待独立审查。

## Task 2 — LangGraph、持久化与预算（引擎角色）

**Files:** pipeline.py、graph_runtime.py、graph_nodes.py、execution.py、run_store.py、output.py、transport.py；对应图/恢复测试。不得修改 Task 1 或 Task 3 文件。

**Interfaces:** 保留 `run_pipeline(request, config, *, dependencies, analysis=None)`，增加可选 policy/progress/store 参数；`create_images(request, config=None, *, analysis=None, policy=None, on_progress=None)`；`resume_images(run_dir, config=None, *, on_progress=None)`；`accept_candidate(run_dir, asset_id, candidate_id, *, reason)`。`ExecutionPolicy` 在 execution.py 定义质量/审核/实际提交预算。事件为 JSON 兼容 dict，含 stage、asset_id、attempt、message、run_dir。

- [x] 写图和存储测试：候选审核前落盘、审核异常恢复零生图、所有候选独立、输入变动/损坏验证。
- [x] 提取上下文准备，所有资产改走真实 StateGraph；SQLite checkpoint 保存无凭证/无字节的状态。
- [x] 持久化输入和逐候选审核，成功候选迁移到 approved（candidates 不保留成功图）；人工接受到 accepted 且独立状态。
- [x] 实现审核重试、最多2轮质量修复、连续无新增可操作反馈停止、取消恢复和并发任务锁。
- [x] transport 在每次实际 POST 前原子预占预算；恢复不能重置；不确定提交不得静默重放。
- [x] 组审完整性和失败状态可见；覆盖所有目标类型并移除旧修复循环。
- [x] 运行对应故障测试，记录接口及结果，等待独立审查。

## Task 3 — CLI、桌面与结果契约（接入角色）

**Files:** __init__.py、__main__.py、contracts.py、desktop.py、desktop_runtime.py、scripts/export_schema.py、schema/、README.md；对应接口/UI测试。

**Consumes:** Task 2 的 create_images/resume_images/accept_candidate、ExecutionPolicy 和事件契约；Task 1 v2 状态。

- [x] 写测试：旧入口仍可调用、新结果状态可序列化、未通过及人工接受不进入自动成功包。
- [x] CLI 增加 resume 和 accept 入口与明确退出码；保留现有参数。
- [x] 桌面展示阶段/次数/停止原因，增加恢复任务与人工接受动作；工作线程进度送主线程。
- [x] 文档说明新目录、恢复限制、预算及 v1/v2 边界；导出 v2 schema，不覆盖 v1 历史契约。
- [x] 运行入口与 UI 测试，记录结果，等待独立审查。

## Task 4 — 集成、独立审查与真实验收

- [x] `python -m pytest -q`；`python -m ruff check src tests scripts examples`；`python -m ruff format --check src tests scripts examples`。
- [x] `python -m compileall -q src scripts`；`python -m build --no-isolation`；验证 wheel 可导入、CLI 帮助及恢复入口。
- [x] 独立审查具体 diff，重点检查五类 Review Focus；修复后仅重跑相关测试再做完整集成检查。
- [x] 建立跨 live 运行持久化预算账本，真实手机壳验收；对比历史误拒，人工检查候选。不能临时放宽审核门槛。
- [x] 记录实际调用次数、输出位置、通过与未验证项目，更新进度，报告交付。
