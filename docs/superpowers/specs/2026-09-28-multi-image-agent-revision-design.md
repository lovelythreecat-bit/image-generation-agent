# 独立图片 Agent：多图要素创作与接入边界修订

日期：2026-09-28。状态：已按用户要求落实专业评审 B1、B2、I1–I4 及三项建议；未实现产品代码。本文是当前实施规格，修订对应关系见 [评审修正记录](../reviews/2026-09-28-review-resolution.md)。

## 1. 已确认需求与文档优先级

用户已确认：一次输入多张素材，生成前自动提取各图要素，再将选定要素与生成要求结合，体现在最终产物；素材不限于只借鉴风格。Agent 应是独立创作核心，方便第三方调用与未来工作台接入，本期不开发工作台。

以下数量上限、字段、默认选择方式及两阶段 API 是推荐实现决策，不冒充用户原话。自动分析必须开箱即用，不要求用户逐张标角色；多图不等于拼贴，也不等于把每张图里的所有商品都塞进产物。

实施依据优先级：**本文 > [评估第 3 节补充](../reviews/2026-09-28-extraction-feasibility.md) > [原设计快照](2026-09-28-standalone-image-agent-design.md)**。原快照与 source-manifest 保持原样，供溯源。本文明确替换原设计的单一 product_image 契约、准备步骤、商品身份来源、生成参考顺序、视觉审核及接入方式；未替换的模型协议、平台文本、像素阈值、配置、重试和文件输出规则继续适用。

## 2. 方案与本期范围

推荐“自动提取 → 可检查的要素计划 → 按产物生成与审核”的轻量异步流水线。相比把所有图直接交给生图模型，它能解释哪些元素被选用、为什么忽略其他元素，也方便第三方在生成前修改选择。相比建立任务服务或通用 Agent 框架，它仍能作为一个 Python 包独立安装。

本期交付 Python API、CLI、版本化 JSON DTO 与转换示例；第三方可将其薄封装为 HTTP。**不实现 HTTP 服务、上传端点、工作台、鉴权、队列、WebSocket、任务持久化、云存储或插件注册框架。** HTTP 指模型供应商调用时仍在本期。核心不读取租户、会话和业务任务状态；request_id 仅用于关联。各请求内素材、计划、结果独立，只有已有供应商限流额度是运行基础设施共享状态。

## 3. 请求与单图兼容

保留原商品信息、平台、输出、模特、比例、模型和可选落盘字段，新增：

```text
CreationRequest
  materials: list[MaterialInput]               # 新路径：1–8 张，保留输入顺序
  creative_brief: str | None                   # 想呈现的内容；style_hint 继续只表达风格
  selection: SelectionSpec                    # 默认 auto，无需先调用分析
  product_image: ImageSource | None            # 旧路径兼容字段
  reference_images: list[ImageSource]          # 旧路径兼容，最多 4 张

MaterialInput
  material_id: str                             # 请求内唯一稳定 ID，非路径
  source: ImageSource                          # Python：path/url/data 三选一
  role_hint: auto | identity | detail | accessory | scene | style # 默认 auto
  subject_hint: str | None                     # 可选，用于说明属于同商品/哪个对象
  element_hints: list[str]                     # 可选，说明希望提取什么，不当成观察事实

SelectionSpec
  mode: auto | explicit                       # 默认 auto
  subject_ids: list[str]                       # explicit 时指定最终主体（可多个）
  required_element_ids: list[str]
  preferred_element_ids: list[str]
  excluded_element_ids: list[str]
```

materials 与旧 product_image/reference_images 两组互斥；两组都空或混用直接校验错误。旧路径转成 material_id=product 的 identity 素材，ref-1…ref-4 的 style 素材，保留“全部分析风格、首张风格进入生成”的行为；单图调用无额外选择负担。旧 CLI `--product/--reference` 保持可用。新路径最多 8 张是产品默认上限，不代表所有供应商支持 8 张生成参考。

material_id 仅允许 1–64 个 ASCII 字母、数字、`-`、`_`，不能重名。selection 的三个要素列表互斥；ID 必须存在于对应分析；主体至少一个。auto 的四个 ID 列表必须为空，可直接附带 creative_brief。需要精确 ID 选择时先分析，之后用 explicit 和同一分析快照提交；create_images 的 explicit 模式缺少 analysis 是 validation 错误，不能拿一次重新发现的 ID 猜测对应关系。analyze_materials 仅接受 auto。所有运行期类型拒绝未知字段，DTO v1 不做静默字段丢弃。

## 4. 自动提取与选择

1. 加载全部输入，验证为可解码图片，按输入顺序计算内容 SHA-256。坏图在任何模型调用前报 InputImageError；不保留敏感本地路径到对外 DTO。
2. 视觉模型一次读取全部图（标签含 material_id，长边 768），同时提取原子事实、归并主体、解析 brief/提示并给出结构化选择建议；不用纯函数猜测自然语言。输出经 Pydantic 及引用完整性校验。IMAGE_AGENT_VISION_IMAGE_LIMIT 默认 12（部署声明，>=1）：发现/快照核对计全部素材，单图审核计 audit_material_ids 去重后 +1，成组审核计三计划审核来源并集 +3；每次调用前校验。容量不足返回 capability 错误，不截断；本期不实现分批聚类。服装补充按已选服装主体各最多一次、旧风格分析按原回退规则执行；身份与必需事实发现失败不得降级生成。
3. 核对商品名/类目与所选主商品或明确组合；配件按其被要求的角色核对，不要求杯盖与“杯子”同名，未选背景商品不使整单失败。Subject.matches_product 表示与该声明角色相符。自动占位名规则沿用原设计；确认所选商品不符返回 failed。多视角合并时保留每条事实来源，不把背面缺少正面图案认作冲突，也不推断不可见细节。
4. 选择层消费 intent.proposal：自动保留核心身份、模型识别出的明确用户要求和相关候选；未被明确要求的场景/风格/配件默认 preferred。多对象是否共同呈现、否定要求及相对关系由该次视觉模型输出结构化字段和原文依据，selection 不做关键词推断、不再调模型。无法确定主体或身份时返回可操作的 needs_input。
5. 生成资产前按平台和镜头编译 AssetElementPlan。商品身份及已选主体必须保留；可选场景/风格与白底主图冲突时排除并记录理由，其他允许的资产仍使用。用户 required 要素与某资产平台限制冲突，只有该资产失败，不连带拒绝其他资产。

```text
MaterialAnalysis
  schema_version: "1.0"
  analysis_id: str
  fingerprint: str                 # 内容 hash、顺序、角色/提示、商品信息、brief、分析规则版本
  status: ready | needs_input | failed
  materials: list[MaterialObservation] # material_id、hash、observed_role、summary
  subjects: list[Subject]
  facts: list[Fact]
  elements: list[Element]
  intent: IntentAnalysis | None   # 仅发现服务失败、status=failed时可None
  issues: list[Issue]
  warnings: list[str]
  error_info: ErrorInfo | None

Subject
  subject_id: str
  material_ids: list[str]
  identity_fact_ids: list[str]      # 不再存无来源链接的自由文本 identity_facts
  representative_material_id: str  # 该主体最清晰的身份视角；必须属于 material_ids
  matches_product: bool

Fact
  fact_id: str
  subject_id: str | None
  description: str                 # 一条独立可核验事实；正面商标与背面拉链必须分开
  evidence: list[{material_id, observation: str}] # 同一事实的可替代来源，非空
  confidence: float                # 0–1；不保证事实真值

Element
  element_id: str                  # 解析后按输入/发现顺序分配，分析快照内稳定
  kind: identity | detail | accessory | scene | style
  subject_id: str | None
  description: str
  fact_ids: list[str]              # 该要素所需的全部原子事实，彼此为 AND

IntentAnalysis
  proposal: SelectionProposal
  constraints: list[IntentConstraint]
  focus_element_ids: list[str]     # feature/closeup 候选焦点，有序且有事实证据
  unmet_requirements: list[Issue]

SelectionProposal
  primary_subject_id: str | None
  subject_ids: list[str]
  required_element_ids: list[str]  # 明确用户要求；默认身份从 Subject 单独补入
  preferred_element_ids: list[str]
  excluded_element_ids: list[str]

IntentConstraint
  constraint_id: str
  kind: placement | co_presence | exclusion | appearance | atmosphere
  subject_ids: list[str]
  element_ids: list[str]
  instruction: str                 # 如“杯盖放在杯子旁，不扣上”
  source: brief | hint
  source_quote: str                # 必须是对应用户输入的原文片段
  source_material_id: str | None   # hint 时必填；brief 时 None
  priority: required | preferred

Issue
  issue_id: str
  code: str                       # 如 ambiguous_subject、conflicting_identity、missing_evidence
  resolution: selection | recheck | reanalyze
  message: str
  subject_ids: list[str]
  material_ids: list[str]
  element_ids: list[str]
  fact_ids: list[str]
  options: list[{id: str, label: str, selection: SelectionSpec | None}]
```

同一 Fact 的 evidence 是 OR（任选一个有效来源可支持生成），多个 required Fact 是 AND（必须全部覆盖）。不把多条事实压在一个 description 中；身份事实与普通要素共用此结构。未知/重复 ID、断开的引用、没有证据的事实、proposal 跨主体引用错误或 source_quote 不存在均为协议错误。核心规范化模型返回 ID 时必须同时重写所有引用；同一快照 ID 不变。低置信度 <0.7 且影响身份/required 时需要澄清；明确请求无来源的商品结构/材质返回 missing_evidence。普通背景气氛只形成来源为 brief 的 IntentConstraint，不伪造 Fact。

auto 采用 proposal；explicit 用传入的完整 subject_ids 与三组元素列表替换 proposal 的列表，主主体为所选列表首项，但不能去掉该主体身份事实。没有显式列入的普通建议元素不自动保留。明确 brief/hint 的 required constraints 继续生效：选择排除了被要求呈现的对象，或选中了exclusion要求不得出现的对象，均返回selection_conflict；exclusion引用已排除对象是正常情况，不能要求把它选回来。冲突需修改brief后重新分析，不能静默覆盖指令。focus从仍被选中的focus_element_ids取首项，再按主体/要素稳定顺序回退；没有可信细节时closeup计划失败。代表图由发现模型选择，纯函数只校验所属主体，不重新评估清晰度。

analysis_id/element_id 只保证同一快照稳定。fingerprint 覆盖素材字节、顺序、角色/提示、商品信息、brief、分析规则版本（本修订为 multi-image-r2）；只改 selection/平台/输出不失效，其余变更抛 StaleAnalysisError。hash 不证明快照真实；传入快照都须核对真实素材、已选事实及 intent 对当前用户原文的忠实性，该调用不能复用外传的“已通过”标记。intent=None的失败快照没有可复用候选，需重新分析，不能生成。

澄清恢复分两段：`resolve_selection` 返回 SelectionDraft（candidate、pending_issues、blocking_issues），先校验 ID 并建立候选，**不因旧 needs_input 或 pending 冲突提前返回**。selection 类问题在选择唯一合法候选后解除；只涉及被排除主体/事实的旧问题可标记 irrelevant，仍保留处理记录。涉及选中事实的 recheck 问题传给 validate_evidence；没有可分辨候选、需重拆混合事实的 reanalyze 问题明确要求换素材/提示重新分析。非法 ID 抛 validation；没有候选或有 blocking 问题才提前 needs_input。

`validate_evidence` 消费 candidate 和 pending issues，返回 EvidenceValidation（outcome=verified|needs_input|failed、subject_checks、fact_checks、intent_valid、issue_resolutions）；每个待复核 issue_id 必须有 resolved/unresolved 及原因，缺项是协议错误。随后 `finalize_selection` 生成 ready/needs_input/failed：所有活跃问题解决且 intent/事实核对通过才 ready；确认商品不符或服务/协议失败为 failed；仍无法判断为 needs_input。自动发现且没有未决问题可免除此额外复核；快照始终复核。`Issue.options[].selection` 是可直接提交的完整 explicit 选择，接入方不用解析 label；reanalyze 选项 selection=None 并说明需改哪些素材。例：红款/蓝款分别有候选时可选红款并复核；已错误归并成一个不可拆候选时要求重新分析，不能仅把蓝色事实删掉蒙混过关。

## 5. 按产物编译参考与约束

AssetElementPlan 包含 target_key、subject_ids、focus_element_id、required/preferred/excluded element_ids、excluded_reasons、constraints（IntentConstraint）、requirements、required_fact_ids、generation_material_ids、audit_material_ids、reference_bindings（图序号→material_id→fact_ids/element_ids）及 issues。每条 Requirement 明确 `target_kind:subject|fact|element|constraint`、target_id、`origin:default_identity|user_required|brief_required|shot_rule|preferred`、`applicability:must_show|preserve_if_visible|not_applicable`、reason。各主体还记录 effective presentation_mode；运行时不得从审核结果反推可见要求。

第一版 required 是**每张请求资产都必须满足**的要求，不做整组分摊：explicit required_element_ids 与 brief/hint 的 required constraints 均为 must_show。平台要求优先，但冲突时使该资产失败，不静默删掉用户要求；镜头规则不能豁免 required。若“必须展示杯盖”和杯壁微距无法同时满足，closeup 在编译期返回 requirement_conflict，其他可满足的资产继续；需要按镜头指定不同要求是后续扩展。

默认身份要求与用户 required 分开：主图/scene 中所选主体全部 must_show；feature/closeup 的默认主体可缩到焦点所属主体，其他主体 not_applicable。焦点主体的身份事实默认 preserve_if_visible（必须保持真实但不强迫把正背面同时拍出），焦点要素的全部事实 must_show。用户 required 覆盖默认镜头豁免，不能在每张图上都把同一 required 排除。白底单件遇多个 must_show 主体则计划失败。成组审核继续只报告，因为用户 required 已由单资产执行，不依赖成组审核兜底。

closeup采用保守可执行规则：所有用户/brief必需事实必须属于选定焦点要素的fact_ids，涉及多个必须可见主体的关系亦不支持微距；超出则编译期requirement_conflict，不让纯函数再次理解自然语言。feature可扩大到全部must_show对象；若平台硬规则不允许则失败。纯氛围约束无需事实覆盖，但仍参加constraint_checks；明确排除对象的exclusion约束语义是“不得出现”，其must_show表示必须执行该约束，并非把被排除对象画出来。

每张产物的生成参考顺序：**主身份代表图 → 其他可见主体代表图（主体顺序）→ 支撑必需事实的图 → 支撑可选细节/配件的图 → 场景 → 风格**。代表图采用 Subject.representative_material_id；仅对本资产可见主体强制加入。同级按输入顺序，一图多用只传一次。编号说明明确每张图支持哪些事实，禁止套用未选商品身份。

`IMAGE_AGENT_GENERATION_REFERENCE_LIMIT` 默认 4（>=1），是部署声明而非自动探测。required_fact_ids 为可见主体身份事实与 must_show 要素事实的并集；代表图先占槽，再按事实稳定顺序，为尚未覆盖的事实加入其输入顺序最早的有效替代来源。一图可覆盖多事实，同一事实的五张替代图只需其中一张；正面商标与背面拉链两事实则必须各自覆盖。不承诺数学最优；超过上限返回 reference_capacity_exceeded，不删必需事实，不自动拼图。余位按优先级加入 optional，可选要素只以文字传达时记录 text_only。此容量门槛也适用于 staged 融合。

generation_material_ids 表示实际发送生图的覆盖集合；audit_material_ids 包含本资产可见主体/事实的全部相关来源（包括未用于生成的替代视角与冲突来源），去重后审核，避免只看选中一张而遗漏矛盾。没有选中的无关主体不加入审核，但已归并到所选主体的矛盾来源不能通过删选图逃避复核。审核来源超视觉容量时按调用失败处理，不静默截断。

白底和 Amazon 主图忽略场景/风格，仍可接收该商品多个身份/细节视角。旧路径首张风格的约定仅适用于旧 reference_images，不限制新 materials 的内容融合。图片生成长边 1024 JPEG、质量审核 768、旧风格分析 512，继续保持。

staged 第一步仅传入选中的场景/风格图，为计划要求可见的主体预留位置；第二步输入身份/必需事实证据，并把 stage 图作为最后的场景参考。stage 占一位，容量不足则尚未开始 staged 就改 strict 并记录原因。修复不得更改 requirements 或以另一镜头豁免失败项。Amazon 主图/白底仍仅 strict；最多一次质量修复且同模型。

## 6. 审核、结果与错误

审核采用冻结的 requirements 与 audit_material_ids，生成图最后放入。可见主体必须 **same_product=true 且 score>=85**；required detail/accessory 要素保真 >=85；其余视觉/平台/融合/偏好/意图沿用原阈值。初审 95/false 按“身份不一致”的质量失败处理，允许既有的一次质量修复，不当作通过，也不额外触发 80–84 复核。80–84 仍最多一次复核，只有 same_product=true 且复核>=85 才替换。

GeneratedImageAudit 返回 subject_checks、fact_checks、element_checks、constraint_checks；所有 ID 唯一且与计划预计审核集合一致，未知/重复/漏 required 项为 protocol 失败（不重新生图）。fact/element 检查含 target ID、presence=present|absent|not_applicable、fidelity_score（适用时0–100，否则None）、reason；constraint_checks 含 constraint_id、satisfied、reason。must_show 返回 absent 或 not_applicable 是质量失败；审核模型不能覆盖计划的 applicability。preserve_if_visible 的事实不在镜头中可记 not_applicable，分数必须 None，不能伪造100；若实际可见则检查保真>=85。preferred 缺失仅报告，required constraint 的 satisfied=false 必须失败。默认身份以主体身份检查与可见事实共同保证，背景变化不计身份不一致。

每个主体一致性 80–84 可做一次聚焦复核；可把同资产待复核主体合并成一次请求，但返回逐主体分数，必须 same_product=true 且 >=85 才替换。required 细节缺失不因主体复核通过而解除。缺必需元素归入质量修复；身份/融合问题可以 staged，其他 strict。审核服务/协议失败仍直接失败，不触发新一轮创作。

asset_id/target_key 固定为 platform.output_type.variant（无 variant 用 default），同请求内唯一；调用方组合 request_id 做跨请求标识。

Asset 除原字段新增 asset_id、element_plan、element_checks、reference_bindings、attempts；attempts 保存各逻辑阶段 standard/strict/staged_scene/staged_fusion 的模型、参考 ID 顺序、最终合规提示词及结局，不包含原图/base64或认证信息。Asset.prompt 仍是最终一次合规提示词。追踪是输入证据与审核报告，不能保证生成模型真实使用了每一图。

旧结果字段继续保留：input_check 是所有选中主体核对的聚合（任一不匹配为 false，needs_input 为 false 且 reason=待澄清），observed_product 汇总带 subject_id 的描述；product_attributes 改为 {subjects: {subject_id: 可见事实}}，单图也用同一结构。presentation_mode 保持请求级解析值，auto 在任一选中主体是服装时可用 model_wear，非服装配件仍按配件呈现；每个 AssetElementPlan 记录各主体展示方式，白底始终 product_only。style_prompt 保留可选风格文本，不代替元素计划。该 product_attributes 结构变化写入迁移说明。

CreationResult 增加 schema_version、request_id、analysis、issues、error_info；status 为 succeeded/partial/failed/**needs_input**。准备阶段需澄清则 needs_input、assets=[]、error_info=None，问题放 issues；准备服务失败则 failed、assets=[]，有结果级 error_info。各资产/成组审核也增加 error_info，保留旧 error 字符串与质量 JSON。partial/逐图全失败的 error_info 只在对应资产，不制造一个不准确的总错误；succeeded 的生成 error_info 为 None，落盘问题另放 output_errors。Asset.error 与 error_info.message 同源。

统一 `ErrorInfo={code:str, kind:validation|configuration|input|stale_analysis|transport|http|protocol|capability|compliance|quality|output, message:str, retryable:bool, status_code:int|None}`。映射固定：超时/连接耗尽→provider_transport（true）；429/502/503/504耗尽→provider_http（true），其他HTTP为false；协议→provider_protocol（false）；容量→reference_capacity_exceeded 或 vision_capacity_exceeded（false）；平台/镜头冲突→requirement_conflict（capability,false）；拦截→compliance_blocked（false）；质量修复耗尽→quality_failed（false）；validation/configuration/input/stale_analysis/output 各使用同名code、retryable=false。原图确认不符用 input_mismatch（input,false）。retryable 是调用方选择是否重提的提示，不自动重跑整单。

异常被捕获时立即构造 ErrorInfo，不能等 DTO 阶段从中文文本反解析。发现返回失败时 MaterialAnalysis.error_info 与 CreationResult.error_info 同步；未获得分析时 result.analysis=None。单文件写入失败仍保留内存成功资产，在 result.output_errors:list[ErrorInfo] 记录失败并告警；JSON写入失败的 OutputError 携带结果。成组失败的 error_info 只属该审核且不改变资产成功。所有消息先脱敏再限制500字符，不导出凭证、字节和本地绝对路径。

## 7. 稳定接入契约

```python
async def analyze_materials(request: CreationRequest, config: AgentConfig | None = None) -> MaterialAnalysis: ...
async def create_images(request: CreationRequest, config: AgentConfig | None = None,
                        *, analysis: MaterialAnalysis | None = None) -> CreationResult: ...
def request_from_dto(dto: CreationRequestDTO, sources: dict[str, ImageSource]) -> CreationRequest: ...
def result_to_bundle(result: CreationResult) -> ResultBundle: ...
```

create_images 不传 analysis 时自动完成全流程；analyze_materials 只分析，不生成、不预留输出目录。分析内部与 create_images 共用准备逻辑，不重复实现。

`CreationRequestDTO` 的 schema_version 固定 "1.0"，materials 是 `{material_id, media_id, role_hint, subject_hint, element_hints}`；其余创作字段与新 CreationRequest 一致，但不包含 ImageSource、output_dir、Python bytes 或旧兼容字段。外部上传得到的 media_id 由第三方 adapter 解析成 sources[media_id]，核心不识别对象存储 URI。缺 media_id 绑定时输入错误；本地路径与 URL 也只经调用方显式绑定，不允许 DTO 任意指定服务器文件路径。

MaterialAnalysis 可直接 JSON 往返，用于工作台展示候选并提交 selection。`CreationResultDTO` 保留创作元数据，将 Asset.image/file_path 替换为 blob_id/mime_type/byte_length；ResultBundle 含 dto 和内存 blobs: dict[blob_id, bytes]，blob_id=asset_id。成功图有字节，失败图无 blob。DTO 中无 base64、Path、异常对象或供应商密钥。第三方把 blobs 写入自己的存储后映射下载 URL，映射不进入核心。

导出 Pydantic JSON Schema（schema/creation-request-v1.json、material-analysis-v1.json、creation-result-v1.json、error-v1.json），测试 JSON round-trip 与绑定映射。未知主版本拒绝，破坏性变更升级主版本。`ErrorDTO={schema_version, request_id, error:ErrorInfo}` 与结果/资产内嵌 ErrorInfo 共用映射；普通异常由 error_to_dto 转换，failed/partial 由 result_to_bundle 原样保留已有结构化错误，均不解析字符串。ValidationError 只导出字段位置/错误类型，不回显输入值。

本期提供无 Web 框架的 adapter 示例；未来 HTTP 层负责鉴权、上传、资源来源限制、超时/取消与HTTP状态映射，不向核心引入 Request/UploadFile/数据库实例。取消传播与可选落盘按第8节执行。

CLI 新增可重复 `--material path`、`--brief`，自动分配 m1/m2…；另支持 `--request-json file --media-root dir` 的 DTO 模式，媒体映射文件为 `--media-map file`（media_id→相对路径，解析须留在 media-root）。这两种新模式与旧 --product/--reference 互斥。`analyze` 子命令导出 analysis.json；create 可用 `--analysis file`，需要精确选择时使用请求 JSON。needs_input 退出码 3；旧 0/2/1 意义不变。CLI 输出简短 issue 与选项，完整分析/计划/追踪写 JSON。可选 result.json 维持原相对文件路径格式，同时包含新字段；它是落盘清单，与对外 ResultDTO 是两个明确格式。

## 8. 验收与风险

离线必验：单图兼容；三视角同商品；商品+细节+配件+背景；多对象歧义与显式组合；冲突型号/颜色；缺证据；白底仅排除可选场景；必需要素平台冲突按资产失败；reference limit/顺序/去重/staged 预留；逐主体审核与缺要素修复；溯源和分析快照校验；DTO 与字节分离、解析绑定、无业务状态。继承原 HTTP、像素、取消、文件、依赖隔离和独立安装测试。

任务3后设置供应商协议检查点：核对届时的官方接口资料、模型参考字段、容量和响应格式，记录资料日期与未验证项；4/8图 MockTransport 通过只代表离线协议实现。真实多图调用另按已确定的模型/素材/数量执行，不因本次文档修订自动开始。未实测时仍可做后续离线工程，但不得宣称容量/效果已通过。最终人工检查多视角、配件、背景及误混，不能仅凭模型自评证明融合准确。

取消约定：生成阶段只在内存保存结果，取消时不导出已完成部分、不开始落盘；预留目录保留，下次需新request_id，首版无恢复。to_thread只做无写盘的图片计算；任务保持强引用并shield，取消清理等待计算结束，再关闭客户端、释放许可并抛CancelledError。最终async save_result是一个整体shield任务，文件写入/原子重命名在该任务内同步执行、文件间设置可调度点，不派生写盘线程。落盘期间取消则等待保存任务完成或报告写盘失败再抛取消；磁盘可能已有结果，但调用不返回成功。请求结束后禁止后台写入、不删除用户已有目录。CLI Ctrl+C=130。反复取消仍须完成既定清理后传播；进程强制终止/断电不在该协作式取消保证内。

调用量示例：一个非服装商品、无额外风格分析、淘宝主图+三详情、无修复/复核时，自动模式为1次发现+4次生图+4次单图审核+1次成组审核=10次逻辑调用。先analyze后create为11次（增加快照证据/意图复核），不是零成本复用。每个额外服装主体的事实补充、风格回退、身份复核另计；一次逻辑HTTP调用最多3次请求。全9平台37资产的生图上限仍是109次，不含这些视觉调用。首版只在文档解释调用量，不实现计费系统。

多图增加视觉 token、上下文长度和推理错误；第 4 节的自动归并与跨图冲突识别是新增主要风险。1–8 张输入、默认 4 张生成参考属于可调整工程限制；复杂多商品、微小标志或密集文字不保证模型稳定实现。预计在原 6–9 人日基础增加约 4–6 人日，合计 10–15 人日（离线开发与审查），真实供应商调试单列。

专业评审的修正已落到本规格与任务验收；原评审保留为历史记录。下一步按更新后的计划实施，运行期测试和真实模型效果仍待开发后验证。
