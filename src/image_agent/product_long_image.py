"""Product-specific planning, prompt compilation and durable section workflow."""

import hashlib
import json

from .appearance import PRODUCT_APPEARANCE_POLICY, SOURCE_VIEW_POLICY
from .errors import ProviderError
from .marketing import MARKETING_POLICY, user_directions
from .product_long_image_models import ProductLongImagePlan, displayed_copy
from .run_store import vision_diagnostic_scope


def selected_product_evidence(context):
    selection, analysis = context.selection, context.analysis
    subjects = [s for s in analysis.subjects if s.subject_id in selection.subject_ids]
    allowed_elements = set(selection.required_element_ids + selection.preferred_element_ids)
    elements = [
        e
        for e in analysis.elements
        if (
            e.subject_id in selection.subject_ids
            and e.element_id not in selection.excluded_element_ids
        )
        or (e.subject_id is None and e.element_id in allowed_elements)
    ]
    fact_ids = {fid for e in elements for fid in e.fact_ids}
    facts = [
        f
        for f in analysis.facts
        if f.subject_id in selection.subject_ids or (f.subject_id is None and f.fact_id in fact_ids)
    ]
    allowed_fact_ids = {f.fact_id for f in facts}
    filtered_elements = []
    for element in elements:
        closed_ids = [fid for fid in element.fact_ids if fid in allowed_fact_ids]
        # A mixed element's free-text description can mention its foreign product too.
        # Drop the contaminated element as a whole rather than forwarding that description.
        if len(closed_ids) == len(element.fact_ids):
            filtered_elements.append(element.model_copy(update={"fact_ids": closed_ids}))
    return {
        "subjects": [s.model_dump() for s in subjects],
        "elements": [e.model_dump() for e in filtered_elements],
        "facts": [f.model_dump() for f in facts],
        "selection": selection.model_dump(),
    }


def product_long_image_context(context):
    """Close the long-only analysis/selection over allowed product evidence.

    A filtered planner payload alone is insufficient: the element compiler,
    reference selector, binding and physical audit must share the same closure.
    The original prepared context remains unchanged for discovery/ordinary assets.
    """
    from .models import Element

    evidence = selected_product_evidence(context)
    subjects = {row["subject_id"] for row in evidence["subjects"]}
    facts = {row["fact_id"] for row in evidence["facts"]}
    elements = [Element.model_validate(row) for row in evidence["elements"]]
    element_ids = {element.element_id for element in elements}
    # Keep safe exclusions available to the compiler without promoting them to copy.
    for element in context.analysis.elements:
        if (
            element.element_id in context.selection.excluded_element_ids
            and (element.subject_id is None or element.subject_id in subjects)
            and set(element.fact_ids) <= facts
        ):
            elements.append(element.model_copy(deep=True))
            element_ids.add(element.element_id)
    constraints = []
    for constraint in context.selection.constraints:
        if set(constraint.element_ids) <= element_ids and set(constraint.subject_ids) <= subjects:
            constraints.append(constraint.model_copy(deep=True))
        elif constraint.priority == "required" and constraint.kind != "exclusion":
            raise ProviderError(
                "required long-image constraint crosses selected product evidence",
                code="long_evidence_outside_selection",
                kind="capability",
            )
    if not set(context.selection.required_element_ids) <= element_ids:
        raise ProviderError(
            "required long-image element crosses selected product evidence",
            code="long_evidence_outside_selection",
            kind="capability",
        )
    selection = context.selection.model_copy(
        update={
            "required_element_ids": [
                eid for eid in context.selection.required_element_ids if eid in element_ids
            ],
            "preferred_element_ids": [
                eid for eid in context.selection.preferred_element_ids if eid in element_ids
            ],
            "excluded_element_ids": [
                eid for eid in context.selection.excluded_element_ids if eid in element_ids
            ],
            "focus_element_ids": [
                eid for eid in context.selection.focus_element_ids if eid in element_ids
            ],
            "constraints": constraints,
        }
    )
    material_ids = {mid for subject in evidence["subjects"] for mid in subject["material_ids"]}
    material_ids |= {
        source["material_id"] for fact in evidence["facts"] for source in fact["evidence"]
    }
    intent = context.analysis.intent.model_copy(
        update={
            "proposal": context.analysis.intent.proposal.model_copy(
                update={
                    key: getattr(selection, key)
                    for key in (
                        "primary_subject_id",
                        "subject_ids",
                        "required_element_ids",
                        "preferred_element_ids",
                        "excluded_element_ids",
                    )
                }
            ),
            "constraints": constraints,
            "focus_element_ids": selection.focus_element_ids,
            "unmet_requirements": [],
        }
    )
    analysis = context.analysis.model_copy(
        update={
            "subjects": [
                subject.model_copy(deep=True)
                for subject in context.analysis.subjects
                if subject.subject_id in subjects
            ],
            "facts": [
                fact.model_copy(deep=True)
                for fact in context.analysis.facts
                if fact.fact_id in facts
            ],
            "elements": elements,
            "materials": [
                material.model_copy(deep=True)
                for material in context.analysis.materials
                if material.material_id in material_ids
            ],
            "intent": intent,
            "issues": [],
        }
    )
    return context.model_copy(
        update={
            "analysis": analysis,
            "selection": selection,
            "materials": tuple(
                material for material in context.materials if material.material_id in material_ids
            ),
        }
    )


def product_long_image_fingerprint(request, target, context):
    payload = {
        "analysis": context.analysis.model_dump(mode="json"),
        "material_hashes": [(m.material_id, m.sha256) for m in context.materials],
        "selection": context.selection.model_dump(mode="json"),
        "platform": target.platform,
        "request": request.model_dump(
            mode="json",
            exclude={"materials", "product_image", "reference_images", "output_dir", "request_id"},
        ),
    }
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()


def _restore_required_copy(plan, required_copy):
    """Bind boundary-only planner edits back to the user's exact strings.

    Keep internal wording, numbers and punctuation significant. Ambiguous strings
    and substantive edits still require model correction rather than guessing.
    """

    def boundary_key(text):
        return text.strip().rstrip("。.!！?？").rstrip()

    required = set(required_copy)
    by_key = {}
    for text in required:
        by_key.setdefault(boundary_key(text), set()).add(text)
    for section in plan.sections:
        slots = [(section, "title")] + [(row, "text") for row in section.copy_items]
        for owner, field in slots:
            text = getattr(owner, field)
            key = boundary_key(text)
            candidates = by_key.get(key, set())
            if key and text not in required and len(candidates) == 1:
                setattr(owner, field, next(iter(candidates)))


def validate_product_long_image_plan(request, target, context, plan):
    plan = ProductLongImagePlan.model_validate(plan)
    options = request.long_image

    def invalid(message):
        raise ProviderError("product long-image plan: " + message)

    # This is a fresh provider draft. Bind request identity locally rather than
    # asking the authoring model to reproduce an internal hash correctly.
    object.__setattr__(
        plan, "input_fingerprint", product_long_image_fingerprint(request, target, context)
    )
    if len(plan.sections) != options.segment_count:
        invalid(f"expected {options.segment_count} sections, received {len(plan.sections)}")
    if len({s.section_id for s in plan.sections}) != len(plan.sections):
        invalid("duplicate section IDs")
    evidence = selected_product_evidence(context)
    facts = {f["fact_id"]: f for f in evidence["facts"]}
    _restore_required_copy(plan, options.required_copy)
    texts = []
    for section in plan.sections:
        texts.extend(displayed_copy(section))
        for row in section.copy_items:
            # Source notes are optional metadata, not proof of appearance or a
            # reason to reject legitimate creative wording. Never bind foreign IDs.
            row.fact_ids = [fid for fid in row.fact_ids if fid in facts]
    missing = [required for required in options.required_copy if required not in texts]
    if missing:
        invalid(
            "required copy missing or changed. Missing exact strings: "
            + json.dumps(missing, ensure_ascii=False)
        )
    object.__setattr__(plan, "source_evidence", evidence)
    return plan


def build_product_long_image_prompt(
    request, target, context, element_plan, plan, section, *, feedback=""
):
    index = next(i for i, s in enumerate(plan.sections) if s.section_id == section.section_id)
    adjacent = [s.visual_prompt for s in plan.sections[max(0, index - 1) : index + 2]]
    return "\n".join(
        [
            "Design the ENTIRE finished e-commerce long-image section: actual selected product, background, scenes, Chinese typography, hierarchy and complete layout. Render every specified copy string directly into the image, legibly and exactly including numbers and punctuation. This is a complete designed frame, not a small illustration or an empty scene template.",
            PRODUCT_APPEARANCE_POLICY,
            SOURCE_VIEW_POLICY,
            MARKETING_POLICY,
            user_directions(request),
            "Original selected Reference Images are the sole authority for product identity. Continuity references supplement style only. Do not invent capabilities, certifications, sales, composition percentages, discounts, dimensions or promises the user did not supply. Preserve user-supplied marketing wording without weakening it or adding disclaimers. Use only the displayed copy below; add no other claims. Do not print source IDs or instructions.",
            f"Product: {request.product_name}; category: {request.category}; platform: {target.platform}; presentation: {target.presentation_mode}; model preference: {target.model_preference}.",
            f"Unified design: {plan.style_direction}\nUser style: {request.style_hint or ''}\nCreative brief: {request.creative_brief or ''}",
            f"Section {index + 1}/{len(plan.sections)}; role {section.role}; entire-frame direction: {section.visual_prompt}\nAdjacent visual continuity: "
            + json.dumps(adjacent, ensure_ascii=False),
            "EXACT COPY (render each complete string):\n" + "\n".join(displayed_copy(section)),
            "SELECTED PRODUCT EVIDENCE AND Reference Image MAPPING:\n"
            + json.dumps(
                selected_product_evidence(context) | {"element_plan": element_plan.model_dump()},
                ensure_ascii=False,
            ),
            "Correction: fix only omitted instructions, altered product appearance, unrequested additions or technical defects. Never correct the user's creative premise or marketing claims. Regenerate this entire designed section with all copy and original product evidence. "
            + feedback
            if feedback
            else "",
        ]
    )


class ProductLongImageWorkflow:
    """One paid image submission per checkpoint, immutable saved section evidence."""

    def __init__(self, nodes):
        self.nodes = nodes

    def runtime(self, state):
        return state["product_long_images"][self.nodes.asset(state).asset_id]

    def current(self, state):
        runtime = self.runtime(state)
        return runtime, runtime["segments"][runtime["section_index"]]

    async def plan(self, state):
        nodes = self.nodes
        asset, target, context = nodes.asset(state), nodes.target(state), nodes.context(state)
        with vision_diagnostic_scope(nodes.store, asset_id=asset.asset_id):
            plan = await nodes.dependencies.vision.plan_product_long_image(
                nodes.request, target, context
            )
            plan = validate_product_long_image_plan(nodes.request, target, context, plan)
        payload = plan.model_dump(mode="json")
        plan_ref = nodes.store.write(
            f"plans/{asset.asset_id}.json",
            json.dumps(payload, ensure_ascii=False, indent=2).encode(),
        )
        state.setdefault("product_long_images", {})[asset.asset_id] = {
            "plan": payload,
            "plan_ref": plan_ref,
            "section_index": 0,
            "segments": [
                {
                    "section_id": s.section_id,
                    "attempts": 0,
                    "audit_attempts": 0,
                    "status": "pending_audit",
                    "candidates": [],
                    "quality": None,
                    "copy_audit": None,
                    "raw": None,
                    "feedback": "",
                }
                for s in plan.sections
            ],
        }
        state["phase"] = "long_generate"

    def references(self, state, asset, context, runtime):
        from .models import GenerationReference
        from .selection import select_references

        nodes = self.nodes
        refs = select_references(
            asset.element_plan,
            context.analysis,
            context.materials,
            limit=nodes.config.generation_reference_limit,
        )
        anchor = next(
            (s for s in runtime["segments"] if s["status"] == "succeeded" and s["candidates"]), None
        )
        if anchor:
            if len(refs) < nodes.config.generation_reference_limit:
                refs += (
                    GenerationReference(
                        stage_id="long-continuity",
                        data=nodes.store.read(anchor["candidates"][-1]["ref"]),
                        role="shoot_continuity",
                    ),
                )
            else:
                warning = (
                    asset.asset_id
                    + ": reference capacity retains required product evidence; long-image continuity uses shared text only"
                )
                if warning not in state["result"]["warnings"]:
                    state["result"]["warnings"].append(warning)
        return refs

    async def generate(self, state):
        import asyncio
        import copy

        from .errors import AgentError
        from .execution import call_scope
        from .models import GenerationAttempt
        from .selection import bind_generation_references, compile_element_plan

        nodes = self.nodes
        asset, target, context = (
            nodes.asset(state),
            nodes.target(state),
            product_long_image_context(nodes.context(state)),
        )
        runtime, segment = self.current(state)
        operation = f"{asset.asset_id}-{segment['section_id']}-{segment['attempts'] + 1}"
        old = nodes.store.operation(operation)
        if old and old["status"] == "saved":
            # Journal was committed before the graph checkpoint: restore exactly that attempt.
            runtime.update(copy.deepcopy(old["long_runtime"]))
            nodes.update_asset(state, nodes.asset(state).model_validate(old["asset_snapshot"]))
            state["phase"] = "long_save_candidate"
            return
        if old and old["status"] in ("started", "submitted"):
            raise ProviderError(
                "previous section generation may have been charged; response missing",
                code="generation_uncertain",
                kind="transport",
            )
        # Recompile on every actual submission, including runs saved by an older
        # version whose asset snapshot still referenced a contaminated element.
        asset.element_plan = compile_element_plan(nodes.request, target, context, nodes.config)
        refs = self.references(state, asset, context, runtime)
        element_plan = bind_generation_references(asset.element_plan, context.analysis, refs)
        plan = ProductLongImagePlan.model_validate(runtime["plan"])
        section = plan.sections[runtime["section_index"]]
        asset.element_plan, asset.reference_bindings = element_plan, element_plan.reference_bindings
        asset.prompt = build_product_long_image_prompt(
            nodes.request,
            target,
            context,
            element_plan,
            plan,
            section,
            feedback=segment["feedback"],
        )
        asset.generation_mode = "strict" if segment["attempts"] else "standard"
        attempt = GenerationAttempt(
            stage=asset.generation_mode,
            model=asset.model,
            reference_ids=[r.material_id or r.stage_id for r in refs],
            prompt=asset.prompt,
            outcome="failed",
        )
        asset.attempts.append(attempt)
        segment.update(
            attempts=segment["attempts"] + 1,
            prompt=asset.prompt,
            element_plan=element_plan.model_dump(mode="json"),
        )
        nodes.update_asset(state, asset)
        nodes.store.mark_operation(operation, "started")
        with call_scope(nodes.ledger, "image", nodes.store, operation):
            cancelled = False
            task = asyncio.create_task(
                nodes.dependencies.generator.generate(
                    prompt=asset.prompt,
                    model=asset.model,
                    size=nodes.request.image_size,
                    aspect_ratio=target.aspect_ratio,
                    references=refs,
                )
            )
            try:
                while True:
                    try:
                        data = await asyncio.shield(task)
                        break
                    except asyncio.CancelledError:
                        if task.cancelled():
                            raise
                        cancelled = True
            except AgentError as error:
                if error.code != "generation_uncertain":
                    nodes.store.mark_operation(operation, "failed")
                raise
        segment["raw"] = nodes.store.write(f"raw/{operation}.bin", data)
        attempt.outcome = "succeeded"
        nodes.update_asset(state, asset)
        nodes.store.mark_operation(
            operation,
            "saved",
            raw=segment["raw"],
            long_runtime=copy.deepcopy(runtime),
            asset_snapshot=asset.model_dump(mode="json"),
        )
        if cancelled:
            raise asyncio.CancelledError()
        state["phase"] = "long_save_candidate"

    async def save_candidate(self, state):
        from .images import generated_image, image_format
        from .transport import compute

        nodes = self.nodes
        asset = nodes.asset(state)
        runtime, segment = self.current(state)
        data = nodes.store.read(segment["raw"])
        await compute(generated_image, data)
        candidate_id = f"c{len(asset.candidates) + 1:04d}"
        ref = nodes.store.write(
            f"segments/{asset.asset_id}/{segment['section_id']}-{segment['attempts']}{image_format(data)[0]}",
            data,
        )
        segment["candidates"].append(
            {
                "candidate_id": candidate_id,
                "ref": ref,
                "raw": segment["raw"],
                "prompt": segment["prompt"],
                "element_plan": segment["element_plan"],
                "quality": None,
                "copy_audit": None,
                "audits": [],
            }
        )
        asset.candidates.append(
            {
                "candidate_id": candidate_id,
                "index": len(asset.candidates) + 1,
                "status": "stage",
                "file_path": ref["path"],
                "sha256": ref["sha256"],
                "quality": None,
                "error_info": None,
                "repair_prompt": segment["feedback"],
                "metadata": {"section_id": segment["section_id"]},
            }
        )
        segment.update(
            raw=None, status="pending_audit", quality=None, copy_audit=None, audit_attempts=0
        )
        asset.status = "pending_audit"
        nodes.update_asset(state, asset)
        state["phase"] = "long_audit"

    def advance(self, state):
        runtime = self.runtime(state)
        # On resumed audit, revisit saved pending/error rows, never regenerate approved sections.
        for i in range(runtime["section_index"] + 1, len(runtime["segments"])):
            segment = runtime["segments"][i]
            if segment["candidates"] and segment["status"] not in ("pending_audit", "audit_error"):
                continue
            runtime["section_index"] = i
            state["phase"] = "long_audit" if segment["candidates"] else "long_generate"
            return
        state["phase"] = "long_compose"

    async def audit(self, state):
        from pydantic import ValidationError

        from .errors import AgentError, make_error_info
        from .models import AssetElementPlan
        from .product_long_image_models import ProductLongImageCopyAudit
        from .quality import check_pixels, evaluate_quality, review_subject_ids
        from .selection import compile_element_plan
        from .transport import compute

        nodes = self.nodes
        asset, target, context = (
            nodes.asset(state),
            nodes.target(state),
            product_long_image_context(nodes.context(state)),
        )
        runtime, segment = self.current(state)
        candidate = segment["candidates"][-1]
        data = nodes.store.read(candidate["ref"])
        generated_plan = AssetElementPlan.model_validate(candidate["element_plan"])
        element_plan = compile_element_plan(nodes.request, target, context, nodes.config)
        audit_plan_payload = element_plan.model_dump(mode="json")
        contaminated = generated_plan.requirements != element_plan.requirements or set(
            generated_plan.audit_material_ids
        ) != set(element_plan.audit_material_ids)
        if contaminated and candidate.get("audit_element_plan") != audit_plan_payload:
            # Historical audits over foreign evidence are not approval evidence for
            # the selected product. Retain their records, but audit this saved image
            # again against the closed selection without another image submission.
            segment["quality"] = segment["copy_audit"] = None
            candidate["audits"].append({"context_rebound": "selected product evidence closure"})
        candidate["audit_element_plan"] = audit_plan_payload
        if contaminated:
            asset.element_plan = element_plan
            asset.reference_bindings = element_plan.reference_bindings
            nodes.update_asset(state, asset)
        plan = ProductLongImagePlan.model_validate(runtime["plan"])
        try:
            if segment["quality"] is None:
                pixels = await compute(
                    check_pixels, data, target, nodes.request.image_size, asset.model
                )
                audit = await nodes.dependencies.vision.audit_image(
                    nodes.request, target, context, element_plan, data
                )
                quality = evaluate_quality(audit, pixels, target, element_plan)
                pending = review_subject_ids(audit, element_plan)
                if pending:
                    review = await nodes.dependencies.vision.review_product(
                        context, element_plan, pending, data
                    )
                    quality = evaluate_quality(audit, pixels, target, element_plan, review=review)
                candidate["quality"] = segment["quality"] = quality.model_dump(mode="json")
                candidate["audits"].append({"quality": candidate["quality"]})
                # Physical audit must survive a copy-audit interruption without being billed again.
                nodes.store.json("state.json", state)
            if segment["copy_audit"] is None:
                copy_audit = await nodes.dependencies.vision.audit_product_long_image_copy(
                    nodes.request, plan.sections[runtime["section_index"]], plan, data
                )
                copy_audit = ProductLongImageCopyAudit.model_validate(copy_audit)
                candidate["copy_audit"] = segment["copy_audit"] = copy_audit.model_dump(mode="json")
                candidate["audits"].append({"copy_audit": candidate["copy_audit"]})
        except (AgentError, ValidationError) as error:
            info = make_error_info(error)
            segment.update(
                status="budget_exhausted" if info.code == "budget_exhausted" else "audit_error",
                error_info=info.model_dump(mode="json"),
                audit_attempts=segment["audit_attempts"] + 1,
            )
            candidate["audits"].append({"error_info": segment["error_info"]})
            if (
                segment["status"] == "audit_error"
                and (info.retryable or info.kind in ("protocol", "validation"))
                and segment["audit_attempts"] <= nodes.policy.max_audit_retries
            ):
                state["phase"] = "long_audit"
            else:
                self.advance(state)
            return
        passed = segment["quality"]["passed"] and segment["copy_audit"]["passed"]
        segment.update(status="succeeded" if passed else "quality_failed", error_info=None)
        if not passed and segment["attempts"] <= nodes.policy.max_quality_repairs:
            segment["feedback"] = json.dumps(
                {"physical_quality": segment["quality"], "copy_audit": segment["copy_audit"]},
                ensure_ascii=False,
            )
            state["phase"] = "long_generate"
        else:
            self.advance(state)

    async def compose(self, state):
        from functools import partial

        from .product_long_image_delivery import compose_best_sections
        from .transport import compute

        nodes = self.nodes
        asset = nodes.asset(state)
        runtime = self.runtime(state)
        runtime['resume_phase'] = 'long_compose'
        await compute(partial(
            compose_best_sections, asset, nodes.target(state), runtime, nodes.store,
            nodes.request.long_image.width,
        ))
        runtime.pop('resume_phase', None)
        nodes.update_asset(state, asset)
        state['phase'] = 'finish_asset'
