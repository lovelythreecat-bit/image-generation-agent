"""Deterministic selection and evidence compilation. No language inference or I/O."""

import hashlib
import json

from .errors import AgentError, ProviderError
from .models import (
    AssetElementPlan,
    ErrorInfo,
    GenerationReference,
    Issue,
    IssueResolution,
    ReferenceBinding,
    Requirement,
    ResolvedSelection,
    SelectionDraft,
    SelectionResolution,
)
from .prompt import is_apparel, strict_catalog

RULE_VERSION = "multi-image-r2"


def analysis_fingerprint(request, materials):
    hints = {m.material_id: m for m in request.normalized_materials()}
    data = {
        "rules": RULE_VERSION,
        "product_name": request.product_name,
        "category": request.category,
        "brief": request.creative_brief,
        "materials": [
            dict(
                material_id=m.material_id,
                sha256=hashlib.sha256(m.data).hexdigest(),
                role=hints[m.material_id].role_hint,
                subject_hint=hints[m.material_id].subject_hint,
                element_hints=hints[m.material_id].element_hints,
            )
            for m in materials
        ],
    }
    return hashlib.sha256(
        json.dumps(data, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
    ).hexdigest()


def resolve_selection(request, analysis):
    if analysis.intent is None:
        return SelectionDraft(
            candidate=None,
            blocking_issues=[
                Issue(
                    issue_id="reanalyze",
                    code="missing_analysis",
                    resolution="reanalyze",
                    message="请重新分析素材",
                )
            ],
        )
    try:
        analysis.validate_intent_source(request)
    except ValueError:
        raise ProviderError("intent source_quote is not supported by request") from None
    proposal = analysis.intent.proposal
    selection = request.selection
    args = (
        proposal.model_dump()
        if selection.mode == "auto"
        else selection.model_dump(exclude={"mode"})
        | {"primary_subject_id": selection.subject_ids[0]}
    )
    subjects = {s.subject_id: s for s in analysis.subjects}
    elements = {e.element_id: e for e in analysis.elements}
    selected_ids = args["required_element_ids"] + args["preferred_element_ids"]
    if (
        not set(args["subject_ids"]) <= subjects.keys()
        or not set(selected_ids + args["excluded_element_ids"]) <= elements.keys()
    ):
        raise AgentError("selection contains unknown IDs")
    if any(
        elements[e].subject_id and elements[e].subject_id not in args["subject_ids"]
        for e in selected_ids
    ):
        raise AgentError("selected element belongs to an unselected subject")
    selected = set(selected_ids)
    candidate = (
        ResolvedSelection(
            **args,
            constraints=analysis.intent.constraints,
            focus_element_ids=[e for e in analysis.intent.focus_element_ids if e in selected],
        )
        if args["subject_ids"] and args["primary_subject_id"]
        else None
    )
    draft = SelectionDraft(candidate=candidate)
    for c in analysis.intent.constraints:
        if c.priority != "required":
            continue
        if c.kind == "exclusion":
            conflict = bool(
                set(c.subject_ids) & set(args["subject_ids"]) or set(c.element_ids) & selected
            )
        else:
            conflict = (
                not set(c.subject_ids) <= set(args["subject_ids"])
                or not set(c.element_ids) <= selected
            )
        if conflict:
            draft.blocking_issues.append(
                Issue(
                    issue_id="selection-" + c.constraint_id[:48],
                    code="selection_conflict",
                    resolution="reanalyze",
                    message="选择与用户必需指令冲突，请修改简述后重新分析",
                    subject_ids=c.subject_ids,
                    element_ids=c.element_ids,
                )
            )
    active_facts = {
        f
        for s in analysis.subjects
        if s.subject_id in args["subject_ids"]
        for f in s.identity_fact_ids
    }
    active_facts.update(
        f for e in analysis.elements if e.element_id in selected for f in e.fact_ids
    )
    active_materials = {
        mid
        for s in analysis.subjects
        if s.subject_id in args["subject_ids"]
        for mid in s.material_ids
    }
    for issue in analysis.issues + analysis.intent.unmet_requirements:
        referenced = bool(
            issue.subject_ids or issue.element_ids or issue.fact_ids or issue.material_ids
        )
        active = (
            bool(set(issue.subject_ids) & set(args["subject_ids"]))
            or bool(set(issue.element_ids) & selected)
            or bool(set(issue.fact_ids) & active_facts)
            or bool(set(issue.material_ids) & active_materials)
        )
        if referenced and not active:
            draft.issue_resolutions.append(
                IssueResolution(
                    issue_id=issue.issue_id, status="irrelevant", reason="问题仅涉及未选对象"
                )
            )
        elif issue.resolution == "reanalyze":
            draft.blocking_issues.append(issue)
        elif (
            issue.resolution == "selection"
            and selection.mode == "explicit"
            and candidate
            and (
                not issue.subject_ids
                or len(set(issue.subject_ids) & set(candidate.subject_ids)) == 1
            )
        ):
            draft.issue_resolutions.append(
                IssueResolution(
                    issue_id=issue.issue_id,
                    status="resolved",
                    reason="已显式选择唯一候选，快照仍需复核",
                )
            )
        else:
            draft.pending_issues.append(issue)
    required_facts = {
        f
        for s in analysis.subjects
        if s.subject_id in args["subject_ids"]
        for f in s.identity_fact_ids
    }
    required_elements = set(args["required_element_ids"])
    required_elements.update(
        e
        for c in analysis.intent.constraints
        if c.priority == "required" and c.kind != "exclusion"
        for e in c.element_ids
    )
    required_facts.update(
        f for e in analysis.elements if e.element_id in required_elements for f in e.fact_ids
    )
    covered = {f for i in draft.pending_issues + draft.blocking_issues for f in i.fact_ids}
    for fact in analysis.facts:
        if fact.fact_id in required_facts - covered and fact.confidence < 0.7:
            draft.pending_issues.append(
                Issue(
                    issue_id="confidence-" + fact.fact_id[:48],
                    code="low_confidence",
                    resolution="recheck",
                    message="必需事实可信度不足，请核对素材",
                    fact_ids=[fact.fact_id],
                )
            )
    return draft


def finalize_selection(draft, evidence=None):
    resolutions = list(draft.issue_resolutions)
    if draft.candidate is None or draft.blocking_issues:
        return SelectionResolution(
            status="needs_input",
            selection=draft.candidate,
            issues=draft.blocking_issues + draft.pending_issues,
            issue_resolutions=resolutions,
        )
    if evidence is None:
        return SelectionResolution(
            status="needs_input" if draft.pending_issues else "ready",
            selection=draft.candidate,
            issues=draft.pending_issues,
            issue_resolutions=resolutions,
        )
    expected = {i.issue_id for i in draft.pending_issues}
    ids = [r.issue_id for r in evidence.issue_resolutions]
    if (
        set(ids) != expected
        or len(ids) != len(set(ids))
        or any(r.status == "irrelevant" for r in evidence.issue_resolutions)
    ):
        raise ProviderError("missing or invalid evidence issue resolutions")
    resolutions += evidence.issue_resolutions
    mismatch = any(not s.same_product for s in evidence.subject_checks)
    if evidence.outcome == "failed" or mismatch:
        return SelectionResolution(
            status="failed",
            selection=draft.candidate,
            issue_resolutions=resolutions,
            error_info=ErrorInfo(
                code="input_mismatch", kind="input", message="所选商品或事实与素材不符"
            ),
        )
    unresolved = {r.issue_id for r in evidence.issue_resolutions if r.status == "unresolved"}
    valid = (
        evidence.intent_valid
        and all(s.score >= 85 for s in evidence.subject_checks)
        and all(f.presence == "present" and f.fidelity_score >= 85 for f in evidence.fact_checks)
    )
    issues = [i for i in draft.pending_issues if i.issue_id in unresolved] + evidence.issues
    if evidence.outcome != "verified" or not valid or unresolved or issues:
        if not issues:
            issues = [
                Issue(
                    issue_id="unverified",
                    code="missing_evidence",
                    resolution="reanalyze",
                    message="证据或用户意图尚未核实，请补充素材后重新分析",
                )
            ]
        return SelectionResolution(
            status="needs_input",
            selection=draft.candidate,
            issues=issues,
            issue_resolutions=resolutions,
        )
    return SelectionResolution(
        status="ready", selection=draft.candidate, issue_resolutions=resolutions
    )


def _conflict(message):
    raise ProviderError(message, kind="capability", code="requirement_conflict")


def compile_element_plan(request, target, context, config):
    analysis, selection = context.analysis, context.selection
    subjects = {s.subject_id: s for s in analysis.subjects}
    elements = {e.element_id: e for e in analysis.elements}
    required = list(selection.required_element_ids)
    preferred = list(selection.preferred_element_ids)
    excluded = list(selection.excluded_element_ids)
    brief_required = set()
    required_subjects = set()
    for c in selection.constraints:
        if c.priority == "required" and c.kind != "exclusion":
            brief_required.update(c.element_ids)
            required_subjects.update(c.subject_ids)
    required = list(dict.fromkeys(required + [e for e in elements if e in brief_required]))
    preferred = [e for e in preferred if e not in required]
    required_subjects.update(elements[e].subject_id for e in required if elements[e].subject_id)
    reasons = {e: "explicit exclusion" for e in excluded}
    if strict_catalog(target):
        if any(elements[e].kind in ("scene", "style") for e in required) or any(
            c.priority == "required" and c.kind == "atmosphere" for c in selection.constraints
        ):
            _conflict("必需场景/风格与平台白底规则冲突")
        for e in list(preferred):
            if elements[e].kind in ("scene", "style"):
                preferred.remove(e)
                excluded.append(e)
                reasons[e] = "platform catalog background"
    focus_candidates = [
        e
        for e in selection.focus_element_ids
        if e in required + preferred and elements[e].kind in ("detail", "accessory", "identity")
    ]
    focus_candidates += [
        e.element_id
        for e in analysis.elements
        if e.element_id in required + preferred and e.kind == "detail"
    ]
    focus = next(iter(focus_candidates), None)
    if target.variant == "closeup" and focus is None:
        _conflict("特写缺少可信的焦点细节")
    cropped = target.variant in ("feature", "closeup") and focus is not None
    focus_subject = elements[focus].subject_id if focus else selection.primary_subject_id
    visible = (
        set(selection.subject_ids)
        if not cropped
        else ({focus_subject} if focus_subject else {selection.primary_subject_id})
        | required_subjects
    )
    user_facts = {f for e in required for f in elements[e].fact_ids}
    if target.variant == "closeup" and (
        not user_facts <= set(elements[focus].fact_ids) or len(visible) > 1
    ):
        _conflict("微距无法同时呈现每资产必需对象或细节")
    if target.output_type == "pdd_white_background" and len(visible) > 1:
        _conflict("白底单件规则不支持多个必需主体")
    plan = AssetElementPlan(
        target_key=target.target_key,
        legacy_input=not bool(request.materials),
        subject_ids=[selection.primary_subject_id]
        + [sid for sid in selection.subject_ids if sid != selection.primary_subject_id],
        focus_element_id=focus,
        required_element_ids=required,
        preferred_element_ids=preferred,
        excluded_element_ids=excluded,
        excluded_reasons=reasons,
        constraints=[
            c
            for c in selection.constraints
            if c.priority == "required" or not strict_catalog(target) or c.kind != "atmosphere"
        ],
    )
    reqs = {}

    def add(kind, id, origin, app, reason):
        key = (kind, id)
        previous = reqs.get(key)
        if (
            previous
            and previous.applicability == "must_show"
            and previous.origin in ("user_required", "brief_required")
        ):
            return
        reqs[key] = Requirement(
            target_kind=kind, target_id=id, origin=origin, applicability=app, reason=reason
        )

    for sid in selection.subject_ids:
        app = "must_show" if sid in visible else "not_applicable"
        add(
            "subject",
            sid,
            "brief_required" if sid in required_subjects else "default_identity",
            app,
            "visible subjects follow shot; required cannot be exempted",
        )
        description = " ".join(f.description for f in analysis.facts if f.subject_id == sid)
        plan.subject_presentations[sid] = (
            "model_wear"
            if target.presentation_mode == "model_wear"
            and (is_apparel(description) or len(selection.subject_ids) == 1)
            else "product_only"
        )
        for fid in subjects[sid].identity_fact_ids:
            add(
                "fact",
                fid,
                "default_identity",
                "not_applicable"
                if sid not in visible
                else "preserve_if_visible"
                if cropped
                else "must_show",
                "preserve identity within shot visibility",
            )
    for eid in required + preferred:
        element = elements[eid]
        origin = (
            "brief_required"
            if eid in brief_required
            else "user_required"
            if eid in required
            else "shot_rule"
            if eid == focus and cropped
            else "preferred"
        )
        app = (
            "must_show" if eid in required or (eid == focus and cropped) else "preserve_if_visible"
        )
        if element.subject_id and element.subject_id not in visible:
            app = "not_applicable"
        add("element", eid, origin, app, "compiled element visibility")
        for fid in element.fact_ids:
            if ("fact", fid) not in reqs or origin != "preferred":
                add("fact", fid, origin, app, "element atomic evidence")
    for c in plan.constraints:
        add(
            "constraint",
            c.constraint_id,
            "brief_required" if c.priority == "required" else "preferred",
            "must_show" if c.priority == "required" else "preserve_if_visible",
            c.instruction,
        )
    plan.requirements = list(reqs.values())
    plan.required_fact_ids = list(
        dict.fromkeys(
            [f for s in analysis.subjects if s.subject_id in visible for f in s.identity_fact_ids]
            + [
                r.target_id
                for r in plan.requirements
                if r.target_kind == "fact"
                and r.applicability == "must_show"
                and r.origin != "preferred"
            ]
        )
    )
    _populate_references(
        plan,
        analysis,
        context.materials,
        config.generation_reference_limit,
        legacy=not bool(request.materials),
    )
    return plan


def _populate_references(plan, analysis, materials, limit, *, legacy=False):
    ordered = [m.material_id for m in materials]
    facts = {f.fact_id: f for f in analysis.facts}
    elements = {e.element_id: e for e in analysis.elements}
    subjects = {s.subject_id: s for s in analysis.subjects}
    visible = [
        r.target_id
        for r in plan.requirements
        if r.target_kind == "subject" and r.applicability != "not_applicable"
    ]
    chosen = list(
        dict.fromkeys(
            subjects[s].representative_material_id for s in plan.subject_ids if s in visible
        )
    )

    def evidence(fid):
        return {e.material_id for e in facts[fid].evidence}

    for fid in plan.required_fact_ids:
        if not set(chosen) & evidence(fid):
            chosen.append(next(mid for mid in ordered if mid in evidence(fid)))
    if len(chosen) > limit:
        raise ProviderError(
            "required evidence exceeds generation reference capacity",
            kind="capability",
            code="reference_capacity_exceeded",
        )
    optional = sorted(
        plan.preferred_element_ids,
        key=lambda e: (
            {"identity": 0, "detail": 1, "accessory": 1, "scene": 2, "style": 3}[elements[e].kind],
            list(elements).index(e),
        ),
    )
    for eid in optional:
        el = elements[eid]
        if el.subject_id and el.subject_id not in visible:
            continue
        for fid in el.fact_ids:
            candidates = evidence(fid)
            if legacy and el.kind == "style":
                candidates &= {"ref-1"}
            if not set(chosen) & candidates and len(chosen) < limit:
                mid = next((m for m in ordered if m in candidates), None)
                if mid:
                    chosen.append(mid)
        if any(not set(chosen) & evidence(f) for f in el.fact_ids):
            plan.text_only_element_ids.append(eid)
    related = set(mid for sid in visible for mid in subjects[sid].material_ids)
    relevant_facts = set(plan.required_fact_ids)
    relevant_facts.update(
        f
        for eid in plan.required_element_ids + plan.preferred_element_ids
        for f in elements[eid].fact_ids
        if not elements[eid].subject_id or elements[eid].subject_id in visible
    )
    related.update(mid for fid in relevant_facts for mid in evidence(fid))
    if legacy:
        related = {m for m in related if not m.startswith("ref-") or m == "ref-1"}
        catalog = (
            plan.target_key.startswith("amazon.main_image.")
            or ".pdd_white_background." in plan.target_key
        )
        if not catalog and "ref-1" in ordered:
            related.add("ref-1")
            if "ref-1" not in chosen and len(chosen) < limit:
                chosen.append("ref-1")
    plan.generation_material_ids = chosen
    plan.audit_material_ids = [m for m in ordered if m in related]
    plan.reference_bindings = [
        ReferenceBinding(
            index=i,
            material_id=mid,
            fact_ids=[
                f.fact_id
                for f in analysis.facts
                if f.fact_id in relevant_facts and mid in evidence(f.fact_id)
            ],
            element_ids=[
                e
                for e in plan.required_element_ids + plan.preferred_element_ids
                if any(mid in evidence(f) for f in elements[e].fact_ids)
            ],
            role="style"
            if legacy and mid == "ref-1"
            else "identity"
            if any(subjects[s].representative_material_id == mid for s in visible)
            else next(
                (
                    elements[e].kind
                    for e in plan.required_element_ids + plan.preferred_element_ids
                    if any(mid in evidence(f) for f in elements[e].fact_ids)
                ),
                "detail",
            ),
        )
        for i, mid in enumerate(chosen, 1)
    ]


def select_references(plan, analysis, materials, *, limit, reserve_stage=False):
    adjusted = plan.model_copy(deep=True)
    adjusted.text_only_element_ids = []
    _populate_references(
        adjusted,
        analysis,
        materials,
        limit - int(reserve_stage),
        legacy=plan.legacy_input,
    )
    by_id = {m.material_id: m.data for m in materials}
    return tuple(
        GenerationReference(
            material_id=b.material_id,
            data=by_id[b.material_id],
            role=b.role,
            element_ids=b.element_ids,
        )
        for b in adjusted.reference_bindings
    )


def bind_generation_references(plan, analysis, references):
    """Rebind an attempt's image numbers while retaining its frozen requirements."""
    rebound = plan.model_copy(deep=True)
    previous = {b.material_id: b for b in plan.reference_bindings if b.material_id}
    bindings = []
    for index, reference in enumerate(references, 1):
        if reference.material_id:
            binding = previous[reference.material_id].model_copy(update={"index": index})
        else:
            binding = ReferenceBinding(
                index=index,
                stage_id=reference.stage_id,
                fact_ids=[],
                element_ids=reference.element_ids,
                role=reference.role,
            )
        bindings.append(binding)
    rebound.reference_bindings = bindings
    rebound.generation_material_ids = [r.material_id for r in references if r.material_id]
    selected = set(rebound.generation_material_ids)
    stage_elements = {eid for r in references if r.stage_id for eid in r.element_ids}
    facts = {f.fact_id: f for f in analysis.facts}
    rebound.text_only_element_ids = [
        e.element_id
        for e in analysis.elements
        if e.element_id in plan.preferred_element_ids
        and e.element_id not in stage_elements
        and any(
            not selected.intersection(v.material_id for v in facts[fid].evidence)
            for fid in e.fact_ids
        )
    ]
    return rebound
