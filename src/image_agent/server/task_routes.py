"""Authenticated media, asynchronous tasks, result delivery, and explicit recovery."""

from typing import Annotated

from fastapi import APIRouter, File, Header, Request, Response, UploadFile

from image_agent.contracts import CreationResultDTO
from image_agent.models import MaterialAnalysis

from .settings import ServerError
from .task_service import AcceptRequest, MediaReference, TaskReference, TaskSubmission

router = APIRouter()
ERRORS = {
    409: {"description": "任务状态、幂等请求或素材绑定冲突"},
    422: {"description": "输入校验失败"},
    429: {"description": "任务队列已满"},
    503: {"description": "数据库或媒体存储不可用"},
}


@router.post(
    "/media",
    status_code=201,
    response_model=MediaReference,
    tags=["素材"],
    operation_id="upload_media",
    summary="上传图片素材",
    description="multipart 上传真实图片，默认最大 20 MiB、6000 万像素；文件名不参与存储路径。",
    responses={413: {"description": "图片字节或像素超限"}, **ERRORS},
)
async def upload_media(request: Request, file: Annotated[UploadFile, File(description="图片文件")]):
    store = request.app.state.media_store
    parts, total = [], 0
    try:
        while chunk := await file.read(1024 * 1024):
            total += len(chunk)
            if total > store.max_upload_bytes:
                raise ServerError(413, "upload_too_large", "Image exceeds the upload limit")
            parts.append(chunk)
        return await request.app.state.task_service.upload(b"".join(parts))
    finally:
        await file.close()


@router.post(
    "/tasks",
    status_code=202,
    response_model=TaskReference,
    tags=["任务"],
    operation_id="submit_task",
    summary="提交异步 Agent 任务",
    description="使用指定 Agent 配置快照入队。相同 Idempotency-Key 与相同规范化请求返回原任务；"
    "更改输入则返回 409。仅接受 media_id，显式选择必须携带服务生成的 analysis_task_id。",
    responses=ERRORS,
)
async def submit_task(
    request: Request,
    body: TaskSubmission,
    idempotency_key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)],
):
    return await request.app.state.task_service.submit_task(
        **body.model_dump(exclude={"request"}),
        request=body.request,
        idempotency_key=idempotency_key,
    )


@router.get(
    "/tasks/{id}",
    response_model=TaskReference,
    tags=["任务"],
    operation_id="get_task",
    summary="查询任务状态",
    responses=ERRORS,
    description="调度状态为 queued/running/completed/failed/interrupted；core_status 独立保留核心质量和预算状态。",
)
async def get_task(id: str, request: Request):
    return await request.app.state.task_service.get_task(id)


@router.get(
    "/tasks/{id}/result",
    response_model=CreationResultDTO | MaterialAnalysis,
    tags=["任务"],
    operation_id="get_task_result",
    summary="获取任务结果",
    responses=ERRORS,
    description="完成后返回素材分析或 CreationResultDTO；未完成返回 409。completed 不表示图片审核通过。",
)
async def get_result(id: str, request: Request):
    return await request.app.state.task_service.get_result(id)


@router.get(
    "/tasks/{id}/blobs/{blob_id}",
    tags=["任务"],
    operation_id="download_task_blob",
    summary="下载任务结果图片",
    response_class=Response,
    description="只可下载该任务通过 result_to_bundle 实际交付的 blob，blob_id 是不透明标识。",
    responses={
        200: {
            "description": "结果图片字节",
            "content": {
                "image/png": {"schema": {"type": "string", "format": "binary"}},
                "image/jpeg": {"schema": {"type": "string", "format": "binary"}},
                "image/webp": {"schema": {"type": "string", "format": "binary"}},
            },
        },
        **ERRORS,
    },
)
async def download_blob(id: str, blob_id: str, request: Request):
    data, mime = await request.app.state.task_service.blob(id, blob_id)
    return Response(data, media_type=mime, headers={"X-Content-Type-Options": "nosniff"})


@router.post(
    "/tasks/{id}/resume",
    status_code=202,
    response_model=TaskReference,
    tags=["任务"],
    operation_id="resume_task",
    summary="显式恢复生成任务",
    responses=ERRORS,
    description="复用原配置快照、候选记录、预算台账和 generation_uncertain 保护；运行中重复恢复返回 409。",
)
async def resume_task(id: str, request: Request):
    return await request.app.state.task_service.recover(id)


@router.post(
    "/tasks/{id}/accept",
    status_code=202,
    response_model=TaskReference,
    tags=["任务"],
    operation_id="accept_task_candidate",
    summary="人工接受已有候选图片",
    responses=ERRORS,
    description="提供 asset_id、candidate_id 和非空 reason，保留原质量结果和人工接受记录。",
)
async def accept_task(id: str, body: AcceptRequest, request: Request):
    return await request.app.state.task_service.recover(id, "accept", body.model_dump())
