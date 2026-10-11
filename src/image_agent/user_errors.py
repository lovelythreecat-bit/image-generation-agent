"""Chinese explanations over existing exceptions and saved task records."""

import re
from dataclasses import dataclass

from pydantic import ValidationError

from .errors import make_error_info
from .sanitization import redact

STAGES = {
    "analyze": "素材分析", "analyze_materials": "素材分析", "prepare": "素材准备",
    "plan_product_long_image": "长图策划", "plan_long_image": "长图策划",
    "long_plan": "长图策划", "plan": "长图策划", "generate": "生图",
    "long_generate": "长图生图", "create": "生成任务", "resume": "恢复任务",
    "audit_image": "图片审核", "audit": "图片审核", "long_audit": "长图审核",
    "audit_product_long_image_copy": "长图文字审核", "review_product": "商品外观审核",
    "long_compose": "长图拼接", "compose": "长图拼接", "render": "长图排版",
    "save_candidate": "图片保存", "long_save_candidate": "长图保存",
    "output": "结果保存", "preview": "图片预览", "load": "加载任务",
    "group_audit": "成组审核", "repair": "图片修复",
}


@dataclass(frozen=True)
class UserFacingError:
    summary: str
    reason: str
    action: str
    details: str

    @property
    def text(self):
        return f"{self.summary}：{self.reason}。{self.action}"


def _get(value, key, default=None):
    return value.get(key, default) if isinstance(value, dict) else getattr(value, key, default)


def _chinese_reason(message):
    if re.search(r"[\u4e00-\u9fff]", message) and not re.search(
        r"source_quote|fact_ids|fact_checks|base64|provider_|validation|schema", message
    ):
        return message.rstrip("。")
    return ""


def format_user_error(error, *, stage=None):
    """Accept ErrorInfo, exceptions, assets, result records or historical dictionaries.

    Saved records alone do not prove resumability. Never promise a resume or automatic
    resubmission here; the existing task recovery machinery makes that decision.
    """
    if isinstance(error, ValidationError):
        error = make_error_info(error)
    info = _get(error, "error_info") or error
    stage = stage or _get(info, "stage")
    status = _get(error, "status", "") or ""
    code = _get(info, "code") or _get(error, "stop_reason") or status or _get(error, "stage")
    kind = _get(info, "kind", "")
    message = _get(info, "message") or _get(error, "error") or ""
    if isinstance(error, BaseException) and not message:
        message = str(error)
    if isinstance(error, str):
        message = error
    message = redact(message)
    lower = message.lower()
    http_status = _get(info, "status_code")
    match = re.search(r"http\s+(\d{3})", lower)
    if http_status is None and match:
        http_status = int(match.group(1))
    # The deepest recorded stage is more precise than the desktop operation name.
    embedded_stage = next((name for name in STAGES if name in lower), None)
    label = (
        stage if stage and re.search(r"[\u4e00-\u9fff]", stage)
        else STAGES.get(_get(info, "stage") or embedded_stage or stage, None)
    )
    if not label:
        label = stage if stage and re.search(r"[\u4e00-\u9fff]", stage) else None
    if not label:
        label = "图片审核" if status in {"audit_error", "pending_audit", "quality_failed"} else (
            "结果保存" if kind == "output" else "生图" if _get(error, "asset_id") else "操作"
        )
    reason, action = "服务未返回具体原因", "输入与已有记录已保留，请查看技术详情定位问题。"
    if code == "generation_uncertain":
        reason = "请求可能已提交，生成结果尚未确认"
        action = "不要重复提交以免重复计费；先核对服务端记录和本地任务记录。"
    elif http_status in (401, 403):
        reason = "服务密钥无效、已过期或没有访问权限"
        action = "请在 API 配置中检查 API 地址、与该地址对应的密钥和模型访问权限。"
    elif http_status == 429:
        reason = "服务限流或额度暂时不足"
        action = "请检查服务额度，等待限流解除后再操作。"
    elif http_status in (400, 404, 405, 422) or kind == "capability":
        reason = "当前服务不支持请求的模型、参数或图片规格"
        action = "请检查 API 配置中的模型、接口类型、尺寸和参考图数量。"
    elif http_status and http_status >= 500:
        reason = "服务端暂时不可用"
        action = "请等待服务恢复；已有图片和任务记录已保留。"
    elif kind == "transport" or "timeout" in lower or "connection" in lower:
        reason = "网络连接失败或服务响应超时"
        action = "请检查网络和服务状态；提交状态未确认时不要重复生图。"
    elif code == "budget_exhausted":
        reason = "本次任务的模型调用预算已用完"
        action = "请核对已完成内容与调用记录，再决定是否增加预算。"
    elif kind == "stale_analysis" or code == "stale_analysis":
        reason = "素材分析与当前输入或分析策略不一致"
        action = "请重新分析当前素材；原始输入与历史图片已保留。"
    elif kind == "input":
        reason = "商品图片无法读取或图片格式无效"
        action = "请检查图片文件是否存在、能否打开及其格式。"
    elif kind == "configuration":
        reason = "模型服务配置不完整或不兼容"
        action = "请检查 API 地址、接口类型、模型和密钥配置。"
    elif kind == "output" or isinstance(error, OSError) and stage != "preview":
        reason = "结果文件无法保存或任务文件无法读取"
        action = "请检查文件是否存在、目录权限和磁盘空间；已有生成记录已保留。"
    elif any(word in lower for word in ("base64", "image response", "no inline image")):
        reason = "生图服务返回的图片数据为空、损坏或格式不受支持"
        action = "请检查生图服务的接口类型和图片响应格式；不要盲目重复提交。"
    elif code in ("provider_refusal", "vision_refusal"):
        reason = "模型服务拒绝处理本次请求"
        action = "请查看服务返回的技术详情，确认模型是否支持本次输入。"
    elif kind == "protocol" or any(word in lower for word in (
        "invalid json", "missing", "source_quote", "schema", "malformed"
    )):
        reason = "模型返回格式无法解析或缺少必要内容"
        action = "这是系统与模型返回的问题，无需补内部编号或来源字段；请保留记录排查。"
    elif kind in ("quality", "compliance") or status in ("quality_failed", "audit_error", "pending_audit"):
        quality = _get(error, "quality") or {}
        copy_audit = _get(error, "copy_audit") or {}
        issues = [_chinese_reason(redact(i)) for i in _get(copy_audit, "issues", [])]
        reason = "；".join(i for i in issues if i) or _chinese_reason(
            _get(quality, "reason", "") or message
        ) or (
            "审核尚未完成" if status == "pending_audit" else "商品外观或指定文字未通过审核，未返回具体差异"
        )
        action = "请核对已保存的候选图片与原图、指定文案；技术详情保留审核记录。"
    elif kind == "validation":
        reason = _chinese_reason(message) or "输入参数或任务数据不符合要求"
        action = "请检查表单参数；若是模型返回或历史任务数据问题，请保留记录排查。"
    elif stage == "preview":
        reason = _chinese_reason(message) or (
            "图片数据不完整或已损坏" if "truncated" in lower
            else "图片无法读取或预览解码失败"
        )
        action = "请检查已保存图片是否完整；生成记录仍保留在任务中。"
    elif message:
        reason = _chinese_reason(message) or "发生未分类异常，暂时无法确定具体原因"
    stop = _get(error, "stop_reason")
    if stop == "no_improvement":
        reason += "；修复后没有改善，已停止自动生成"
    elif stop in ("max_quality_repairs", "quality_repair_limit"):
        reason += "；已达到自动修复次数上限"
    return UserFacingError(f"{label}失败", reason, action, message)
