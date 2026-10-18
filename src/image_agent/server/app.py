"""HTTP application wiring; resource I/O begins only within lifespan."""
from contextlib import asynccontextmanager
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from starlette.exceptions import HTTPException

from image_agent.errors import AgentError

from .auth import require_token
from .settings import ServerError, ServerSettings


class ErrorResponse(BaseModel):
    code: str = Field(description="稳定的机器可读错误码", examples=["validation"])
    message: str = Field(description="不含凭据与本地路径的错误摘要", examples=["请求参数不符合要求"])
    request_id: str = Field(description="本次请求追踪标识")


class HealthResponse(BaseModel):
    status: str = Field(description="最小健康状态：ok 或 unavailable")


def create_app(settings: ServerSettings) -> FastAPI:
    from .config_routes import router as config_router
    from .task_routes import router as task_router

    @asynccontextmanager
    async def lifespan(app):
        from .config_service import ConfigService
        from .database import Database
        from .media import MediaStore
        from .secrets import SecretCipher
        from .task_service import TaskService
        from .worker import Worker

        cipher = SecretCipher(settings.secret_key)
        database = Database(settings.database_url)
        app.state.database = database
        media = MediaStore(settings.data_dir, max_upload_bytes=settings.max_upload_bytes,
                           max_image_pixels=settings.max_image_pixels)
        app.state.media_store = media
        config = ConfigService(database.session_factory, cipher)
        app.state.config_service = config
        service = TaskService(database.session_factory, config, media, cipher, settings)
        app.state.task_service = service
        worker = Worker(service, concurrency=settings.worker_concurrency)
        app.state.worker = worker
        try:
            try:
                media.initialize()
                await database.initialize()
                await worker.start()
                app.state.runtime_ready = True
                app.state.initialization_failed = False
            except (SQLAlchemyError, OSError, ServerError):
                app.state.runtime_ready = False
                app.state.initialization_failed = True
            yield
        finally:
            app.state.runtime_ready = False
            await worker.stop()
            await database.close()

    app = FastAPI(
        title="image-agent HTTP 服务", version="0.2.0",
        description=("商品图片生成与素材分析的异步接口，以及持久化服务、模型和 Agent 配置管理。"
                     "业务接口要求 Bearer Token；任务提交要求 Idempotency-Key。"
                     "已接收任务使用加密配置快照，调度完成状态与核心业务状态分别返回。"),
        root_path=settings.root_path, lifespan=lifespan,
        openapi_tags=[{"name": name, "description": description} for name, description in (
            ("服务配置", "服务地址、协议和只写加密凭据管理"),
            ("模型配置", "模型与供应商服务关联管理"),
            ("Agent 配置", "图片别名、视觉阶段路由和运行参数管理"),
            ("素材", "受控图片上传；默认最大 20 MiB、6000 万像素"),
            ("任务", "异步执行、结果交付、显式恢复和人工接受"),
            ("健康检查", "公开最小存活与数据库、存储就绪状态"),
        )],
    )
    app.state.settings = settings
    app.state.runtime_ready = False
    app.state.initialization_failed = False
    app.state.database = None
    app.state.media_store = None

    @app.middleware("http")
    async def request_id(request, call_next):
        request.state.request_id = str(uuid4())
        try:
            response = await call_next(request)
        except Exception:
            # Do not let Uvicorn log provider/SQL exception payloads or credentials.
            response = error_response(request, 500, "internal_error", "服务暂时无法处理请求")
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    def error_response(request, status, code, message):
        headers = {"WWW-Authenticate": "Bearer"} if status == 401 else None
        return JSONResponse(status_code=status, headers=headers, content={
            "code": code, "message": message,
            "request_id": getattr(request.state, "request_id", str(uuid4())),
        })

    @app.exception_handler(ServerError)
    async def server_error(request: Request, exc: ServerError):
        return error_response(request, exc.status_code, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error_response(request, 422, "validation", "请求参数不符合要求")

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request: Request, exc: SQLAlchemyError):
        return error_response(request, 503, "database_unavailable", "数据库暂时不可用")

    @app.exception_handler(AgentError)
    async def agent_error(request: Request, exc: AgentError):
        return error_response(request, 422, exc.code, "Agent 请求无法处理")

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException):
        return error_response(request, exc.status_code, "http_error", "HTTP 请求无法处理")

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        return error_response(request, 500, "internal_error", "服务暂时无法处理请求")

    async def require_resources(request: Request):
        if request.app.state.database is None or request.app.state.initialization_failed:
            raise ServerError(503, "service_unavailable", "服务资源尚未就绪")

    errors = {status: {"model": ErrorResponse, "description": description} for status, description in (
        (401, "缺少或无效 Token"), (404, "资源不存在"), (409, "版本、引用或任务状态冲突"),
        (422, "请求校验失败；不回显敏感输入"), (429, "队列已满"), (503, "数据库或服务资源不可用"),
    )}
    for router in (config_router, task_router):
        app.include_router(router, prefix="/api/v1", responses=errors,
                           dependencies=[Depends(require_token), Depends(require_resources)])

    @app.get("/health/live", tags=["健康检查"], response_model=HealthResponse,
             operation_id="health_live", summary="检查服务存活", description="公开接口；仅返回最小存活状态。")
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["健康检查"], response_model=HealthResponse,
             responses={503: {"model": HealthResponse, "description": "数据库或存储不可用"}},
             operation_id="health_ready", summary="检查服务就绪", description="公开接口；检查数据库及受控磁盘存储，不暴露连接信息。")
    async def ready():
        database, media = app.state.database, app.state.media_store
        healthy = False
        if app.state.runtime_ready and database is not None and media is not None:
            try:
                healthy = await database.ready() and media.ready()
            except Exception:
                healthy = False
        if not healthy:
            return JSONResponse(status_code=503, content={"status": "unavailable"})
        return {"status": "ok"}

    return app
