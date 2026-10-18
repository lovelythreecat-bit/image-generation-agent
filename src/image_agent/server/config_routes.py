"""Authenticated CRUD routers mounted by the application at /api/v1."""

from typing import Annotated

from fastapi import APIRouter, Query, Request, Response

from .schemas import (
    AgentConfigCreate,
    AgentConfigPatch,
    AgentConfigRead,
    ErrorResponse,
    ModelConfigCreate,
    ModelConfigPatch,
    ModelConfigRead,
    ServiceCreate,
    ServicePatch,
    ServiceRead,
)

router = APIRouter(
    responses={
        status: {"model": ErrorResponse, "description": description}
        for status, description in (
            (404, "配置不存在"),
            (409, "陈旧 version 或被其他配置引用"),
            (422, "字段、协议或路由校验失败"),
            (503, "数据库不可用"),
        )
    }
)
Limit = Annotated[int, Query(ge=1, le=100, description="每页条数，默认 20，最大 100")]
Offset = Annotated[int, Query(ge=0, description="从零开始的分页偏移")]


@router.get(
    "/services",
    response_model=list[ServiceRead],
    tags=["服务配置"],
    operation_id="list_services",
    summary="分页查询服务配置",
    description="返回脱敏配置，仅用 has_api_key 标记凭据状态。",
)
async def list_services(request: Request, limit: Limit = 20, offset: Offset = 0):
    return await request.app.state.config_service.list_services(limit, offset)


@router.post(
    "/services",
    response_model=ServiceRead,
    status_code=201,
    tags=["服务配置"],
    operation_id="create_service",
    summary="新增服务配置",
    description="凭据仅写入并使用部署密钥加密；不接受 api_key_env。",
)
async def create_service(body: ServiceCreate, request: Request):
    return await request.app.state.config_service.create_service(body)


@router.get(
    "/services/{identifier}",
    response_model=ServiceRead,
    tags=["服务配置"],
    operation_id="get_service",
    summary="查询指定服务配置",
    description="按服务 UUID 查询；只返回凭据是否存在，不返回凭据内容。",
)
async def get_service(identifier: str, request: Request):
    return await request.app.state.config_service.get_service(identifier)


@router.patch(
    "/services/{identifier}",
    response_model=ServiceRead,
    tags=["服务配置"],
    operation_id="update_service",
    summary="更新服务配置",
    description="必须携带当前 version；api_key 省略保留、非空替换、null/空值拒绝；被引用服务不能停用。",
)
async def update_service(identifier: str, body: ServicePatch, request: Request):
    return await request.app.state.config_service.update_service(identifier, body)


@router.delete(
    "/services/{identifier}",
    status_code=204,
    tags=["服务配置"],
    operation_id="delete_service",
    summary="删除服务配置",
    description="被模型引用时返回 409。",
)
async def delete_service(identifier: str, request: Request):
    await request.app.state.config_service.delete_service(identifier)
    return Response(status_code=204)


@router.get(
    "/model-configs",
    response_model=list[ModelConfigRead],
    tags=["模型配置"],
    operation_id="list_model_configs",
    summary="分页查询模型配置",
    description="使用 limit/offset 查询模型及其服务关联，每页最多 100 条。",
)
async def list_model_configs(request: Request, limit: Limit = 20, offset: Offset = 0):
    return await request.app.state.config_service.list_model_configs(limit, offset)


@router.post(
    "/model-configs",
    response_model=ModelConfigRead,
    status_code=201,
    tags=["模型配置"],
    operation_id="create_model_config",
    summary="新增模型配置",
    description="服务必须存在且启用；模型引用的协议在 Agent 路由绑定时校验。",
)
async def create_model_config(body: ModelConfigCreate, request: Request):
    return await request.app.state.config_service.create_model_config(body)


@router.get(
    "/model-configs/{identifier}",
    response_model=ModelConfigRead,
    tags=["模型配置"],
    operation_id="get_model_config",
    summary="查询指定模型配置",
    description="按模型 UUID 查询模型名称、服务关联及当前版本。",
)
async def get_model_config(identifier: str, request: Request):
    return await request.app.state.config_service.get_model_config(identifier)


@router.patch(
    "/model-configs/{identifier}",
    response_model=ModelConfigRead,
    tags=["模型配置"],
    operation_id="update_model_config",
    summary="更新模型配置",
    description="version 防止覆盖并发修改；被 Agent 引用的模型不能停用，变更服务必须保持所有路由协议有效。",
)
async def update_model_config(identifier: str, body: ModelConfigPatch, request: Request):
    return await request.app.state.config_service.update_model_config(identifier, body)


@router.delete(
    "/model-configs/{identifier}",
    status_code=204,
    tags=["模型配置"],
    operation_id="delete_model_config",
    summary="删除模型配置",
    description="被 Agent 路由引用时返回 409。",
)
async def delete_model_config(identifier: str, request: Request):
    await request.app.state.config_service.delete_model_config(identifier)
    return Response(status_code=204)


@router.get(
    "/agent-configs",
    response_model=list[AgentConfigRead],
    tags=["Agent 配置"],
    operation_id="list_agent_configs",
    summary="分页查询 Agent 配置",
    description="使用 limit/offset 查询配置方案及图片、视觉路由，每页最多 100 条。",
)
async def list_agent_configs(request: Request, limit: Limit = 20, offset: Offset = 0):
    return await request.app.state.config_service.list_agent_configs(limit, offset)


@router.post(
    "/agent-configs",
    response_model=AgentConfigRead,
    status_code=201,
    tags=["Agent 配置"],
    operation_id="create_agent_config",
    summary="新增完整 Agent 配置",
    description="图片别名、视觉阶段和运行参数在同一事务中保存并通过核心 AgentConfig 校验；必须包含 vision。",
)
async def create_agent_config(body: AgentConfigCreate, request: Request):
    return await request.app.state.config_service.create_agent_config(body)


@router.get(
    "/agent-configs/{identifier}",
    response_model=AgentConfigRead,
    tags=["Agent 配置"],
    operation_id="get_agent_config",
    summary="查询指定 Agent 配置",
    description="按配置 UUID 查询运行参数及模型路由，不解析或返回供应商凭据。",
)
async def get_agent_config(identifier: str, request: Request):
    return await request.app.state.config_service.get_agent_config(identifier)


@router.patch(
    "/agent-configs/{identifier}",
    response_model=AgentConfigRead,
    tags=["Agent 配置"],
    operation_id="update_agent_config",
    summary="原子更新 Agent 配置",
    description="传入的路由字典整体替换；任何校验失败均回滚。更新只影响新任务，已提交任务使用加密快照。",
)
async def update_agent_config(identifier: str, body: AgentConfigPatch, request: Request):
    return await request.app.state.config_service.update_agent_config(identifier, body)


@router.delete(
    "/agent-configs/{identifier}",
    status_code=204,
    tags=["Agent 配置"],
    operation_id="delete_agent_config",
    summary="删除 Agent 配置",
    description="存在引用该配置的持久化任务时返回 409。",
)
async def delete_agent_config(identifier: str, request: Request):
    await request.app.state.config_service.delete_agent_config(identifier)
    return Response(status_code=204)
