# Standalone Image Agent Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Alternatively use superpowers:subagent-driven-development if the user selects delegation. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在当前独立工作区实现可安装的 `image_agent` Python 包和 CLI，支持多图自动提取、按产物选用要素及多来源审核，提供稳定第三方接入契约并脱离原项目基础设施。

**Architecture:** 迁移平台、提示词和审核规则；重写纯内存异步流水线。`pipeline.py` 是唯一编排者，先提取多图候选，再编译资产要素计划；每张资产独立完成生成、多来源质检和最多一次质量修复。HTTP、图片处理和可选文件输出独立，测试通过内部依赖注入运行。

**Tech Stack:** Python >=3.12；运行时 httpx>=0.27,<1、pydantic>=2.7,<3、Pillow>=12.3,<13；标准库 argparse/logging/asyncio；hatchling 构建；开发用 pytest、pytest-asyncio、ruff、build。

**Spec:** 当前规格是 [多图与接入修订设计](../specs/2026-09-28-multi-image-agent-revision-design.md)，优先级最高。继承 [原设计快照](../specs/2026-09-28-standalone-image-agent-design.md) + [可行性评估及契约补充](../reviews/2026-09-28-extraction-feasibility.md)。三者一起阅读；修订设计 > 评估第 3 节 > 原快照。来源版本见 `../reviews/2026-09-28-source-manifest.json`。

**Status:** 已按用户要求修正独立评审的 B1、B2、I1–I4 及三项建议，见 [评审修正记录](../reviews/2026-09-28-review-resolution.md)。本轮只更新规格与验收计划，未开始实现；仍按十个任务顺序推进，不新增工作台或服务框架。

## Global Constraints

- 项目根为 `F:\Work\电商\imageAgent`，包在 `src/image_agent/`，不额外嵌套项目层。
- 不修改来源项目；无租户、任务库、检索、视频、social、SSE、配额、记账、Celery、LangGraph、LangChain、MinIO、Milvus、SQLAlchemy 运行时依赖。
- 公开入口 `async def create_images(request: CreationRequest, config: AgentConfig | None = None, *, analysis: MaterialAnalysis | None = None) -> CreationResult`；API 支持路径/HTTP(S)/bytes，CLI 图片只接受本地路径。
- 平台：amazon、taobao、tmall、jd、pinduoduo、shopee、lazada、shein、temu；输出 main_image、detail_page、pdd_white_background。名称及类目必填；新 materials 1–8 张、角色默认 auto；旧 product_image + 最多 4 reference_images 互斥兼容。
- 原设计第 9 节全部 IMAGE_AGENT_* 环境名、默认模型、180 秒超时、图片模型并发 2 原样保留；另增 GENERATION_REFERENCE_LIMIT=4 和 VISION_IMAGE_LIMIT=12（完整环境名以 IMAGE_AGENT_ 开头）；这两个值是部署容量声明，真实能力另验。不自动加载 `.env`，不用外部全局代理环境。
- 每次自动调用只做一次素材发现；传入分析快照仍核对必需证据；输出主图→详情→白底，平台按输入顺序，详情 scene→feature→closeup；白底只生成 pinduoduo 一个资产。
- 图像模型参考长边 1024、视觉 768、风格 512；请求内串行；跨请求按真实图片模型限流。
- HTTP 总共 3 次、等待 2/4 秒；状态 429/502/503/504；ConnectError/ConnectTimeout/ReadTimeout/WriteTimeout；不带熔断。
- 每个可见主体 same_product=true 且一致性 >=85、required detail/accessory 保真 >=85；初审高分/false 是质量失败，80–84 仅一次复核。视觉 >=70、平台 >=70、模特服装融合 >=80、适用的模特偏好 >=80、镜头意图 >=75。成组差异与覆盖均 >=75，仅报告。
- 发现模型同时产出 IntentAnalysis/SelectionProposal，selection 不理解任意自然语言、不新增模型调用。Fact 内 evidence 是 OR，required Fact 集合是 AND；主体身份也引用 fact_id。
- 用户 explicit/brief required 均为每资产 must_show；镜头只能豁免默认要求，模型不能改 applicability。快照选择先产生可复核 draft，最终 ready 在证据核对之后决定。
- 异常与 failed/partial/分析失败统一 ErrorInfo，禁止依靠 error 中文字符串反解析；取消遵守规格第8节，不返回普通失败、不遗留后台写盘。
- 通用长边下限 1K=768、2K=1536、4K=3000；gpt-image-2 为 768；比例相对误差 <=0.06；近白边比例 >=0.90，RGB 每通道 >=250；白底改用精确 480×480、<3 MiB 特例。
- 每张只允许一次质量修复；strict 或 staged；Amazon 主图/白底仅 strict；不自动升级模型；失败资产无图片文件。
- 默认测试禁止联网、禁止付费调用、禁止读取来源项目配置。运行时不 import 来源项目；来源文档及 MIT 版权允许保留项目名。
- 本计划代码块均为待实现的测试契约示例，不表示现有代码已可运行。任务完成后记录命令和结果，不能只勾选复选框。

## Review Focus

1. 意图到真实 selection 的衔接、OR/AND 事实覆盖、required 与镜头豁免：完整分析 fixture 贯穿纯函数，不用 fake 预先替它完成关键决定（任务 1、4、5、5A、6）。
2. 第三方返回 HTTP 200 但 JSON/base64/schema 坏、content 为字符串：有明确失败，不能死循环重试或漏判成功（任务 3、5、6）。
3. 多调用、模型别名相同、请求取消、连续 asyncio.run：共享限额、不死锁、不泄露客户端（任务 3）。
4. Windows 路径、重复 ID、写盘失败及取消：不覆盖，取消不残留后台写盘；partial 与抛错都保留 ErrorInfo（任务 1、6、7、7A）。
5. 快照篡改/陈旧、歧义恢复、media_id 未绑定与容量不足：选择能继续复核，缺证据不生成，初审高分/false 不放行（任务 4、5A、6、7A）。

## 文件及内部接口地图

```text
pyproject.toml / README.md / .env.example / .gitignore / LICENSE
src/image_agent/
  __init__.py       # 公共 API 导出
  __main__.py       # argparse 入口及退出码
  config.py        # AgentConfig、from_env、模型别名
  models.py        # 请求、结果、视觉 schema、内部纯数据
  errors.py        # 配置/输入/供应商/输出异常
  platforms.py     # 平台原始规则
  prompt.py        # 词表、展示、镜头、修复提示词
  compliance.py    # 拦截、市场改写、平台后缀和警告
  transport.py     # HTTP、重试、客户端生命周期、模型并发
  generate.py      # 生图 payload 和图片响应解析
  images.py        # 图片加载、预处理、格式识别
  quality.py       # 像素、白底导出、评分和重试选择
  vision.py        # 原图、服装、风格、单图、复核、成组模型调用
  selection.py     # 主体/要素选择、资产计划、参考排序与容量
  contracts.py     # 版本化 DTO、绑定和内存 blob bundle
  pipeline.py      # 依赖协议、内部 run_pipeline、公开 create_images
  output.py        # 可选落盘、结果 JSON
tests/
  conftest.py / fakes.py
  test_models.py / test_config.py / test_platforms.py / test_prompt.py
  test_compliance.py / test_transport.py / test_generate.py
  test_images.py / test_quality.py / test_vision.py
  test_selection.py / test_contracts.py / test_pipeline.py / test_output.py / test_cli.py / test_isolation.py
  fixtures/prompt_cases.json
scripts/live_smoke.py / examples/integration_adapter.py / schema/*.json
docs/migration.md
```

内部共享数据由任务 1 定义，避免各模块自造字段：

- `AssetTarget(platform, output_type, variant, aspect_ratio, presentation_mode, model_preference)`；白底的 effective preference=auto。
- `PreparedContext(materials: tuple[LoadedMaterial,...], analysis: MaterialAnalysis, selection: ResolvedSelection, input_check: ProductInputAudit, presentation_mode, product_attributes: dict, style_prompt: str | None, warnings: list[str])`；LoadedMaterial 保存 material_id、bytes、sha256、顺序与角色；禁止混用旧单 product/references 上下文。
- 任务1定义 `MaterialInput/SelectionSpec/MaterialAnalysis/Subject/Fact/Element/IntentAnalysis/SelectionProposal/IntentConstraint/Issue/Requirement/AssetElementPlan/ElementCheck/ReferenceBinding/GenerationAttempt`，具体字段及 OR/AND 语义按修订规格3–6节；`PreparedAnalysis` 组合 LoadedMaterial 与 MaterialAnalysis，不直接序列化。
- `ResolvedSelection(primary_subject_id, subject_ids, required_element_ids, preferred_element_ids, excluded_element_ids, constraints, focus_element_ids)`；`SelectionDraft(candidate:ResolvedSelection|None, pending_issues:list[Issue], blocking_issues:list[Issue], issue_resolutions)`；`SelectionResolution(status:ready|needs_input|failed, selection:ResolvedSelection|None, issues, issue_resolutions, error_info:ErrorInfo|None)`。
- `EvidenceValidation(outcome:verified|needs_input|failed, subject_checks, fact_checks, intent_valid:bool, issue_resolutions, issues)`；IssueResolution={issue_id,status:resolved|irrelevant|unresolved,reason}。验证模型只返回 resolved/unresolved，irrelevant 仅供选择层记录已排除对象问题。这些共享类型全部在任务1定义，任务5不依赖5A的实现。
- `ComplianceResult(blocked: bool, prompt: str, warnings: list[str])`。
- `PixelChecks(passed: bool, width: int, height: int, reason: str, white_border_ratio: float | None, export_bytes: int | None)`。
- `GeneratedImageAudit` 含 subject_checks（subject_id、score、same_product、reason）、fact_checks（fact_id、presence、fidelity_score|None、reason）、element_checks（element_id、presence、fidelity_score|None、reason）、constraint_checks（constraint_id、satisfied、reason）及原视觉/平台/融合/偏好/意图分数；presence=present|absent|not_applicable。`FocusedProductAudit` 按 subject_id 返回身份字段。重复/未知/漏必需 ID 为 protocol 错误；must_show 回 not_applicable 为质量失败。`QualityReport` 保留上述列表、model_passed、复核前后值、deterministic_checks、passed、failed_checks、reason。
- `DetailSetAudit(platform, passed, distinctiveness: int | None, role_coverage: int | None, issues, reason, error: str | None)`；服务失败时分数 None。
- `AgentError` 子类 `ConfigurationError/InputImageError/StaleAnalysisError/ProviderError/OutputError`；ProviderError 带 code、kind、status_code、脱敏 message；OutputError 可带 result。`ErrorInfo` 字段和映射按规格第6节，MaterialAnalysis/CreationResult/Asset/DetailSetAudit 均有可空 error_info；CreationResult.output_errors 默认为空列表。旧 Asset.error 保留为同一消息。`errors.py` 提供 `make_error_info(error: AgentError | pydantic.ValidationError) -> ErrorInfo`，捕获点与 DTO 共用；质量/拦截/编译失败用相同类型直接构造，retryable 不重启任务。

## 任务依赖

顺序：1 → 2 → 3 → 4 → 5 → 5A → 6 → 7 → 7A → 8，共 10 个任务。2 先用手工 AssetElementPlan fixture 验证提示词；3/5 依赖任务 1 类型，5A 接通选择逻辑，6 组装；7A 最后提供对外转换及 CLI JSON 模式，不倒置依赖。所有运行时代码路径均相对于 src/image_agent，测试路径相对于 tests；任务中短文件名按此解析。

## 执行约定

每任务先写契约测试并记录失败，再完成最小实现，最后跑所属测试；只对有行为的代码写测试，不为文档或脚手架机械造测试。以下命令从项目根运行，Python 解释器统一 `.venv\Scripts\python.exe`。任务结束只提交已验收文件，保留失败测试证据摘要；不提交凭证、虚拟环境、缓存、输出图。

### Task 1: 建立独立环境、请求与结果契约

**Files:** 创建根配置/许可/忽略文件，`src/image_agent/{__init__,config,models,errors}.py`，`tests/{conftest,test_models,test_config}.py`。

**Interfaces:** 提供上述数据类型及修订设计 CreationRequest/Asset/CreationResult（含 needs_input、逐主体审核、attempts 与溯源）；`ImageSource(path: Path | None=None, url: str | None=None, data: bytes | None=None)`；`AgentConfig.from_env() -> AgentConfig`、`model_name(alias: str) -> str`。

- [ ] 在共享模型测试加入 B1/B2/I1/I2/I3：完整 IntentAnalysis 与代表图、原子 Fact 与引用闭合、Requirement.origin/applicability、带完整 SelectionSpec 的 Issue.options、SelectionDraft 与 EvidenceValidation、ErrorInfo；未知/重复ID、无来源事实、缺引用、非法source_quote均不能进入选择。auto不能带ID列表；explicit无同快照在API调用时拒绝。使用当前规则版本 `multi-image-r2` 参与fingerprint，本期未发布故DTO仍为1.0。

- [ ] 核对来源 manifest，记录差异；当前目录初始化 Git（若届时已是仓库则沿用）。找到可用 Python 3.12+，创建本地 `.venv`。uv 缓存设项目 `.uv-cache`，不使用来源 `.venv`；安装受网络或权限阻止时按环境要求请求授权。
- [ ] 建立 pyproject 与 dev extra；注册 `image-agent = image_agent.__main__:main`；hatchling src 布局；复制 MIT LICENSE 并保留原版权。先只导出已定义类型，公共 create_images 在任务 6 接入。
- [ ] 先写材料/兼容校验：materials 1/8 接受、0/9 拒绝，ID 唯一与合法字符，角色可省略为 auto，新旧字段混用拒绝；selection 列表互斥，旧路径归一化为 product/ref-N；新增两容量配置必须正整数。再写以下原枚举/配置断言：

```python
assert request(platforms=[" TAOBAO ", "taobao"]).platforms == ["taobao"]
assert request(platforms=["shopee"]).market == "SG"
with pytest.raises(ValidationError):
    request(output_types=["pdd_white_background"], platforms=["taobao"])
with pytest.raises(ValidationError):
    ImageSource(path="a.png", data=b"x")
with pytest.raises(ValidationError):
    request(request_id="../escape")
assert AgentConfig.from_env().model_name("fast") == "openai/gpt-image-2"
```

- [ ] 运行 `python -m pytest tests/test_models.py tests/test_config.py -q`（此处及后文 python 指上述 venv 解释器），确认预期失败；不得以无法收集所有测试掩盖具体契约。
- [ ] 实现校验：未知字段拒绝，空名称/类目拒绝，平台输出去重，HTTP(S) URL 严格限定，图片源恰好一个，request_id Windows 校验，正数 timeout/concurrency；密钥用 SecretStr 且禁止 repr 暴露，创建真实客户端时才要求密钥。复制设计默认值，style_model 缺省跟随 quality_model。
- [ ] 跑上述测试通过，检查包导入无网络/环境读取副作用，提交 `feat: define standalone image agent contracts`。

### Task 2: 平台、展示、提示词与合规迁移

**Files:** 创建 `platforms.py`、`prompt.py`、`compliance.py`；测试 `test_platforms.py`、`test_prompt.py`、`test_compliance.py`、`fixtures/prompt_cases.json`；记录 `docs/migration.md`。

**Interfaces:** `resolve_presentation(request, observed_product: str="") -> str`；`build_targets(request, presentation_mode: str) -> list[AssetTarget]`；`build_prompt(request, target, context, plan: AssetElementPlan, mode: str="standard") -> str`；`build_stage_prompt(request, target, plan: AssetElementPlan, final_prompt: str) -> str`；`apply_compliance(request, target, prompt: str) -> ComplianceResult`。platforms 保留原函数签名。

- [ ] 用手工 plan fixture 测试多来源标签、只写入已选商品、可选 scene 不污染白底、两主体均被保留、closeup 不被强制全身；元素 ID/素材绑定不参加市场数字替换（仅自然语言提示词保持原兼容行为）。
- [ ] 断言 prompt 消费结构化 placement/co_presence/exclusion 等 constraint 的指令与必需要素；只接收已编译的 requirements，不能在拼词时把 user_required 改成 preferred/not_applicable。
- [ ] 按来源 manifest 对应函数建立少量人工核对的提示词 fixture，覆盖淘宝纯商品、服装 no_face、Amazon 场景、白底；注明来源和有意变更，不在测试时 import 来源包。
- [ ] 先写以下测试及完整性断言：

```python
assert aspect_ratio_for("temu", "detail_page") == "1:1"
assert aspect_ratio_for("shein", "detail_page") == "3:4"
assert resolve_presentation(explicit_product_only, "shirt") == "product_only"
assert "江南巷弄" not in amazon_scene_prompt
assert "face must not be visible" in no_face_prompt
assert pdd_target.presentation_mode == "product_only"
assert apply_compliance(cn_request, target, "item 4").prompt.startswith("item 6")
assert apply_compliance(request(), target, "weapon").blocked
assert not apply_compliance(request(), pdd_target, "shirt").blocked
```

- [ ] 运行上述 3 个测试文件，记录失败；随后迁移平台文本、场景/服装/类别词表、自动命名正则、主图和详情镜头，按纯函数实现目标排序与展示优先级。
- [ ] 实现先基础/修复文案、再合规改写、最后平台与禁水印后缀；显式比例覆盖提示放在最终指令中且质检使用同值。单测确认平台模板自身的 `fake` 不误触危险词拦截。警告排序稳定，品牌/IP 仅警告。
- [ ] 加测白底只一个资产、忽略风格与偏好；strict 三镜头纠正、staged 第一阶段文案不要求“复制未提供的原图”；原平台文本与 fixture 核对通过后提交 `feat: extract image creation rules`。

### Task 3: 独立 HTTP、模型并发和生图协议

**Files:** 创建 `transport.py`、`generate.py`；测试 `test_transport.py`、`test_generate.py`。

**Interfaces:** `JsonTransport.post_json(path: str, payload: dict) -> dict`；`HttpTransport(config, *, client: httpx.AsyncClient | None=None, sleep=asyncio.sleep)` 支持 async context manager；注入客户端不归它关闭，自建客户端必须关闭。`ImageGenerator(transport, config: AgentConfig, *, encoder: Callable[[bytes, int], bytes])` 提供 async `generate(*, prompt: str, model: str, size: str, aspect_ratio: str, references: tuple[GenerationReference,...]) -> bytes`；`extract_image_bytes(payload: dict) -> bytes`；GenerationReference={material_id 或 stage_id, data:bytes, role, element_ids}，严格按传入顺序，明确编号与用途，generator 不选图。model 参数为真实模型名；encoder 在本任务用测试替身，任务 4 提供真实 encode_jpeg，任务 6 组装时注入。

- [ ] 用 MockTransport 和记录 sleep 的替身先断言：

```python
assert await post_statuses([429, 503, 200]) == {"ok": True}
assert sleeps == [2.0, 4.0]
assert await attempts_for_status(400) == 1
assert await attempts_for_status(503) == 3
assert extract_image_bytes({"data": [{"b64_json": "aW1hZ2U="}]}) == b"image"
```

- [ ] 再写参数化测试：三种图响应优先级、空 content、字符串 content、坏 base64、非 JSON 200；3 类别名映射由 config 测试，generator 只认真实 model；gpt-image-2 quality=low/medium/high，其他 resolution=1K/2K/4K；n=1；多图 1/4 张顺序与标签保持、去重由 selection 负责；stage 无身份图，fusion stage 最后一张；超过配置上限为 capability 错误且不发请求。
- [ ] 运行两文件确认失败；实现固定重试列表、异常分类、500 字符上限与凭证/data URL/base64 脱敏。响应解析不擅自下载供应商外链，不引入协议之外的自动回退。
- [ ] 实现真实模型共享并发许可：同模型别名共用额度、不同模型独立。不得全局缓存 loop-bound Semaphore；可使用线程安全计数/锁加可取消异步等待。配置冲突明确报错。HTTP 重试期间持有同一生图许可，所有退出路径释放。
- [ ] 测试同模型峰值 <=2、连续 asyncio.run、多事件循环争用、取消等待者和持有者、客户端关闭、trust_env=False。上述协议测试通过即可独立提交，不依赖任务 4；提交信息 `feat: add isolated image provider transport`。
- [ ] 设置供应商协议检查点，在 `docs/migration.md` 记录当时官方资料链接/日期、`/images`/input_references/各模型容量与响应形式、未核实项；没有真实调用时标记“仅离线协议实现”，4张fake不是模型能力证据。网络访问按环境权限处理；本任务不自动进行付费调用，未实测不阻止后续离线开发。

### Task 4: 图片输入、白底导出与确定性质量规则

**Files:** 创建 `images.py`、`quality.py`；测试 `test_images.py`、`test_quality.py`；接通 generate 的预处理。

**Interfaces:** `load_image(source: ImageSource, *, client: httpx.AsyncClient) -> bytes`（async）；`encode_jpeg(data: bytes, max_px: int) -> bytes`；`image_extension(data: bytes) -> str`；`export_white_background(data: bytes) -> bytes`；`check_pixels(data: bytes, target: AssetTarget, size: str, model: str) -> PixelChecks`；`evaluate_quality(audit: GeneratedImageAudit, pixels: PixelChecks, target: AssetTarget, plan: AssetElementPlan, review: FocusedProductAudit | None=None) -> QualityReport`；`choose_retry(target, quality: QualityReport) -> str`。

- [ ] Pillow 在内存生成真实 PNG/JPEG 样本，测试以下边界，不用仅含文件头的假图：

```python
assert check_pixels(square_1536, main_target, "2K", "other").passed
assert not check_pixels(square_1535, main_target, "2K", "other").passed
assert check_pixels(square_768, main_target, "4K", "openai/gpt-image-2").passed
assert check_pixels(export_white_background(square_1536), pdd_target, "4K", "other").passed
assert evaluate_quality(scores(subject_consistency={"s1": 85}), good_pixels, target, plan).passed
assert not evaluate_quality(scores(subject_consistency={"s1": 84}), good_pixels, target, plan).passed
assert choose_retry(amazon_target, low_consistency_quality) == "strict"
assert choose_retry(taobao_target, low_consistency_quality) == "staged"
```

- [ ] 加测比例 6% 边界、白色阈值 250/249、白边 90% 边界、3 MiB 排除边界、透明背景白色合成、EXIF 方向与不放大小图。输入 URL 非图/超时、路径不可读、坏图统一 InputImageError；保留原始输入字节，仅发送模型时缩放。
- [ ] 运行两测试文件确认失败，实施像素与分数规则，优先用标准库/Pillow，不引入 numpy。白底沿用来源缩放导出算法；API 主图详情不转码，所有图片编码 CPU 操作由调用层放入 asyncio.to_thread。
- [ ] 扩展评分测试：初审95/false是质量失败且走既有修复、85/true通过、84/true待一次复核；多主体90/84独立替换；重复/漏/未知主体或必需fact ID为协议失败；required detail=84/missing、required constraint=false失败；optional absent仅报告。must_show回not_applicable失败，计划豁免的非焦点主体不计失败。
- [ ] 本地决定passed；仅原有非适用融合/偏好分置100，事实/要素not_applicable用None不伪造分数。聚焦复核只替换身份结论，不解除required细节/关系失败。中文失败格式固定“未通过指标：…”。跑测试和任务3生图测试通过，提交 `feat: implement image IO and quality rules`。

### Task 5: 视觉准备、单图审核与成组审核

**Files:** 创建 `vision.py`；测试 `test_vision.py`、可复用 `tests/fakes.py`。

**Interfaces:** `VisionClient(transport: JsonTransport, config: AgentConfig)`：async `analyze_materials(request, materials: tuple[LoadedMaterial,...]) -> MaterialAnalysis`、`validate_evidence(request, materials, analysis, draft: SelectionDraft) -> EvidenceValidation`、`extract_garments(request, materials, subject_id: str) -> GarmentStructureAudit`、`describe_style(request, references: tuple[bytes,...]) -> str`、`audit_image(request, target, context, plan: AssetElementPlan, image: bytes) -> GeneratedImageAudit`、`review_product(context, plan: AssetElementPlan, subject_ids: tuple[str,...], image: bytes) -> FocusedProductAudit`、`audit_detail_set(platform: str, context, plans: tuple[AssetElementPlan,...], images: tuple[bytes,bytes,bytes]) -> DetailSetAudit`。失败抛ProviderError；发现API的捕获点填MaterialAnalysis.error_info，不能伪装needs_input。风格不完整也抛protocol错误。

- [ ] 发现响应同时包含subjects、原子facts、elements、intent.proposal/constraints/focus、issues；用完整模型响应fixture覆盖三视角、杯盖放旁边且不扣上、否定组合、背景商品不选、冲突颜色/型号及source_quote。vision只调用一次发现，selection不再猜brief。
- [ ] 覆盖未知material/fact/subject引用、替代证据不足、低置信度、缺结构，显式required不能被发现模型降成preferred。快照validate_evidence核对全部原始素材、候选事实、intent原文和pending问题；缺issue_resolution或不合法结果为protocol。逐图使用plan.audit_material_ids+生成图，成组用三计划审核来源并集+3，超VISION_IMAGE_LIMIT在调用前失败；不能只审核生图选用的替代图。
- [ ] 构造假 transport，记录模型、schema、图片顺序和缩放尺寸。写自动占位名/真实名分支、服装字段范围、原图与生成图标签、平台审核指令测试。
- [ ] 写风格完整性和回退测试：

```python
assert len(await style_model_calls(same_models=True, incomplete=True)) == 1
assert await style_model_calls(same_models=False, incomplete=True) == ["style", "quality"]
assert len(style_request_image_blocks) == 4
assert len(legacy_single_asset_style_references) == 1
with pytest.raises(ProviderError):
    await vision.audit_image(request(), target, context, plan, image_with_bad_schema_response)
```

- [ ] 运行 test_vision 确认失败；用 `/chat/completions`、JSON object 响应格式与明确 JSON 指令实现，不导入 SDK/LangChain；Pydantic 对 bool、0–100 分数、列表字段等验证，禁止缺失分数默认通过。风格响应 JSON 外层按评估约定。
- [ ] 测试风格 19/20 词与句尾、不完整介词结尾、只允许一次不同模型内容回退、传输失败不进行额外风格逻辑回退；验证视觉图片长边 768/风格512。审计 score 75 边界、审核失败异常、复核不自行变更资产状态。
- [ ] 测试通过提交 `feat: add structured image vision checks`。

### Task 5A: 多图主体、要素选择与参考编译

**Files:** 创建 `selection.py`、`tests/test_selection.py`；完善 `models.py` 的共享结构。

**Interfaces:** `resolve_selection(request: CreationRequest, analysis: MaterialAnalysis) -> SelectionDraft`；`finalize_selection(draft: SelectionDraft, evidence: EvidenceValidation | None=None) -> SelectionResolution`；`compile_element_plan(request, target: AssetTarget, context: PreparedContext, config: AgentConfig) -> AssetElementPlan`；`select_references(plan: AssetElementPlan, analysis: MaterialAnalysis, materials: tuple[LoadedMaterial,...], *, limit: int, reserve_stage: bool=False) -> tuple[GenerationReference,...]`；`analysis_fingerprint(request, materials: tuple[LoadedMaterial,...]) -> str`。全部返回类型在任务1定义，evidence为None仅适用于本次自动发现且无pending问题；快照必须由pipeline传实际复核结果。

- [ ] 将任务5完整响应fixture送入真实resolve_selection/finalize_selection，不用fake预选：同商品三视角→一主体；杯+盖及放置关系→两对象/关系保留；否定组合/背景商品→排除；explicit删掉要求呈现的对象或选中exclusion对象→selection_conflict，exclusion引用已排除对象则合法；未知ID与交叉列表拒绝。
- [ ] 覆盖草稿澄清：旧needs_input不直接阻断；选唯一红款且蓝款独立候选被排除→candidate可复核；活跃身份冲突进入pending而非blocking；无法拆分的合并候选→reanalyze；options.selection能直接用于下一请求，最终判定消费issue_resolutions，不能改status绕过。
- [ ] 参考覆盖用原子facts计算：同事实5张OR证据只需1槽；正面标志/背面拉链AND必须各覆盖；一图多事实去重；代表图占槽；4槽确实不足才资产失败；staged预留1槽不足改strict。断言generation_material_ids与audit_material_ids不同，审核保留相关替代/冲突视角。
- [ ] 资产规则测试：optional场景仅白底排除；required场景导致白底requirement_conflict；主图/scene全主体must_show，特写仅默认非焦点主体可豁免；用户required杯盖与不兼容微距在编译期失败；每条requirement有origin/applicability/reason；不允许一整组资产把同一用户required都标not_applicable。
- [ ] `python -m pytest tests/test_selection.py -q`，记录预期失败。
- [ ] 实现以上纯函数，只消费intent.proposal/constraints与显式选择，不分析brief语义、不发模型请求。记录required_fact_ids、要求来源、可见范围、text_only与排除理由；代表图由分析提供，不能用像素函数临时重新选身份图。参考覆盖遵照Fact内OR、集合AND，顺序不依赖JSON键顺序；asset_id/target_key固定为platform.output_type.variant（无variant用default）。
- [ ] 加测 fingerprint：同字节换路径不变，字节/顺序/角色/brief 变则改变，只改 selection/目标不变；analysis ID 在同快照复用不变。hash 不作为信任证明，外部快照核对由任务 6 的 validate_evidence 执行。
- [ ] 跑上述测试及任务 2 提示词测试通过，提交 `feat: plan multi-image elements per asset`。
### Task 6: 内存流水线与单资产重试

**Files:** 创建 `pipeline.py`，接通 `__init__.py`；测试 `test_pipeline.py`，扩展 `fakes.py`。

**Interfaces:** `Dependencies(vision: VisionProtocol, generator: GeneratorProtocol, load: async Callable[[ImageSource], bytes])`；协议签名与任务 3/5 相同。`async run_pipeline(request: CreationRequest, config: AgentConfig, *, dependencies: Dependencies, analysis: MaterialAnalysis | None=None) -> CreationResult` 只做内存计算；内部 prepare_analysis(request, config, dependencies) -> PreparedAnalysis 为分析与生成共用；公开 analyze_materials 返回 MaterialAnalysis 且不预留输出目录；公开 create_images 管理真实客户端、输入加载依赖和任务 7 可选落盘。

- [ ] 先写一条离线成功链路：原图→准备→淘宝主图+详情三张→成组审计；断言 4 资产、准备一次、准确顺序、结果 status=succeeded、request 未被修改。
- [ ] 写关键重试断言（脚本化 fake 每一步返回真实图片和已校验分数）：

```python
assert generation_calls_for("scene") == 1
assert generation_calls_for("feature") == 2  # 仅 feature 意图失败，strict 后通过
assert generation_calls_for("closeup") == 1
assert result.status == "succeeded"
assert feature_asset.generation_mode == "strict"
assert result.assets[0].image == original_provider_bytes
```

- [ ] 顺序固定：校验/加载→发现或快照schema/fingerprint→resolve_selection产SelectionDraft→无candidate或blocking时needs_input；否则快照或pending问题进入validate_evidence→finalize_selection→只有ready继续展示/服装/风格→目标/requirements/证据容量→合规→生成→白底导出→像素/视觉/必要复核→本地评分→一次修复。pending与旧needs_input不得在复核前直接终止。坏图直接失败；可解码但像素失败仍审核以保存诊断。
- [ ] staged 两次生成由 pipeline 编排：stage 仅场景/风格，fusion 带所有必需身份/细节证据及末尾 stage 字节；若 stage 占位导致必需来源超限，开始修复前改 strict 并追踪原因；最终 Asset.prompt 保存融合请求的最终合规提示词，失败保存最后尝试提示词；同一真实模型，不升级。
- [ ] 扩展参数化测试：原图不符/服务失败不生成；合规失败其他图继续；传输/协议/质检服务失败不触发创作重试；复核失败不默许成功；staged 第一步失败无第二步；Amazon/白底强制 strict；可选服装细节补充/风格准备降级 warnings（核心主体与 required 分析失败不得降级）；仅三图成功才成组、成组失败不降级资产；partial/failed 聚合；所有失败 image/file_path=None。
- [ ] 加集成测试：默认不标角色的 3 视角、商品+配件+背景、歧义 needs_input 且 0 生图；可选背景只在淘宝使用；必需背景冲突只令白底失败；必需来源容量不足无生图；快照篡改事实复核失败、陈旧素材抛 StaleAnalysisError；请求间不得残留选择。检查 asset_id、attempts、来源 ID 与实际模型 payload 一致；多主体旧字段按修订规格聚合。
- [ ] 增加恢复正向链路：analyze needs_input→读取option.selection→explicit携带原快照→真实validate_evidence被调用一次且解决pending→create成功；未解决则0生图。模型只用脚本响应fake，resolve/finalize/compile/evaluate用真实函数；增加同一主体95/false修复、required不适用失败、替代图不丢必需背面细节端到端断言。
- [ ] 每个异常捕获点立即make_error_info；分析/准备失败填结果级，逐资产失败填资产级，partial总error_info为空，needs_input只用issues。测试超时/容量/协议/质量/拦截/商品不符区分、retryable仅传输/可重试HTTP为true。单次成功结果不得残留之前失败尝试的最终error_info；attempts仍保留历史结局。
- [ ] 取消生成/CPU预处理时停止新请求，等待持有引用的纯计算线程完成，关闭客户端/释放许可后抛CancelledError；不触发save_result，不把取消记录为failed资产。文件阶段语义由任务7测试。
- [ ] 覆盖白底单独请求与混合服装请求、旧 4 参考图全部风格分析/仅第一张进入普通出图/白底不传风格；新路径多身份细节图进入白底；取消向上传播；无 output_dir 无文件系统写入。测试通过，提交 `feat: orchestrate standalone creation and per-image repair`。

### Task 7: 文件输出和命令行

**Files:** 创建 `output.py`、`__main__.py`，修改公共 create_images 输出集成；测试 `test_output.py`、`test_cli.py`。

**Interfaces:** `reserve_output(request: CreationRequest) -> Path | None`；`async save_result(result: CreationResult, directory: Path) -> CreationResult`；`run_cli(argv: list[str] | None=None) -> int`；`main() -> None`调用sys.exit(run_cli())。公共create_images持有并shield保存任务，文件操作在任务内同步、文件间可调度，不派生写盘线程；取消收尾按规格8节。

- [ ] 增加 --material 可重复且顺序稳定、--brief、analyze 子命令/analysis.json、--analysis；新旧输入互斥。analyze 不预留产物目录；create 遇 needs_input 保存 issues/分析且无图，CLI 退出 3。JSON 请求模式在 7A 接通。
- [ ] 用临时目录和假 create_images 写 CLI 测试：0/2/1/3 退出码（3=needs_input）、参数错误为 1（覆盖 argparse 默认的 2）、可重复平台/输出/参考、--market/--request-id、create 的 --out 必填、只接受路径；终端不打印长提示词或密钥。
- [ ] 写输出测试：

```python
assert jpeg_asset.file_path.endswith(".jpg")
assert saved_jpeg.read_bytes() == original_jpeg
assert manifest["assets"][0]["image"] == "taobao/main_image.jpg"
assert manifest["assets"][0]["file_path"] == "taobao/main_image.jpg"
with pytest.raises(OutputError):
    reserve_output(same_request_again)
assert model_calls_when_output_exists == 0
```

- [ ] 运行失败测试，再实现模型调用前独占创建运行目录；每张成功图写临时文件后原子替换，JSON 同理。路径确保在预留目录内；不清理/覆盖已有用户目录，不创建失败图文件。原图核对失败/needs_input 也在指定目录写 result.json；快照、element_plan、element_checks、attempts 同样可序列化且不含输入字节。
- [ ] 模拟单文件权限/写入失败：保留内存成功资产、file_path=None、warning；JSON 保留空路径不伪称已导出，CLI 返回1。result.json 写入失败抛带 result 的 OutputError。重复 ID/无ID 重复 create 均提前拒绝；Windows 保留名测试在跨平台测试中仍执行。
- [ ] 上述单文件失败同时写result.output_errors的ErrorInfo（不改成功资产的生成error_info）；未进入保存时取消不导出部分、保留目录，后续新ID；保存开始后shield整个保存任务，取消时等待完成/失败再传播。用事件同步测试取消和再次取消，不用sleep猜时序；断言调用结束后无后台写盘，原子处理收尾，用户目录未被覆盖/删除；CLI Ctrl+C=130。
- [ ] 命令行只做参数和展示，不重复编排规则；run_cli 捕获预期异常，意外编程错误保留可诊断性但不输出密钥/base64。测试通过提交 `feat: expose CLI and optional artifact output`。

### Task 7A: 稳定 JSON 契约与第三方绑定示例

**Files:** 创建 `contracts.py`、`tests/test_contracts.py`、`examples/integration_adapter.py`、`schema/{creation-request-v1,material-analysis-v1,creation-result-v1,error-v1}.json`；修改 `__init__.py`、`__main__.py`、`test_cli.py`。

**Interfaces:** `CreationRequestDTO/CreationResultDTO/ErrorDTO` 按规格7节；`request_from_dto(dto: CreationRequestDTO, sources: dict[str,ImageSource]) -> CreationRequest`；`result_to_bundle(result: CreationResult) -> ResultBundle`；`error_to_dto(error: AgentError | pydantic.ValidationError, request_id: str | None=None) -> ErrorDTO`；ErrorDTO={schema_version,request_id,error:ErrorInfo}，异常转换调用任务1 make_error_info；result转换保留捕获时已有ErrorInfo。ResultBundle={dto:CreationResultDTO,blobs:dict[str,bytes]}，与落盘JSON分开。

- [ ] 先写 DTO 往返测试：schema_version=1.0；未知主版本/字段拒绝；缺 media_id 绑定失败；同一 source 多角色由 material_id 区分；DTO 不含 bytes/Path/output_dir；succeeded/partial/failed/needs_input 均可 json.dumps；bundle blob_id 对应 asset_id，失败无 blob，metadata 无 base64/密钥/本地绝对路径。
- [ ] I3闭环：partial里超时与容量失败、准备ProviderError导致assets=[]、直接ValidationError三路径经DTO后code/kind/retryable/status_code一致；output_errors保留；不从中文reason推断类型。B1/B2/I1/I2新增intent/facts/requirements/option.selection完整JSON往返，恢复示例实际执行同一选择函数。
- [ ] `python -m pytest tests/test_contracts.py -q`，记录预期失败。
- [ ] 实现转换与 JSON Schema 导出；对 DTO schema 的 required/枚举/版本断言，避免依赖内部运行期对象结构。传回分析不能标记“已审核”；只允许走 create_images 的真实证据复核。
- [ ] 用内存 media_id→bytes 绑定演示 analyze→展示要素→显式 selection→create→bundle。只写调用函数和 main，不安装 FastAPI、不创建服务器/存储/任务框架。
- [ ] 接通 --request-json、--media-map、--media-root；测试绑定路径限定根目录、拒绝绝对/越界路径，旧/新 CLI 模式互斥；needs_input 退出 3，所有 issue 保存 JSON。CLI analysis 文件往返 ID 不漂移。
- [ ] 跑 `python -m pytest tests/test_contracts.py tests/test_cli.py -q` 通过，示例用 fake API 离线执行，提交 `feat: expose versioned integration contracts`。
### Task 8: 独立安装、隔离检查和交付文档

**Files:** 完善 `README.md`、`.env.example`、`docs/migration.md`；创建 `tests/test_isolation.py`、`scripts/live_smoke.py`；校验任务 7A 的示例与 schema；修改 pyproject（必要时）。

**Interfaces:** 手工验收脚本复用公共 API，默认单平台单主图；须显式 `--live` 才可联网，真实调用不纳入 pytest。README 给可复制的 Python/PowerShell 使用示例。

- [ ] 新增隔离测试：通过 AST/import 检查 runtime 不依赖禁止包；import image_agent 不连接网络/创建文件；仅 IMAGE_AGENT_* 配置生效。测试启动时拦截真实 socket 连接，MockTransport 不受影响；测试不得要求来源目录存在。
- [ ] README 覆盖新多图自动模式、显式要素选择、三来源、9 平台、三输出、按资产参考顺序与容量、白底例外、大小写/重复项、两个重试层、图像格式、同ID目录冲突、输出失败、环境变量、CLI 退出码、运行成本数量级和未实测限制；DTO/media_id 绑定、分析快照复核、needs_input、逐主体审核与同对象多视角案例。说明 `image_size` 对 gpt-image-2 是质量档，规则不是平台上架保证。
- [ ] README补充每资产required语义、Issue.options恢复、原子事实OR/AND、95/false失败、结构化错误、取消目录/130规则。调用量例子与规格8节一致：非服装无风格、淘宝主图+三详情无修复=10次逻辑调用；先分析再生成=11次；额外服装/风格/复核/HTTP重试另计。沿用任务3协议检查记录，不将离线capacity测试写成供应商验证。
- [ ] `docs/migration.md` 记录来源许可、hash、复用点、原设计补充项，尤其数字替换、白底导出后审核、单图重试；列出本次多图契约、参考排序、status 和输入兼容变更。来源文件hash变化有提示；测试不去读取来源目录。
- [ ] 跑 `python -m pytest -q`、`python -m ruff check src tests scripts`、`python -m ruff format --check src tests scripts`，全部通过后 `python -m build`。记录真实输出及测试数量，不预填成功。
- [ ] 在第二个干净虚拟环境安装 wheel（只装 wheel 所需运行依赖），从仓库外临时目录运行 import、`python -m image_agent --help`、`image-agent --help`，检查 wheel METADATA 只有 3 项运行依赖；通过 MockTransport 的离线公共 API 冒烟验证无需来源源码。
- [ ] 写真实验收脚本并检查 `--help`，本任务默认不运行付费调用。后续明确真实验收范围后，先一件非服装商品/一平台主图，再服装模特+详情与白底，再同商品 3 视角、商品+细节+配件+背景、明确双商品组合和冲突素材；记录模型响应、最终图片、打分与人工观察，不以打分代替人工检查。
- [ ] 自查规格覆盖、依赖、异常分类和原项目未改动；按执行方式进行最终代码审查，修复问题后仅重跑受影响测试及必要完整检查。提交 `test: verify standalone packaging and document usage`。

## 里程碑与完成定义

| 里程碑 | 任务 | 可核查结果 | 人工排期估算 |
| --- | --- | --- | --- |
| M1 契约与规则 | 1–2 | 多图/旧输入兼容、请求/规则离线测试通过 | 1.5–2 天 |
| M2 模型、分析与要素计划 | 3–5、5A | 意图→选择→原子证据/镜头计划离线通过；另记录任务3供应商协议检查点，未实测明确标记 | 4–6 天 |
| M3 创作与接入 | 6–7、7A | 自动多图、单图兼容、DTO、CLI、追踪与文件输出 | 3–4.5 天 |
| M4 可交付包 | 8 | 全量检查、wheel 干净安装、schema/示例与审查 | 1.5–2.5 天 |

全部必需任务完成才可声明“独立包已实现并通过离线验收”。若没有真实供应商调用，只能同时注明“真实模型出图尚未验收”。原项目行为、服务和数据库不在本计划修改范围内。

总计约 10–15 人日；真实多图模型可用性与效果单独验收，不把离线测试当成模型质量保证。

## 规格覆盖索引

| 当前修订规格 | 任务 |
| --- | --- |
| 1–3 多图范围、输入与兼容 | 1、7 |
| 4 自动发现、证据、澄清与快照 | 4、5、5A、6 |
| 5 资产适用、参考容量、staged | 2、3、5A、6 |
| 6 多主体审核、追踪、状态 | 1、4、5、6、7 |
| 7 无状态 API、DTO、media_id/CLI | 1、6、7、7A、8 |
| 8 验收风险与隔离 | 8，保留全部既有回归 |

下表是继承的原设计章节；发生替换按上表当前规格实现。

| 设计章节 | 任务 |
| --- | --- |
| 1–4 目标/边界/行为差异 | 1、2、6、8 |
| 5 请求与返回 | 1、6、7 |
| 6 准备与执行顺序 | 2、4、5、6 |
| 7.1–7.2 提示词及合规 | 2 |
| 7.3 模型协议 | 3、4 |
| 7.4–7.5 像素/视觉/复核/成组 | 4、5、6 |
| 8 重试 | 3、6 |
| 9–10 配置及结构 | 1、3、8 |
| 11–13 CLI/文件/错误 | 1、3、6、7 |
| 14–16 测试/隔离/非目标 | 全部，最终由 8 验收 |

## 计划自查记录

- 已核对原设计每节与任务映射；将 480 尺寸冲突、格式冲突、原图核对服务失败、客户端注入缺口写入评估补充。
- create_images 保留原两个位置参数，新增可选 analysis；新增 analyze_materials 和版本化 DTO；单图兼容、needs_input、CLI 3 与 JSON 状态一致。
- 五项 Review Focus 均落到对应测试步骤；来源快照与 SHA-256 清单已一起归档。
- 本轮按B1/B2/I1–I4更新共享类型、意图提案、原子证据、must_show、draft→evidence→finalize、ErrorInfo及95/false规则；增加三项建议的协议检查点、取消落盘契约和调用量示例。十任务结构不变，待实现测试不在文档阶段标通过。
- 后续实施应先读当前修订规格、原快照与评估三份文件，再执行任务；若用户调整契约补充，先更新此计划及对应测试约定。
