"""Path-free long-image JSON contracts and trusted local media/font adapters."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from .contracts import MaterialDTO
from .errors import AgentError
from .images import image_format
from .long_image_models import (
    LongImageAudit,
    LongImageIssue,
    LongImagePlan,
    LongImagePlanningResult,
    LongImageRequest,
    LongImageResult,
    LongImageUsage,
    LongProductContext,
    LongProductSpec,
    LongRequiredCopy,
    LongScene,
    LongTheme,
    UsageCounts,
)
from .models import (
    AssetStatus,
    ErrorInfo,
    Id,
    ImageSource,
    MaterialAnalysis,
    Model,
    QualityReport,
    SelectionSpec,
)


class LongImageOptionsDTO(Model):
    width: int = Field(default=750, strict=True, ge=400, le=2400)
    theme: LongTheme = "auto"
    slice_height: int = Field(default=1500, strict=True, ge=500, le=4000)
    font_id: Id | None = None


class LongImageRequestDTO(Model):
    schema_version: Literal["1.0"] = "1.0"
    task_type: Literal["long_image"] = "long_image"
    scene: LongScene
    title: str = Field(min_length=1, max_length=120)
    content_brief: str = Field(min_length=1, max_length=20000)
    materials: list[MaterialDTO] = Field(default_factory=list, max_length=8)
    style_hint: str | None = None
    product_context: LongProductContext | None = None
    product_specs: list[LongProductSpec] = Field(default_factory=list, max_length=30)
    service_notes: list[str] = Field(default_factory=list, max_length=10)
    required_copy: list[LongRequiredCopy] = Field(default_factory=list)
    selection: SelectionSpec = Field(default_factory=SelectionSpec)
    options: LongImageOptionsDTO = Field(default_factory=LongImageOptionsDTO)
    image_model: Literal["pro", "fast", "base"] = "pro"
    image_size: Literal["1K", "2K", "4K"] = "2K"
    request_id: Id | None = None

    @model_validator(mode="after")
    def validate_local_fields(self):
        data = self.model_dump(exclude={"schema_version", "task_type", "materials", "options"})
        data["materials"] = [
            m.model_dump(exclude={"media_id"}) | {"source": ImageSource(data=b"dto-validation")}
            for m in self.materials
        ]
        data["options"] = self.options.model_dump(exclude={"font_id"})
        LongImageRequest(**data)
        return self


class LongImagePlanDTO(LongImagePlan):
    task_type: Literal["long_image"] = "long_image"


class LongImagePlanningResultDTO(Model):
    schema_version: Literal["1.0"] = "1.0"
    task_type: Literal["long_image"] = "long_image"
    planning_id: Id
    status: Literal["ready", "needs_input", "failed"]
    plan: LongImagePlanDTO | None = None
    analysis: MaterialAnalysis | None = None
    issues: list[LongImageIssue] = Field(default_factory=list)
    usage: UsageCounts | None = None
    record_id: Id | None = None
    warnings: list[str] = Field(default_factory=list)
    error_info: ErrorInfo | None = None


class LongBlobMetadata(Model):
    blob_id: str | None = None
    mime_type: str | None = None
    byte_length: int | None = Field(default=None, ge=0)
    sha256: str | None = None


class LongCandidateDTO(LongBlobMetadata):
    candidate_id: Id
    index: int = Field(default=0, ge=0)
    status: str = "pending_audit"
    selected: bool = False
    audit: QualityReport | LongImageAudit | None = None
    quality: QualityReport | None = None
    error_info: ErrorInfo | None = None
    repair_prompt: str | None = None
    acceptance: dict | None = None


class LongSlotAssetDTO(LongBlobMetadata):
    slot_id: Id
    source: Literal["source", "generate"]
    source_status: Literal["available", "missing", "invalid"] | None = None
    status: AssetStatus | None = None
    selected_candidate_id: Id | None = None
    candidates: list[LongCandidateDTO] = Field(default_factory=list)
    asset_revision: int = Field(default=0, ge=0)
    audit: QualityReport | LongImageAudit | None = None
    error_info: ErrorInfo | None = None


class LongImageArtifactDTO(LongBlobMetadata):
    artifact_id: Id
    kind: Literal["section", "page", "slice"]
    status: AssetStatus
    availability: Literal["current", "stale", "absent"]
    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)
    delivery_revision: int = Field(default=0, ge=0)
    dependencies: list[Id] = Field(default_factory=list)
    layout: dict = Field(default_factory=dict)


class LongImageResultDTO(Model):
    schema_version: Literal["1.0"] = "1.0"
    task_type: Literal["long_image"] = "long_image"
    run_id: Id | None = None
    status: AssetStatus
    plan: LongImagePlanDTO | None = None
    issues: list[LongImageIssue] = Field(default_factory=list)
    error_info: ErrorInfo | None = None
    image_assets: list[LongSlotAssetDTO] = Field(default_factory=list)
    sections: list[LongImageArtifactDTO] = Field(default_factory=list)
    page: LongImageArtifactDTO | None = None
    slices: list[LongImageArtifactDTO] = Field(default_factory=list)
    page_checks: list[dict] = Field(default_factory=list)
    usage: LongImageUsage = Field(default_factory=LongImageUsage)
    warnings: list[str] = Field(default_factory=list)
    output_errors: list[ErrorInfo] = Field(default_factory=list)
    delivery_revision: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class LongImageResultBundle:
    dto: LongImageResultDTO
    blobs: dict[str, bytes]


def long_request_from_dto(
    dto: LongImageRequestDTO,
    sources: dict[str, ImageSource],
    *,
    font_bindings: dict[str, Path | str] | None = None,
) -> LongImageRequest:
    materials = []
    for material in dto.materials:
        if material.media_id not in sources:
            raise AgentError("media_id has no source binding")
        materials.append(
            material.model_dump(exclude={"media_id"}) | {"source": sources[material.media_id]}
        )
    options = dto.options.model_dump(exclude={"font_id"})
    if dto.options.font_id:
        if not font_bindings or dto.options.font_id not in font_bindings:
            raise AgentError("font_id has no local font binding")
        options["font_path"] = font_bindings[dto.options.font_id]
    return LongImageRequest(
        **dto.model_dump(exclude={"schema_version", "task_type", "materials", "options"}),
        materials=materials,
        options=options,
    )


_PATH = re.compile(
    r"(?i)(?<![\w])(?:[a-z]:[/\\]|\\\\)[^\s\"'<>;,]+"
    r"|(?<![\w:/])/(?:[^\s/\"'<>;,]+/)+[^\s\"'<>;,]*"
)
_LOCAL_KEYS = {
    "path",
    "run_dir",
    "output_dir",
    "font_path",
    "file_path",
    "ledger_ref",
    "local_record_ref",
    "image",
    "data",
}


def _public(value, *, key=""):
    """Strip local references at arbitrary depth, including path-bearing errors."""
    if isinstance(value, Model):
        value = value.model_dump(mode="python")
    if isinstance(value, dict):
        return {
            name: _public(item, key=name)
            for name, item in value.items()
            if name not in _LOCAL_KEYS and not name.endswith(("_path", "_dir", "_ledger_ref"))
        }
    if isinstance(value, (list, tuple)):
        return [_public(item, key=key) for item in value]
    if isinstance(value, Path):
        return "[local reference]"
    if isinstance(value, bytes):
        return None
    if isinstance(value, str) and key not in {"text", "pointer", "source_quote"}:
        return _PATH.sub("[local reference]", value)
    return value


def long_planning_to_dto(result: LongImagePlanningResult) -> LongImagePlanningResultDTO:
    data = _public(result.model_dump(exclude={"record"}))
    data["record_id"] = result.record.record_id if result.record else None
    # Explicit None remains unavailable rather than fabricated zero usage.
    return LongImagePlanningResultDTO(**data)


def long_result_to_bundle(result: LongImageResult) -> LongImageResultBundle:
    # Never interpolate request_id or a local directory into the namespace.
    prefix = f"long_image/{result.run_id or uuid4().hex}/{result.delivery_revision}/"
    blobs = {}

    def attach(data, image, suffix):
        if image is None:
            return data
        blob_id = prefix + suffix
        if blob_id in blobs:
            raise AgentError("duplicate long-image blob ID")
        blobs[blob_id] = image
        return data | {
            "blob_id": blob_id,
            "mime_type": image_format(image)[1],
            "byte_length": len(image),
            "sha256": hashlib.sha256(image).hexdigest(),
        }

    assets = []
    for asset in result.image_assets:
        data = _public(asset.model_dump(exclude={"candidates"}))
        candidates = []
        for candidate in asset.candidates:
            candidate_data = _public(
                {k: v for k, v in candidate.items() if k in LongCandidateDTO.model_fields}
            )
            candidate_id = candidate_data.get("candidate_id")
            # Validate safe IDs before using them in blob namespaces.
            parsed = LongCandidateDTO(**candidate_data)
            candidates.append(
                attach(
                    parsed.model_dump(),
                    candidate.get("image", candidate.get("data")),
                    f"candidate/{asset.slot_id}/{candidate_id}",
                )
            )
        data["candidates"] = candidates
        if asset.source != "source" or asset.source_status == "available":
            data = attach(data, asset.image, f"asset/{asset.slot_id}")
        assets.append(data)

    def artifact_data(artifact, suffix):
        data = _public(artifact.model_dump())
        # Stale files remain reviewable locally, without a current download blob.
        if artifact.availability == "current":
            data = attach(data, artifact.image, suffix)
        return data

    data = _public(result.model_dump(exclude={"image_assets", "sections", "page", "slices"}))
    data.update(
        image_assets=assets,
        sections=[artifact_data(a, f"section/{a.artifact_id}") for a in result.sections],
        page=artifact_data(result.page, "page") if result.page else None,
        slices=[artifact_data(a, f"slice/{i}") for i, a in enumerate(result.slices)],
    )
    return LongImageResultBundle(dto=LongImageResultDTO(**data), blobs=blobs)
