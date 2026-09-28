"""Validated runtime contracts; importing this module has no I/O side effects."""

import re
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator

Id = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Score = Annotated[int, Field(strict=True, ge=0, le=100)]
Role = Literal["auto", "identity", "detail", "accessory", "scene", "style"]
Platform = Literal[
    "amazon", "taobao", "tmall", "jd", "pinduoduo", "shopee", "lazada", "shein", "temu"
]
OutputType = Literal["main_image", "detail_page", "pdd_white_background"]
Presence = Literal["present", "absent", "not_applicable"]
PLATFORMS = ("amazon", "taobao", "tmall", "jd", "pinduoduo", "shopee", "lazada", "shein", "temu")
OUTPUTS = ("main_image", "detail_page", "pdd_white_background")


class Model(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_assignment=True)


def unique(values, label="IDs"):
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")
    return values


class ImageSource(Model):
    path: Path | None = None
    url: str | None = None
    data: bytes | None = Field(default=None, repr=False, exclude=True)

    @model_validator(mode="after")
    def source(self):
        if sum(x is not None for x in (self.path, self.url, self.data)) != 1:
            raise ValueError("exactly one image source required")
        if self.url:
            url = urlsplit(self.url)
            if (
                url.scheme not in ("http", "https")
                or not url.hostname
                or url.username
                or url.password
            ):
                raise ValueError("HTTP(S) URL without credentials required")
        elif self.url is not None:
            raise ValueError("empty URL")
        return self


class MaterialInput(Model):
    material_id: Id
    source: ImageSource
    role_hint: Role = "auto"
    subject_hint: str | None = None
    element_hints: list[str] = Field(default_factory=list)


class SelectionSpec(Model):
    mode: Literal["auto", "explicit"] = "auto"
    subject_ids: list[Id] = Field(default_factory=list)
    required_element_ids: list[Id] = Field(default_factory=list)
    preferred_element_ids: list[Id] = Field(default_factory=list)
    excluded_element_ids: list[Id] = Field(default_factory=list)

    @model_validator(mode="after")
    def lists(self):
        unique(self.subject_ids)
        unique(self.required_element_ids + self.preferred_element_ids + self.excluded_element_ids)
        if self.mode == "auto" and any(
            (
                self.subject_ids,
                self.required_element_ids,
                self.preferred_element_ids,
                self.excluded_element_ids,
            )
        ):
            raise ValueError("auto selection cannot contain IDs")
        if self.mode == "explicit" and not self.subject_ids:
            raise ValueError("explicit selection requires subjects")
        return self


class CreationRequest(Model):
    product_name: str
    category: str
    platforms: list[Platform]
    output_types: list[OutputType]
    materials: list[MaterialInput] = Field(default_factory=list, max_length=8)
    creative_brief: str | None = None
    selection: SelectionSpec = Field(default_factory=SelectionSpec)
    product_image: ImageSource | None = None
    reference_images: list[ImageSource] = Field(default_factory=list, max_length=4)
    style_hint: str | None = None
    presentation_mode: Literal["auto", "model_wear", "product_only"] = "auto"
    model_preference: Literal["auto", "female", "male", "no_face"] = "auto"
    market: str | None = None
    aspect_ratio: Literal["auto", "1:1", "3:4", "4:5", "9:16", "16:9", "4:1", "1:4"] = "auto"
    image_size: Literal["1K", "2K", "4K"] = "2K"
    image_model: Literal["pro", "fast", "base"] = "pro"
    output_dir: Path | None = None
    request_id: str | None = None

    @field_validator("product_name", "category")
    @classmethod
    def nonempty(cls, value):
        if not value.strip():
            raise ValueError("must not be blank")
        return value.strip()

    @field_validator("platforms", "output_types", mode="before")
    @classmethod
    def enumerations(cls, values, info):
        values = list(dict.fromkeys(v.strip().lower() for v in values))
        allowed = PLATFORMS if info.field_name == "platforms" else OUTPUTS
        if not values or any(v not in allowed for v in values):
            raise ValueError("empty or unknown platform/output")
        return values

    @field_validator("request_id")
    @classmethod
    def safe_component(cls, value):
        if value is not None:
            if (
                not value
                or len(value) > 128
                or ".." in value
                or value[-1] in ". "
                or re.search(r'[<>:"/\\|?*\x00-\x1f]', value)
                or re.fullmatch(r"(?i)(CON|PRN|AUX|NUL|COM[1-9¹²³]|LPT[1-9¹²³])(?:\..*)?", value)
            ):
                raise ValueError("request_id must be a Windows-safe path component")
        return value

    @model_validator(mode="after")
    def compatible(self):
        if self.materials:
            if self.product_image is not None or self.reference_images:
                raise ValueError("materials and legacy inputs are mutually exclusive")
            unique([m.material_id for m in self.materials])
        elif self.product_image is None:
            raise ValueError("at least one material required")
        if "materials" in self.model_fields_set and not self.materials:
            raise ValueError("materials must contain 1–8 items")
        if "pdd_white_background" in self.output_types and "pinduoduo" not in self.platforms:
            raise ValueError("white background requires pinduoduo")
        if self.market is None:
            p = self.platforms[0]
            object.__setattr__(
                self,
                "market",
                "CN"
                if p in ("taobao", "tmall", "jd", "pinduoduo")
                else "SG"
                if p in ("shopee", "lazada")
                else "US",
            )
        else:
            object.__setattr__(self, "market", self.market.strip().upper())
        return self

    def normalized_materials(self):
        if self.materials:
            return tuple(self.materials)
        return (
            MaterialInput(material_id="product", source=self.product_image, role_hint="identity"),
            *(
                MaterialInput(material_id=f"ref-{i}", source=s, role_hint="style")
                for i, s in enumerate(self.reference_images, 1)
            ),
        )


class ErrorInfo(Model):
    code: str
    kind: Literal[
        "validation",
        "configuration",
        "input",
        "stale_analysis",
        "transport",
        "http",
        "protocol",
        "capability",
        "compliance",
        "quality",
        "output",
    ]
    message: str
    retryable: bool = False
    status_code: int | None = None


class MaterialObservation(Model):
    material_id: Id
    sha256: str
    observed_role: Role
    summary: str


class Evidence(Model):
    material_id: Id
    observation: str = Field(min_length=1)


class Fact(Model):
    fact_id: Id
    subject_id: Id | None
    description: str = Field(min_length=1)
    evidence: list[Evidence] = Field(min_length=1)
    confidence: float = Field(ge=0, le=1)


class Subject(Model):
    subject_id: Id
    material_ids: list[Id] = Field(min_length=1)
    identity_fact_ids: list[Id] = Field(min_length=1)
    representative_material_id: Id
    matches_product: StrictBool


class Element(Model):
    element_id: Id
    kind: Literal["identity", "detail", "accessory", "scene", "style"]
    subject_id: Id | None
    description: str = Field(min_length=1)
    fact_ids: list[Id] = Field(min_length=1)


class SelectionProposal(Model):
    primary_subject_id: Id | None
    subject_ids: list[Id]
    required_element_ids: list[Id]
    preferred_element_ids: list[Id]
    excluded_element_ids: list[Id]


class IntentConstraint(Model):
    constraint_id: Id
    kind: Literal["placement", "co_presence", "exclusion", "appearance", "atmosphere"]
    subject_ids: list[Id]
    element_ids: list[Id]
    instruction: str = Field(min_length=1)
    source: Literal["brief", "hint"]
    source_quote: str = Field(min_length=1)
    source_material_id: Id | None
    priority: Literal["required", "preferred"]


class IssueOption(Model):
    id: Id
    label: str
    selection: SelectionSpec | None


class Issue(Model):
    issue_id: Id
    code: str
    resolution: Literal["selection", "recheck", "reanalyze"]
    message: str
    subject_ids: list[Id] = Field(default_factory=list)
    material_ids: list[Id] = Field(default_factory=list)
    element_ids: list[Id] = Field(default_factory=list)
    fact_ids: list[Id] = Field(default_factory=list)
    options: list[IssueOption] = Field(default_factory=list)


class IntentAnalysis(Model):
    proposal: SelectionProposal
    constraints: list[IntentConstraint]
    focus_element_ids: list[Id]
    unmet_requirements: list[Issue]


class MaterialAnalysis(Model):
    schema_version: Literal["1.0"] = "1.0"
    analysis_id: Id
    fingerprint: str
    status: Literal["ready", "needs_input", "failed"]
    materials: list[MaterialObservation]
    subjects: list[Subject]
    facts: list[Fact]
    elements: list[Element]
    intent: IntentAnalysis | None
    issues: list[Issue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    error_info: ErrorInfo | None = None

    @model_validator(mode="after")
    def references(self):
        tables = {}
        for name, items, key in (
            ("material", self.materials, "material_id"),
            ("subject", self.subjects, "subject_id"),
            ("fact", self.facts, "fact_id"),
            ("element", self.elements, "element_id"),
        ):
            ids = [getattr(x, key) for x in items]
            unique(ids, name)
            tables[name] = set(ids)

        def refs(values, table):
            unique(values)
            if not set(values) <= tables[table]:
                raise ValueError(f"unknown {table} reference")

        facts = {f.fact_id: f for f in self.facts}
        for f in self.facts:
            refs([e.material_id for e in f.evidence], "material")
            refs([f.subject_id] if f.subject_id else [], "subject")
        for s in self.subjects:
            refs(s.material_ids, "material")
            refs(s.identity_fact_ids, "fact")
            if s.representative_material_id not in s.material_ids:
                raise ValueError("representative image outside subject")
            if any(facts[f].subject_id != s.subject_id for f in s.identity_fact_ids):
                raise ValueError("identity fact belongs to another subject")
            if any(
                e.material_id not in s.material_ids
                for f in self.facts
                if f.subject_id == s.subject_id
                for e in f.evidence
            ):
                raise ValueError("subject fact evidence outside subject materials")
        for e in self.elements:
            refs(e.fact_ids, "fact")
            refs([e.subject_id] if e.subject_id else [], "subject")
            if e.subject_id and any(facts[f].subject_id != e.subject_id for f in e.fact_ids):
                raise ValueError("element crosses subject facts")
        if self.intent is None:
            if self.status != "failed":
                raise ValueError("intent required unless discovery failed")
            return self
        p = self.intent.proposal
        if self.status == "ready" and (not p.subject_ids or p.primary_subject_id is None):
            raise ValueError("ready analysis requires a selected primary subject")
        refs(p.subject_ids, "subject")
        if p.primary_subject_id is not None and p.primary_subject_id not in p.subject_ids:
            raise ValueError("primary subject not selected")
        refs(p.required_element_ids + p.preferred_element_ids + p.excluded_element_ids, "element")
        selected = set(p.required_element_ids + p.preferred_element_ids)
        if any(
            e.subject_id and e.subject_id not in p.subject_ids
            for e in self.elements
            if e.element_id in selected
        ):
            raise ValueError("proposal element outside selected subjects")
        refs(self.intent.focus_element_ids, "element")
        unique([c.constraint_id for c in self.intent.constraints])
        for c in self.intent.constraints:
            refs(c.subject_ids, "subject")
            refs(c.element_ids, "element")
            if (c.source == "brief" and c.source_material_id is not None) or (
                c.source == "hint" and c.source_material_id not in tables["material"]
            ):
                raise ValueError("invalid constraint source")
        issues = self.issues + self.intent.unmet_requirements
        unique([i.issue_id for i in issues])
        for i in issues:
            for kind in tables:
                refs(getattr(i, f"{kind}_ids"), kind)
            unique([o.id for o in i.options])
            for option in i.options:
                if option.selection:
                    if option.selection.mode != "explicit" or i.resolution == "reanalyze":
                        raise ValueError("option must be actionable explicit selection")
                    refs(option.selection.subject_ids, "subject")
                    refs(
                        option.selection.required_element_ids
                        + option.selection.preferred_element_ids
                        + option.selection.excluded_element_ids,
                        "element",
                    )
        return self

    def validate_intent_source(self, request):
        materials = {m.material_id: m for m in request.normalized_materials()}
        if self.intent:
            for c in self.intent.constraints:
                if c.source == "brief":
                    sources = [request.creative_brief or ""]
                else:
                    m = materials.get(c.source_material_id)
                    sources = [m.subject_hint or "", *m.element_hints] if m else []
                if not any(c.source_quote in s for s in sources):
                    raise ValueError("constraint source_quote absent from user input")


class LoadedMaterial(Model):
    material_id: Id
    data: bytes = Field(repr=False, exclude=True)
    sha256: str
    order: int
    role_hint: Role


class ResolvedSelection(SelectionProposal):
    constraints: list[IntentConstraint]
    focus_element_ids: list[Id]


class IssueResolution(Model):
    issue_id: Id
    status: Literal["resolved", "irrelevant", "unresolved"]
    reason: str


class SelectionDraft(Model):
    candidate: ResolvedSelection | None
    pending_issues: list[Issue] = Field(default_factory=list)
    blocking_issues: list[Issue] = Field(default_factory=list)
    issue_resolutions: list[IssueResolution] = Field(default_factory=list)


class SelectionResolution(Model):
    status: Literal["ready", "needs_input", "failed"]
    selection: ResolvedSelection | None
    issues: list[Issue] = Field(default_factory=list)
    issue_resolutions: list[IssueResolution] = Field(default_factory=list)
    error_info: ErrorInfo | None = None


class SubjectCheck(Model):
    subject_id: Id
    score: Score
    same_product: StrictBool
    reason: str


class PresenceCheck(Model):
    presence: Presence
    fidelity_score: Score | None
    reason: str

    @model_validator(mode="after")
    def score_presence(self):
        if (self.presence == "present") != (self.fidelity_score is not None):
            raise ValueError("only present checks require a score")
        return self


class FactCheck(PresenceCheck):
    fact_id: Id


class ElementCheck(PresenceCheck):
    element_id: Id


class ConstraintCheck(Model):
    constraint_id: Id
    satisfied: StrictBool
    reason: str


class EvidenceValidation(Model):
    outcome: Literal["verified", "needs_input", "failed"]
    subject_checks: list[SubjectCheck]
    fact_checks: list[FactCheck]
    intent_valid: StrictBool
    issue_resolutions: list[IssueResolution]
    issues: list[Issue] = Field(default_factory=list)


class AssetTarget(Model):
    platform: str
    output_type: str
    variant: str | None = None
    aspect_ratio: str
    presentation_mode: Literal["product_only", "model_wear"]
    model_preference: Literal["auto", "female", "male", "no_face"] = "auto"

    @property
    def target_key(self):
        return f"{self.platform}.{self.output_type}.{self.variant or 'default'}"


class Requirement(Model):
    target_kind: Literal["subject", "fact", "element", "constraint"]
    target_id: Id
    origin: Literal["default_identity", "user_required", "brief_required", "shot_rule", "preferred"]
    applicability: Literal["must_show", "preserve_if_visible", "not_applicable"]
    reason: str


class ReferenceBinding(Model):
    index: int
    material_id: Id
    fact_ids: list[Id]
    element_ids: list[Id]
    role: str


class AssetElementPlan(Model):
    target_key: str
    legacy_input: bool = False
    subject_ids: list[Id]
    focus_element_id: Id | None = None
    required_element_ids: list[Id] = Field(default_factory=list)
    preferred_element_ids: list[Id] = Field(default_factory=list)
    excluded_element_ids: list[Id] = Field(default_factory=list)
    excluded_reasons: dict[str, str] = Field(default_factory=dict)
    constraints: list[IntentConstraint] = Field(default_factory=list)
    requirements: list[Requirement] = Field(default_factory=list)
    required_fact_ids: list[Id] = Field(default_factory=list)
    generation_material_ids: list[Id] = Field(default_factory=list)
    audit_material_ids: list[Id] = Field(default_factory=list)
    reference_bindings: list[ReferenceBinding] = Field(default_factory=list)
    subject_presentations: dict[str, str] = Field(default_factory=dict)
    text_only_element_ids: list[Id] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class ProductInputAudit(Model):
    matches: StrictBool
    observed_product: str
    reason: str


class PreparedAnalysis(Model):
    materials: tuple[LoadedMaterial, ...]
    analysis: MaterialAnalysis


class PreparedContext(PreparedAnalysis):
    selection: ResolvedSelection
    input_check: ProductInputAudit
    presentation_mode: str
    product_attributes: dict = Field(default_factory=dict)
    style_prompt: str | None = None
    warnings: list[str] = Field(default_factory=list)


class ComplianceResult(Model):
    blocked: bool
    prompt: str
    warnings: list[str] = Field(default_factory=list)


class PixelChecks(Model):
    passed: bool
    width: int
    height: int
    reason: str
    white_border_ratio: float | None = None
    export_bytes: int | None = None


class GeneratedImageAudit(Model):
    subject_checks: list[SubjectCheck]
    fact_checks: list[FactCheck]
    element_checks: list[ElementCheck]
    constraint_checks: list[ConstraintCheck]
    visual_quality: Score
    platform_compliance: Score
    garment_fusion: Score
    model_preference: Score
    output_intent: Score
    passed: StrictBool
    reason: str


class FocusedProductAudit(Model):
    subject_checks: list[SubjectCheck]


class GarmentStructureAudit(Model):
    items: list[dict[str, str | int | list[str]]]


class QualityReport(GeneratedImageAudit):
    model_passed: bool
    original_subject_checks: list[SubjectCheck] = Field(default_factory=list)
    reviewed_subject_checks: list[SubjectCheck] = Field(default_factory=list)
    deterministic_checks: PixelChecks
    failed_checks: list[str]


class DetailSetAudit(Model):
    platform: str
    passed: StrictBool
    distinctiveness: Score | None
    role_coverage: Score | None
    issues: list[str]
    reason: str
    error: str | None = None
    error_info: ErrorInfo | None = None


class GenerationReference(Model):
    material_id: Id | None = None
    stage_id: Id | None = None
    data: bytes = Field(repr=False, exclude=True)
    role: str
    element_ids: list[Id] = Field(default_factory=list)

    @model_validator(mode="after")
    def one_id(self):
        if (self.material_id is None) == (self.stage_id is None):
            raise ValueError("one reference ID required")
        return self


class GenerationAttempt(Model):
    stage: Literal["standard", "strict", "staged_scene", "staged_fusion"]
    model: str
    reference_ids: list[str]
    prompt: str
    outcome: Literal["succeeded", "failed"]
    error_info: ErrorInfo | None = None


class Asset(Model):
    asset_id: str
    platform: str
    output_type: str
    variant: str | None = None
    status: Literal["succeeded", "failed"]
    image: bytes | None = Field(default=None, repr=False, exclude=True)
    file_path: str | None = None
    model: str
    prompt: str = ""
    generation_mode: Literal["standard", "strict", "staged"] = "standard"
    quality: QualityReport | None = None
    error: str | None = None
    error_info: ErrorInfo | None = None
    element_plan: AssetElementPlan | None = None
    element_checks: list[ElementCheck] = Field(default_factory=list)
    reference_bindings: list[ReferenceBinding] = Field(default_factory=list)
    attempts: list[GenerationAttempt] = Field(default_factory=list)


class CreationResult(Model):
    schema_version: Literal["1.0"] = "1.0"
    request_id: str | None = None
    status: Literal["succeeded", "partial", "failed", "needs_input"]
    presentation_mode: str = "product_only"
    style_prompt: str | None = None
    product_attributes: dict = Field(default_factory=dict)
    input_check: ProductInputAudit
    warnings: list[str] = Field(default_factory=list)
    assets: list[Asset] = Field(default_factory=list)
    detail_set_audits: list[DetailSetAudit] = Field(default_factory=list)
    analysis: MaterialAnalysis | None = None
    issues: list[Issue] = Field(default_factory=list)
    error_info: ErrorInfo | None = None
    output_errors: list[ErrorInfo] = Field(default_factory=list)
