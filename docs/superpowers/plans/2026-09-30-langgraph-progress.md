# LangGraph 重构执行记录

## 用户确认

完整迁移现有功能；保留入口允许结果 v2；可验证外观硬审核、功能声明不误拒；approved/candidates/accepted 分离；最多2轮定向修复；真实验收合计生图10/视觉30（重试计入）；使用既有配置及仓库外密钥文件；复测原手机壳素材。

## 基线

- 分支 codex/langgraph-refactor，保留已有未提交修改。
- 本地基线备份 `.superpowers/langgraph-baseline-20260930/`，90个文件，不含密钥；历史 out 不改动。
- 原图 SHA-256 与失败记录一致。
- 本会话此前基线测试 259 passed；迁移将针对新契约更新断言。
- 安装 LangGraph 1.2.12、langgraph-checkpoint-sqlite 3.1.1。

## 接口与所有权

- domain_rules：models/selection/quality/prompt/vision/compliance；新增 build_repair_prompt 和来源可验证性。
- graph_engine：pipeline/output/transport + graph/execution/store；create_images/resume_images/accept_candidate 为 async；所有实际 POST 扣预算。
- entrypoints：CLI/desktop/public API/contracts/schema/README；消费明确事件，UI主线程更新。
- coordinator：pyproject/锁文件、基线、集成、独立审查与真实预算账本。

### 统一状态契约

Asset 增加 pending_audit,audit_error,quality_failed,needs_input,budget_exhausted,accepted,generation_uncertain，保留 succeeded/failed；candidates:list[dict]、stop_reason。候选字段 candidate_id,index,status,file_path,quality,error_info,repair_prompt。CreationResult 默认版本2.0，兼容解析1.0；run_dir、usage。外部 DTO 不泄露本地候选路径。

### 实施决策

- 未分类旧事实不统一豁免：默认保持 visible_appearance 的旧要求；新发现明确分类来源。功能声明与可见产品印花分开。
- Graph 将原始生成响应字节先持久化再结束生成节点；保存/解码与审核分节点，降低收费后丢图窗口。未记录响应的已提交操作进入 generation_uncertain。
- 使用现有工作目录与专用分支，先备份保护大量当前改动；角色按文件所有权隔离，不复制凭证到工作树。

## 验证记录

实施中，后续追加每个角色测试、审查和真实调用证据。

- 业务语义：72项专项测试通过；具体报告 `langgraph-domain-report.md`。旧事实默认仍为可见外观，新供应商发现必须显式返回分类，不靠商品关键词豁免。
- 接入层：56项入口/桌面/契约测试通过，最后5项候选历史UI回归通过；报告 `langgraph-entrypoints-report.md`。
- 引擎中期：82项图恢复/流水线/输出/协议/验收测试通过，最终边界修复和独立审查进行中。
- 协调者：更新 live_smoke 离线测试，改为模拟供应商边界而非绕过整个流水线，4项通过；wheel smoke 更新事实分类。
- 依赖已通过 `uv sync --locked --extra dev` 同步，发行版本0.2.0；初步 wheel/sdist 构建成功，最终版本会重新构建。
- 独立审查使用本次工作区基线差异包，避免把之前用户修改算作本次重构。
- 新增独立跨进程故障测试任务：候选保存后强制退出，另一个进程重审且禁止调用生成器。
- 凭证文件实际为两个按协议标注的凭证，真实验收分别绑定 image 与 vision 服务，不跨服务借用。

## 集成验收（2026-09-30）

- 全量离线测试：`pytest -q -p no:cacheprovider --tb=short`，333 passed in 34.50s。
- Ruff 检查通过；格式检查 65 files already formatted；compileall 通过。
- 0.2.0 wheel 和 sdist 构建成功；独立虚拟环境安装后确认从 site-packages 导入，公开 API smoke 通过。
- 发行包内容检查：wheel 30 个文件、sdist 111 个文件；指定凭证内容匹配均为 0，私有输出/密钥路径均为 0。
- 跨进程强制退出测试通过：候选落盘后进程退出，再启动进程只审核已有图，生图累计仍为 1。
- 独立审查 6 项发现已修复并复核，详见 `langgraph-review-report.md`；未发现剩余收费或恢复 P1。
- 真实验收的第三方目的地为 `https://penguinapi.vip/v1`；自动审批要求明确目的地后，用户已单独确认允许发送指定原图与提示词并使用指定凭证。
- 第一次真实调用在分析阶段结束：意图 source_quote 原文校验失败，生图 0、视觉 1。发现该校验位于格式纠正循环外，已交业务角色修复，仍保持严格原文验证；不删除或重置共享预算。
- 真实验收尚未完成，以上离线结论不代表真实模型生成质量。

### 首次真实调用暴露问题后的最终补丁

- 引用来源与素材 ID/顺序校验移入现有纠正回调；保留原文精确匹配，禁止把产品名、分类、图片 OCR 或跨行归纳作为用户引用。连续两次无效仍明确失败。
- 44 项业务专项测试通过；独立审查新增 5 项边界回归通过，补丁审查批准。
- 最终全量测试：338 passed in 48.10s；Ruff、65 文件格式检查、compileall、wheel/sdist 重建全部通过。
- 最终 wheel 重新安装到独立虚拟环境后 API smoke 通过；最终包扫描 wheel 30 / sdist 113 文件，凭证匹配与私有路径条目均为 0。

### 真实验收完成

- 第二次运行 `20260930-114550-a9f896ea` 成功，单次生图 1、视觉 4；全程累计生图 1/10、视觉 5/30（含第一次失败），共享账本不变。
- 原图中的磁吸、液态壳被正确归为功能声明；审核明确 not_applicable。可见颜色、结构及原生文字/角色印花继续保真检查。
- 对照原图目视检查完成；approved 1 个图像，candidates 0 文件，图像哈希一致；完成任务 resume 新增调用为 0。
- 实际图片 1024×1024：原有 GPT Image 2 适配的 2K 表示 medium 质量，不是 2048 像素，本次未更改。
- 首张图通过，未触发真实质量修复。离线故障注入覆盖修复与恢复；多平台真实生成及手工桌面交互未在本次单素材真实验收中验证。
- 最终交付证据汇总于 `2026-09-30-langgraph-acceptance.md`。保留用户原有修改，未提交、推送或重置历史结果。
