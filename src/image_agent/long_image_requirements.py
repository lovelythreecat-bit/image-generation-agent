"""Extract and validate source-bound page copy before any image generation."""

import hashlib
import json
import re

from pydantic import ValidationError

from .errors import AgentError
from .long_image_models import (
    LongImagePlan,
    LongImageRequest,
    LongImageRequirement,
    LongTextSource,
    section_text_units,
)


def _invalid(message):
    raise AgentError(message, code="plan_invalid")


def _content_hash(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def collect_text_requirements(request: LongImageRequest) -> list[LongImageRequirement]:
    requirements = []

    def add(rid, text, pointer, *, start=None, end=None, occurrences=1):
        requirements.append(
            LongImageRequirement(
                requirement_id=rid,
                text=text,
                source=LongTextSource(pointer=pointer, start=start, end=end),
                order=len(requirements),
                occurrences=occurrences,
            )
        )

    add("title", request.title, "/title")
    for index, spec in enumerate(request.product_specs):
        add(f"spec-{index}-name", spec.name, f"/product_specs/{index}/name")
        add(f"spec-{index}-value", spec.value, f"/product_specs/{index}/value")
    for index, text in enumerate(request.service_notes):
        add(f"service-{index}", text, f"/service_notes/{index}")
    for index, copy in enumerate(request.required_copy):
        add(
            copy.requirement_id,
            copy.text,
            f"/required_copy/{index}/text",
            occurrences=copy.occurrences,
        )

    # Restrict extraction to explicit copy instructions immediately before quotes.
    # Ordinary creative instructions remain instructions, never automatic page copy.
    marker = (
        r"(?:原样(?:展示|显示|呈现|保留|写出)?|必须(?:展示|显示|呈现|写出|保留)|文案)\s*[:：]?\s*"
    )
    quoted = r'(?:“(?P<cn>[^”]+)”|「(?P<jp>[^」]+)」|"(?P<en>[^\"]+)")'
    for match in re.finditer(marker + quoted, request.content_brief):
        group = next(name for name in ("cn", "jp", "en") if match.group(name) is not None)
        start, end = match.span(group)
        add(f"brief-{start}-{end}", match.group(group), "/content_brief", start=start, end=end)
    ids = [r.requirement_id for r in requirements]
    if len(ids) != len(set(ids)):
        _invalid("required_copy IDs conflict with local text requirement IDs")
    return requirements


def planning_fingerprint(request: LongImageRequest, material_hashes: dict[str, str]) -> str:
    data = request.model_dump(mode="json", exclude={"materials", "output_dir", "request_id"})
    data["options"].pop("font_path", None)
    data["materials"] = [m.model_dump(mode="json", exclude={"source"}) for m in request.materials]
    return _content_hash({"request": data, "material_hashes": material_hashes})


def _source_value(request, source):
    if source.model_authored:
        _invalid("requirements must be bound to user input")
    value = request.model_dump(mode="python")
    try:
        for part in source.pointer[1:].split("/"):
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(value, list):
                if not re.fullmatch(r"0|[1-9][0-9]*", part):
                    _invalid("invalid array source pointer")
                value = value[int(part)]
            else:
                value = value[part]
    except (KeyError, IndexError, TypeError):
        _invalid("text source pointer does not resolve")
    if not isinstance(value, str):
        _invalid("text source must point to a string")
    if source.start is not None:
        if source.end > len(value):
            _invalid("text source range is outside input")
        value = value[source.start : source.end]
    return value


def validate_long_image_plan(
    request: LongImageRequest,
    plan: LongImagePlan,
    *,
    material_hashes: dict[str, str],
    analysis=None,
) -> None:
    try:
        # Revalidate model_copy/model_construct inputs and mutable nested containers.
        plan = LongImagePlan.model_validate(plan.model_dump(mode="python", warnings=False))
    except (ValidationError, AttributeError, TypeError, ValueError) as error:
        _invalid(f"invalid long-image plan structure: {error}")
    if plan.scene != request.scene or plan.input_fingerprint != planning_fingerprint(
        request, material_hashes
    ):
        _invalid("plan does not match current request and material hashes")
    expected_materials = {m.material_id for m in request.materials}
    if set(material_hashes) != expected_materials or any(not h for h in material_hashes.values()):
        _invalid("material hash bindings must cover exactly the request materials")
    for slot in plan.image_slots:
        if not set(slot.material_ids) <= expected_materials:
            _invalid("unknown slot material reference")

    requirements = {r.requirement_id: r for r in plan.requirements}
    for expected in collect_text_requirements(request):
        actual = requirements.get(expected.requirement_id)
        if (
            actual is None
            or any(
                getattr(actual, key) != getattr(expected, key)
                for key in ("text", "source", "order", "occurrences", "required")
            )
            or actual.kind not in ("page_text", "section_text")
        ):
            _invalid(f"local text requirement changed or omitted: {expected.requirement_id}")
    for requirement in plan.requirements:
        if _source_value(request, requirement.source) != requirement.text:
            _invalid(f"requirement text differs from input source: {requirement.requirement_id}")

    coverage = {r.requirement_id: [] for r in plan.requirements if r.kind != "slot_visual"}
    read_order = []
    for section in plan.sections:
        for unit in section_text_units(section):
            if not unit.source.model_authored and _source_value(request, unit.source) != unit.text:
                _invalid(f"text differs from its source: {unit.unit_id}")
            # Identical strings with different IDs each require a separate rendered unit.
            if len(unit.requirement_ids) > 1:
                _invalid("a text unit cannot satisfy multiple requirement occurrences")
            for rid in unit.requirement_ids:
                req = requirements[rid]
                if unit.text != req.text or unit.source != req.source:
                    _invalid(f"required copy changed or forged: {rid}")
                if req.section_ids and section.section_id not in req.section_ids:
                    _invalid(f"required copy is in the wrong section: {rid}")
                coverage[rid].append(unit.unit_id)
                if req.order is not None:
                    read_order.append(req.order)
    for rid, units in coverage.items():
        requirement = requirements[rid]
        if requirement.required and len(units) != requirement.occurrences:
            _invalid(f"required copy occurrence count differs: {rid}")
    if read_order != sorted(read_order):
        _invalid("required copy reading order differs from input")

    if request.scene == "product_detail":
        if analysis is None or plan.analysis_ref is None or plan.resolved_selection is None:
            _invalid("product plan requires analysis snapshot binding and resolved selection")
    elif plan.analysis_ref is not None or plan.resolved_selection is not None:
        _invalid("nonproduct plan cannot bind product analysis or selection")
    if plan.analysis_ref is not None:
        ref = plan.analysis_ref
        if (
            analysis is None
            or ref.analysis_id != analysis.analysis_id
            or ref.fingerprint != analysis.fingerprint
            or ref.content_hash != _content_hash(analysis.model_dump(mode="json"))
        ):
            _invalid("product analysis snapshot binding is stale or forged")
        if {m.material_id: m.sha256 for m in analysis.materials} != material_hashes:
            _invalid("product analysis material hashes differ")
        resolved = plan.resolved_selection
        if not resolved.subject_ids or resolved.primary_subject_id not in resolved.subject_ids:
            _invalid("resolved product selection requires a selected primary subject")
        subjects = {s.subject_id for s in analysis.subjects}
        elements = {e.element_id for e in analysis.elements}
        if (
            not set(resolved.subject_ids) <= subjects
            or not set(
                resolved.required_element_ids
                + resolved.preferred_element_ids
                + resolved.excluded_element_ids
            )
            <= elements
        ):
            _invalid("resolved selection references unknown product evidence")
        if request.selection.mode == "explicit" and any(
            getattr(request.selection, key) != getattr(resolved, key)
            for key in (
                "subject_ids",
                "required_element_ids",
                "preferred_element_ids",
                "excluded_element_ids",
            )
        ):
            _invalid("resolved selection differs from explicit user selection")
