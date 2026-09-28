"""Versioned external JSON DTOs and explicit media binding; no storage framework."""

from dataclasses import dataclass
from typing import Literal

from pydantic import Field, model_validator

from .errors import AgentError, make_error_info
from .images import image_format
from .models import (
    AssetElementPlan,
    CreationRequest,
    CreationResult,
    DetailSetAudit,
    ElementCheck,
    ErrorInfo,
    GenerationAttempt,
    Id,
    ImageSource,
    Issue,
    MaterialAnalysis,
    Model,
    ProductInputAudit,
    QualityReport,
    ReferenceBinding,
    Role,
    SelectionSpec,
)


class MaterialDTO(Model):
    material_id: Id
    media_id: str = Field(min_length=1)
    role_hint: Role = "auto"
    subject_hint: str | None = None
    element_hints: list[str] = Field(default_factory=list)


class CreationRequestDTO(Model):
    schema_version: Literal["1.0"] = "1.0"
    product_name: str
    category: str
    platforms: list[str]
    output_types: list[str]
    materials: list[MaterialDTO] = Field(min_length=1, max_length=8)
    creative_brief: str | None = None
    selection: SelectionSpec = Field(default_factory=SelectionSpec)
    style_hint: str | None = None
    presentation_mode: Literal["auto", "model_wear", "product_only"] = "auto"
    model_preference: Literal["auto", "female", "male", "no_face"] = "auto"
    market: str | None = None
    aspect_ratio: Literal["auto", "1:1", "3:4", "4:5", "9:16", "16:9", "4:1", "1:4"] = "auto"
    image_size: Literal["1K", "2K", "4K"] = "2K"
    image_model: Literal["pro", "fast", "base"] = "pro"
    request_id: str | None = None

    @model_validator(mode="after")
    def validate_request_fields(self):
        data = self.model_dump(exclude={"schema_version", "materials"})
        data["materials"] = [
            m.model_dump(exclude={"media_id"})
            | {"source": ImageSource(data=b"validation-placeholder")}
            for m in self.materials
        ]
        normalized = CreationRequest(**data)
        for field in (
            "product_name",
            "category",
            "platforms",
            "output_types",
            "market",
            "request_id",
        ):
            object.__setattr__(self, field, getattr(normalized, field))
        return self


class AssetDTO(Model):
    asset_id: str
    platform: str
    output_type: str
    variant: str | None = None
    status: Literal["succeeded", "failed"]
    blob_id: str | None = None
    mime_type: str | None = None
    byte_length: int | None = None
    model: str
    prompt: str
    generation_mode: Literal["standard", "strict", "staged"]
    quality: QualityReport | None = None
    error: str | None = None
    error_info: ErrorInfo | None = None
    element_plan: AssetElementPlan | None = None
    element_checks: list[ElementCheck] = Field(default_factory=list)
    reference_bindings: list[ReferenceBinding] = Field(default_factory=list)
    attempts: list[GenerationAttempt] = Field(default_factory=list)


class CreationResultDTO(Model):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str | None = None
    status: Literal["succeeded", "partial", "failed", "needs_input"]
    presentation_mode: str
    style_prompt: str | None = None
    product_attributes: dict
    input_check: ProductInputAudit
    warnings: list[str]
    assets: list[AssetDTO]
    detail_set_audits: list[DetailSetAudit]
    analysis: MaterialAnalysis | None = None
    issues: list[Issue]
    error_info: ErrorInfo | None = None
    output_errors: list[ErrorInfo]


class ErrorDTO(Model):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str | None = None
    error: ErrorInfo


@dataclass(frozen=True)
class ResultBundle:
    dto: CreationResultDTO
    blobs: dict[str, bytes]


def request_from_dto(dto: CreationRequestDTO, sources: dict[str, ImageSource]) -> CreationRequest:
    materials = []
    for material in dto.materials:
        if material.media_id not in sources:
            raise AgentError("media_id has no source binding")
        materials.append(
            material.model_dump(exclude={"media_id"}) | {"source": sources[material.media_id]}
        )
    return CreationRequest(
        **dto.model_dump(exclude={"schema_version", "materials"}), materials=materials
    )


def result_to_bundle(result: CreationResult) -> ResultBundle:
    blobs = {}
    assets = []
    for asset in result.assets:
        data = asset.model_dump(exclude={"image", "file_path"})
        if asset.status == "succeeded" and asset.image is not None:
            if asset.asset_id in blobs:
                raise AgentError("duplicate result asset_id")
            blobs[asset.asset_id] = asset.image
            data.update(
                blob_id=asset.asset_id,
                mime_type=image_format(asset.image)[1],
                byte_length=len(asset.image),
            )
        assets.append(AssetDTO(**data))
    dto = CreationResultDTO(**result.model_dump(exclude={"assets"}), assets=assets)
    return ResultBundle(dto=dto, blobs=blobs)


def error_to_dto(error, request_id=None):
    return ErrorDTO(request_id=request_id, error=make_error_info(error))
