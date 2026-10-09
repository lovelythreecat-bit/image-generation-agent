"""Local long-image contracts. Byte payloads and local references stay local."""

from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, PrivateAttr, StrictBool, field_validator, model_validator

from .models import (
    AssetStatus,
    ErrorInfo,
    Id,
    MaterialAnalysis,
    MaterialInput,
    Model,
    Platform,
    QualityReport,
    ResolvedSelection,
    SelectionSpec,
    unique,
)

LongScene = Literal[
    "product_detail", "brand_intro", "promotion", "tutorial", "comparison", "custom"
]
LongTheme = Literal["auto", "clean", "warm", "premium"]
LongAspectRatio = Literal["1:1", "3:4", "4:5", "9:16", "16:9"]
UsageCounts = dict[str, Annotated[int, Field(strict=True, ge=0)]]


class LongImageOptions(Model):
    width: int = Field(default=750, strict=True, ge=400, le=2400)
    theme: LongTheme = "auto"
    slice_height: int = Field(default=1500, strict=True, ge=500, le=4000)
    font_path: Path | None = None


class LongProductContext(Model):
    product_name: str = Field(min_length=1)
    category: str = Field(min_length=1)
    platform: Platform | None = None
    presentation_mode: Literal["auto", "model_wear", "product_only"] = "auto"
    model_preference: Literal["auto", "female", "male", "no_face"] = "auto"

    @field_validator("product_name", "category")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class LongProductSpec(Model):
    name: str = Field(min_length=1)
    value: str = Field(min_length=1)


class LongRequiredCopy(Model):
    requirement_id: Id
    text: str = Field(min_length=1)
    section_hint: str | None = None
    occurrences: int = Field(default=1, strict=True, ge=1, le=12)


class LongImageRequest(Model):
    scene: LongScene
    title: str = Field(min_length=1, max_length=120)
    content_brief: str = Field(min_length=1, max_length=20000)
    materials: list[MaterialInput] = Field(default_factory=list, max_length=8)
    style_hint: str | None = None
    product_context: LongProductContext | None = None
    product_specs: list[LongProductSpec] = Field(default_factory=list, max_length=30)
    service_notes: list[Annotated[str, Field(min_length=1)]] = Field(
        default_factory=list, max_length=10
    )
    required_copy: list[LongRequiredCopy] = Field(default_factory=list)
    selection: SelectionSpec = Field(default_factory=SelectionSpec)
    options: LongImageOptions = Field(default_factory=LongImageOptions)
    image_model: Literal["pro", "fast", "base"] = "pro"
    image_size: Literal["1K", "2K", "4K"] = "2K"
    output_dir: Path | None = None
    request_id: Id | None = None

    @field_validator("title", "content_brief")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        # Preserve code points, including leading/trailing spaces and newlines.
        return value

    @model_validator(mode="after")
    def compatible(self):
        unique([m.material_id for m in self.materials], "material IDs")
        unique([c.requirement_id for c in self.required_copy], "copy IDs")
        if self.scene == "product_detail":
            if self.product_context is None or not self.materials:
                raise ValueError(
                    "product_detail requires product context and real source materials"
                )
        elif self.selection.mode != "auto":
            raise ValueError("explicit selection is supported only for product_detail")
        return self

    def normalized_materials(self):
        return tuple(self.materials)


class LongTextSource(Model):
    """JSON Pointer plus optional Unicode-codepoint half-open source range."""

    pointer: str | None = None
    start: int | None = Field(default=None, strict=True, ge=0)
    end: int | None = Field(default=None, strict=True, ge=0)
    model_authored: bool = False

    @model_validator(mode="after")
    def valid_source(self):
        if self.model_authored:
            if any(v is not None for v in (self.pointer, self.start, self.end)):
                raise ValueError("model-authored text has no input source")
        elif self.pointer is None or not self.pointer.startswith("/"):
            raise ValueError("input text requires a JSON Pointer")
        if (self.start is None) != (self.end is None):
            raise ValueError("both source range bounds required")
        if self.start is not None and self.end <= self.start:
            raise ValueError("empty or reversed source range")
        return self


class LongImageRequirement(Model):
    requirement_id: Id
    kind: Literal["page_text", "section_text", "slot_visual"] = "page_text"
    required: bool = True
    text: str = Field(min_length=1)
    source: LongTextSource
    section_ids: list[Id] = Field(default_factory=list)
    slot_ids: list[Id] = Field(default_factory=list)
    order: int | None = Field(default=None, strict=True, ge=0)
    occurrences: int = Field(default=1, strict=True, ge=1, le=12)


class LongImageTextUnit(Model):
    unit_id: Id
    kind: Literal["heading", "body", "bullet", "cell"]
    text: str = Field(min_length=1)
    source: LongTextSource = Field(default_factory=lambda: LongTextSource(model_authored=True))
    requirement_ids: list[Id] = Field(default_factory=list)


class LongImageSection(Model):
    section_id: Id
    role: Literal["hero", "image_text", "bullets", "steps", "table", "closing"]
    text_units: list[LongImageTextUnit] = Field(default_factory=list)
    table_rows: list[list[LongImageTextUnit]] = Field(default_factory=list)
    slot_ids: list[Id] = Field(default_factory=list)

    @model_validator(mode="after")
    def table_shape(self):
        if self.table_rows and self.role != "table":
            raise ValueError("table_rows require table role")
        if self.table_rows:
            width = len(self.table_rows[0])
            if width < 2 or any(len(row) != width for row in self.table_rows):
                raise ValueError("table requires at least two equally sized columns")
            if any(cell.kind != "cell" for row in self.table_rows for cell in row):
                raise ValueError("table rows require cell text units")
        unique(self.slot_ids, "section slot IDs")
        return self


class LongImageSlot(Model):
    slot_id: Id
    source: Literal["source", "generate"]
    required: bool = True
    material_ids: list[Id] = Field(default_factory=list)
    role: str = Field(min_length=1)
    prompt: str = ""
    aspect_ratio: LongAspectRatio | None = None
    requirement_ids: list[Id] = Field(default_factory=list)
    section_ids: list[Id] = Field(default_factory=list)

    @model_validator(mode="after")
    def strategy(self):
        for values in (self.material_ids, self.requirement_ids, self.section_ids):
            unique(values)
        if self.source == "source":
            if len(self.material_ids) != 1 or self.prompt or self.aspect_ratio is not None:
                raise ValueError(
                    "source slot requires exactly one material, empty prompt and native ratio"
                )
        elif not self.prompt.strip() or self.aspect_ratio is None:
            raise ValueError("generate slot requires prompt and supported aspect_ratio")
        return self


class LongAnalysisRef(Model):
    analysis_id: Id
    content_hash: str
    fingerprint: str


class LongImagePlan(Model):
    schema_version: Literal["1.0"] = "1.0"
    planner_version: str = "1.0"
    input_fingerprint: str
    scene: LongScene
    theme: LongTheme = "auto"
    visual_direction: str = ""
    requirements: list[LongImageRequirement] = Field(default_factory=list)
    image_slots: list[LongImageSlot] = Field(default_factory=list)
    sections: list[LongImageSection] = Field(min_length=2, max_length=12)
    analysis_ref: LongAnalysisRef | None = None
    resolved_selection: ResolvedSelection | None = None

    @model_validator(mode="after")
    def references(self):
        section_ids = {s.section_id for s in self.sections}
        slot_ids = {s.slot_id for s in self.image_slots}
        requirements = {r.requirement_id: r for r in self.requirements}
        unique([s.section_id for s in self.sections], "section IDs")
        unique([s.slot_id for s in self.image_slots], "slot IDs")
        unique([r.requirement_id for r in self.requirements], "requirement IDs")
        unique([u.unit_id for s in self.sections for u in section_text_units(s)], "text unit IDs")
        if sum(s.source == "generate" for s in self.image_slots) > 6:
            raise ValueError("at most six generated slots")
        for section in self.sections:
            if not set(section.slot_ids) <= slot_ids:
                raise ValueError("unknown section slot")
            for unit in section_text_units(section):
                unique(unit.requirement_ids)
                if not set(unit.requirement_ids) <= requirements.keys():
                    raise ValueError("unknown text requirement")
                if any(requirements[r].kind == "slot_visual" for r in unit.requirement_ids):
                    raise ValueError("visual requirement cannot be assigned to local text")
        for slot in self.image_slots:
            reverse = {s.section_id for s in self.sections if slot.slot_id in s.slot_ids}
            if not reverse or reverse != set(slot.section_ids) or not reverse <= section_ids:
                raise ValueError("slot section references must match reverse references")
            if not slot.required:
                raise ValueError("referenced slots must be required")
            if not set(slot.requirement_ids) <= requirements.keys():
                raise ValueError("unknown slot requirement")
            for rid in slot.requirement_ids:
                req = requirements[rid]
                if req.kind != "slot_visual" or slot.slot_id not in req.slot_ids:
                    raise ValueError("slot may reference only its own visual requirements")
        for requirement in self.requirements:
            unique(requirement.section_ids)
            unique(requirement.slot_ids)
            if (
                not set(requirement.section_ids) <= section_ids
                or not set(requirement.slot_ids) <= slot_ids
            ):
                raise ValueError("unknown requirement target")
            if requirement.kind == "slot_visual":
                reverse = {
                    s.slot_id
                    for s in self.image_slots
                    if requirement.requirement_id in s.requirement_ids
                }
                if not reverse or reverse != set(requirement.slot_ids):
                    raise ValueError("visual requirement reverse references must agree")
            elif requirement.slot_ids or (
                requirement.kind == "section_text" and not requirement.section_ids
            ):
                raise ValueError("invalid local text requirement targets")
        return self


def section_text_units(section: LongImageSection) -> list[LongImageTextUnit]:
    return [*section.text_units, *(cell for row in section.table_rows for cell in row)]


class LongImageIssue(Model):
    issue_id: Id
    code: str
    message: str
    resolution: Literal["input", "selection", "recheck", "reanalyze"] = "input"
    field: str | None = None
    required: bool = True
    subject_ids: list[Id] = Field(default_factory=list)
    material_ids: list[Id] = Field(default_factory=list)
    element_ids: list[Id] = Field(default_factory=list)
    options: list[dict] = Field(default_factory=list)


class LongImageAuditCheck(Model):
    requirement_id: Id | None = None
    kind: Literal["purpose", "subject", "unrequested_text", "style"]
    passed: StrictBool
    reason: str
    severity: Literal["info", "warning", "critical"]


class LongImageAudit(Model):
    slot_id: Id
    candidate_id: Id
    checks: list[LongImageAuditCheck]
    passed: StrictBool
    repairable: StrictBool
    repair_prompt: str | None = None


class LongPlanningRecord(Model):
    """A storage binding descriptor, never a client-asserted trust certificate.

    Consumers must load record_id through storage and compare all content hashes.
    Missing usage means unavailable, including external/unbound historical records.
    """

    record_id: Id
    planning_id: Id
    request_hash: str
    plan_hash: str
    analysis_hash: str | None = None
    selection_summary: dict = Field(default_factory=dict)
    usage: UsageCounts | None = None
    local_record_ref: str | None = None
    ledger_ref: str | None = None


class LongImagePlanningResult(Model):
    schema_version: Literal["1.0"] = "1.0"
    task_type: Literal["long_image"] = "long_image"
    planning_id: Id
    status: Literal["ready", "needs_input", "failed"]
    plan: LongImagePlan | None = None
    analysis: MaterialAnalysis | None = None
    issues: list[LongImageIssue] = Field(default_factory=list)
    usage: UsageCounts | None = None
    record: LongPlanningRecord | None = None
    warnings: list[str] = Field(default_factory=list)
    error_info: ErrorInfo | None = None


class LongImageUsage(Model):
    execution: UsageCounts | None = None
    linked_planning: dict[Id, UsageCounts | None] = Field(default_factory=dict)
    display_total: UsageCounts | None = None
    planning_id: Id | None = None

    @model_validator(mode="after")
    def totals(self):
        counts = [self.execution, *self.linked_planning.values()]
        if any(c is None for c in counts):
            object.__setattr__(self, "display_total", None)
        else:
            total = {}
            for count in counts:
                for key, value in count.items():
                    total[key] = total.get(key, 0) + value
            object.__setattr__(self, "display_total", total)
        return self


class LongSlotAsset(Model):
    # Store-verified local payloads: candidate ID -> (relative path, hash, bytes).
    # None preserves the legacy local inline adapter; {} means hydration was
    # attempted and no fallback to unverified inline candidate bytes is allowed.
    _candidate_payloads: dict[str, tuple[str, str, bytes]] | None = PrivateAttr(default=None)

    slot_id: Id
    source: Literal["source", "generate"]
    source_status: Literal["available", "missing", "invalid"] | None = None
    status: AssetStatus | None = None
    selected_candidate_id: Id | None = None
    candidates: list[dict] = Field(default_factory=list)
    image: bytes | None = Field(default=None, repr=False, exclude=True)
    file_path: str | None = None
    sha256: str | None = None
    asset_revision: int = Field(default=0, ge=0)
    audit: QualityReport | LongImageAudit | None = None
    error_info: ErrorInfo | None = None

    @model_validator(mode="after")
    def source_metadata(self):
        if self.source == "source":
            if (
                self.status is not None
                or self.audit is not None
                or self.candidates
                or self.selected_candidate_id
            ):
                raise ValueError(
                    "source assets cannot claim generated status, candidates or visual audit"
                )
        elif self.source_status is not None:
            raise ValueError("generated asset cannot claim source_status")
        return self


class LongImageArtifact(Model):
    artifact_id: Id
    kind: Literal["section", "page", "slice"]
    status: AssetStatus = "failed"
    availability: Literal["current", "stale", "absent"] = "absent"
    image: bytes | None = Field(default=None, repr=False, exclude=True)
    file_path: str | None = None
    sha256: str | None = None
    width: int = Field(default=0, ge=0)
    height: int = Field(default=0, ge=0)
    delivery_revision: int = Field(default=0, ge=0)
    dependencies: list[Id] = Field(default_factory=list)
    layout: dict = Field(default_factory=dict)


class RenderedLongImage(Model):
    sections: list[LongImageArtifact]
    page: LongImageArtifact
    slices: list[LongImageArtifact]
    layout: dict = Field(default_factory=dict)
    checks: list[dict] = Field(default_factory=list)
    render_fingerprint: str


class LongImageResult(Model):
    schema_version: Literal["1.0"] = "1.0"
    task_type: Literal["long_image"] = "long_image"
    run_id: Id | None = None
    run_dir: str | None = None
    status: AssetStatus
    plan: LongImagePlan | None = None
    issues: list[LongImageIssue] = Field(default_factory=list)
    error_info: ErrorInfo | None = None
    image_assets: list[LongSlotAsset] = Field(default_factory=list)
    sections: list[LongImageArtifact] = Field(default_factory=list)
    page: LongImageArtifact | None = None
    slices: list[LongImageArtifact] = Field(default_factory=list)
    page_checks: list[dict] = Field(default_factory=list)
    usage: LongImageUsage = Field(default_factory=LongImageUsage)
    warnings: list[str] = Field(default_factory=list)
    output_errors: list[ErrorInfo] = Field(default_factory=list)
    delivery_revision: int = Field(default=0, ge=0)
