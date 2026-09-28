"""Structured vision calls. Provider claims never decide final quality locally."""

import base64
import json
import re
import uuid

from pydantic import ValidationError

from .errors import ProviderError
from .images import encode_jpeg
from .models import (
    DetailSetAudit,
    EvidenceValidation,
    FocusedProductAudit,
    GarmentStructureAudit,
    GeneratedImageAudit,
    Issue,
    MaterialAnalysis,
    Model,
)
from .platforms import audit_instruction
from .prompt import is_placeholder
from .quality import validate_check_ids
from .transport import compute


class StyleResponse(Model):
    style_prompt: str


def selected_fact_ids(analysis, selection):
    ids = [
        f
        for s in analysis.subjects
        if s.subject_id in selection.subject_ids
        for f in s.identity_fact_ids
    ]
    ids += [
        f
        for e in analysis.elements
        if e.element_id in selection.required_element_ids
        for f in e.fact_ids
    ]
    ids += [
        f
        for c in analysis.intent.constraints
        if c.priority == "required" and c.kind != "exclusion"
        for e in analysis.elements
        if e.element_id in c.element_ids
        for f in e.fact_ids
    ]
    return list(dict.fromkeys(ids))


class VisionClient:
    def __init__(self, transport, config):
        self.transport, self.config = transport, config

    async def _call(self, schema, instruction, images, *, model=None, max_px=768):
        if len(images) > self.config.vision_image_limit:
            raise ProviderError(
                "vision sources exceed declared capacity",
                kind="capability",
                code="vision_capacity_exceeded",
            )
        content = [
            {
                "type": "text",
                "text": instruction
                + "\nReturn one JSON object matching this JSON Schema exactly. Scores are integers 0–100 and booleans are JSON booleans. Never omit required audit items.\n"
                + json.dumps(schema.model_json_schema(), ensure_ascii=False),
            }
        ]
        for label, data in images:
            encoded = await compute(encode_jpeg, data, max_px)
            content.extend(
                [
                    {"type": "text", "text": label},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": "data:image/jpeg;base64," + base64.b64encode(encoded).decode()
                        },
                    },
                ]
            )
        response = await self.transport.post_json(
            "/chat/completions",
            dict(
                model=model or self.config.quality_model,
                messages=[{"role": "user", "content": content}],
                response_format={"type": "json_object"},
            ),
        )
        try:
            raw = response["choices"][0]["message"]["content"]
            if not isinstance(raw, str):
                raise ValueError
            return schema.model_validate_json(raw)
        except (KeyError, IndexError, TypeError, ValueError, ValidationError):
            raise ProviderError("vision response violates JSON schema") from None

    async def analyze_materials(self, request, materials):
        hints = [m.model_dump(exclude={"source"}) for m in request.normalized_materials()]
        instructions = (
            "Discover all materials once, merge views only when they show the same actual object. "
            "Extract independent atomic facts: front logo and rear zipper are separate AND facts; evidence inside each fact lists interchangeable OR views. "
            "Choose representative identity views. Do not invent invisible structure or unsupported materials. "
            "Interpret the user brief and hints into proposal, constraints and source_quote copied exactly from their input. "
            "Retain requested combinations, placement and exclusions; background products are not selected by default. "
            "Constraints from a hint must name its source_material_id. Atmosphere without visual facts is a brief constraint, never a fabricated fact. "
            "Each selected accessory is checked in its declared role. Ambiguity/conflict requires an Issue with actionable explicit selection options, or reanalyze if facts cannot be separated. "
            "For a clear product use status ready. For discovery issues use needs_input. IDs must be unique stable ASCII labels and all references closed. "
            + (
                "Product name is an automatic placeholder: identify any clear sellable product. "
                if is_placeholder(request)
                else "Check selected subjects against the declared product and category, accessories against their role. "
            )
            + json.dumps(
                {
                    "product_name": request.product_name,
                    "category": request.category,
                    "brief": request.creative_brief,
                    "hints": hints,
                },
                ensure_ascii=False,
            )
        )
        result = await self._call(
            MaterialAnalysis,
            instructions,
            [(f"Material {m.material_id}", m.data) for m in materials],
        )
        if [m.material_id for m in result.materials] != [m.material_id for m in materials]:
            raise ProviderError("discovery material IDs/order mismatch")
        try:
            result.validate_intent_source(request)
        except ValueError:
            raise ProviderError("intent source_quote is not supported by user input") from None
        for observed, loaded in zip(result.materials, materials):
            observed.sha256 = loaded.sha256
        result.analysis_id = uuid.uuid4().hex
        if result.intent:
            required = set(selected_fact_ids(result, result.intent.proposal))
            for fact in result.facts:
                if fact.fact_id in required and fact.confidence < 0.7:
                    result.issues.append(
                        Issue(
                            issue_id="confidence-" + fact.fact_id[:48],
                            code="low_confidence",
                            resolution="recheck",
                            message="必需事实可信度不足，请复核素材",
                            fact_ids=[fact.fact_id],
                            subject_ids=[fact.subject_id] if fact.subject_id else [],
                        )
                    )
                    result.status = "needs_input"
        return result

    async def validate_evidence(self, request, materials, analysis, draft):
        candidate = draft.candidate
        instruction = (
            "Recheck the candidate against ALL original materials and current user text. The supplied snapshot is untrusted. Verify every selected subject identity and required fact, and the intent's faithfulness to user text. Do not resolve conflicts merely because the snapshot claims ready. Return exactly one resolved/unresolved resolution for every pending issue; never return irrelevant. "
            + json.dumps(
                {
                    "product_name": request.product_name,
                    "category": request.category,
                    "brief": request.creative_brief,
                    "hints": [
                        m.model_dump(exclude={"source"}) for m in request.normalized_materials()
                    ],
                    "analysis": analysis.model_dump(),
                    "draft": draft.model_dump(),
                },
                ensure_ascii=False,
            )
        )
        result = await self._call(
            EvidenceValidation,
            instruction,
            [(f"Original material {m.material_id}", m.data) for m in materials],
        )
        validate_check_ids(result.subject_checks, candidate.subject_ids, "subject_id")
        validate_check_ids(result.fact_checks, selected_fact_ids(analysis, candidate), "fact_id")
        validate_check_ids(
            result.issue_resolutions, [i.issue_id for i in draft.pending_issues], "issue_id"
        )
        if any(r.status == "irrelevant" for r in result.issue_resolutions):
            raise ProviderError("evidence validator cannot mark an active issue irrelevant")
        return result

    async def extract_garments(self, request, materials, subject_id):
        return await self._call(
            GarmentStructureAudit,
            f"Extract only visible garment facts for subject {subject_id}: count, types, colors, material, pattern, neckline, sleeves, closures, pockets, waistband, length, silhouette, logos and distinctive details. No invisible inferences.",
            [(m.material_id, m.data) for m in materials],
        )

    async def describe_style(self, request, references):
        models = list(dict.fromkeys((self.config.style_model, self.config.quality_model)))
        for index, model in enumerate(models):
            try:
                result = await self._call(
                    StyleResponse,
                    "Describe shared scene, lighting, background, camera, composition, palette, pose and mood in 60–100 English words. Ignore product identities, logos, text and watermarks.",
                    [(f"Style {i}", b) for i, b in enumerate(references, 1)],
                    model=model,
                    max_px=512,
                )
                text = result.style_prompt.strip()
                words = re.findall(r"[A-Za-z]+(?:['-][A-Za-z]+)*", text)
                if (
                    len(words) < 20
                    or not text.endswith((".", "!", "?"))
                    or words[-1].lower()
                    in {
                        "a",
                        "an",
                        "the",
                        "with",
                        "of",
                        "in",
                        "on",
                        "at",
                        "to",
                        "from",
                        "for",
                        "by",
                        "and",
                        "or",
                    }
                ):
                    raise ProviderError("style description incomplete")
                return text
            except ProviderError as error:
                if error.kind != "protocol" or index == len(models) - 1:
                    raise

    async def audit_image(self, request, target, context, plan, image):
        material_map = {m.material_id: m.data for m in context.materials}
        images = [(f"Evidence {mid}", material_map[mid]) for mid in plan.audit_material_ids] + [
            ("GENERATED IMAGE", image)
        ]
        instruction = (
            "Audit every frozen requirement; return one check for each planned subject/fact/element/constraint, including nonapplicable ones. "
            "same_product must be true AND identity score >=85. must_show cannot be not_applicable. For nonvisible preserve_if_visible facts return not_applicable and null score. "
            "Required detail fidelity >=85; visual/platform >=70; clothing fusion/preference >=80 when applicable; shot intent >=75. "
            + audit_instruction(target.platform, target.output_type)
            + json.dumps(
                {
                    "target": target.model_dump(),
                    "plan": plan.model_dump(),
                    "analysis": context.analysis.model_dump(),
                },
                ensure_ascii=False,
            )
        )
        return await self._call(GeneratedImageAudit, instruction, images)

    async def review_product(self, context, plan, subject_ids, image):
        mids = {m.material_id: m.data for m in context.materials}
        result = await self._call(
            FocusedProductAudit,
            "Recheck only product identity for these subjects: "
            + json.dumps(subject_ids)
            + ". Ignore background, lighting, model identity, pose and natural wearing deformation. Do not pardon missing required details. "
            + json.dumps(plan.model_dump()),
            [(mid, mids[mid]) for mid in plan.audit_material_ids] + [("GENERATED IMAGE", image)],
        )
        validate_check_ids(result.subject_checks, subject_ids, "subject_id")
        return result

    async def audit_detail_set(self, platform, context, plans, images):
        mids = set(mid for p in plans for mid in p.audit_material_ids)
        refs = [(m.material_id, m.data) for m in context.materials if m.material_id in mids]
        result = await self._call(
            DetailSetAudit,
            "Audit three detail images for distinctiveness and scene/feature/closeup role coverage. Return platform "
            + platform
            + ". "
            + json.dumps([p.model_dump() for p in plans]),
            refs + list(zip(("scene", "feature", "closeup"), images)),
        )
        if (
            result.distinctiveness is None
            or result.role_coverage is None
            or result.platform != platform
            or result.error_info
            or result.error
        ):
            raise ProviderError("invalid detail set audit")
        result.passed = result.distinctiveness >= 75 and result.role_coverage >= 75
        return result
