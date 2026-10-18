"""Truth-preserving adapter for long-image product discovery and scoped assets."""

from dataclasses import dataclass, field

from .completion import completion_problems, maybe_complete_analysis
from .config import AgentConfig
from .errors import AgentError, ProviderError, StaleAnalysisError
from .long_image_models import LongImageRequirement
from .long_image_requirements import validate_long_image_plan
from .long_image_store import content_hash
from .marketing import creative_input
from .models import (
    AssetElementPlan,
    AssetTarget,
    ErrorInfo,
    GenerationReference,
    IntentConstraint,
    MaterialAnalysis,
    PreparedContext,
    ProductInputAudit,
    Requirement,
)
from .platforms import generation_instruction
from .prompt import build_prompt, resolve_presentation
from .quality import validate_check_ids
from .selection import (
    analysis_fingerprint,
    bind_generation_references,
    compile_element_plan,
    finalize_selection,
    normalize_discovery_attribution,
    resolve_selection,
    select_references,
)


@dataclass(frozen=True)
class LongProductEvidenceRequest:
    """Only real product fields; never constructs a legacy CreationRequest."""

    request: object
    visual_brief: str | None = None

    @property
    def product_name(self):
        return self.request.product_context.product_name

    @property
    def category(self):
        return self.request.product_context.category

    @property
    def creative_brief(self):
        return self.request.content_brief if self.visual_brief is None else self.visual_brief

    @property
    def style_hint(self):
        return self.request.style_hint

    @property
    def selection(self):
        return self.request.selection

    @property
    def materials(self):
        return self.request.materials

    @property
    def platforms(self):
        value = self.request.product_context.platform
        return [value] if value else []

    @property
    def output_types(self):
        return ["detail_page"]

    @property
    def presentation_mode(self):
        return self.request.product_context.presentation_mode

    @property
    def model_preference(self):
        return self.request.product_context.model_preference

    def normalized_materials(self):
        return self.request.normalized_materials()


@dataclass
class PreparedLongProduct:
    materials: tuple
    analysis: MaterialAnalysis
    status: str
    context: PreparedContext | None = None
    evidence: object | None = None
    issues: list = field(default_factory=list)
    error_info: ErrorInfo | None = None
    config: AgentConfig = field(default_factory=AgentConfig)


@dataclass(frozen=True)
class PreparedLongSlot:
    references: tuple[GenerationReference, ...]
    prompt: str
    target: AssetTarget
    requirements: tuple[LongImageRequirement, ...]
    element_plan: AssetElementPlan | None = None
    context: PreparedContext | None = None
    evidence_request: LongProductEvidenceRequest | None = None


def long_discovery_fingerprint(request, materials):
    adapter = LongProductEvidenceRequest(request)
    return content_hash(
        {
            "version": "long-product-1",
            "discovery": analysis_fingerprint(adapter, materials),
            "title": request.title,
            "specs": [s.model_dump(mode="json") for s in request.product_specs],
            "service_notes": request.service_notes,
            "required_copy": [c.model_dump(mode="json") for c in request.required_copy],
        }
    )


def _validate_materials(request, materials):
    import hashlib

    if [m.material_id for m in materials] != [m.material_id for m in request.materials] or any(
        hashlib.sha256(m.data).hexdigest() != m.sha256 for m in materials
    ):
        raise AgentError(
            "product material IDs or hashes differ from actual inputs", code="plan_invalid"
        )


def _context(request, materials, analysis, selection):
    selected = [s for s in analysis.subjects if s.subject_id in selection.subject_ids]
    if not selected or any(not s.matches_product for s in selected):
        raise AgentError(
            "selected product does not match declared context", code="input_mismatch", kind="input"
        )
    observed = "; ".join(
        f.description for f in analysis.facts if f.subject_id in selection.subject_ids
    )
    adapter = LongProductEvidenceRequest(request)
    return PreparedContext(
        materials=tuple(materials),
        analysis=analysis,
        selection=selection,
        input_check=ProductInputAudit(
            matches=True, observed_product=observed, reason="所选真实商品证据已核对"
        ),
        presentation_mode=resolve_presentation(adapter, observed),
        user_input=creative_input(adapter),
        warnings=list(analysis.warnings),
    )


async def _review_product_evidence(adapter, materials, analysis, draft, vision):
    if draft.candidate is None or draft.blocking_issues:
        return None
    evidence = await vision.validate_evidence(adapter, materials, analysis, draft)
    expected = {
        "subject_id": draft.candidate.subject_ids,
        "issue_id": [i.issue_id for i in draft.pending_issues],
    }
    for key, ids in expected.items():
        rows = (
            evidence.issue_resolutions
            if key == "issue_id"
            else getattr(evidence, key.removesuffix("_id") + "_checks")
        )
        validate_check_ids(rows, ids, key)
    return evidence


async def prepare_long_product(request, materials, *, analysis, vision, config=None):
    """Caller owns the enclosing vision call_scope for all discovery/evidence POSTs."""
    if request.scene != "product_detail":
        raise AgentError("product adapter requires product_detail")
    config = config or getattr(vision, "config", None) or AgentConfig()
    materials = tuple(materials)
    _validate_materials(request, materials)
    if len(materials) > config.vision_image_limit:
        raise ProviderError(
            "product discovery exceeds vision capacity",
            code="vision_capacity_exceeded",
            kind="capability",
        )
    adapter = LongProductEvidenceRequest(request)
    supplied = analysis is not None
    if request.selection.mode == "explicit" and not supplied:
        raise AgentError(
            "explicit selection requires the same analysis snapshot", code="stale_analysis"
        )
    fingerprint = long_discovery_fingerprint(request, materials)
    if supplied:
        analysis = MaterialAnalysis.model_validate(
            analysis.model_dump() if isinstance(analysis, MaterialAnalysis) else analysis
        )
        if analysis.fingerprint != fingerprint:
            raise StaleAnalysisError(
                "analysis does not match long-image discovery content or materials"
            )
    else:
        discovered = await vision.analyze_materials(adapter, materials)
        analysis = MaterialAnalysis.model_validate(discovered.model_dump())
        analysis = normalize_discovery_attribution(analysis, adapter)
        if [m.material_id for m in analysis.materials] != [m.material_id for m in materials]:
            raise ProviderError("discovery material IDs/order mismatch")
        analysis.fingerprint = fingerprint
        for observed, material in zip(analysis.materials, materials, strict=True):
            observed.sha256 = material.sha256
    if {m.material_id: m.sha256 for m in analysis.materials} != {
        m.material_id: m.sha256 for m in materials
    }:
        raise StaleAnalysisError("analysis hashes differ from source bytes")
    if analysis.status == "failed" or analysis.error_info is not None:
        return PreparedLongProduct(
            materials,
            analysis,
            "failed",
            error_info=analysis.error_info
            or ErrorInfo(code="provider_protocol", kind="protocol", message="素材分析失败"),
            config=config,
        )
    analysis = await maybe_complete_analysis(adapter, materials, analysis, vision, config)
    draft = resolve_selection(adapter, analysis)
    evidence = None
    # A supplied snapshot is always rechecked unless a trusted planning record was resolved
    # by prepare_long_plan, which uses product_from_plan instead of this paid path.
    if draft.candidate and not draft.blocking_issues and (supplied or draft.pending_issues):
        evidence = await _review_product_evidence(adapter, materials, analysis, draft, vision)
    resolution = finalize_selection(draft, evidence)
    if resolution.status == "needs_input" and evidence is not None and resolution.issues:
        completed = await maybe_complete_analysis(
            adapter, materials, analysis, vision, config, additional_issues=resolution.issues
        )
        if completed is not analysis:
            analysis = completed
            draft = resolve_selection(adapter, analysis)
            evidence = await _review_product_evidence(adapter, materials, analysis, draft, vision)
            resolution = finalize_selection(draft, evidence)
            if resolution.status == "needs_input" and completion_problems(
                adapter, analysis, additional_issues=resolution.issues
            ):
                raise ProviderError(
                    "evidence review still reports unresolved creative planning gaps"
                )
    product = PreparedLongProduct(
        materials,
        analysis,
        resolution.status,
        evidence=evidence,
        issues=resolution.issues,
        error_info=resolution.error_info,
        config=config,
    )
    if resolution.status == "ready":
        product.context = _context(request, materials, analysis, resolution.selection)
    return product


def product_from_plan(request, materials, plan, analysis, config=None):
    """Pure local reconstruction AFTER the caller validated/verified the ready plan."""
    _validate_materials(request, materials)
    validate_long_image_plan(
        request,
        plan,
        material_hashes={m.material_id: m.sha256 for m in materials},
        analysis=analysis,
    )
    if analysis.fingerprint != long_discovery_fingerprint(request, materials):
        raise StaleAnalysisError("analysis discovery fingerprint differs from request")
    draft = resolve_selection(LongProductEvidenceRequest(request), analysis)
    if draft.candidate != plan.resolved_selection or draft.blocking_issues:
        raise AgentError(
            "plan selection differs from frozen discovery and user selection", code="plan_invalid"
        )
    return PreparedLongProduct(
        tuple(materials),
        analysis,
        "ready",
        context=_context(request, materials, analysis, plan.resolved_selection),
        config=config or AgentConfig(),
    )


def slot_visual_requirements(slot, plan):
    return tuple(
        r
        for r in plan.requirements
        if r.kind == "slot_visual" and r.requirement_id in slot.requirement_ids
    )


def validate_slot_binding(slot, plan):
    if not any(saved == slot for saved in plan.image_slots):
        raise AgentError("slot does not match its frozen plan", code="plan_invalid")


def compile_long_generic_slot(request, slot, plan, materials, config=None):
    """Compile a generic visual without product selection, scores or legacy context."""
    from .long_image_vision import validate_slot_copy_isolation
    from .marketing import MARKETING_POLICY

    if request.scene == "product_detail" or slot.source != "generate":
        raise AgentError(
            "generic compilation requires a nonproduct generated slot", code="plan_invalid"
        )
    config = config or AgentConfig()
    validate_slot_binding(slot, plan)
    _validate_materials(request, materials)
    validate_long_image_plan(
        request, plan, material_hashes={m.material_id: m.sha256 for m in materials}
    )
    validate_slot_copy_isolation(plan)
    if len(slot.material_ids) > config.generation_reference_limit:
        raise ProviderError(
            "required generic source evidence exceeds reference capacity",
            code="reference_capacity_exceeded",
            kind="capability",
        )
    by_id = {m.material_id: m for m in materials}
    requirements = slot_visual_requirements(slot, plan)
    references = tuple(
        GenerationReference(material_id=mid, data=by_id[mid].data, role="source", element_ids=[])
        for mid in slot.material_ids
    )
    target = AssetTarget(
        platform="",
        output_type="long_image",
        variant=slot.role,
        aspect_ratio=slot.aspect_ratio,
        presentation_mode="product_only",
    )
    prompt = (
        "Create ONLY this generic illustration/photo. Preserve referenced subjects/marks; "
        "do not invent facts or text/watermarks. Page prose and dates are drawn locally. "
        "Use source images for the requested purpose, not product identity or clothing constraints.\n"
        + MARKETING_POLICY
        + "\n"
        + plan.visual_direction
        + "\n"
        + "\n".join([*(r.text for r in requirements), slot.prompt])
    )
    return PreparedLongSlot(references, prompt, target, requirements)


def compile_long_product_slot(request, slot, plan, product):
    from .long_image_vision import validate_slot_copy_isolation

    if slot.source != "generate" or product.status != "ready" or product.context is None:
        raise AgentError(
            "only generated slots with ready product evidence can be compiled", code="plan_invalid"
        )
    validate_slot_binding(slot, plan)
    _validate_materials(request, product.materials)
    validate_long_image_plan(
        request,
        plan,
        material_hashes={m.material_id: m.sha256 for m in product.materials},
        analysis=product.analysis,
    )
    if (
        plan.analysis_ref is None
        or plan.analysis_ref.content_hash != content_hash(product.analysis.model_dump(mode="json"))
        or plan.resolved_selection != product.context.selection
    ):
        raise AgentError("slot product snapshot does not match plan", code="plan_invalid")
    validate_slot_copy_isolation(plan, analysis=product.analysis)
    requirements = slot_visual_requirements(slot, plan)
    context = product.context.model_copy(deep=True)
    selection = context.selection
    # The long plan allocates creative requirements to individual slots. Discovery
    # constraints describe the whole brief and cannot remain per-slot obligations.
    # Keep selected subjects/elements and their physical evidence; shared styling
    # is supplied by visual_direction below, while audit IDs/priorities come from
    # this slot's allocated requirements.
    selection.constraints = []
    # Program-rendered copy never becomes a visual requirement. Native labels remain
    # visible facts and selected elements in the original evidence snapshot.
    for requirement in requirements:
        selection.constraints.append(
            IntentConstraint(
                constraint_id=requirement.requirement_id,
                kind="appearance",
                subject_ids=[],
                element_ids=[],
                instruction=requirement.text,
                source="brief",
                source_quote=requirement.text,
                source_material_id=None,
                priority="required" if requirement.required else "preferred",
            )
        )
    brief = "\n".join([slot.prompt, *(r.text for r in requirements)])
    adapter = LongProductEvidenceRequest(request, visual_brief=brief)
    context.user_input = creative_input(adapter)
    context.shoot_plan = {"visual_direction": plan.visual_direction}
    target = AssetTarget(
        platform=request.product_context.platform or "",
        output_type="detail_page",
        variant=slot.role if slot.role in {"scene", "feature", "closeup"} else "hero",
        aspect_ratio=slot.aspect_ratio,
        presentation_mode=context.presentation_mode,
        model_preference=request.product_context.model_preference,
    )
    element_plan = compile_element_plan(adapter, target, context, product.config)
    # Physical native print is product appearance even when discovery proposed its
    # element as optional style. It may be out of a crop, but visible print cannot
    # be silently exempted from fidelity checks as optional scene decoration.
    visible_subjects = {
        r.target_id
        for r in element_plan.requirements
        if r.target_kind == "subject" and r.applicability != "not_applicable"
    }
    native_facts = {
        f.fact_id
        for f in context.analysis.facts
        if f.subject_id in visible_subjects
        and f.effective_verifiability == "visible_appearance"
        and any(e.source_type == "product_label" for e in f.evidence)
    }
    for fid in native_facts:
        requirement = next(
            (
                r
                for r in element_plan.requirements
                if r.target_kind == "fact" and r.target_id == fid
            ),
            None,
        )
        if requirement is None:
            element_plan.requirements.append(
                Requirement(
                    target_kind="fact",
                    target_id=fid,
                    origin="default_identity",
                    applicability="preserve_if_visible",
                    reason="Preserve physical native labels and artwork when visible",
                )
            )
        elif requirement.origin == "preferred":
            requirement.origin = "default_identity"
    element_plan.required_fact_ids = list(
        dict.fromkeys([*element_plan.required_fact_ids, *sorted(native_facts)])
    )
    native_materials = {
        e.material_id
        for f in context.analysis.facts
        if f.fact_id in native_facts
        for e in f.evidence
    }
    element_plan.audit_material_ids = [
        m.material_id
        for m in product.materials
        if m.material_id in set(element_plan.audit_material_ids) | native_materials
    ]
    references = select_references(
        element_plan,
        context.analysis,
        product.materials,
        limit=product.config.generation_reference_limit,
    )
    element_plan = bind_generation_references(element_plan, context.analysis, references)
    prompt = (
        build_prompt(adapter, target, context, element_plan)
        + "\n"
        + generation_instruction(target.platform, target.output_type).replace(
            "3:4", slot.aspect_ratio
        )
        + "\nThis slot uses ratio "
        + slot.aspect_ratio
        + ". "
        + "Draw only this slot's visual. Page title, prose, dates and tables are drawn locally. "
        + "Do not add page copy; preserve all product-native labels and artwork.\n"
        + plan.visual_direction
        + "\n"
        + brief
    )
    return PreparedLongSlot(
        references, prompt, target, requirements, element_plan, context, adapter
    )
