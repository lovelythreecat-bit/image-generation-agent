# 商品模型生成长图规格

日期：2026-10-10。状态：用户已确认需求及自主规划后实施；本规格用于当前实施。此前通用图文长图规格继续约束旧 API，不用于新商品入口。

## 目标与边界

从上传的真实商品图片、选中商品的分析证据及用户输入，生成类似 750×6153 的电商详情长图。商品主视觉、痛点、卖点、成分/材质、场景、使用方法是可选内容角色，由商品及已有信息决定。缺少材料、功效、方法或参数的可靠输入时，不创造对应事实；可以省略该主题或改为有证据支持的视觉细节。

图像生成模型必须生成每个区段的完整画面，包含商品、背景、短文案、场景和版式。程序仅等比标准化宽度、纵向拼接、连续像素切片和保存。禁止将模型产物降为局部配图，再由 Pillow、Tkinter、HTML 或其他程序绘制标题、正文、参数表、装饰版式或水印到商品长图。不得调用旧 `render_long_layout` 完成新商品输出。

复用现有 `create_images`、`MaterialAnalysis`、`ResolvedSelection`、`compile_element_plan`、参考绑定与图像生成 transport；保留 LangGraph、RunStore、BudgetLedger、审核和恢复机制。新路径是商品任务中的一个输出类型。旧 `create_long_image`、通用长图 CLI/DTO 和已有保存任务保持兼容。无新依赖、无新增独立测试页、无大范围重构。

当前工作区实施；禁止所有 Git 操作、分支/worktree、提交及远端操作。实现 agents 不修改 `.agent-runs/.../state.json`，仅主调度更新运行账本。产品任务自己的 RunStore 检查点 `state.json` 是正常持久化功能，不受运行账本禁写约束。

## 请求契约

`models.py` 增加 `OutputType`/`OUTPUTS` 值 `long_image`，`CreationRequest.long_image: ProductLongImageOptions | None = None`。选择该输出且未提供 options 时补齐默认值；未选择时允许保留输入但不启用长图路径、不增加模型调用。UI 未选择时传 `None`。

`ProductLongImageOptions` 字段：

| 字段 | 默认 | 验证 |
|---|---|---|
| `width` | `750` | 严格整数，400–2400 |
| `segment_count` | `6` | 严格整数，3–12 |
| `segment_aspect_ratio` | `"3:4"` | `"3:4"`、`"4:5"`、`"9:16"` |
| `known_info` | `""` | 字符串，最多 12000 字符 |
| `required_copy` | `[]` | 最多 30 条；每条 1–500 字符且非空白；保留原始字符 |

沿用商品名称、分类、`creative_brief`、`style_hint`、`presentation_mode`、`model_preference`、素材和 selection，以及现有图像模型别名/尺寸。参数只对长图输出生效，不改变混选的主图/三张详情图/白底图。

默认 6 个 3:4 区段，在 750 宽度下约 6000 高。6153 是参考长度，不强制输出这一高度；成品高度必须按实际完整区段等比缩放后的高度求和，不能通过裁剪、拉伸或补空白凑尺寸。最大成品 2400×60000 且不超过 60,000,000 像素，超限返回可诊断输出错误并保留原段。按 1500 像素高生成连续 PNG 切片（末片允许更短），无需新增 UI 切片参数。

`CreationRequestDTO` 同步增加可选 `long_image`，保持 schema_version 1.0。旧请求/结果缺少新字段仍可读取。新 output 需要新版客户端识别。每平台一个 `Asset(output_type="long_image", variant=None, asset_id="<platform>.long_image.default")`，出现在同一 `CreationResult` 和同一商品任务目录，继续 schema_version 2.0/task_type product。现有最终整图 blob 转换继续工作；段文件/切片路径只存在本地记录，不泄漏到 DTO。

## 策划与事实边界

先通过现有素材分析/选择准备一次 `PreparedContext`；非 ready、歧义、显式选择没有同一快照及快照不匹配时保持现有阻断行为，不提交长图生成。不要转换到旧 LongImageRequest 再重新识别商品。

每平台先调用一次结构化商品长图策划，输入必须包含当前平台、完整已选主体/元素/相关事实与事实来源、选中商品的原始图片、用户创作和风格要求、`known_info`、`required_copy` 和长图 options。输出统一风格、恰好 `segment_count` 个稳定有序区段；每段有角色、短文案、事实/输入来源、完整画面视觉指令。段数是视觉连续段数，不是必须填满六种万能内容模块。

建议在 `product_long_image_models.py` 定义：

- `ProductLongImageCopy(copy_id, text, source_kind, source_quote, fact_ids)`：`source_kind` 为 `fact/known_info/required_copy/brief/editorial`；事实引用必须属于已选商品，输入引用必须能定位到真实输入；editorial 只能是无事实主张的标题/过渡语。
- `ProductLongImageSection(section_id, role, title, copy, visual_prompt)`：role 为 `hero/pain_points/selling_points/materials/scenes/usage/specs/custom`；完整画面设计要求独立于角色，不提供本地布局框坐标。
- `ProductLongImagePlan(schema_version="1.0", input_fingerprint, style_direction, sections)`：本地验证来源、区段唯一 ID、段数、文案长度和 required_copy 覆盖。首段应为商品主视觉；其余根据事实选择和排序。

每条 required_copy 必须至少一次逐字符进入某段文案及该段生图提示词。保留用户提供的营销原文与数字，不额外要求证明、不削弱、不加免责声明；模型不得自行增加性能、认证、销量、成分比例、折扣、尺寸或售后承诺。视觉外观推断不能变成已知功能事实；图片的 promotional_text/inference 不自动视为实证。

策划指纹覆盖分析内容/素材哈希、resolved selection、平台、商品及创作信息、风格、long_image options。商品发现的现有 `analysis_fingerprint` 不需要因宽度/段数/known_info 改变而废弃；长图策划必须因对应输入变化而更新。不向全局 discovery 强加新的必填长图响应字段。

策划及各段编译提示词保存到任务目录/结果 JSON，可审查。生成提示词显式要求模型绘制指定中文短文案、商品、背景、层级、场景和布局；包含统一风格、区段序号/主题、相邻段衔接及只使用已选商品证据。避免继承旧长图“文字由程序绘制/不要添加页内文案”的指令。

## 生成与审核

第一版采用连续语义分段生成，使用已支持的 3:4/4:5/9:16 比例，避免要求 transport 输出超长比例。不需要建立供应商超长能力表或付费尝试后才回退。

每段复用 `compile_element_plan`、`select_references`、`bind_generation_references`。任何连续性参考只能补充已选真实商品参考；绝不能挤掉必需证据或替代商品身份。可用第一个已审核区段作 `shoot_continuity` 参考，容量不足时保留真实商品证据并记录警告，统一文字风格指令始终保留。

每次生图进入现有 image call_scope/预算台账，结果先存 raw 和操作 journal，再存 immutable segment candidate，最后审核。单个段失败只修复该段，使用现有 `ExecutionPolicy.max_quality_repairs` 和 `max_audit_retries` 上限。不得套用只生成空场景再融合的 staged 流程；strict 修复也必须产出完整含文案的区段。

逐段检查像素比例、已选商品外观/原生标签，以及当前段文案清晰准确、用户数字和必需原文、计划主题覆盖、无新增未经输入支持的事实。结构化文案审核建议 `ProductLongImageCopyAudit(passed, text_matches, no_invented_claims, layout_readable, issues)`，与现有外观 QualityReport 一起保留。模型审核不能伪装为确定性的逐字 OCR 证明；错误/不确定时保留候选及真实状态，不能判定成功。

所有段有完整像素时，即使某段审核失败也可生成整图候选用于审查；其状态继承 audit_error/quality_failed/budget_exhausted/pending_audit 等严重状态。缺任何必需段时不拼接伪完整长图。最终 succeeded 必须所有段外观与文案审核通过、像素组装和保存成功；人工接受完整候选保持 accepted 且保留失败检查。新长图不得进入固定三张的 `audit_detail_set`。

## 持久化与恢复

复用当前商品任务的 graph phases、预算 SQLite、操作 journal 和锁。增加有界长图专用阶段并把重逻辑放新模块；阶段建议 `long_plan/long_generate/long_save_candidate/long_audit/long_compose`。保存平台长图计划、当前段 index、每段尝试/候选/质量/文案审核、原始哈希引用及完整合成候选。

最终 `Asset.candidates` 保留可预览整图；中间段如果放该列表必须 status=stage，使现有人工接受/DTO 成品判断继续拒绝中间产物。段详情可放本地候选 metadata，最终 candidate 引用完整计划、segment 审核与切片，保留来源文件。不得把最后一段审核冒充整图质量；没有聚合 QualityReport 时 asset.quality 可以为 None，使用逐段审核 metadata 明确成功依据。

恢复验证所有段/raw/最终整图/切片路径与哈希，包括未挂入最终 candidates 的运行时引用。已 journal-saved 结果只读取并继续保存/审核，不重复 POST；submitted/started 无记录响应保持 generation_uncertain，禁止猜测重试。取消后保存已返回的图片并等待后台清理。完成后 audit_error/pending_audit 恢复到对应长图审核段，不能走普通整图 audit 并重新生图。本地拼接/保存失败可以只重跑本地阶段，图片预算不变。

组装时逐段等比缩至 width，计算累计 y 边界，粘贴完整 RGB 像素后导出 PNG。切片按区间 [y0,y1) 裁出；重拼须与整图逐像素一致。路径限定任务目录、保存原子化。源段不可因最终成品被移入 approved 后清理掉。

## 桌面交互

现有商品图片入口增加“长图”复选项，允许与其它图片类型共同选择。只勾选后展示宽度、区段数、区段比例、已知商品信息、必须出现的文案；延用现有创作要求和风格。取消勾选保留界面输入但请求不启用长图，不切换 mode、不重建素材/分析、不新建任务。

不再展示需要切换的“图文长图”模式入口。可保留内部 `set_mode("long_image")` 和旧通用长图 panel 以加载/查看历史任务，不删除旧 API/历史测试支持。长图生成仍走当前 start(create) -> create_images，同一后台 worker、取消/进度、素材选择、结果列表和候选审核。

长图结果在当前商品结果区支持适应宽度、缩放、纵向滚动，候选标识和状态明确；复用旧 panel 的有界后台预览机制或抽取相同 worker。解码不得阻塞 Tk；切换图片/候选及关闭窗口时旧异步结果不可覆盖新预览。已知信息等内容参数仅使未来长图策划变化，不清空不相关的素材分析。

## 验收与兼容

离线带假 providers 的集成验收必须证明：同一商品分析/selection；真实选中商品 reference；每段完整设计提示词含特定商品信息/短文案；不调用旧排版或任何绘字路径；一平台一个完整 Asset；混选维持主图/详情图/白底行为；默认段数6及约750×6000；切片像素还原；候选审核语义；取消/审核错误/本地保存失败/不确定提交的恢复不重复计费。

旧 `test_long_image_product.py::test_exact_copy_does_not_enter_slot_must_show_but_native_label_does` 等断言仅约束旧 create_long_image 配图路径，必须保留，不反转；新商品 long_image 测试必须反向证明页内文案进入图像模型。桌面旧 mode 测试可继续通过内部方法，关于“可见模式选择器”的断言应迁移为商品复选项且无独立模式入口。README 说明两条 API 的差异并提供新 CreationRequest 示例。

离线测试只能证明链路与状态，不能证明某供应商实际中文/商品一致性达到参考图质量。review 必须核对提示词和输出路径；若没有用户授权的真实调用与画面证据，最终说明尚未完成真实模型视觉验收，不能用合成假图声称效果已验证。
