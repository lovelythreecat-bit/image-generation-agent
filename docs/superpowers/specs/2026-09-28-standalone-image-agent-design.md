# 独立图片创作 Agent 设计

日期：2026-09-28

状态：构想。本文只定义以后要拆出的包，不修改 MediaForge。

## 1. 要做成什么

从当前商品图片创作流水线里，拆出一个可以单独运行的 Python 包 `image_agent`。调用方传入商品图、商品信息和可选参考图，包内完成提示词、平台规则、出图、质量检查和一次修复重试，并返回图片字节与检查结果。

这里的 Agent 是一个有固定步骤的创作运行时，入口是 `create_images()`。它不和用户对话，不自己决定要不要检索，也不调用工具规划下一步。

现有 MediaForge 继续按原样运行。拆包时不改它的页面、接口、Celery 任务和数据库，也不让两边互相 import。

## 2. 不做什么

第一版明确不做这些事：

- 不连接、不读取 MediaForge 的 PostgreSQL、Redis、Milvus、MinIO、租户和任务表。
- 不实现爆款检索、向量、重排、指定 SKU 参考。参考图由调用方传入。
- 不实现任务中心、确认卡片、SSE、配额、费用记账和 Celery 重试。
- 不生成视频。
- 不包含社媒图 `social`。现有 `SocialWorker` 能跑，但 `SkuInput` 不接受这个输出类型；社媒平台也不在平台规则表里，比例会落到默认 `1:1`。第一版不把这个半成品带出去。
- 不使用 LangGraph、LangChain、Celery。当前批次图是固定顺序，独立包用普通异步函数表达即可。
- 不把 MediaForge 改成调用这个包。两边以后可以各自演进，规则变更时手动同步，不共享代码。

## 3. 和现有流水线的对应关系

现有一次创作在 `process_sku` 里跑 LangGraph：校验、准备上下文、按输出类型出图、质量失败则整单重试一次、收尾并写入任务。独立包只保留中间的创作步骤。

| 现有代码 | 独立包 |
| --- | --- |
| `mediaforge/workers/platform_rules.py` | 原样带走平台规则、比例、生成指令和审核指令 |
| `mediaforge/workers/compliance/checker.py` | 带走危险词拦截、市场用字替换、品牌/IP 警告、场景放宽、平台指令、水印禁令 |
| `mediaforge/workers/image/base.py` 的展示方式、提示词、原图核对、服装结构、风格分析、像素检查、视觉质检 | 带走，去掉检索和存储 |
| `mediaforge/workers/image/main_image.py` | 主图镜头说明 |
| `mediaforge/workers/image/detail_page.py` | 场景、卖点、特写三种镜头，以及三图成组检查 |
| `mediaforge/workers/image/pinduoduo_white_background.py` | 白底图提示词，以及导出为 480×480 PNG |
| `mediaforge/workers/openrouter_client.py` 的图片请求、响应解析、HTTP 重试 | 带走图片和视觉模型调用，不带走视频 |
| `mediaforge/orchestrator/batch_graph.py`、`nodes.py`、`tasks.py`、`dispatcher.py` | 不带走。任务、租户、整单重试留在 MediaForge |
| `mediaforge/rag/`、`mediaforge/db/`、`mediaforge/storage/` | 不带走 |

## 4. 有意改变的行为

这些差异要在拆包时按本文实现，不要为了“和现在一模一样”把任务系统带回来。

1. **按单张图重试，不整单重跑。** 现在只要有一张质量检查失败，就会清空全部结果并重跑该 SKU 的所有图。独立包只对失败的那一张再生成一次。已经通过的图保留。
2. **拼多多白底图请求必须包含拼多多平台。** 现在如果平台列表里没有拼多多，白底图 worker 会静默返回空结果。独立包在进入模型调用前直接拒绝这种请求。
3. **图片来源只有本地路径、HTTP(S) URL 和原始字节。** 不识别 MediaForge 的对象存储地址。URL 由本包自己下载。
4. **参考图的顺序有意义。** 全部参考图参与风格文字分析；只有第一张作为风格参考图发给图片模型。调用方自己决定哪张放第一。
5. **不落任务库。** 结果默认只在返回值里。调用方如果传入输出目录，本包才把 PNG 写到磁盘。
6. **传输失败和创作失败分开。** 对 429、502、503、504 以及连接和读写超时，同一次调用最多请求 3 次；第 1 次失败后等 2 秒，第 2 次失败后等 4 秒，第 3 次失败则该张图失败。这不算创作重试。创作重试只有质量检查失败后的那一次。
7. **不带熔断器。** 现有 OpenRouter 客户端有进程内熔断。独立包第一版只有上面的有限次请求，避免把 MediaForge 的熔断状态带进来。

## 5. 调用契约

### 5.1 请求

```text
CreationRequest
  product_image: ImageSource          # 必填，商品原图
  product_name: str                   # 必填
  category: str                       # 必填
  platforms: list[str]                # 至少一个，见下方平台表
  output_types: list[str]             # 至少一个：main_image、detail_page、pdd_white_background
  reference_images: list[ImageSource] # 可空
  style_hint: str | None
  presentation_mode: auto | model_wear | product_only    # 默认 auto
  model_preference: auto | female | male | no_face       # 默认 auto
  market: str | None                  # 缺省时按第一个平台推断
  aspect_ratio: auto | 1:1 | 3:4 | 4:5 | 9:16 | 16:9 | 4:1 | 1:4   # 默认 auto
  image_size: 1K | 2K | 4K            # 默认 2K
  image_model: pro | fast | base      # 默认 pro
  output_dir: path | None             # 缺省不写文件
  request_id: str | None              # 只用于日志和文件名，不是任务 ID
```

`ImageSource` 必须且只能提供三种来源之一：本地路径、`http`/`https` URL、原始字节。字节方式只给 Python API 使用，命令行只用路径。

平台白名单与现有规则表一致：`amazon`、`taobao`、`tmall`、`jd`、`pinduoduo`、`shopee`、`lazada`、`shein`、`temu`。大小写不敏感，内部转成小写。

市场缺省时按第一个平台推断：淘宝、天猫、京东、拼多多为 `CN`；Shopee、Lazada 为 `SG`；其余为 `US`。

校验失败时抛出请求错误，不调用任何模型：

- 商品名为空、类目为空、平台为空、输出类型为空或含未知值。
- 请求了 `pdd_white_background`，但平台列表没有 `pinduoduo`。
- 图片源缺失，或同一种图片同时给了多种来源。
- 参考图超过 4 张。风格分析把每张图都发给视觉模型，4 张是第一版上限。

### 5.2 返回

```text
CreationResult
  status: succeeded | partial | failed
  presentation_mode: model_wear | product_only
  style_prompt: str | None
  product_attributes: dict
  input_check: { matches, observed_product, reason }
  warnings: list[str]
  assets: list[Asset]
  detail_set_audits: list[DetailSetAudit]

Asset
  platform: str
  output_type: str
  variant: str | None          # 详情图为 scene、feature、closeup；其余为空
  status: succeeded | failed
  image: bytes | None
  file_path: str | None
  model: str                   # 实际模型名，不是 pro/fast/base
  prompt: str                  # 合规改写后的最终提示词
  generation_mode: standard | strict | staged
  quality: dict | None
  error: str | None
```

`succeeded` 表示每张请求的图都通过。`partial` 表示至少一张成功、至少一张失败。`failed` 表示没有成功图。原图核对失败时 `status=failed`，`assets` 为空，原因写在 `input_check`。

详情图的成组检查放在 `detail_set_audits`，不改变单张图的 `status`。三张都成功才做这项检查；它只报告这三张是否彼此不同、是否分别完成场景、卖点和特写。

### 5.3 Python 入口

```python
async def create_images(
    request: CreationRequest,
    config: AgentConfig | None = None,
) -> CreationResult:
    ...
```

`config` 缺省时从环境变量读取。测试传入替身客户端，不访问网络。

## 6. 处理顺序

一次 `create_images()` 按下面的顺序执行。准备阶段只做一次；出图阶段按「输出类型 × 平台 × 镜头」逐张进行，同一请求内串行，避免一次请求并行打满供应商。

1. 校验请求并加载图片。下载 URL，把路径和字节都规范成内存中的图片字节。内存中保留原图。发给模型时再缩小：视觉检查长边 768，图片生成的参考图长边 1024，风格分析长边 512。
2. 核对原图。视觉模型判断画面里的商品是否与名称、类目一致。名称和类目都是自动占位时，只检查画面里有没有清楚的可售商品，并写出观察到的商品。占位规则与现有实现相同：类目是空、`general`、`unknown`、`其他`，且名称是 `用户上传商品`、粘贴/上传自动名，或 `sku-...` 这种编号。
3. 原图不一致则停止，不生成任何图。
4. 决定展示方式。调用方指定了 `model_wear` 或 `product_only` 时照用。`auto` 时，模特偏好是 `female`、`male`、`no_face` 则用模特上身；否则看名称和类目是否命中服装词，命中则模特上身，否则纯商品。核对原图之后还有一条补救：原本判成纯商品，但观察到的商品属于上装、下装、连衣裙或外套时，改成模特上身。
5. 展示方式是模特上身时，从原图提取可见服装事实：件数、类型、颜色、材质、图案、领型、袖长、门襟、口袋、腰头、衣长、廓形、标识和显著细节。提取失败不中断，事实留空，后续提示词不写这段硬约束。
6. 有参考图时，分析它们共同的场景、光线、背景、机位、构图、配色、姿势和气氛，写成一段 60 到 100 词的英文风格描述。忽略参考图里的商品身份、标志、水印和文字。描述不完整或调用失败时，`style_prompt` 为空，出图继续。完整的标准是至少 20 个英文单词、结尾是句号、问号或感叹号，且不以冠词或介词结尾。风格模型先调用；不完整时再用质检模型试一次。两个模型配置成同一个时只调用一次。
7. 对每一张目标图：拼提示词、跑合规、调用图片模型、做像素检查和视觉质检、按需要导出。失败且属于质量问题则按第 10 节重试一次。
8. 某个平台的场景、卖点、特写都成功时，追加一次详情成组检查。
9. 如果给了 `output_dir`，把成功图片写成 PNG。返回汇总结果。

出图顺序固定为：主图、详情图、拼多多白底图。每个输出类型内部按请求里的平台顺序。详情图镜头顺序为场景、卖点、特写。

## 7. 一张图怎么生成

### 7.1 提示词

提示词由这些部分按顺序拼接，商品原图始终是商品身份的依据：

1. 声明要生成的是原图中的同一商品，写上名称和类目。禁止改品类、廓形、结构、颜色、材质、图案、比例和显著细节，禁止换成参考图里的商品。
2. 展示方式。纯商品：商品是唯一主体，不加人。模特上身：真人自然穿着原图中的每一件衣服；主图要求全身且商品完整可见；详情图按该镜头的构图。`no_face` 要求脸不可见但衣服完整。禁止把平铺图贴到身上，要求真实褶皱、垂坠、张力和遮挡。
3. 有服装事实时，把 JSON 作为硬约束附上。
4. 有 `style_hint` 时写入用户的场景和风格。亚马逊主图和拼多多白底图忽略场景化风格说明。
5. 平台名称。
6. 镜头说明。用户风格命中场景词，且不是亚马逊主图、不是白底图时，从镜头说明里去掉「干净背景、棚拍、虚化环境、电商精修感」这类限制背景的短语。
7. 有风格描述时，说明它只补充用户和平台没有规定的视觉细节。

镜头说明：

| 输出 | 镜头 | 意图 |
| --- | --- | --- |
| 主图 | hero | 商品完整、居中、清楚，灯光干净 |
| 详情图 | scene | 可信使用环境；模特上身时换姿势和机位，不能是白底目录图 |
| 详情图 | feature | 肩到髋或同等紧构图，突出原图能支持的版型、垂坠或结构，不写文字 |
| 详情图 | closeup | 原图可见的一个结构或材质细节，不能是全身图 |
| 拼多多白底图 | 无变体 | 正面、单件、纯白、居中、无模特、无阴影 |

场景词列表沿用现有 `SCENE_KEYWORDS`，包括中文的场景、外景、街头、咖啡馆等，以及英文的 lifestyle、outdoor、scene、street、garden、beach、forest、cafe、park。服装词列表沿用现有 `_APPAREL_TERMS` 和 `_CATEGORY_FAMILIES`。拆包时把这两份词表放进 `prompt.py`，不要再从 MediaForge 导入。

### 7.2 合规

合规在提示词拼完后执行，改写结果才是最终提示词。

- 提示词小写后含 `bomb`、`weapon`、`drug`、`counterfeit`、`fake`、`replica` 之一：这张图失败，错误为拦截，不调用图片模型。其他图继续。
- 市场用字替换：`CN` 和 `VN` 把提示词中的 `4` 换成 `6`；`PH` 把 `13` 换成 `12`。这是现有行为，第一版保持不变。
- 品牌词 `nike`、`adidas`、`gucci`、`chanel` 和 IP 词 `disney`、`marvel`、`pokemon` 不拦截，写入结果的 `warnings`。
- 追加平台生成指令。场景风格放宽主图规则，但亚马逊主图和拼多多白底图不放宽，并在警告里说明。
- 追加禁止复制参考图中的文字、水印、标志、价格标和图形覆盖。

平台指令和审核指令直接来自 `platform_rules.py` 的 `generation_instruction` 与 `audit_instruction`。比例来自 `aspect_ratio_for`：主图用该平台主图比例，详情图用详情比例，白底图固定 `1:1`。请求里的 `aspect_ratio` 不是 `auto` 时，主图和详情图改用请求值；白底图仍固定 `1:1`。

### 7.3 图片模型请求

别名映射到配置里的真实模型名。发给 OpenRouter 图片接口的内容：

- 模型名、一条提示词、`n=1`、宽高比。
- 原图作为第一张参考，说明它是商品身份依据。
- 第一张风格参考图作为下一张参考，说明只能借鉴构图、光线、背景、机位和气氛。
- 两张图都压到长边 1024 像素的 JPEG 再放进请求。
- 模型名是 `openai/gpt-image-2` 时，尺寸别名映射为质量：`1K=low`、`2K=medium`、`4K=high`。其他模型把 `1K`、`2K`、`4K` 作为分辨率传入。

响应先读 `data[].b64_json`，再读 Gemini 风格的 `choices[].message.images`，最后读 `message.content` 里的 data URL。都没有图片则这张图失败。

同一进程内，每个真实模型名一个信号量，默认并发 2。这只限制多个调用重叠时的供应商压力；一次 `create_images()` 内部仍然逐张生成。

### 7.4 质量检查

生成后先做确定性检查，再做视觉检查。两边都通过才算成功。

确定性检查：

- 长边下限：`1K` 为 768，`2K` 为 1536，`4K` 为 3000。`openai/gpt-image-2` 固定至少 768，因为它用质量档而不是分辨率档。
- 宽高比误差不超过 6%。
- 亚马逊主图和拼多多白底图检查边缘近白。把图缩到 256 以内，取约 8% 边带，近白像素（RGB 都不少于 250）占比必须达到 90%。

视觉检查把原图、风格参考图（如果有）和生成图一起交给质检模型，按 0 到 100 打分：

| 指标 | 通过线 | 何时生效 |
| --- | --- | --- |
| 商品一致性 | 85 | 始终 |
| 视觉质量 | 70 | 始终 |
| 平台合规 | 70 | 始终 |
| 服装融合 | 80 | 仅模特上身 |
| 模特偏好 | 80 | 模特偏好不是 auto |
| 输出意图 | 75 | 始终 |
| 确定性检查 | 全部通过 | 始终 |

商品一致性落在 80 到 84 时，再做一次只看商品是否相同的复核。复核认为是同一商品且分数不少于 85，则采用复核分数。忽略背景、光线、模特身份、姿势和穿着造成的正常形变。

纯商品时，服装融合记 100，不作为失败项。模特偏好为 auto 时，偏好分记 100。

失败原因使用中文，格式保持「未通过指标：商品一致性 82<85；…」，方便以后对照现有任务中心的文案。各项分数和确定性检查结果放进 `quality`。

拼多多白底图在检查前先导出：铺到白底、缩放到 480×480、存 PNG。像素检查和视觉检查都针对这张导出图。导出结果必须正好是 480×480，且小于 3MB。导出失败也算这张图失败。主图和详情图返回模型原始字节，不改画布尺寸，也不转码。保存文件时按文件头决定扩展名：PNG 为 `.png`，JPEG 为 `.jpg`，无法识别时为 `.img`。白底图始终是 `.png`。

### 7.5 详情成组检查

一个平台的三张详情图都成功后，对原图加三张生成图打两个分：差异度、角色覆盖。两项都不少于 75 才算成组通过。结果只进入 `detail_set_audits`，不触发重试，也不把已成功的单张改成失败。

## 8. 重试

每张图最多因为质量问题再生成一次。传输错误用完第 6 节的 HTTP 重试后，直接失败，不再进入创作重试。

选择重试方式：

- 商品一致性低于 85，或服装融合低于 80，或失败原因含「融合、穿着、人体、贴合、服装、上身」：使用 `staged`。
- 亚马逊主图和拼多多白底图即使命中上面的条件，也不做分步融合，改用 `strict`。这两种图不允许先生成场景再贴商品。
- 其余质量失败使用 `strict`。

`strict` 在原提示词后追加纠正句：优先保证商品身份、完整可见、平台背景和构图。详情图再按镜头追加一句：卖点图不能重复成普通正面目录图；场景图必须看见可信环境；特写必须是真实微距。

`staged` 分两步。第一步不传商品原图，只传风格参考图，生成场景和自然姿势，并要求商品区域留空。第二步把第一步的图片字节当作风格参考，连同商品原图和原提示词再生成。第二步的提示词追加融合要求：接触、褶皱、比例和遮挡要自然，同时保住原图细节。第一步失败则这张图失败，没有第二步。

重试仍使用请求里的同一个模型别名，不自动升级到 `pro`。

合规拦截和原图不一致不重试。

## 9. 配置

本包只读自己的环境变量，不读取 MediaForge 的 `Settings`。

| 变量 | 含义 | 缺省 |
| --- | --- | --- |
| `IMAGE_AGENT_OPENROUTER_API_KEY` | 必填，缺失则创建客户端时报错 | 无 |
| `IMAGE_AGENT_OPENROUTER_BASE_URL` | 接口根路径 | `https://openrouter.ai/api/v1` |
| `IMAGE_AGENT_MODEL_PRO` | `pro` | `google/gemini-3.1-flash-image` |
| `IMAGE_AGENT_MODEL_FAST` | `fast` | `openai/gpt-image-2` |
| `IMAGE_AGENT_MODEL_BASE` | `base` | `qwen/qwen-image-3-pro` |
| `IMAGE_AGENT_QUALITY_MODEL` | 原图核对、服装结构、视觉质检、成组检查 | `openai/gpt-4o-mini` |
| `IMAGE_AGENT_STYLE_MODEL` | 参考图风格描述 | 与质检模型相同 |
| `IMAGE_AGENT_HTTP_PROXY` | 可选代理 | 空 |
| `IMAGE_AGENT_REQUEST_TIMEOUT_SECONDS` | 单次 HTTP 超时 | 180 |
| `IMAGE_AGENT_MODEL_CONCURRENCY` | 每个图片模型的进程内并发 | 2 |

视觉调用走聊天补全接口，要求 JSON 对象，并用本包内的 Pydantic 模型校验。图片调用走 `/images`。两个接口都使用上面的密钥、根路径、代理、超时和同一套 HTTP 重试。

`.env.example` 放在包目录内。数值可以和 MediaForge 的图片模型配置相同，但变量名不同，避免误读项目根目录的 `.env`。

## 10. 包结构

建议放在仓库根目录的 `image_agent/`。它有自己的 `pyproject.toml`，依赖只保留 `httpx`、`pydantic` 和 `Pillow`。安装和测试都在这个目录里进行，不依赖仓库根的 MediaForge 环境。

```text
image_agent/
  pyproject.toml
  README.md
  .env.example
  src/image_agent/
    __init__.py        # 导出 create_images、CreationRequest、CreationResult
    __main__.py        # 命令行
    config.py
    models.py          # 请求、结果、质检 JSON 结构
    pipeline.py        # 第 6 节的顺序和重试
    prompt.py          # 展示方式、镜头、提示词拼接、词表
    platforms.py       # 自 platform_rules.py 迁入
    compliance.py
    vision.py          # 原图核对、服装结构、风格描述、质检、成组检查
    generate.py        # 图片请求、响应解析、分步融合的第一步
    quality.py         # 分数线、像素检查、白底导出
    images.py          # 路径、URL、字节的加载和缩放
  tests/
    test_prompt.py
    test_compliance.py
    test_quality.py
    test_pipeline.py
```

模块职责：

- `pipeline.py` 是唯一编排者。它调用其他模块，其他模块不互相调用编排。
- `vision.py` 和 `generate.py` 依赖 HTTP 客户端接口。测试用假客户端替换。
- `platforms.py` 不发起网络请求，方便单独核对文案。
- 命令行只做参数转换和把结果写到目录，创作规则不写在命令行里。

## 11. 命令行

```text
image-agent create \
  --product ./shirt.jpg \
  --name "亚麻衬衫" \
  --category "女装上衣" \
  --platform taobao \
  --platform tmall \
  --output main_image \
  --output detail_page \
  --reference ./ref-a.jpg \
  --reference ./ref-b.jpg \
  --style "江南巷弄，自然光" \
  --presentation auto \
  --model-preference female \
  --model pro \
  --size 2K \
  --aspect auto \
  --out ./out
```

`--platform` 和 `--output` 可重复。`--reference` 可重复，顺序就是第 4 节的参考图顺序。命令行退出码：全部成功为 0，部分成功为 2，全部失败或参数错误为 1。标准输出打印每张图的平台、输出类型、镜头、状态和文件路径；失败时打印错误。不打印完整提示词，避免终端被长文本淹没。提示词写在输出目录的 `result.json` 里。

## 12. 输出文件

传入输出目录时：

```text
{output_dir}/{request_id 或 create}/
  taobao/main_image.png
  taobao/detail_page/scene.jpg
  taobao/detail_page/feature.png
  taobao/detail_page/closeup.png
  pinduoduo/pdd_white_background.png
  result.json
```

主图和详情图的扩展名按第 7.4 节的文件头决定，上面的 `.jpg` 只是示例。白底图固定 `.png`。

`result.json` 含第 5.2 节的全部字段，但把图片字节换成相对路径。失败图没有文件。未传输出目录时不创建目录，图片只在返回值的 `image` 字段里。

## 13. 错误

| 情况 | 行为 |
| --- | --- |
| 请求不合法 | 抛出校验错误，无模型调用 |
| 缺少 API 密钥 | 抛出配置错误，无模型调用 |
| 图片打不开、URL 不是图片、下载失败 | 抛出输入错误，无生成 |
| 原图与商品信息不一致 | 返回 `failed`，不生成 |
| 合规拦截 | 该张 `failed`，错误写明拦截，其他张继续 |
| 供应商最终失败、响应无图片 | 该张 `failed`，错误带状态码或“响应中没有图片” |
| 质量检查未通过且重试仍失败 | 该张 `failed`，保留最后一次的质量 JSON |
| 风格分析或服装结构分析失败 | 记入 `warnings`，继续生成 |
| 一张请求中途有成功有失败 | 返回 `partial` |

不要把供应商响应里的整段正文写进异常。截断到 500 字符，且不记录图片 base64。

## 14. 测试

默认测试不访问网络，也不读取 MediaForge 配置。

- 提示词：纯商品、模特上身、`no_face`、场景词去掉背景限制、亚马逊场景不被放宽、服装事实被写入。
- 合规：危险词拦截、品牌只警告、平台指令被追加、白底图指令替换普通主图指令。
- 像素：比例误差、长边不足、白边不足、拼多多 480 导出。
- 流水线：用假视觉客户端和假图片客户端。覆盖原图不一致提前结束、单张质量失败只重试该张、`staged` 对亚马逊主图降级为 `strict`、详情三张都成功才出现成组检查、参考图第一张进入图片请求而其余只进入风格分析。
- 命令行：用临时目录和假的 `create_images`，检查退出码和 `result.json`。命令行测试不重跑创作规则。

真实 OpenRouter 调用不放进默认测试。以后若要手工验收，单独写一个需要密钥的脚本，不作为包的通过条件。

## 15. 拆包时按这个核对

实现时用下面的清单对照，避免把项目耦合带进去：

1. 全文搜索新包，确认没有 `mediaforge`、`celery`、`langgraph`、`minio`、`milvus`、`sqlalchemy`。
2. 确认没有商品检索、租户、任务 ID、配额和对象存储客户端。
3. 用同一张商品图和同一段风格说明，对比新包拼出的主图提示词与现有 `_build_prompt` 加 `ComplianceChecker` 的结果。平台指令、展示方式和水印禁令应一致。
4. 质量分数线与第 7.4 节表格一致。
5. 只让一张详情图质检失败，确认另外两张不会被重跑。
6. 在不设置 MediaForge 环境变量、只设置 `IMAGE_AGENT_` 变量时，命令行可以完成一次生成。

## 16. 以后可以加、这次不加

- 社媒图。先补齐平台比例和公开输出类型，再加进这个包。
- 调用方传入已写好的风格描述，从而跳过风格模型。
- 把详情成组检查失败变成重试条件。
- 让 MediaForge 改为调用这个包。那是另一次改造，前提是这个包已经能独立运行。
