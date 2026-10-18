# Agent HTTP 服务与 MySQL 配置管理开发计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. 2026-10-14 用户已明确要求按本计划多 Agent 推进；使用 task-orchestrator 记录并行任务，共享文件保持单一写入者。

**Goal:** 将现有 image-agent 作为 HTTP 服务对外提供，并通过 MySQL 和 HTTP 管理模型及 Agent 配置。

**Architecture:** 新增独立 server 服务层，负责 HTTP、鉴权、MySQL、媒体存储和任务调度。服务层将数据库配置转换为现有 AgentConfig，复用现有 Python API 和 DTO；核心图片流程继续使用现有任务目录、SQLite 检查点与预算台账。

**Tech Stack:** Python >=3.12；现有 Pydantic 2、httpx、LangGraph；服务层计划采用 FastAPI、Uvicorn、SQLAlchemy 2 asyncio、asyncmy、Alembic、cryptography、python-multipart；MySQL 5.7.17+ / 8.0、InnoDB、utf8mb4（用户本机为 5.7.17，实施需兼容该版本）。

**Spec:** 本文“已确认需求与边界”和“接口及行为约定”是本计划随附的需求说明；项目上下文参考 ../specs/2026-09-29-configurable-api-design.md。本文是后续本功能开发的主执行文档。

**Status:** 已完成开发及验收；三个实现 Agent、独立安全评审和主调度集成验收已收尾，账本状态 complete。记录日期：2026-10-14（Asia/Shanghai）。

## 已确认需求与边界

用户已确认的需求：

1. Agent 提供可供外部系统调用的 HTTP 接口。
2. 模型配置等 Agent 配置持久化到 MySQL。
3. HTTP 接口同时覆盖配置的查询、新增、修改、删除和使用。
4. 提供 Swagger UI、ReDoc、OpenAPI JSON 和带 Token 的在线接口调试。

本计划的实施约定：

- 保留 CLI、桌面和 Python API 的现有配置加载方式；HTTP 服务使用数据库配置作为执行配置来源。
- 管理完整的服务配置、模型配置、图片模型别名、视觉/规划/审核阶段路由及 Agent 运行参数，不能把现有多模型结构简化成单一默认模型。
- 图片任务使用异步提交、状态查询和结果下载，避免生成耗时依赖 HTTP 长连接。
- MySQL 保存配置、媒体元数据和 HTTP 任务元数据；图片字节及核心运行记录保存于服务端受控目录。
- 首期采用单服务进程和受限并发工作器；不引入 Redis、Celery、对象存储、多租户或分布式调度。
- 商品图片生成（包括商品 long_image）、素材分析为首期执行范围；通用图文长图 API 暂继续保留 Python/CLI 入口，不在本期增加全部长图 HTTP 操作。
- 不新增业务前端；本期交付 HTTP 服务、Swagger UI / ReDoc 在线接口文档和 Markdown 接入说明。

## Global Constraints

- 核心包 import image_agent 不连接数据库、不启动服务、不产生网络或文件写入。
- 服务依赖放入 pyproject.toml 的可选 server extra，核心 __init__.py 不导入 server。
- AgentConfig.from_mapping 是数据库配置进入核心的统一校验边界；结构化配置不得混入 legacy provider 字段。
- 图片协议沿用 openrouter_images / openai_images；视觉协议沿用 chat_completions。
- 使用 ORM/参数化查询；配置修改和关联校验在事务中完成。
- HTTP 响应、校验错误、日志、OpenAPI 示例和本地核心配置快照不得包含原始 API Key。
- 服务层使用显式持有的配置；不得在数据库不可用时静默改用旧环境变量配置。
- 保留原有 schema_version、DTO、预算约束、人工接受记录和 generation_uncertain 行为。
- 只修改本功能相关内容，不覆盖用户其他改动；不自动提交、部署或执行付费模型调用。
- 每完成一个任务更新本文复选框、验证记录和下一步；测试未执行或失败时不得标记完成。

## 已核实的项目上下文

- pyproject.toml：Python 包与 CLI，当前未声明 HTTP 框架或 MySQL 依赖。
- src/image_agent/config.py：ServiceConfig、ModelRoute、AgentConfig；支持 services、image_models 和 stages。
- src/image_agent/pipeline.py：analyze_materials、create_images、resume_images、accept_candidate。
- src/image_agent/contracts.py：CreationRequestDTO、request_from_dto、result_to_bundle、ErrorDTO。
- src/image_agent/run_store.py、execution.py、graph_runtime.py：现有持久化、预算与恢复机制。
- tests/test_isolation.py：核心依赖隔离和导入无副作用检查；不能为了新增服务删除这些检查。
- 原设计允许数据库适配器提供 mapping，核心不直接依赖数据库；本次采用该扩展点。
- 仓库根目录及已检查的父目录未发现 AGENTS.md；新增文件所在目录的规则在实施前再检查。

## 接口及行为约定

### 配置管理

统一前缀 /api/v1，所有配置和任务/媒体接口使用 Bearer Token 鉴权。服务启动凭据与数据库连接通过环境变量提供，不写入代码。

| 资源 | 接口 | 行为 |
| --- | --- | --- |
| 服务配置 | GET/POST /services；GET/PATCH/DELETE /services/{id} | 管理 base_url、协议、端点、加密凭据及启用状态 |
| 模型配置 | GET/POST /model-configs；GET/PATCH/DELETE /model-configs/{id} | 管理模型名称、关联服务、描述和启用状态 |
| Agent 配置 | GET/POST /agent-configs；GET/PATCH/DELETE /agent-configs/{id} | 管理配置方案、运行参数及 image_models/stages 路由 |

- 所有 ID 由服务生成 UUID；列表使用 limit/offset，limit 默认 20、最大 100。
- 新建返回 201，删除成功返回 204；未找到 404，校验失败 422，引用/版本冲突 409，数据库不可用 503。
- 每条配置有 version；PATCH 必须携带当前 version，陈旧版本返回 409。
- API Key 创建时为写入字段；PATCH 不传表示保留，非空值表示替换，null 或空字符串拒绝；查询只返回 has_api_key，不返回明文或密文。
- API Key 用 Fernet 加密，密钥来自 IMAGE_AGENT_SERVER_SECRET_KEY；禁止自动生成临时密钥，避免服务重启后无法解密。
- HTTP 配置首期使用持久化凭据，不开放 api_key_env；旧文件导入时由部署端显式解析环境引用后加密保存。
- 模型只能引用存在且协议匹配的服务；完整 Agent 配置必须通过 AgentConfig 校验。
- Agent 配置通过 image_models 的 pro/fast/base 和 stages 的现有阶段名引用 model_config_id，stages.vision 必须存在。
- 不额外建立全局默认模型；提交任务必须指定 agent_config_id，图片请求仍通过原 DTO 的 image_model 选择配置中的别名。
- AgentConfig 中的超时、模型并发、参考图限制、视觉图片限制和补齐次数均可持久化配置，复用现有默认值和校验规则。
- 被模型引用的服务、被 Agent 路由引用的模型不能删除或停用；删除时返回 409。停用 Agent 配置后禁止提交新任务。
- 所有配置更新只影响新提交任务；已接收任务在提交事务中保存加密配置快照及引用版本，排队和执行期间均不重新解析路由。

### Agent 调用与交付

| 接口 | 行为 |
| --- | --- |
| POST /api/v1/media | multipart 上传图片，返回 media_id；默认最大 20 MiB，并限制解码像素数不超过 6000 万 |
| POST /api/v1/tasks | 提交 kind=analyze_materials 或 create_images、agent_config_id、CreationRequestDTO；返回 202 和 task_id |
| GET /api/v1/tasks/{id} | 查询调度状态、核心业务状态、进度、错误摘要和所用配置版本 |
| GET /api/v1/tasks/{id}/result | 返回分析结果或 CreationResultDTO；任务未完成时返回 409 |
| GET /api/v1/tasks/{id}/blobs/{blob_id} | 下载 result_to_bundle 实际导出的结果图片 |
| POST /api/v1/tasks/{id}/resume | 显式恢复已有生成任务，复用原配置快照和核心恢复语义；运行中重复恢复返回 409 |
| POST /api/v1/tasks/{id}/accept | 提交 asset_id、candidate_id、非空 reason，复用 accept_candidate |
| GET /health/live；GET /health/ready | 存活检查、数据库及存储就绪检查；仅返回最小状态，无连接信息 |

- 外部输入只使用 media_id，不接受服务器文件路径、output_dir 或任意远程图片 URL；服务通过 request_from_dto 绑定真实素材。
- POST /tasks 必须携带 Idempotency-Key。相同 key 和相同规范化请求返回原 task_id；相同 key 不同请求返回 409。
- 任务输入、媒体归属/存在性和配置有效性在入队前校验；密钥不进入幂等哈希或公开任务字段。
- 分析结果复用通过服务生成的 analysis_task_id 绑定原素材和分析快照；禁止信任客户端声明的分析可信度或任意本地记录路径。
- 调度状态固定为 queued/running/completed/failed/interrupted；核心业务状态单独返回，不将 completed 等同于图片质量通过。
- 单进程启动时可继续 queued 任务；之前的 running 标记 interrupted，只有显式 resume 可恢复。禁止以自动重试重新发起可能已收费的生图。
- 服务使用数据库短事务领取任务，不在调用模型期间持有事务；设置队列容量和工作并发，满载返回 429。
- 核心输出目录由任务 ID 派生并验证路径边界；下载仅访问属于该任务的 blob，不泄露本地路径。
- 所有错误返回统一 code/message/request_id 格式；沿用核心错误分类，校验错误不回显敏感 input。

### 在线接口文档

在线文档是本期交付要求，随服务启动提供，不以 Markdown 文件替代。

| 地址 | 用途 |
| --- | --- |
| GET /docs | Swagger UI，浏览接口并通过 Try it out 在线调试 |
| GET /redoc | ReDoc，阅读接口及请求/响应 Schema |
| GET /openapi.json | OpenAPI JSON，供客户端生成、接口工具导入及文档渲染 |

- 文档和 OpenAPI 由实际 FastAPI 路由及 Pydantic DTO 自动生成；不得另维护一份脱离实现的接口定义。
- 按服务配置、模型配置、Agent 配置、素材、任务、健康检查分组；每个接口有中文说明、唯一 operation_id、请求参数和响应定义。
- 字段包含必填性、类型、默认值、枚举、范围与示例；说明分页、version、Idempotency-Key、任务状态和错误码。
- OpenAPI 定义 HTTP Bearer security scheme；Swagger UI 的 Authorize 输入 Token 后，调试受保护接口自动携带 Authorization 请求头。
- 页面及 Schema 可直接浏览；实际配置、媒体和任务调用仍使用鉴权，Authorize 不绕过业务接口校验。
- 明确 multipart 图片上传、202 异步提交、409 冲突、429 满载、503 不可用及二进制结果下载的内容类型和示例。
- API Key 标记为只写敏感字段；响应 Schema 不包含 API Key/密文，示例只使用占位值，Schema 不读取真实数据库配置。
- 文档使用当前服务的相对地址；说明反向代理的 root_path 配置，确保部署在路径前缀下时，页面、Schema 和在线调试仍可用。
- 验收包括 HTTP 获取、OpenAPI 契约检查以及浏览器实际打开页面和完成一次带 Token 的模拟调用；只验证 HTML 返回 200 不足以确认文档可用。

## 数据库与文件结构

MySQL 表：

- provider_services：服务地址、协议、端点、api_key_encrypted、enabled、version、时间戳。
- model_configs：service_id 外键、模型名称、描述、enabled、version、时间戳。
- agent_configs：配置名称、运行参数 JSON、enabled、version、时间戳。
- agent_model_routes：agent_config_id/model_config_id 外键、route_kind、route_name；同 Agent 的路由名唯一。
- http_media：media_id、受控存储引用、MIME、字节数、内容哈希、时间戳。
- http_tasks：任务种类、规范化输入、幂等 key/哈希、配置版本及加密快照、调度状态、核心状态、结果/运行目录引用、时间戳。

任务和媒体元数据采用 MySQL；不把本期扩展为核心 SQLite 状态迁移。配置关系由外键和事务共同保护。迁移中明确唯一索引、删除限制和 utf8mb4；数据库迁移只能显式执行，不在 import 或每次请求时执行。

新增模块均位于 src/image_agent/server/：

| 文件 | 单一职责 |
| --- | --- |
| __init__.py、__main__.py | 无副作用包入口与服务启动 |
| settings.py、auth.py | 服务环境配置、鉴权 |
| database.py、db_models.py | 数据库生命周期、表映射 |
| schemas.py | HTTP 请求/响应 DTO |
| secrets.py | 凭据及任务快照加解密 |
| config_repository.py、config_service.py | 配置事务、校验和 AgentConfig 组装 |
| config_routes.py | 配置 CRUD HTTP 路由 |
| media.py | 受控媒体和结果 blob 存储 |
| task_repository.py、task_service.py | 任务持久化、幂等及状态变更 |
| worker.py | 限流调度、现有 Agent API 调用与恢复 |
| task_routes.py、app.py | 任务 HTTP 路由、应用生命周期、健康检查、错误映射及在线文档配置 |

其他文件：新增 server-alembic.ini、migrations/server/、docs/http-api.md；修改 pyproject.toml、uv.lock、.env.example、README.md。仅在服务适配确有需要时修改核心文件，并先补相应兼容测试。

## Review Focus

1. 修改或停用配置后，已入队任务必须继续使用原配置快照（任务 2、4）。
2. PATCH 密钥缺省、替换、非法空值及校验错误均不得泄露密钥（任务 2、3）。
3. 并发重复提交或进程中断不能造成重复付费生图（任务 4、5）。
4. 媒体 ID、blob ID、文件名和图片内容不能导致越界读取或超限解码（任务 4）。
5. HTTP 完成状态不能抹去 needs_input、quality_failed、budget_exhausted、generation_uncertain 等核心业务状态（任务 5）。

## 执行任务

每个实现任务使用先写有意义的失败测试、确认失败原因、实现、验证的顺序。完成后在“执行记录”记下改动文件、实际运行命令及结果。以下路径和模块是计划新增文件，不是已经存在的代码。

### 任务 1：服务启动与核心隔离

文件：server/__init__.py、__main__.py、settings.py、auth.py、app.py；pyproject.toml、uv.lock、.env.example；tests/test_server_bootstrap.py、tests/test_server_docs.py、tests/test_isolation.py。

接口：create_app(settings) 返回 FastAPI；ServerSettings 读取显式服务环境配置；require_token 验证 Bearer Token。应用通过生命周期初始化资源，不在 import 时产生 I/O。

- [x] 测试无 Token/错误 Token 返回 401、有效 Token 通过、启动缺少必要参数明确失败、import image_agent 不需要服务依赖且无副作用。
- [x] 为服务创建 server extra 和入口 python -m image_agent.server，默认绑定 127.0.0.1；对外绑定由部署参数显式设置。
- [x] 声明 IMAGE_AGENT_SERVER_DATABASE_URL、TOKEN、SECRET_KEY、DATA_DIR、WORKER_CONCURRENCY、QUEUE_CAPACITY；并发默认 1、队列容量默认 100。
- [x] 建立统一错误映射和最小健康检查。
- [x] 启用 /docs、/redoc、/openapi.json；配置服务名称、版本、中文简介、接口分组和 Bearer security scheme；测试页面地址、Schema 地址和鉴权声明。
- [x] 验证：.venv\Scripts\python.exe -m pytest tests/test_server_bootstrap.py tests/test_server_docs.py tests/test_isolation.py -q。

### 任务 2：MySQL、加密及配置解析

文件：database.py、db_models.py、secrets.py、config_repository.py、config_service.py；server-alembic.ini、migrations/server/；tests/test_server_config_service.py、tests/test_server_mysql.py。

接口：resolve_agent_config(agent_config_id) 返回通过校验的 AgentConfig 和引用版本；配置快照包含完整解析配置，其密钥只在加密载荷中保存。

- [x] 测试 services/image_models/stages 往返一致、协议不匹配和缺 vision 被拒绝、旧环境变量不混入、密钥加解密及错误密钥失败。
- [x] 创建六张表及初始化迁移；实现异步连接生命周期和事务边界。
- [x] 配置读写完成后调用 AgentConfig.from_mapping；配置错误不得含原始 key 或连接密码。
- [x] 验证 MySQL 外键、路由唯一性、版本冲突及配置快照一致性；集成测试必须使用专用测试数据库，不运行在生产数据库。
- [x] 验证：.venv\Scripts\python.exe -m pytest tests/test_server_config_service.py tests/test_server_mysql.py -q；未提供测试数据库时 MySQL 测试明确 skip，并记录为未验收。

### 任务 3：配置管理 HTTP 接口

文件：schemas.py、config_routes.py、config_service.py；tests/test_server_config_api.py。

接口：按“配置管理”表实现 services、model-configs、agent-configs 三类 CRUD，统一分页、version 和错误格式。

- [x] 测试 CRUD、分页、启停、引用冲突、陈旧 version、未知字段和 key PATCH 三种语义。
- [x] 实现完整 Agent 路由原子更新，失败不得留下部分配置。
- [x] 查询接口仅返回 has_api_key；请求体/校验错误/日志中的 key 全部脱敏。
- [x] 为配置接口声明实际响应模型、中文字段说明、请求示例和主要错误响应；OpenAPI 区分密钥写入字段与脱敏读取字段。
- [x] 验证：.venv\Scripts\python.exe -m pytest tests/test_server_config_api.py tests/test_service_config.py -q。

### 任务 4：素材、任务入队和幂等

文件：media.py、task_repository.py、task_service.py、task_routes.py；tests/test_server_media.py、tests/test_server_tasks.py。

接口：submit_task(kind, agent_config_id, request, idempotency_key) 返回任务引用；resolve_media(media_ids) 返回供 request_from_dto 使用的 ImageSource 映射。

- [x] 测试上传有效图片、20 MiB 边界/超限、解码超限、伪造图片、缺失媒体、路径越界及未鉴权访问。
- [x] 测试重复 key 同请求只产生一个任务，不同请求返回 409；并发提交也由数据库唯一约束保护。
- [x] 在接收任务时固化配置快照、请求和媒体绑定；更改模型配置后 queued 任务保持旧配置。
- [x] 实现 202 入队、429 队列满载、任务查询和 blob 下载边界；拒绝客户端路径和运行配置覆盖字段。
- [x] 验证：.venv\Scripts\python.exe -m pytest tests/test_server_media.py tests/test_server_tasks.py tests/test_contracts.py -q。

### 任务 5：执行、结果交付与恢复

文件：worker.py、task_service.py、task_routes.py；tests/test_server_worker.py、tests/test_server_recovery.py。

接口：worker 调用 analyze_materials/create_images；结果通过 result_to_bundle 导出；恢复和接受分别复用 resume_images/accept_candidate。

- [x] 用现有 fakes/httpx.MockTransport 验证任务使用 MySQL 解析配置、媒体正确绑定、结果 DTO 与 blob 一致。
- [x] 验证 analyze_materials 和 create_images 两种操作；包括商品 long_image，analysis_task_id 的素材/快照绑定和不可信分析拒绝。
- [x] 工作器领取任务后释放数据库事务，限制并发；结果业务状态原样保存。
- [x] 测试 provider 超时、配置解密失败、数据库失联、结果保存失败都返回真实错误；请求断开不丢失已接收任务。
- [x] 测试重启时 running 变 interrupted、queued 可继续，显式 resume 不重复提交已保存或不确定的生图；并发 resume/accept 不重复执行。
- [x] 验证：.venv\Scripts\python.exe -m pytest tests/test_server_worker.py tests/test_server_recovery.py tests/test_public_api.py tests/test_graph_recovery.py tests/test_product_long_image_recovery.py -q。

### 任务 6：配置导入、在线接口文档与运行说明

文件：server/__main__.py、app.py、schemas.py、config_routes.py、task_routes.py、docs/http-api.md、README.md、.env.example；tests/test_server_config_import.py、tests/test_server_docs.py。

接口：python -m image_agent.server import-config --file <配置文件> --name <方案名>，显式导入旧配置；不自动扫描或读取用户私有文件。

- [x] 测试结构化配置导入，legacy 配置规范化，环境凭据缺失时原子失败，敏感信息不进入输出。
- [x] 文档列出安装 server extra、设置环境变量、显式执行迁移、启动服务与磁盘数据保留要求。
- [x] 提供上传素材→创建服务/模型/Agent 配置→提交任务→轮询→下载结果的完整调用示例。
- [x] 说明如何恢复/接受、API Key 更新语义、单进程部署限制、配置快照和业务状态区别。
- [x] 为全部接口补齐在线文档的中文说明、字段约束、请求/响应示例、错误码及内容类型；README 和接入说明给出 /docs、/redoc、/openapi.json 地址。
- [x] 测试 OpenAPI 包含所有计划接口、Bearer security scheme、幂等请求头、图片上传和二进制下载定义，且不包含真实密钥；验证路径前缀部署下的文档和调试地址。
- [x] 验证：.venv\Scripts\python.exe -m pytest tests/test_server_config_import.py tests/test_server_docs.py -q；浏览器打开 /docs、/redoc，使用 Swagger UI 的 Authorize 和 Try it out 完成一次模拟接口调用。

### 任务 7：最终验收

- [x] 跑服务测试：.venv\Scripts\python.exe -m pytest tests -k server -q。
- [x] 跑完整离线回归：.venv\Scripts\python.exe -m pytest -q。
- [x] 跑静态检查：.venv\Scripts\python.exe -m ruff check src tests scripts。
- [x] 跑构建：.venv\Scripts\python.exe -m build，检查核心包可单独安装、server extra 可启动。
- [x] 使用真实专用 MySQL 完成迁移、CRUD、外键/并发、服务重启后配置与任务持久性验证；SQLite/mock 不能替代这一项。
- [x] 使用模拟模型完成完整 HTTP 调用闭环；真实供应商验证仅在用户显式授权且配置就绪后执行，并单列费用/结果记录。
- [x] 检查原 CLI、桌面、Python API 和 DTO 的兼容性，并逐项核对下方验收标准。
- [x] 验证在线文档页面实际渲染、接口完整、字段清晰，Authorize 后可以调试；未授权的业务调用仍返回 401。

## 验收标准

- [x] 外部调用方能够上传真实素材，通过 HTTP 提交 Agent 任务并下载结果。
- [x] 所有服务、模型、路由和 Agent 运行配置存入 MySQL，重启后仍可用。
- [x] HTTP 可完成模型配置全生命周期管理，并切换 Agent 各阶段使用的模型。
- [x] 新任务读取更新配置，已接收任务使用提交时快照。
- [x] 密钥仅加密保存，公开字段、错误和日志不泄露凭据。
- [x] 重复请求、服务中断和恢复不绕过原有预算或重复发起不确定的付费调用。
- [x] 核心业务状态及人工接受信息保持真实，失败不伪装成功。
- [x] 迁移、启动、接口和恢复文档可供外部调用方按步骤使用。
- [x] /docs 和 /redoc 可在线访问，/openapi.json 可导入接口工具；Swagger UI 支持带 Token 调试，并与实际接口保持一致。

## 执行记录与接续规则

| 日期 | 阶段 | 实际结果 | 下一步 |
| --- | --- | --- | --- |
| 2026-10-14 | 制定并保存计划 | 已阅读项目配置、公共 API、DTO、隔离测试和既有配置设计；仅新增本 Markdown，未实现服务、安装依赖或执行模型调用 | 开始开发时先复核工作区及目录规则，再执行任务 1 |
| 2026-10-14 | 启动多 Agent 实施 | 用户授权并行推进；创建 `.agent-runs/2026-10-14-http-api/` 账本及共享接口合同；三个实现 agent 分别负责配置/MySQL、媒体/任务/恢复、服务/文档/导入；server extra 已安装 | 先完成各范围测试，再独立集成验收 |
| 2026-10-14 | MySQL 兼容与测试环境 | 用户明确授权使用本机 MySQL 并创建专用库；已确认 5.7.17-log，创建 `image_agent_http_test_20261014_a139de`；仅调整支持版本和调度 SQL 兼容性，不更改核心语义 | 在该库执行迁移、CRUD、并发及持久性验证 |
| 2026-10-14 | 首轮集成验收与安全评审 | 三条实现范围已完成；当前服务 61 项测试通过（含真实 MySQL 3 项）；MySQL 双协议完整 HTTP 闭环 2 项通过；Edge 实际 Swagger Authorize/Try it out、ReDoc 及路径前缀验收通过；构建、静态检查通过。独立评审确认脱敏字典键遗漏和枚举误替换两项问题，已派发修复，尚不标记整体完成 | 修复后重验全部服务、构建及安装，并完成独立复审与完整回归 |
| 2026-10-14 | 补充在线接口文档要求 | 按用户新增要求，将 Swagger UI、ReDoc、OpenAPI JSON、Token 在线调试和文档验证纳入任务 1/3/6/7 及验收标准；仍未开始产品实现 | 后续按更新后的计划开发 |


| 2026-10-14 | 完整回归与兼容性 | `.venv/Scripts/python.exe -m pytest -q` 实际完成 1511 passed（1009.48 秒）；核心生产模块未改动。之后只修改新服务脱敏边界及其测试，并以全部当前服务测试重验；不将较早完整回归冒充最终新增用例的全量运行 | 完成修复后服务重验与独立复审 |
| 2026-10-14 | 最终服务、MySQL、构建验收 | `.venv/Scripts/python.exe -m pytest tests -k server -q`：94 passed、1448 deselected；`IMAGE_AGENT_ACCEPTANCE_DATABASE_URL` 指向专用库运行 `tests/test_server_integration.py -q`：2 passed，覆盖 openrouter_images/openai_images、分析复用、幂等、blob 下载和重启查询；`ruff check src tests scripts`、`python -m build` 通过。原数据库凭据未写入交付文件 | 无待验收的数据库项 |
| 2026-10-14 | 安装包与在线文档验收 | 核心独立安装无需 FastAPI/SQLAlchemy，模拟公共 API smoke 通过；最终 wheel 的 62 个 Python 模块与源码及安装结果逐字节一致，server extra 的实际 MySQL/存储/工作器启动通过。Edge 实际渲染 Swagger/ReDoc，在 `/image-agent` 前缀下 Authorize/Try it out 返回 200，未鉴权业务请求返回 401 | 可按 docs/http-api.md 安装和启动 |
| 2026-10-14 | 安全修复及独立复审完成 | 两轮修复覆盖供应商字典键/值、模式合法的供应商 ID、引用/身份映射、伪造哈希、数值与错误字段；31 项安全回归纳入最终服务验收。最终独立复审重跑原 HTTP 复现并确认先前 P1/P2 已解决；客户端/服务端标识、真实预算、恢复和人工接受语义保留 | 本计划完成，账本及所有原始报告/证据保留 |

后续 Agent 必须先读本文及执行记录，复核 git status，再继续第一个未完成任务。只有对应验证实际通过才勾选；缺 MySQL、依赖或模型凭据时记录受影响的具体验收项，不宣布整个功能已完成。常规实现细节在本文范围内自主决定；变更接口、扩大范围或改变核心语义时先记录原因及影响，必要时向用户确认。最终交付记录实际检查结果与剩余限制。

最终证据：`.agent-runs/2026-10-14-http-api/evidence/root-final-validation-3.json`、`full-final-1.txt`、`browser-acceptance.json`、`wheel-latest-validation-3.json`；最终独立评审见同目录 `reports/REVIEW-e8652095-e2c8-462e-b034-b709cea1ea4e.md`，账本一致性见 `evidence/final-reconcile.json`。

交付范围为单服务进程。真实 MySQL 验收版本为用户本机 5.7.17-log；未另外运行 MySQL 8.0 实例。没有自动提交、部署或真实供应商付费调用。仅额外修正 `scripts/wheel_smoke.py` 的过期模拟分析夹具，使其包含当前核心要求的 creative_plan；核心产品行为未改动。
