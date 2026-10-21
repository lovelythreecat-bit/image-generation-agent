"""Long-image planning and strict slot audits over the existing vision protocol."""

import json
import re
from uuid import uuid4

from pydantic import Field

from .errors import AgentError, ProviderError
from .images import generated_image
from .long_image_models import (
    LongAnalysisRef,
    LongImageAudit,
    LongImageIssue,
    LongImagePlan,
    LongImagePlanningResult,
)
from .long_image_requirements import planning_fingerprint, validate_long_image_plan
from .long_image_store import content_hash
from .marketing import MARKETING_POLICY
from .models import GeneratedImageAudit, Model
from .quality import check_pixels, evaluate_quality, validate_check_ids
from .selection import resolve_selection
from .vision import VisionClient


class LongPlanResponse(Model):
    status: str
    plan: LongImagePlan | None = None
    issues: list[LongImageIssue] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


def _can_complete_visual_gaps(issues):
    """Only clearly visual choices, with no source/selection/factual blockers."""
    codes = {
        "missing_style",
        "missing_layout",
        "missing_composition",
        "missing_lighting",
        "missing_scene",
        "missing_palette",
        "missing_mood",
        "missing_visual_direction",
    }
    fields = {
        None,
        "style_hint",
        "visual_direction",
        "style",
        "layout",
        "composition",
        "lighting",
        "scene",
        "palette",
        "mood",
        "theme",
        "options.theme",
    }
    return bool(issues) and all(
        issue.code in codes
        and issue.field in fields
        and issue.resolution in {"input", "reanalyze"}
        and not (issue.subject_ids or issue.material_ids or issue.element_ids or issue.options)
        for issue in issues
    )


def explicit_section_count(request):
    text = request.content_brief
    match = re.search(r"(?i)(\d{1,2})\s*(?:段|区块|sections?|blocks?)", text)
    if match:
        return int(match.group(1))
    match = re.search(r"([二两三四五六七八九十]|十二|十一)\s*(?:段|个区块)", text)
    if match:
        return {
            "二": 2,
            "两": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
            "十一": 11,
            "十二": 12,
        }[match.group(1)]
    return None


def validate_section_count(request, plan):
    count = explicit_section_count(request)
    if count is not None and count != len(plan.sections):
        raise AgentError(
            "plan differs from explicitly requested section count", code="plan_invalid"
        )
    structured = has_explicit_structure(request)
    if count is None and not structured and not 4 <= len(plan.sections) <= 8:
        raise AgentError("default planning requires four to eight sections", code="plan_invalid")


def has_explicit_structure(request):
    text = request.content_brief
    if re.search(r"(?:结构|区块顺序|段落顺序|步骤|sections|structure)\s*[:：]", text, re.I):
        return True
    if re.search(
        r"(?:\d+|[二两三四五六七八九十]|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)\s*(?:步|steps?)",
        text,
        re.I,
    ):
        return True
    return len(re.findall(r"(?:^|\n)\s*(?:\d+|[一二三四五六七八九十]+)[.、)）]\s*\S", text)) >= 2


def ensure_page_copy_isolated(plan, text, *, analysis=None, code="plan_invalid"):
    """Exact required copy/quoted prose cannot become generated overlays or repairs.

    Subject names/headings can describe the image's subject; the visual requirement
    ID contract keeps them out of must_show. Native labels remain physical evidence.
    """
    native = (
        [
            f.description
            for f in analysis.facts
            if f.effective_verifiability == "visible_appearance"
            and any(e.source_type == "product_label" for e in f.evidence)
        ]
        if analysis
        else []
    )
    for requirement in plan.requirements:
        pointer = requirement.source.pointer or ""
        explicit_copy = pointer == "/content_brief" or pointer.startswith("/required_copy/")
        if (
            requirement.kind != "slot_visual"
            and explicit_copy
            and requirement.text in text
            and not any(requirement.text in label for label in native)
        ):
            raise AgentError(
                "program-rendered exact copy cannot enter slot prompts or repair", code=code
            )


def validate_slot_copy_isolation(plan, analysis=None):
    ensure_page_copy_isolated(plan, plan.visual_direction, analysis=analysis)
    for slot in plan.image_slots:
        if slot.source == "generate":
            ensure_page_copy_isolated(plan, slot.prompt, analysis=analysis)
    for requirement in plan.requirements:
        if requirement.kind == "slot_visual":
            ensure_page_copy_isolated(plan, requirement.text, analysis=analysis)


def validate_long_audit(audit, slot, plan, candidate_id):
    if audit.slot_id != slot.slot_id or audit.candidate_id != candidate_id:
        raise ProviderError("audit slot/candidate binding mismatch", code="audit_error")
    requirements = {
        r.requirement_id: r
        for r in plan.requirements
        if r.kind == "slot_visual" and r.requirement_id in slot.requirement_ids
    }
    base = {"purpose", "subject", "style", "unrequested_text"}
    seen_base, seen_requirements = set(), set()
    for check in audit.checks:
        if check.requirement_id is None:
            if check.kind in seen_base:
                raise ProviderError("duplicated generic audit check", code="audit_error")
            seen_base.add(check.kind)
        else:
            if (
                check.requirement_id not in requirements
                or check.requirement_id in seen_requirements
                or check.kind == "unrequested_text"
            ):
                raise ProviderError(
                    "unknown, duplicate or invalid visual requirement check", code="audit_error"
                )
            seen_requirements.add(check.requirement_id)
    if seen_base != base or seen_requirements != requirements.keys():
        raise ProviderError("missing applicable audit checks", code="audit_error")
    passed = not any(
        (not c.passed and (c.requirement_id is None or requirements[c.requirement_id].required))
        or (not c.passed and c.severity == "critical")
        for c in audit.checks
    )
    if (
        audit.passed != passed
        or (passed and audit.repairable)
        or (audit.repairable and not (audit.repair_prompt or "").strip())
    ):
        raise ProviderError("inconsistent audit outcome or repair instruction", code="audit_error")
    try:
        ensure_page_copy_isolated(plan, audit.repair_prompt or "", code="audit_error")
    except AgentError as error:
        raise ProviderError(error.message, code="audit_error") from None
    return audit


class LongImageVisionClient(VisionClient):
    async def plan(self, request, materials, requirements, *, analysis=None):
        hashes = {m.material_id: m.sha256 for m in materials}
        fingerprint = planning_fingerprint(request, hashes)
        selection = None
        analysis_ref = None
        source_ready = analysis is None
        if analysis is not None:
            from .long_image_product import LongProductEvidenceRequest

            draft = resolve_selection(LongProductEvidenceRequest(request), analysis)
            selection = draft.candidate
            source_ready = (
                analysis.status != "failed"
                and analysis.error_info is None
                and selection is not None
                and not draft.blocking_issues
                and not draft.pending_issues
            )
            analysis_ref = LongAnalysisRef(
                analysis_id=analysis.analysis_id,
                fingerprint=analysis.fingerprint,
                content_hash=content_hash(analysis.model_dump(mode="json")),
            )
        count = explicit_section_count(request)
        instruction = (
            "Plan a coherent Chinese illustrated long page for the exact scene. Supported scenes: "
            "product_detail, brand_intro, promotion, tutorial, comparison, custom. "
            "Use 4–8 sections by default; an explicit user structure permits 2–12. Follow an explicit count exactly. "
            "At most SIX generated image slots; zero is allowed. Text-only sections have empty slot_ids. "
            "Source slots reuse exactly one real material with empty prompt and null aspect_ratio; never audit or generate them. "
            "Generated slots need concrete visual prompt and supported ratio. Every slot must be referenced, with matching reverse section IDs. "
            "Keep one shared visual_direction for all slots. Place supplied requirements in exact reading order, preserving every code point and occurrence. "
            "One text_unit satisfies one requirement occurrence. Page/section text is drawn by the local renderer, "
            "NEVER put page copy, title, dates, specs or service text in visual_direction, image-slot prompts or slot_visual requirements. "
            "Product-native labels/artwork remain image evidence and must be preserved. "
            "For product-generated slots choose role hero/scene/feature/closeup according to this slot's intended framing. "
            "Only user-authored visual instructions may become slot_visual requirements; bind exact source pointer/range and reverse slot IDs. "
            "Model-authored optional visual choices belong slot prompts/visual_direction, not requirements. "
            "Do not invent dates, prices, discounts, testimonials, certification, exclusive brand/product facts or concealed product details. "
            "Ask structured needs_input issues with field and resolution=input for genuinely required missing facts. "
            "Author optional style, layout, composition, lighting and scene choices from existing inputs; "
            "unspecified aesthetics do not require new user information. Never use such choices to invent factual content. "
            "Do not require platform/materials/product context for nonproduct scenes. "
            "Use tutorial steps, comparison tables, or other appropriate roles without requiring a fixed ecommerce format. "
            "ready requires complete plan and no required issues; needs_input requires actionable issues and no plan. "
            + MARKETING_POLICY
            + "\n"
            + json.dumps(
                {
                    "request": request.model_dump(
                        mode="json", exclude={"materials", "output_dir", "options"}
                    ),
                    "theme": request.options.theme,
                    "style_hint": request.style_hint,
                    "materials": [m.model_dump(mode="json", exclude={"data"}) for m in materials],
                    "material_hints": [
                        m.model_dump(mode="json", exclude={"source"}) for m in request.materials
                    ],
                    "input_fingerprint": fingerprint,
                    "explicit_section_count": count,
                    "requirements": [r.model_dump(mode="json") for r in requirements],
                    "analysis": analysis.model_dump(mode="json") if analysis else None,
                    "analysis_ref": analysis_ref.model_dump() if analysis_ref else None,
                    "resolved_selection": selection.model_dump() if selection else None,
                },
                ensure_ascii=False,
            )
        )

        def validate(result):
            if result.status == "needs_input":
                if result.plan is not None or not any(i.required for i in result.issues):
                    raise ProviderError("needs_input requires blocking issues and no plan")
                if source_ready and _can_complete_visual_gaps(result.issues):
                    raise ProviderError(
                        "complete optional visual planning gaps from the existing inputs: "
                        + ", ".join(issue.code for issue in result.issues)
                        + "; choose visual direction and return a validated complete plan without inventing facts"
                    )
                return result
            if (
                result.status != "ready"
                or result.plan is None
                or any(i.required for i in result.issues)
            ):
                raise ProviderError("ready requires nonblocked complete plan")
            if (
                result.plan.resolved_selection != selection
                or result.plan.analysis_ref != analysis_ref
            ):
                raise ProviderError("plan must retain exact frozen product snapshot and selection")
            try:
                validate_long_image_plan(
                    request, result.plan, material_hashes=hashes, analysis=analysis
                )
                validate_section_count(request, result.plan)
                validate_slot_copy_isolation(result.plan, analysis)
            except AgentError as error:
                raise ProviderError(error.message, code="plan_invalid") from None
            return result

        response = await self._call(
            LongPlanResponse,
            instruction,
            [(f"Material {m.material_id}", m.data) for m in materials],
            stage="plan_long_image",
            validate=validate,
        )
        return LongImagePlanningResult(
            planning_id=uuid4().hex,
            status=response.status,
            plan=response.plan,
            analysis=analysis,
            issues=response.issues,
            warnings=response.warnings,
        )

    async def audit(self, slot, plan, image, *, candidate_id, materials):
        if slot.source != "generate":
            raise AgentError(
                "source slots do not receive generated visual audits", code="audit_error"
            )
        decoded = generated_image(image)
        a, b = map(int, slot.aspect_ratio.split(":"))
        if abs((decoded.width / decoded.height) / (a / b) - 1) > 0.06 + 1e-12:
            raise AgentError("generated image ratio fails technical check", code="image_invalid")
        requirements = [
            r
            for r in plan.requirements
            if r.kind == "slot_visual" and r.requirement_id in slot.requirement_ids
        ]
        instruction = (
            "Audit ONLY this generic generated slot: purpose, source subject/marking preservation, style and unrequested text/watermarks. "
            "Return exactly one check without requirement_id for each kind: purpose, subject, style, unrequested_text. "
            "Return exactly one additional check per supplied visual requirement_id (kind purpose/subject/style). "
            "Never invent or omit IDs, or use page-copy requirements. No product/clothing/platform scores apply. "
            "Page text is rendered separately; its absence inside this image is expected. "
            "passed iff all base checks and required visual checks pass and no critical failure exists. "
            "Passed audits have repairable=false. Repairable failures need a nonblank slot-specific repair_prompt, "
            "with no page copy. Preserve product-native/source markings. "
            + MARKETING_POLICY
            + "\n"
            + json.dumps(
                {
                    "slot": slot.model_dump(),
                    "visual_direction": plan.visual_direction,
                    "requirements": [r.model_dump() for r in requirements],
                    "candidate_id": candidate_id,
                },
                ensure_ascii=False,
            )
        )
        by_id = {m.material_id: m for m in materials}
        if not set(slot.material_ids) <= by_id.keys():
            raise AgentError("slot source evidence is missing", code="plan_invalid")
        images = [(f"Source {mid}", by_id[mid].data) for mid in slot.material_ids] + [
            ("GENERATED SLOT", image)
        ]
        try:
            return await self._call(
                LongImageAudit,
                instruction,
                images,
                stage="audit_long_image_asset",
                validate=lambda result: validate_long_audit(result, slot, plan, candidate_id),
            )
        except ProviderError as error:
            if error.code == "budget_exhausted":
                raise
            raise ProviderError(
                error.message, code="audit_error", kind=error.kind, status_code=error.status_code
            ) from None

    async def audit_product_slot(self, request, slot, prepared, image, *, size=None, model=None):
        """Product reports keep the existing deterministic QualityReport semantics."""
        if slot.source != "generate":
            raise AgentError(
                "source slots do not receive generated product audits", code="audit_error"
            )
        if prepared.element_plan is None or prepared.context is None:
            raise AgentError("product audit requires prepared evidence", code="audit_error")
        pixels = check_pixels(
            image,
            prepared.target,
            size or request.image_size,
            model or self.config.model_name(request.image_model),
        )
        if not pixels.passed:
            raise AgentError(pixels.reason, code="image_invalid")
        element_plan = prepared.element_plan
        expected = {
            kind + "_id": [r.target_id for r in element_plan.requirements if r.target_kind == kind]
            for kind in ("subject", "element", "constraint")
        }

        def validate(audit):
            for key, ids in expected.items():
                validate_check_ids(getattr(audit, key.removesuffix("_id") + "_checks"), ids, key)
            return audit

        from .appearance import PRODUCT_APPEARANCE_AUDIT, PRODUCT_APPEARANCE_POLICY
        from .platforms import audit_instruction

        instruction = (
            "Audit the frozen subject, element and constraint requirements for this single long-image slot. Facts are creative context only; do not return independent fact checks or evaluate the truth of claims. "
            "Return each expected ID once, no missing/additional checks. Preserve original labels, designs and visible structure. "
            "Page copy is rendered locally and does not enter this audit. Check only slot-specific visual requirements, "
            "never require an image to prove marketing claims. Scores and physical identity decisions follow the existing product audit schema. "
            "Nonvisible preserve_if_visible may be not_applicable; must_show may not. Clear altered appearance is same_product=false. "
            + PRODUCT_APPEARANCE_POLICY
            + PRODUCT_APPEARANCE_AUDIT
            + MARKETING_POLICY
            + audit_instruction(prepared.target.platform, "detail_page")
            + json.dumps(
                {
                    "expected_check_ids": expected,
                    "slot_id": slot.slot_id,
                    "target": prepared.target.model_dump(),
                    "plan": element_plan.model_dump(),
                    "facts": [
                        f.model_dump()
                        for f in prepared.context.analysis.facts
                        if f.fact_id in element_plan.required_fact_ids
                    ],
                    "user_input": prepared.context.user_input,
                },
                ensure_ascii=False,
            )
        )
        materials = {m.material_id: m.data for m in prepared.context.materials}
        images = [
            (f"Evidence {mid}", materials[mid]) for mid in element_plan.audit_material_ids
        ] + [("GENERATED SLOT", image)]
        try:
            audit = await self._call(
                GeneratedImageAudit,
                instruction,
                images,
                stage="audit_long_image_asset",
                validate=validate,
                nonapplicable_fields=("garment_fusion", "model_preference")
                if prepared.target.presentation_mode == "product_only"
                else (),
            )
            return evaluate_quality(audit, pixels, prepared.target, element_plan)
        except ProviderError as error:
            raise ProviderError(
                error.message, code="audit_error", kind=error.kind, status_code=error.status_code
            ) from None
