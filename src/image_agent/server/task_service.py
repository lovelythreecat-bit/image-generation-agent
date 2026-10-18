"""HTTP task adaptation with encrypted immutable configuration and trusted media binding."""

import asyncio
import hashlib
import hmac
import json
import uuid
from typing import Literal

from pydantic import Field, ValidationError, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from image_agent.config import LEGACY_FIELDS, AgentConfig
from image_agent.contracts import (
    CreationRequestDTO,
    CreationResultDTO,
    ResultBundle,
    request_from_dto,
    result_to_bundle,
)
from image_agent.errors import AgentError
from image_agent.models import (
    GarmentStructureAudit,
    ImageSource,
    LoadedMaterial,
    MaterialAnalysis,
    Model,
)

from .db_models import HttpMedia, HttpTask
from .settings import ServerError
from .task_repository import TaskRepository


class TaskSubmission(Model):
    kind: Literal["analyze_materials", "create_images"] = Field(
        description="素材分析或商品图片生成操作", examples=["create_images"]
    )
    agent_config_id: str = Field(
        min_length=1, max_length=36, description="启用的 Agent 配置 UUID；提交时固化配置快照"
    )
    request: CreationRequestDTO = Field(description="版本化创建请求；素材只能通过 media_id 绑定")
    analysis_task_id: str | None = Field(
        default=None,
        max_length=36,
        description="服务已完成的分析任务 UUID；必须与本请求原素材和分析输入一致",
    )

    @model_validator(mode="after")
    def analysis_selection(self):
        if self.kind == "analyze_materials" and (
            self.analysis_task_id or self.request.selection.mode != "auto"
        ):
            raise ValueError("analysis requires automatic selection without an analysis task")
        if self.request.selection.mode == "explicit" and not self.analysis_task_id:
            raise ValueError("explicit selection requires analysis_task_id")
        return self


class TaskReference(Model):
    task_id: str = Field(description="任务 UUID", examples=["2ef2ac81-79e0-4f10-a042-97e178970135"])
    status: Literal["queued", "running", "completed", "failed", "interrupted"] = Field(
        description="调度状态；completed 只表示执行已完成，质量请查看 core_status"
    )
    core_status: str | None = Field(
        default=None,
        description="核心业务状态，包括 needs_input、quality_failed、budget_exhausted、generation_uncertain 等",
    )
    config_versions: dict = Field(
        default_factory=dict, description="提交时 Agent、模型和服务配置版本"
    )
    error: dict | None = Field(default=None, description="安全的 code/message 错误摘要")
    progress: dict | None = Field(
        default=None, description="当前 stage、asset_id、attempt；不包含服务器路径"
    )


class MediaReference(Model):
    media_id: str = Field(description="上传素材 UUID")
    mime_type: str = Field(description="解码检测得到的图片 MIME", examples=["image/png"])
    byte_size: int = Field(description="上传文件字节数")
    sha256: str = Field(description="素材内容 SHA-256")


class AcceptRequest(Model):
    asset_id: str = Field(
        min_length=1,
        max_length=200,
        description="结果中的 asset_id",
        examples=["taobao.main_image.default"],
    )
    candidate_id: str = Field(
        min_length=1, max_length=200, description="已有候选 candidate_id", examples=["c0001"]
    )
    reason: str = Field(
        min_length=1,
        max_length=2000,
        description="非空人工接受理由，写入原核心记录",
        examples=["已人工检查候选图片"],
    )

    @model_validator(mode="after")
    def nonempty(self):
        if not self.reason.strip():
            raise ValueError("reason must not be blank")
        return self


def config_mapping(config):
    mapping = config.model_dump(mode="json", exclude=LEGACY_FIELDS)
    for name, service in config.services.items():
        mapping["services"][name]["api_key"] = service.credential()
        mapping["services"][name].pop("api_key_env", None)
    return mapping


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sanitize_text(value, secrets, root):
    for secret in sorted(set(secrets), key=len, reverse=True):
        if secret:
            value = value.replace(secret, "[redacted]")
    for path in (str(root), root.as_posix()):
        value = value.replace(path, "[path redacted]")
    return value


def sanitize_mapping(value, sanitize_key, sanitize_value):
    """Retain colliding values; reserve original safe names before assigning suffixes.

    A secret-bearing key becoming '[redacted]' never overwrites an existing
    '[redacted]' key. The rewritten key receives '#2', '#3', etc., skipping all
    original safe names and every other rewritten base name deterministically.
    """
    bases = {key: sanitize_key(key) for key in value}
    reserved = set(bases.values())
    unchanged = {key for key, base in bases.items() if key == base}
    result = {}
    for key, item in value.items():
        base = bases[key]
        target = base
        if target in result or (target in unchanged and key != target):
            number = 2
            target = f"{base}#{number}"
            while target in result or target in reserved:
                number += 1
                target = f"{base}#{number}"
        result[target] = sanitize_value(key, item)
    return result


def sanitize_payload(value, secrets, root):
    """Sanitize arbitrary provider content, including mapping keys, never a whole DTO."""
    if isinstance(value, dict):
        return sanitize_mapping(
            value,
            lambda key: sanitize_text(str(key), secrets, root),
            lambda key, item: sanitize_payload(item, secrets, root),
        )
    if isinstance(value, list):
        return [sanitize_payload(item, secrets, root) for item in value]
    if isinstance(value, str):
        return sanitize_text(value, secrets, root)
    if value is None or isinstance(value, (int, float, bool)):
        # Arbitrary supplier JSON can echo a numeric-looking credential as a
        # number rather than text. Typed protocol scalars bypass this helper.
        serialized = canonical(value)
        redacted = sanitize_text(serialized, secrets, root)
        return value if redacted == serialized else redacted
    return value


# Only core-built delivery/routing metadata positions are retained verbatim.
# Provider-created pattern IDs and all their references use the alias mapper.
# ErrorInfo.stage and stop_reason are not trusted: a provider error can supply them.
# This allowlist never applies to arbitrary maps; plain protocol values are bounded.
SEMANTIC_FIELDS = {
    "asset_id",
    "candidate_id",
    "blob_id",
    "request_id",
    "target_key",
    "reference_ids",
    "mime_type",
    "model",
    "platform",
    "output_type",
    "variant",
    "presentation_mode",
    "generation_mode",
    "role",
    "stage_id",
    "status",
}
IDENTITY_MAP_FIELDS = {"excluded_reasons", "subject_presentations"}
CLIENT_MATERIAL_FIELDS = {
    "material_id",
    "material_ids",
    "representative_material_id",
    "source_material_id",
    "generation_material_ids",
    "audit_material_ids",
}
PROTOCOL_VALUES = {
    "status": {
        "stage",
        "succeeded",
        "failed",
        "pending_audit",
        "audit_error",
        "quality_failed",
        "needs_input",
        "budget_exhausted",
        "accepted",
        "generation_uncertain",
        "partial",
    },
    "presentation_mode": {"model_wear", "product_only"},
    "role": {
        "auto",
        "identity",
        "detail",
        "accessory",
        "scene",
        "style",
        "shoot_continuity",
        "repair_evidence",
    },
}


def sanitize_product_attributes(value, analysis, secrets, root, provider_id):
    """Synchronize provider ID aliases in core subject/fact attribute envelopes."""
    subject_ids = {subject["subject_id"] for subject in (analysis or {}).get("subjects", [])}
    facts = {fact["fact_id"]: fact.get("subject_id") for fact in (analysis or {}).get("facts", [])}

    def subject_attributes(subject_id, attributes):
        if not isinstance(attributes, dict):
            return sanitize_payload(attributes, secrets, root)

        def attribute_key(key):
            if key == "garments":
                return key  # Core-owned garment envelope; its item maps remain untrusted.
            if facts.get(key) == subject_id:
                return provider_id(key)
            return sanitize_text(str(key), secrets, root)

        def attribute_value(key, item):
            if key == "garments":
                try:
                    garment = GarmentStructureAudit.model_validate(item)
                except ValidationError:
                    return sanitize_payload(item, secrets, root)
                return sanitize_public_model(GarmentStructureAudit, garment, secrets, root)
            return sanitize_payload(item, secrets, root)

        return sanitize_mapping(attributes, attribute_key, attribute_value)

    def attribute_value(key, item):
        if key != "subjects" or not isinstance(item, dict):
            return sanitize_payload(item, secrets, root)
        return sanitize_mapping(
            item,
            lambda identifier: (
                provider_id(identifier)
                if identifier in subject_ids
                else sanitize_text(str(identifier), secrets, root)
            ),
            lambda identifier, data: (
                subject_attributes(identifier, data)
                if identifier in subject_ids
                else sanitize_payload(data, secrets, root)
            ),
        )

    return sanitize_mapping(
        value,
        lambda key: key if key == "subjects" else sanitize_text(str(key), secrets, root),
        attribute_value,
    )


def validate_public_model(model_type, value):
    if isinstance(value, Model):
        # Validate a fresh mapping: Pydantic may otherwise trust an existing model
        # instance produced with model_construct or mutation bypassing validation.
        value = value.model_dump(mode="json", warnings=False)
    try:
        return model_type.model_validate(value)
    except ValidationError:
        raise ServerError(
            500, "invalid_public_result", "Task public result validation failed"
        ) from None


def trusted_input_hashes(request, materials):
    """Only hashes computed from bound input bytes are retained as protocol metadata."""
    from image_agent.selection import analysis_fingerprint

    return {hashlib.sha256(material.data).hexdigest() for material in materials} | {
        analysis_fingerprint(request, materials)
    }


def sanitize_public_model(
    model_type,
    value,
    secrets,
    root,
    *,
    client_material_ids=(),
    trusted_hashes=(),
    identity_key=None,
):
    validated = validate_public_model(model_type, value)
    raw = validated.model_dump(mode="json")
    schema = model_type.model_json_schema()
    definitions = schema.get("$defs", {})
    analysis = raw.get("analysis") or {}
    referenced_ids = {
        "excluded_reasons": {element["element_id"] for element in analysis.get("elements", [])},
        "subject_presentations": {
            subject["subject_id"] for subject in analysis.get("subjects", [])
        },
    }

    def resolved_shape(item, shape):
        if "$ref" in shape:
            shape = definitions[shape["$ref"].rsplit("/", 1)[-1]]
        if "anyOf" in shape:
            for branch in shape["anyOf"]:
                candidate = resolved_shape(item, branch)
                kind = candidate.get("type")
                if (
                    (item is None and kind == "null")
                    or (isinstance(item, str) and kind == "string")
                    or (isinstance(item, dict) and kind == "object")
                    or (isinstance(item, list) and kind == "array")
                    or (isinstance(item, (int, float)) and kind in ("integer", "number"))
                ):
                    return candidate
            return {}
        return shape

    client_material_ids = set(client_material_ids)
    reserved_ids = set(client_material_ids)

    def collect_ids(item, shape):
        shape = resolved_shape(item, shape)
        if isinstance(item, str) and ("pattern" in shape):
            reserved_ids.add(item)
        elif isinstance(item, dict) and "properties" in shape:
            for key, data in item.items():
                collect_ids(data, shape["properties"][key])
        elif isinstance(item, list):
            for data in item:
                collect_ids(data, shape.get("items", {}))

    collect_ids(raw, schema)
    aliases = {}
    # No fixed prefix: even a one-character credential may overlap it. Remove
    # forbidden single characters, then reject candidates containing any key.
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
    alphabet = "".join(character for character in alphabet if character not in secrets)

    def provider_id(identifier):
        if not any(secret and secret in identifier for secret in secrets):
            return identifier
        if identifier in aliases:
            return aliases[identifier]
        if not identity_key:
            raise ServerError(
                500, "identity_redaction_failed", "Private identity context is required"
            )
        for attempt in range(256):
            if not alphabet:
                break
            seed = f"image-agent-public-provider-id:{identifier}:{attempt}".encode()
            # The persisted encrypted configuration token is private to this
            # task and unchanged on recovery. A public alias is not an unkeyed
            # digest that could be checked against guessed weak credentials.
            number = int.from_bytes(hmac.digest(identity_key.encode(), seed, "sha512"), "big")
            candidate = ""
            for _ in range(48 if len(alphabet) > 1 else attempt % 64 + 1):
                candidate += alphabet[number % len(alphabet)]
                number //= len(alphabet)
            if candidate not in reserved_ids and not any(
                secret and secret in candidate for secret in secrets
            ):
                aliases[identifier] = candidate
                reserved_ids.add(candidate)
                return candidate
        raise ServerError(
            500, "identity_redaction_failed", "Provider identity cannot be safely published"
        )

    def walk(item, shape, field=None):
        shape = resolved_shape(item, shape)
        if isinstance(item, dict):
            if "properties" in shape:
                return {
                    key: walk(data, shape["properties"][key], key) for key, data in item.items()
                }
            if field == "product_attributes":
                return sanitize_product_attributes(
                    item, raw.get("analysis"), secrets, root, provider_id
                )
            if field in IDENTITY_MAP_FIELDS:
                # The DTO annotates these keys as plain str. Known analysis IDs
                # remain provider-owned and must pass through the same alias map.
                return sanitize_mapping(
                    item,
                    lambda key: (
                        provider_id(key)
                        if key in referenced_ids[field]
                        else sanitize_text(str(key), secrets, root)
                    ),
                    lambda key, data: (
                        data
                        if field == "subject_presentations"
                        and data in {"product_only", "model_wear"}
                        else sanitize_payload(data, secrets, root)
                    ),
                )
            if field == "usage":
                return sanitize_mapping(
                    item,
                    lambda key: (
                        key
                        if key in {"image_calls", "vision_calls"}
                        else sanitize_text(str(key), secrets, root)
                    ),
                    lambda key, data: (
                        data
                        if key in {"image_calls", "vision_calls"}
                        and isinstance(data, int)
                        and not isinstance(data, bool)
                        else sanitize_payload(data, secrets, root)
                    ),
                )
            return sanitize_payload(item, secrets, root)
        if isinstance(item, list):
            if field == "reference_ids":
                return list(item)
            return [walk(data, shape.get("items", {}), field) for data in item]
        if isinstance(item, str):
            if field in {'sha256', 'fingerprint'} and item in trusted_hashes:
                return item
            if "pattern" in shape:
                # Format and reference validation do not establish source trust.
                # Only client material IDs in material-reference positions and
                # core-generated stage IDs retain their literal identity here.
                if (
                    field in CLIENT_MATERIAL_FIELDS and item in client_material_ids
                ) or field == "stage_id":
                    return item
                return provider_id(item)
            if "enum" in shape or "const" in shape:
                return item
            if field in {"sha256", "fingerprint"} and item in trusted_hashes:
                return item
            if field in SEMANTIC_FIELDS:
                if field not in PROTOCOL_VALUES or item in PROTOCOL_VALUES[field]:
                    return item
            return sanitize_text(item, secrets, root)
        return item

    # Recheck relational references and field constraints after text sanitation.
    return validate_public_model(model_type, walk(raw, schema))


def analysis_binding(request):
    return {k: v for k, v in request.items() if k not in ("selection", "request_id")}


class TaskService:
    def __init__(self, session_factory, config_service, media_store, cipher, settings):
        self.repository = TaskRepository(session_factory)
        self.config_service = config_service
        self.media_store = media_store
        self.cipher = cipher
        self.settings = settings
        # This deployment supports a single service process; DB unique/CAS protect retries too.
        self.submission_lock = asyncio.Lock()

    async def upload(self, data):
        ref = self.media_store.store(data)
        try:
            async with self.repository.transaction() as session:
                session.add(HttpMedia(**ref))
        except BaseException:
            self.media_store.path(ref["storage_ref"]).unlink(missing_ok=True)
            raise
        return {"media_id": ref["id"], **{k: ref[k] for k in ("mime_type", "byte_size", "sha256")}}

    async def resolve_media(self, media_ids, session=None):
        if session is None:
            async with self.repository.transaction() as opened:
                return await self.resolve_media(media_ids, opened)
        sources = {}
        for identifier in set(media_ids):
            row = await session.get(HttpMedia, identifier)
            if row is None:
                raise ServerError(404, "media_not_found", "Input media was not found")
            data = self.media_store.read(row.storage_ref)
            if hashlib.sha256(data).hexdigest() != row.sha256:
                raise ServerError(409, "media_integrity", "Input media integrity validation failed")
            sources[identifier] = ImageSource(data=data)
        return sources

    async def submit_task(
        self, kind, agent_config_id, request, idempotency_key, analysis_task_id=None
    ):
        try:
            return await self._submit_task(
                kind, agent_config_id, request, idempotency_key, analysis_task_id
            )
        except IntegrityError:
            # A concurrent service instance committed the unique key first.
            normalized = TaskSubmission(
                kind=kind,
                agent_config_id=agent_config_id,
                request=request,
                analysis_task_id=analysis_task_id,
            )
            request_hash = hashlib.sha256(
                canonical(normalized.model_dump(mode="json")).encode()
            ).hexdigest()
            async with self.repository.transaction() as session:
                existing = await session.scalar(
                    select(HttpTask).where(HttpTask.idempotency_key == idempotency_key)
                )
                if existing and existing.request_hash == request_hash:
                    return self.reference(existing)
                if existing:
                    raise ServerError(
                        409, "idempotency_conflict", "Idempotency-Key was used for another request"
                    )
                raise ServerError(
                    503, "database_unavailable", "Task database is unavailable"
                ) from None

    async def _submit_task(
        self, kind, agent_config_id, request, idempotency_key, analysis_task_id=None
    ):
        submission = TaskSubmission(
            kind=kind,
            agent_config_id=agent_config_id,
            request=request,
            analysis_task_id=analysis_task_id,
        )
        if not idempotency_key or not idempotency_key.strip() or len(idempotency_key) > 200:
            raise ServerError(422, "invalid_idempotency_key", "A valid Idempotency-Key is required")
        normalized = submission.model_dump(mode="json")
        request_hash = hashlib.sha256(canonical(normalized).encode()).hexdigest()
        async with self.submission_lock, self.repository.transaction() as session:
            existing = await session.scalar(
                select(HttpTask).where(HttpTask.idempotency_key == idempotency_key)
            )
            if existing:
                if existing.request_hash != request_hash:
                    raise ServerError(
                        409, "idempotency_conflict", "Idempotency-Key was used for another request"
                    )
                return self.reference(existing)
            count = await session.scalar(
                select(func.count())
                .select_from(HttpTask)
                .where(HttpTask.status.in_(("queued", "running")))
            )
            if count >= self.settings.queue_capacity:
                raise ServerError(429, "queue_full", "Task queue is full")
            sources = await self.resolve_media([m.media_id for m in request.materials], session)
            request_from_dto(request, sources)
            config, versions = await self.config_service.resolve_agent_config_in_session(
                session, agent_config_id
            )
            config.model_name(request.image_model)
            if analysis_task_id:
                analysis_task = await session.get(HttpTask, analysis_task_id)
                if (
                    not analysis_task
                    or analysis_task.kind != "analyze_materials"
                    or (analysis_task.status != "completed" or not analysis_task.result)
                ):
                    raise ServerError(409, "analysis_unavailable", "Analysis task is not available")
                if analysis_binding(
                    analysis_task.normalized_request["request"]
                ) != analysis_binding(normalized["request"]):
                    raise ServerError(
                        409, "analysis_binding", "Analysis task does not match these inputs"
                    )
                normalized["analysis_snapshot"] = analysis_task.result["analysis"]
            identifier = str(uuid.uuid4())
            task = HttpTask(
                id=identifier,
                kind=kind,
                agent_config_id=agent_config_id,
                normalized_request=normalized,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                config_versions=versions,
                config_snapshot_encrypted=self.cipher.encrypt_mapping(config_mapping(config)),
                status="queued",
                operation="initial",
                run_ref=self.media_store.task_run_ref(identifier),
            )
            session.add(task)
            await session.flush()
            return self.reference(task)

    @staticmethod
    def reference(task):
        return {
            "task_id": task.id,
            "status": task.status,
            "core_status": task.core_status,
            "config_versions": task.config_versions,
            "progress": task.progress,
            "error": {"code": task.error_code, "message": task.error_message}
            if task.error_code
            else None,
        }

    async def get_task(self, task_id):
        return self.reference(await self.repository.get(task_id))

    async def get_result(self, task_id):
        task = await self.repository.get(task_id)
        if task.status != "completed" or not task.result:
            raise ServerError(409, "result_unavailable", "Task result is not available")
        return task.result.get("dto", task.result.get("analysis"))

    async def blob(self, task_id, blob_id):
        task = await self.repository.get(task_id)
        if task.status != "completed" or not task.result:
            raise ServerError(409, "result_unavailable", "Task result is not available")
        ref = task.result.get("blobs", {}).get(blob_id)
        if ref is None:
            raise ServerError(404, "blob_not_found", "Task blob was not found")
        prefix = f"tasks/{task.id}/blobs/"
        if not ref["storage_ref"].startswith(prefix):
            raise ServerError(404, "blob_not_found", "Task blob was not found")
        data = self.media_store.read(ref["storage_ref"])
        if hashlib.sha256(data).hexdigest() != ref["sha256"]:
            raise ServerError(409, "blob_integrity", "Result blob integrity validation failed")
        asset = next(a for a in task.result["dto"]["assets"] if a["blob_id"] == blob_id)
        return data, asset["mime_type"]

    async def recover(self, task_id, operation="resume", operation_input=None):
        task = await self.repository.get(task_id)
        if task.kind != "create_images":
            raise ServerError(409, "not_recoverable", "Only generation tasks can be recovered")
        if not self.media_store.path(task.run_ref).joinpath("run.json").is_file():
            raise ServerError(409, "run_unavailable", "No persisted generation run is available")
        if operation == "accept":
            operation_input = AcceptRequest.model_validate(operation_input).model_dump()
        async with self.submission_lock:
            await self.repository.requeue(
                task_id, operation, operation_input, self.settings.queue_capacity
            )
        return await self.get_task(task_id)

    async def execute(self, task):
        from image_agent import pipeline

        try:
            config = AgentConfig.from_mapping(
                self.cipher.decrypt_mapping(task.config_snapshot_encrypted)
            )
            secrets = [service.credential() for service in config.services.values()]
            dto = CreationRequestDTO.model_validate(task.normalized_request["request"])
            client_material_ids = {material.material_id for material in dto.materials}

            async def progress(event):
                safe = {key: event[key] for key in ("stage", "asset_id", "attempt") if key in event}
                # stage/asset_id/attempt are core protocol metadata, not provider prose.
                await self.repository.finish(task.id, progress=safe)

            run = self.media_store.path(task.run_ref)
            if task.operation == "resume":
                result = await pipeline.resume_images(run, config, on_progress=progress)
            elif task.operation == "accept":
                result = await pipeline.accept_candidate(run, **task.operation_input)
            else:
                sources = await self.resolve_media([m.media_id for m in dto.materials])
                request = request_from_dto(dto, sources)
                request.output_dir = run.parent
                request.request_id = "run"
                materials = tuple(
                    LoadedMaterial(
                        material_id=material.material_id,
                        data=material.source.data,
                        sha256=hashlib.sha256(material.source.data).hexdigest(),
                        order=index,
                        role_hint=material.role_hint,
                    )
                    for index, material in enumerate(request.normalized_materials())
                )
                trusted_hashes = trusted_input_hashes(request, materials)
                if task.kind == "analyze_materials":
                    await progress({"stage": "analyze_materials", "attempt": 0})
                    analysis = await pipeline.analyze_materials(request, config)
                    public_analysis = sanitize_public_model(
                        MaterialAnalysis,
                        analysis,
                        secrets,
                        self.media_store.root,
                        client_material_ids=client_material_ids,
                        trusted_hashes=trusted_hashes,
                        identity_key=task.config_snapshot_encrypted,
                    )
                    payload = {"analysis": public_analysis.model_dump(mode="json")}
                    core_status = analysis.status
                    await self.repository.finish(
                        task.id,
                        status="completed",
                        core_status=core_status,
                        result=payload,
                        error_code=None,
                        error_message=None,
                    )
                    return
                snapshot = task.normalized_request.get("analysis_snapshot")
                analysis = MaterialAnalysis.model_validate(snapshot) if snapshot else None
                result = await pipeline.create_images(
                    request, config, analysis=analysis, on_progress=progress
                )
            if task.operation in {"resume", "accept"}:
                # Recovery uses the original immutable run inputs, not current
                # uploaded media/config records or supplier-provided hashes.
                from image_agent.run_store import RunStore

                input_store = RunStore(run)
                trusted_hashes = trusted_input_hashes(
                    input_store.request(), input_store.materials()
                )
            bundle = result_to_bundle(result)
            trusted_hashes = set(trusted_hashes) | {
                hashlib.sha256(data).hexdigest() for data in bundle.blobs.values()
            }
            public_dto = sanitize_public_model(
                CreationResultDTO,
                bundle.dto,
                secrets,
                self.media_store.root,
                client_material_ids=client_material_ids,
                trusted_hashes=trusted_hashes,
                identity_key=task.config_snapshot_encrypted,
            )
            # MediaStore's blob IDs, storage refs and hashes are internal metadata.
            # They must never pass through arbitrary text redaction.
            payload = self.media_store.save_bundle(task.id, ResultBundle(public_dto, bundle.blobs))
            payload["dto"] = validate_public_model(
                CreationResultDTO,
                payload["dto"],
            ).model_dump(mode="json")
            await self.repository.finish(
                task.id,
                status="completed",
                core_status=result.status,
                result=payload,
                error_code=None,
                error_message=None,
            )
        except asyncio.CancelledError:
            await self.repository.finish(
                task.id,
                status="interrupted",
                error_code="interrupted",
                error_message="Execution stopped; explicit recovery is required",
            )
            raise
        except Exception as error:
            code = (
                error.code if isinstance(error, (AgentError, ServerError)) else "execution_failed"
            )
            await self.repository.finish(
                task.id,
                status="failed",
                error_code=code,
                error_message="Task execution failed; inspect the task state before recovery",
            )
