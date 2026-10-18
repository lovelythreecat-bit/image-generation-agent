# HTTP 服务接入

服务复用现有 Python API，首期支持 `analyze_materials` 和 `create_images`（包括商品 `long_image`）。配置、媒体和任务接口统一位于 `/api/v1`，全部要求 `Authorization: Bearer <Token>`。通用图文长图继续使用原 Python/CLI 入口。

## 安装与启动

Windows 已完成依赖安装、`server.config.json` 配置及数据库迁移后，双击仓库根目录的 `start-webapi.cmd` 即可启动服务。脚本自动切换到项目目录并使用 `.venv`，启动失败时保留窗口显示错误，按 `Ctrl+C` 停止服务。

以下安装与迁移命令在仓库根目录执行；仅安装分发包时，还需获取同版本的 `server-alembic.ini` 与 `migrations/server` 管理文件。

Python ≥3.12；MySQL 5.7.17+ / 8.0，InnoDB、utf8mb4。当前开发验收目标包含本机 MySQL 5.7.17；专用数据库的实际验收结果见执行记录。生产环境需自行准备专用数据库及最小权限账号。

```powershell
# 仓库开发安装
uv sync --extra server --extra dev
# 或已发布的包
pip install "image-agent[server]"

# 仅首次复制，随后编辑文件中的数据库连接、访问 Token 和加密密钥
Copy-Item server.config.example.json server.config.json
# 仅首次生成，将输出填入配置文件 secret_key；重启必须使用同一密钥
uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# 明确执行迁移；导入和服务启动都不会自动建表或迁移
uv run python -m alembic -c server-alembic.ini upgrade head
uv run python -m image_agent.server
# 可选：显式指定对外监听地址与端口
uv run python -m image_agent.server serve --host 0.0.0.0 --port 8000
```

默认读取当前工作目录的 `server.config.json`，不读取服务环境变量或 `.env`。配置文件必须是 JSON 对象，必填 `database_url`、`token`、`secret_key`、`data_dir`，未知字段、重复字段、错误类型和无效密钥会明确拒绝。MySQL 数据库须先手动创建，并向连接账号授予所需权限；迁移只创建表。URL 中用户名、密码的特殊字符需进行 URL 编码。

```json
{
  "database_url": "mysql+asyncmy://app_user:replace-with-db-password@127.0.0.1:3306/image_agent?charset=utf8mb4",
  "token": "replace-with-long-random-token",
  "secret_key": "replace-with-generated-fernet-key",
  "data_dir": "./server-data",
  "host": "127.0.0.1",
  "port": 8000
}
```

`data_dir` 的相对路径以配置文件所在目录为基准；Windows 路径建议使用 `/`。默认并发 `worker_concurrency=1`、队列容量 `queue_capacity=100`、监听地址 `127.0.0.1`、端口 `8000`；图片默认最大 20 MiB、解码最多 6000 万像素，可通过 `max_upload_bytes` 和 `max_image_pixels` 调整。完整可选字段见仓库根目录的 `server.config.example.json`。

指定另一份部署配置时，启动、导入和迁移显式使用同一文件；启动参数 `--host/--port` 可覆盖文件值：

```powershell
uv run python -m alembic -c server-alembic.ini -x config=F:/deploy/server.config.json upgrade head
uv run python -m image_agent.server --config F:/deploy/server.config.json
uv run python -m image_agent.server import-config --config F:/deploy/server.config.json --file examples/config.openai.json --name "导入方案"
```

真实 `server.config.json` 与默认数据目录 `server-data/` 已加入 `.gitignore`。配置文件包含数据库密码、访问 Token 和加密密钥，应保存在受控位置；使用自定义文件名时自行将其排除在版本控制之外。模板中的占位符必须替换为实际值。以下 `uv run` 命令用于仓库开发；若使用 pip 安装并已激活虚拟环境，可省略 `uv run`。

仅部署一个服务进程；不要使用多个 Uvicorn workers 或多个实例共享同一目录。工作器有界并发，不引入外部队列。保留整个 `DATA_DIR`，包括原图、输出、任务记录、SQLite 检查点和预算台账；MySQL、数据目录及稳定 Fernet 密钥应配套备份。API Key 和任务配置快照仅加密保存，查询接口只返回 `has_api_key`。数据库不可用时服务不读取旧环境配置作为替代，`/health/ready` 返回 503；`/health/live` 仅表明进程存活。若启动阶段数据库或存储不可用，业务接口持续返回 503，修复资源后重启服务以启动工作器。

## 在线文档与代理前缀

启动后访问 [Swagger UI](http://127.0.0.1:8000/docs)、[ReDoc](http://127.0.0.1:8000/redoc)、[OpenAPI JSON](http://127.0.0.1:8000/openapi.json)。页面和 Schema 公开浏览。Swagger UI 点击 **Authorize**，仅填写 Token 本身，即可在 **Try it out** 中调用业务接口；没有 Token 的业务调用仍返回 401。OpenAPI 从实际路由生成，包含中文接口说明、字段约束、状态码与 Bearer scheme。

反向代理部署到 `/image-agent` 时在配置文件设置 `"root_path": "/image-agent"`，代理剥离该前缀转发请求。文档 URL 为 `/image-agent/docs`、`/image-agent/redoc` 和 `/image-agent/openapi.json`；Schema 的相对 `servers` 地址让在线调试使用相同前缀。代理需保留 Authorization、Idempotency-Key 和上传请求体，并设置适当上传限制。

## 上传、配置、提交和下载完整示例

以下 PowerShell 示例使用占位凭据与模型名称；先选择实际供应商兼容的协议、模型与端点。执行图片任务会调用该供应商并可能产生费用。

```powershell
$base = "http://127.0.0.1:8000/api/v1"
$deployment = Get-Content ./server.config.json -Raw -Encoding UTF8 | ConvertFrom-Json
$headers = @{ Authorization = "Bearer $($deployment.token)" }
function Post-Json($path, $body) {
    Invoke-RestMethod -Uri "$base/$path" -Method Post -Headers $headers `
        -ContentType "application/json; charset=utf-8" -Body ($body | ConvertTo-Json -Depth 30)
}

# curl.exe 支持 multipart；将实际商品图片替换到 file 参数
$media = curl.exe -sS "$base/media" -H "Authorization: Bearer $($deployment.token)" `
    -F "file=@cup.png" | ConvertFrom-Json

$imageService = Post-Json "services" @{
    name="图片服务"; base_url="https://api.example.com/v1"; protocol="openai_images"
    api_key="replace-with-provider-key"; generation_path="/images/generations"; edit_path="/images/edits"
}
$visionService = Post-Json "services" @{
    name="视觉服务"; base_url="https://api.example.com/v1"; protocol="chat_completions"
    api_key="replace-with-provider-key"; chat_path="/chat/completions"
}
$imageModel = Post-Json "model-configs" @{ service_id=$imageService.id; model="gpt-image-2" }
$visionModel = Post-Json "model-configs" @{ service_id=$visionService.id; model="gpt-4o-mini" }
$agent = Post-Json "agent-configs" @{
    name="商品图片方案"; parameters=@{request_timeout_seconds=180; model_concurrency=2; model_completion_attempts=4}
    image_models=@{pro=$imageModel.id; fast=$imageModel.id; base=$imageModel.id}
    stages=@{vision=$visionModel.id; analyze_materials=$visionModel.id; audit_image=$visionModel.id}
}
$request = @{
    schema_version="1.0"; product_name="蓝色陶瓷杯"; category="杯具"
    platforms=@("taobao"); output_types=@("main_image"); image_model="fast"
    materials=@(@{material_id="cup"; media_id=$media.media_id; role_hint="identity"})
    creative_brief="突出真实杯身外观"; style_hint="浅色背景"
}
$taskHeaders = @{ Authorization=$headers.Authorization; "Idempotency-Key"=[guid]::NewGuid().ToString() }
$task = Invoke-RestMethod "$base/tasks" -Method Post -Headers $taskHeaders `
    -ContentType "application/json; charset=utf-8" `
    -Body (@{kind="create_images"; agent_config_id=$agent.id; request=$request} | ConvertTo-Json -Depth 30)
do {
    Start-Sleep -Seconds 2
    $state = Invoke-RestMethod "$base/tasks/$($task.task_id)" -Headers $headers
} while ($state.status -in @("queued", "running"))
$state # 查看 core_status 和 error；completed 不代表所有审核通过
if ($state.status -eq "completed") {
    $result = Invoke-RestMethod "$base/tasks/$($task.task_id)/result" -Headers $headers
    $result.assets | Where-Object blob_id | ForEach-Object {
        Invoke-WebRequest "$base/tasks/$($task.task_id)/blobs/$($_.blob_id)" `
            -Headers $headers -OutFile "$($_.asset_id).png"
    }
}
```

上传只接受可解码图片，不信任原文件名；返回 `media_id`、MIME、字节数和 SHA256。外部请求只能引用已有 media_id，不接受本地路径、远程 URL、output_dir、可信分析声明或预算覆盖。创建服务、模型、Agent 返回 201；提交任务返回 202；下载实际导出 blob 返回对应图片 MIME（例如 image/png）。

将上述提交中的 `kind` 改为 `analyze_materials` 可先分析。分析结果由 `/tasks/{id}/result` 返回；后续生成提交可携带顶层 `analysis_task_id`，并用该分析返回的实际 ID 设置 `request.selection`。素材及分析快照需匹配，客户端不能自行声明可信分析。商品长图在 `request.output_types` 加入 `long_image`，并在 `request.long_image` 设置核心 DTO 的宽度、区段数、比例、已知信息与必需文案；完整字段见在线 Schema。

## 更新、恢复与错误语义

三个配置资源都支持 GET 列表、POST、GET/PATCH/DELETE 单项。列表使用 `limit=20&offset=0`，limit 最大 100。每次 PATCH 必须携带 GET 返回的当前 version；陈旧 version 返回 409。关联中的服务或模型不能删除或停用；停用 Agent 后禁止新任务。

```powershell
# 不传 api_key 保留凭据；非空 api_key 替换；null、空串或全空白拒绝
Invoke-RestMethod "$base/services/$($imageService.id)" -Method Patch -Headers $headers `
    -ContentType "application/json" -Body (@{version=$imageService.version; api_key="replace-with-new-key"} | ConvertTo-Json)
# interrupted 生成任务显式恢复，继续使用原快照与原预算记录
Post-Json "tasks/$($task.task_id)/resume" @{}
# 使用真实返回的 asset_id/candidate_id，原因不能只含空白
Post-Json "tasks/$($task.task_id)/accept" @{asset_id="actual-asset-id";candidate_id="actual-candidate-id";reason="已人工核对商品与文案"}
```

分析与显式选择请使用本服务结果返回的 ID，不依赖供应商的原始 ID 或自行拼装。服务可能规范化含敏感信息的供应商标识，并保持各字段及映射的关联引用一致；客户端的素材标识和服务端图片下载标识仍由对应输入与任务绑定。

`Idempotency-Key` 必填；同 key、同规范化请求返回原 task_id，同 key、不同请求返回 409。重试应保留原 key。配置在提交事务中形成加密快照及版本记录，后续配置修改仅影响新任务。调度状态为 queued/running/completed/failed/interrupted；core_status 保留 needs_input、quality_failed、budget_exhausted、generation_uncertain 等实际业务状态。未完成任务的 result 返回 409。

启动时 queued 任务可继续，遗留 running 任务标记 interrupted；不会自动重提可能已收费的生图。resume 复用原核心恢复语义，已保存候选不重复生图，不确定的提交保持 generation_uncertain；运行中重复恢复或并发接受返回冲突。接受不修改生成事实，仅记录人工接受原因。

所有错误以 `code/message/request_id` 返回，响应头含 X-Request-ID，校验错误不回显输入。常见状态码：401 鉴权失败；404 不存在；409 版本、引用、幂等或任务状态冲突；413 上传字节超限；422 配置或图片校验失败；429 队列满；503 数据库或资源不可用。错误和任务摘要不暴露原始密钥、SQL 参数或服务器路径。

## 旧配置显式导入

```powershell
uv run python -m image_agent.server import-config --file examples/config.openai.json --name "导入方案"
```

只读取明确传入的文件，不扫描用户目录。结构化 services/image_models/stages 原样规范化；legacy provider 配置转换成图片服务、视觉服务与相应路由。文件中的 api_key_env 仅在本次明确导入时由部署环境解析，然后加密持久化；缺少凭据或校验失败时原子失败，不留下半套配置。成功仅输出 agent_config_id，失败不打印原始文件、密钥或数据库连接。HTTP 配置不开放 api_key_env；核心 CLI 和 Python API 的旧配置加载行为保持原样。


## 2026-10-18 白底图与商品长图交付

`pdd_white_background` 保留协议枚举名称以兼容历史任务，界面显示“白底图”。支持全部已支持平台，并按请求平台创建目标，不隐式改为拼多多。

商品 `long_image` 结果的 AssetDTO 增加可空 `long_image_review`，绑定最终候选 ID、实际成图 SHA-256、宽高及按拼接顺序排列的分段记录（section_id、status、quality、copy_audit、y/width/height）。仅投影与最终成图摘要一致的候选；不暴露本地路径、素材引用或原始 metadata。`quality=null` 不代表审核缺失，长图使用分段实物与文字审核，不伪造统一 QualityReport；调用方核对完整数量、审核及最终字节。历史无证据长图不被自动认定通过。通用图文长图 CLI/Python 入口不变。
