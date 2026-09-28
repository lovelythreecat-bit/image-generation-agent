# image-agent

独立、无状态的商品图片创作 Python 包：多图发现 → 主体/要素选择 → 按资产编译证据 → 生图 → 多来源审核 → 至多一次质量修复。

Python ≥3.12，运行依赖只有 `httpx`、`pydantic`、`Pillow`。不需要数据库、队列、Web 服务或来源项目。真实模型效果尚未验收；当前验证范围是离线逻辑、协议、CLI 与安装隔离。

## 安装与配置

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install .
$env:IMAGE_AGENT_OPENROUTER_API_KEY = "your-key"
.venv\Scripts\image-agent.exe --help
```

不自动加载 `.env`，不读取通用 `HTTP_PROXY` 或来源项目配置。`.env.example` 仅供手动配置参考。真实客户端建立时才要求密钥，测试无需真实凭证。

| 环境变量（均以 `IMAGE_AGENT_` 开头） | 默认值 |
| --- | --- |
| `OPENROUTER_API_KEY` | 无，建立客户端时必填 |
| `OPENROUTER_BASE_URL` | `https://openrouter.ai/api/v1` |
| `MODEL_PRO` | `google/gemini-3.1-flash-image` |
| `MODEL_FAST` | `openai/gpt-image-2` |
| `MODEL_BASE` | `qwen/qwen-image-3-pro` |
| `QUALITY_MODEL` | `openai/gpt-4o-mini` |
| `STYLE_MODEL` | 跟随 QUALITY_MODEL |
| `HTTP_PROXY` | 无 |
| `REQUEST_TIMEOUT_SECONDS` | 180 |
| `MODEL_CONCURRENCY` | 2，每个真实图片模型进程内共享 |
| `GENERATION_REFERENCE_LIMIT` | 4 |
| `VISION_IMAGE_LIMIT` | 12 |

后两项是部署容量声明，不是供应商实测能力。同模型别名共用并发额度；重叠请求若声明不同额度，明确报配置错误。请求内部始终串行。

## Python 自动多图

```python
import asyncio
from image_agent import CreationRequest, ImageSource, MaterialInput, create_images

request = CreationRequest(
    product_name="陶瓷杯", category="厨房用品",
    materials=[
        MaterialInput(material_id="front", source=ImageSource(path="front.jpg")),
        MaterialInput(material_id="back", source=ImageSource(path="back.jpg")),
        MaterialInput(material_id="lid", source=ImageSource(path="lid.jpg")),
    ],
    creative_brief="杯盖放在杯子旁，不扣上",
    platforms=["taobao"], output_types=["main_image"],
)
result = asyncio.run(create_images(request))
for asset in result.assets:
    if asset.image is not None:
        print(asset.asset_id, len(asset.image))
```

API 接受本地路径、HTTP(S) URL、原始 `bytes`，每个 `ImageSource` 恰好一种来源。默认不落盘。调用方可传 `AgentConfig(...)`，无需依赖环境变量。

新输入支持 1–8 张，`role_hint` 默认 `auto`，可指定 identity/detail/accessory/scene/style。同一商品三个视角应归并为一个主体，保留不同事实来源；不同商品不会默认全部入图。明确双商品组合、配件摆放与否定要求由一次视觉发现产生结构化意图。

旧 `product_image=ImageSource(...)` 加最多 4 张 `reference_images` 仍可用，与 `materials` 互斥。旧参考图全部参与风格分析，普通资产只使用第一张风格图；白底和 Amazon 主图不使用风格参考。

## 分析、选择与澄清恢复

```python
from image_agent import analyze_materials, SelectionSpec

async def select_then_create(request):
    analysis = await analyze_materials(request)
    if analysis.status == "failed":
        return analysis
    # 展示 analysis.subjects / elements / issues，让调用方选择真实 ID。
    if analysis.status == "needs_input":
        choices = [o for i in analysis.issues for o in i.options if o.selection]
        if not choices:
            return analysis  # reanalyze：先补充素材或修改 brief
        request.selection = choices[0].selection  # 实际产品中由用户选择
    else:
        proposal = analysis.intent.proposal
        request.selection = SelectionSpec(
            mode="explicit",
            **proposal.model_dump(exclude={"primary_subject_id"}),
        )
    return await create_images(request, analysis=analysis)
```

精确选择必须携带同一分析快照，不能拿新发现的 ID 猜测对应。`Issue.options[].selection` 是完整可提交的 explicit 选择；不需要解析中文标签。无法拆开的混合身份必须重新分析，不能删掉冲突事实蒙混过关。

快照 fingerprint 覆盖字节、顺序、素材角色/提示、商品信息、brief 和规则版本 `multi-image-r2`。改 selection 或目标平台不使快照过期，换素材/brief 会抛 `StaleAnalysisError`。hash 不是信任证明：回传快照始终重新核对真实素材、事实与用户意图，旧 `needs_input` 可以通过合法选择和复核恢复。

一个 Fact 内的 evidence 是 OR（替代视角任选其一）；多个必需 Fact 是 AND（全部覆盖）。代表图先占槽，再补齐事实来源；例如正面商标和背面拉链不能互相替代。审核会保留其他相关视角，不只看生图使用的那几张。

`required_element_ids` 和 brief 中必需要求对**每张请求资产**生效。镜头只能豁免默认身份可见范围，不能豁免用户 required。必需场景与白底冲突、杯盖与不兼容微距冲突时，只失败对应资产；可选场景在白底中排除并记原因。纯氛围没有视觉事实时保留为约束，不伪造证据。

## 平台、输出与审核

支持 amazon、taobao、tmall、jd、pinduoduo、shopee、lazada、shein、temu。平台大小写与首尾空格会规范化，重复项按首次出现去重。市场按首平台推断：国内平台 CN，Shopee/Lazada SG，其余 US。

输出顺序固定：`main_image` → `detail_page`（scene、feature、closeup）→ `pdd_white_background`。每类按输入平台顺序。白底必须选择 pinduoduo，且只生成一个资产；固定纯商品、无模特偏好，导出精确 480×480 PNG、<3 MiB 后再审核。主图与详情保留供应商原始字节，按文件头写 `.png`、`.jpg`，其他可解码格式 `.img`。

每资产参考顺序：主身份代表图 → 其他可见主体代表图 → 必需事实来源 → 可选细节/配件 → 场景 → 风格。去重且不截断必需证据，容量不足返回 capability。可选要素仅能文字传递时记录 `text_only_element_ids`。审核图像集合加生成图超过视觉容量也明确失败。

逐主体审核要求 `same_product=true` 且一致性 ≥85，**95 分但 false 仍失败**；80–84 最多一次聚焦复核。必需细节/配件保真 ≥85，视觉/平台 ≥70，适用的服装融合/模特偏好 ≥80，镜头意图 ≥75。审核模型不能将 must_show 改成 not_applicable。三张详情全部成功后做成组审核，差异和覆盖 ≥75，仅报告。

通用长边下限：1K=768、2K=1536、4K=3000；gpt-image-2 的 `image_size` 表示 low/medium/high 质量档，长边统一至少768。比例相对误差 ≤6%；Amazon 主图和白底要求边带近白比例 ≥90%（RGB 每通道 ≥250）。显式比例覆盖主图/详情默认值，白底仍为1:1。平台文本是规则快照，**不是上架合规保证**。

两层重试互相独立：HTTP 仅对 429/502/503/504、连接错误、连接/读/写超时共尝试3次，等待2/4秒；可解码图的质量失败只对该资产修复一次，strict或staged，不升级模型。staged包含场景、融合两次生图；预留场景槽后容量不足则先改strict。Amazon主图/白底只strict。坏JSON/schema/base64、坏图、审核服务失败、合规拦截不触发创作重试。

为了兼容来源规则，CN/VN自然语言提示词中的4改6，PH中的13改12；可能影响商品数字或结构描述。素材/要素ID绑定不改写。危险词在追加平台模板前检查，品牌/IP只告警。

## CLI

```powershell
image-agent create --material front.jpg --material back.jpg `
  --name "陶瓷杯" --category "厨房用品" --brief "保持正面图案和背面结构" `
  --platform taobao --output main_image --out out --request-id run-001

image-agent analyze --material front.jpg --name "陶瓷杯" --category "厨房用品" `
  --platform taobao --output main_image --out analysis

image-agent create --product shirt.jpg --reference style.jpg `
  --name "亚麻衬衫" --category "女装上衣" --platform taobao --output detail_page `
  --presentation model_wear --model-preference no_face --model fast --size 2K --out out
```

CLI图片只接受本地路径。`--platform`、`--output`、`--reference`、`--material`可重复；支持`--market`、`--aspect`、`--style`、`--analysis analysis/analysis.json`。退出码：0全部成功，2部分成功，1失败/参数或输出错误，3待澄清，130取消。终端不打印长提示词，完整追踪保留在JSON。

## 第三方 JSON 契约

```python
from image_agent import CreationRequestDTO, ImageSource, request_from_dto, result_to_bundle

dto = CreationRequestDTO(
    product_name="陶瓷杯", category="厨房用品",
    platforms=["taobao"], output_types=["main_image"],
    materials=[{"material_id":"front", "media_id":"upload-123"}],
)
request = request_from_dto(dto, {"upload-123": ImageSource(data=uploaded_bytes)})
# result = await create_images(request)
# bundle = result_to_bundle(result)
# bundle.dto.model_dump(mode="json") 是纯JSON；bundle.blobs[asset_id] 是图片字节。
```

`schema/*.json`提供请求、分析、结果和错误v1契约。未知字段/版本拒绝。DTO没有任意服务器路径、output_dir或内嵌base64；缺media_id绑定直接拒绝。上传、鉴权、存储与下载URL由调用方负责，示例见`examples/integration_adapter.py`。

CLI也支持`--request-json request.json --media-map media.json --media-root media --out out`，其中media.json为`{"upload-123":"front.jpg"}`。映射只允许根目录内相对路径；与普通输入/创作参数互斥。请求JSON可携带explicit selection，再用`--analysis`提供原快照。

错误统一为`ErrorInfo(code, kind, message, retryable, status_code)`，异常用`error_to_dto`转换，结果已有错误原样保留，不解析中文字符串。准备服务失败是结果级错误；partial或逐图全失败在对应资产记录；needs_input只用issues。`retryable`供调用方决定是否重提，不自动重跑整单。

## 输出、取消与调用量

指定output_dir时，模型调用前独占预留`output_dir/(request_id或create)`。已存在即拒绝，Windows保留设备名、路径分隔符、尾随点/空格等无效。成功图片和result.json原子写入，失败资产无图片文件。单图片写盘失败仍保留内存成功结果，file_path为空，记录output_errors；清单失败抛携带result的OutputError。落盘JSON中的image/file_path是相对文件路径，与外部blob DTO是两种格式。

生成期间取消不导出已完成部分，保留预留目录，下次使用新ID。纯计算线程完成清理后再传播取消；保存开始后整个保存过程受保护，重复取消也会等待保存结束，调用结束后没有后台写盘。磁盘可能已有结果，但被取消的调用不会返回成功；强制终止/断电不在此保证内。

非服装、无风格、淘宝主图+三详情且无需修复：自动模式10次逻辑调用（发现1+生图4+单图审核4+成组1）；先分析再生成11次（增加快照复核）。额外服装补充、风格回退、聚焦复核及HTTP重试另计。9平台37资产最坏109次生图逻辑调用，视觉调用另计。复杂组合、微小标志、密集文字和模型自评都有误差，真实验收须人工看图。

## 开发与验收

```powershell
python -m pip install -e ".[dev]"
python -m pytest -q
python -m ruff check src tests scripts examples
python -m ruff format --check src tests scripts examples
python -m build --no-isolation
python scripts/live_smoke.py --help
```

默认pytest拦截真实网络，不读取来源项目。真实出图必须显式`--live`，默认一平台一主图，另行记录图片、评分和人工观察。协议资料核对见`docs/provider-protocol.md`；来源许可、hash和行为变化见`docs/migration.md`。
