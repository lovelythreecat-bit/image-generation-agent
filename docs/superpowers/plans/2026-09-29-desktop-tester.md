# Desktop Tester Implementation Plan

> **For agentic workers:** Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 在现有 Python agent 上提供轻量中文桌面测试入口。
**Architecture:** 桌面模块负责 UI，runtime 负责请求和后台任务；原有公开 API 承担分析、生成、落盘。
**Tech Stack:** Python 3.12+ / Tkinter / Pillow / pytest。
**Spec:** `docs/superpowers/specs/2026-09-29-desktop-tester-design.md`

## Global Constraints
- 无新增运行依赖，无 HTTP 服务。
- 密钥仅内存保存，不修改环境或配置文件。
- 当前工作区包含未提交的最新 API，原地新增模块，不覆盖既有改动。

## Review Focus
- 立即取消及重复取消不会错过任务初始化或中断清理。
- 不同 API 服务的密钥不会相互借用。
- 修改素材或 brief 后不能复用旧选择。
- partial/failed/needs_input 与保存失败均需可见。
- 缩小窗口仍可滚动访问参数；后台不操作 Tk。

## Task 1: Runtime
Files: `src/image_agent/desktop_runtime.py`, `tests/test_desktop.py`.
Interfaces: `build_request(paths, **fields)`, `with_credentials(config, keys)`, `BackgroundJob.start(factory) / cancel() / events`.
- [x] 写测试并观察 RED：请求验证、服务密钥隔离、成功/异常、取消等待清理、禁止重复提交。
- [x] 实现 runtime 并运行 `python -m pytest tests/test_desktop.py -q`，预期全部通过。

## Task 2: Desktop and launcher
Files: `src/image_agent/desktop.py`, `scripts/desktop.py`, `start-desktop.cmd`, `tests/test_desktop_ui.py`, `README.md`.
- [x] 写 Tk 冒烟测试：配置模型动态读取、参数构建、输入变动丢弃分析、后台完成展示与关闭。
- [x] 实现双栏界面、预览、JSON、澄清恢复、密码输入、双击入口。
- [x] 运行 Tk 冒烟、全套 pytest、ruff、wheel build；检查窗口截图。
- [x] 完成独立代码审查与必要修复，更新使用说明。

## 验收记录
- 基线：190 项测试通过；新增 18 项后台与 Tk 测试后，全套 208 项通过。
- 离线端到端：桌面表单 → `create_images` → 真实处理/审核/落盘管线 → 窗口预览；仅外部模型使用 FakeVision/FakeGenerator。
- 独立审查发现澄清来源不完整：已为 `intent.unmet_requirements` 与 `result.issues` 增加失败回归，再修复合并/去重；原快照保持不变。
- 测试另定位并修复：实际运行输出目录、保存异常时保留结果、清空素材预览、关闭窗口清理轮询。
- 首次全套运行出现一次 Tk 重复初始化异常（203 passed / 1 error），单独运行未复现；测试改为共享一个 Tcl/Tk 解释器，逐用例创建 Toplevel，并在主线程回收 Tk 对象；最终全套通过。
- 实际检查 1200×820、980×660 窗口，左侧可滚动，底部动作固定；截图在本地忽略目录 `out/desktop-default.png` 和 `out/desktop-small.png`。
- 未执行真实付费 API 请求。未提交或改动既有 agent 核心文件；新增功能接入当前未提交的 API 配置实现。
