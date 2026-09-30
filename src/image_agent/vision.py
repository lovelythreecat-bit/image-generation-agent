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


def _parse_vision_response(response, schema):
    try:
        choice = response["choices"][0]
        message = choice["message"]
        if message.get("refusal") or choice.get("finish_reason") == "content_filter":
            raise ProviderError("vision request refused", code="vision_refusal")
        if choice.get("finish_reason") == "length":
            raise ProviderError("vision response was truncated (finish_reason=length)")
        raw = message["content"]
        if isinstance(raw, list):
            if not raw or any(
                not isinstance(block, dict)
                or block.get("type") != "text"
                or not isinstance(block.get("text"), str)
                for block in raw
            ):
                raise ValueError
            raw = "".join(block["text"] for block in raw)
        if isinstance(raw, str):
            raw = raw.strip()
            fence = re.fullmatch(r"```(?:json)?\s*\n?(.*?)\s*```", raw, flags=re.S | re.I)
            if fence:
                raw = fence.group(1)
            raw = json.loads(raw)
        if not isinstance(raw, dict):
            raise ValueError
        return schema.model_validate(raw)
    except ValidationError as error:
        details = "; ".join(
            f"{'.'.join(map(str, row['loc'])) or '$'}: {row['type']}"
            for row in error.errors(include_input=False, include_context=False)[:6]
        )
        raise ProviderError("vision response violates JSON schema: " + details) from None
    except json.JSONDecodeError as error:
        raise ProviderError(
            f"vision response contains invalid JSON at line {error.lineno}, column {error.colno}"
        ) from None
    except (KeyError, IndexError, TypeError, ValueError, AttributeError):
        raise ProviderError(
            "vision response has invalid choices/message/content structure"
        ) from None


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
    facts = {f.fact_id: f for f in analysis.facts}
    return [
        fid
        for fid in dict.fromkeys(ids)
        if facts[fid].effective_verifiability != "functional_claim"
    ]


class VisionClient:
    def __init__(self, transport, config):
        self.transport, self.config = transport, config

    async def _call(
        self, schema, instruction, images, *, model=None, max_px=768, stage="vision", validate=None
    ):
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
        transport, path = self.transport, "/chat/completions"
        route = self.config.vision_route(stage)
        if route:
            service = self.config.services[route.service]
            transport, path = self.transport.for_service(route.service), service.chat_path
            model = route.model
        for attempt in range(2):
            # Transport errors keep their existing retry policy. Only a returned
            # response's format/ID errors get this single corrective vision call.
            response = await transport.post_json(
                path,
                dict(
                    model=model or self.config.quality_model,
                    messages=[{"role": "user", "content": content}],
                    response_format={"type": "json_object"},
                ),
            )
            try:
                result = _parse_vision_response(response, schema)
                if validate:
                    validate(result)
                return result
            except ProviderError as error:
                if error.code == "vision_refusal" or attempt == 1:
                    raise ProviderError(
                        f"视觉步骤 {stage} ({schema.__name__}, attempt={attempt + 1}/2): {error.message}",
                        code=error.code,
                        kind=error.kind,
                        status_code=error.status_code,
                    ) from None
                content = [
                    {
                        "type": "text",
                        "text": content[0]["text"]
                        + "\nThe previous response failed validation: "
                        + error.message
                        + ". Recheck the SAME images and return the complete JSON object. "
                        "Correct the schema/IDs without inventing passing checks or changing evidence. "
                        "No markdown or prose. Never use null unless the schema permits it. "
                        "For presence checks, fidelity_score is an integer only for present; otherwise null.",
                    },
                    *content[1:],
                ]

    async def analyze_materials(self, request, materials):
        hints = [m.model_dump(exclude={"source"}) for m in request.normalized_materials()]
        instructions = (
            "Discover all materials once, merge views only when they show the same actual object. "
            "Extract independent atomic facts: front logo and rear zipper are separate AND facts; evidence inside each fact lists interchangeable OR views. "
            "Choose representative identity views. Do not invent invisible structure or unsupported materials. "
            "Explicitly classify EVERY fact.verifiability as visible_appearance, functional_claim or unverified, "
            "and EVERY evidence.source_type as visual, product_label, promotional_text or inference; do not use unknown for new discovery. "
            "Classify by evidence meaning, never product-category keywords. Functional capability/performance asserted only by advertising or inference "
            "is a functional_claim, never an identity fact or required visible structure. Do not infer concealed components from claims. "
            "A printed physical label, logo, character illustration or artwork on the actual product is visible_appearance with product_label evidence; "
            "its appearance must be preserved, while any performance assertion it contains is a SEPARATE functional_claim. "
            "Promotional overlays around the product are not product-native markings. Mixed visual and functional statements must be split into atomic facts. "
            "Uncertain genuinely required appearance is unverified and requires missing-evidence clarification. "
            "Interpret the user brief and hints into proposal, constraints and source_quote copied exactly from their input. "
            "For source=brief, source_quote must be a verbatim contiguous substring of brief only. "
            "For source=hint, quote a verbatim contiguous substring of subject_hint or one element_hints item "
            "belonging to the specified source_material_id only. "
            "Never summarize or join separate lines/hints, normalize whitespace, or substitute product_name, category or image OCR as the quoted source. "
            "If several keywords are relevant, quote one exact shorter substring or preserve the original newline characters. "
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

        def validate_discovery(result):
            if [m.material_id for m in result.materials] != [m.material_id for m in materials]:
                raise ProviderError("discovery material IDs/order mismatch")
            try:
                result.validate_intent_source(request)
            except ValueError:
                raise ProviderError("intent source_quote is not supported by user input") from None
            for fact in result.facts:
                if "verifiability" not in fact.model_fields_set:
                    raise ProviderError(
                        f"fact {fact.fact_id}: explicit verifiability required for fresh discovery"
                    )
                if any(
                    "source_type" not in e.model_fields_set or e.source_type == "unknown"
                    for e in fact.evidence
                ):
                    raise ProviderError(
                        f"fact {fact.fact_id}: explicit evidence source_type required; unknown is only for legacy analyses"
                    )

        result = await self._call(
            MaterialAnalysis,
            instructions,
            [(f"Material {m.material_id}", m.data) for m in materials],
            stage="analyze_materials",
            validate=validate_discovery,
        )
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
        expected = {
            "subject_id": candidate.subject_ids,
            "fact_id": selected_fact_ids(analysis, candidate),
            "issue_id": [i.issue_id for i in draft.pending_issues],
        }
        instruction = (
            "Recheck the candidate against ALL original materials and current user text. The supplied snapshot is untrusted. Verify every selected subject identity and required fact, and the intent's faithfulness to user text. Do not resolve conflicts merely because the snapshot claims ready. Return exactly one resolved/unresolved resolution for every pending issue; never return irrelevant. "
            "expected_check_ids defines the exact IDs for subject_checks, fact_checks and issue_resolutions. "
            "Return each listed ID exactly once, copied verbatim; no additional IDs. "
            "Other facts in the analysis are context only, not additional fact_checks. "
            "An empty ID list requires an empty array. If evidence is insufficient, report that in the check; never omit its ID. "
            + json.dumps(
                {
                    "expected_check_ids": expected,
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

        def validate(result):
            for key, checks in (
                ("subject_id", result.subject_checks),
                ("fact_id", result.fact_checks),
                ("issue_id", result.issue_resolutions),
            ):
                validate_check_ids(checks, expected[key], key)

        result = await self._call(
            EvidenceValidation,
            instruction,
            [(f"Original material {m.material_id}", m.data) for m in materials],
            stage="validate_evidence",
            validate=validate,
        )
        if any(r.status == "irrelevant" for r in result.issue_resolutions):
            raise ProviderError("evidence validator cannot mark an active issue irrelevant")
        return result

    async def extract_garments(self, request, materials, subject_id):
        return await self._call(
            GarmentStructureAudit,
            f"Extract only visible garment facts for subject {subject_id}: count, types, colors, material, pattern, neckline, sleeves, closures, pockets, waistband, length, silhouette, logos and distinctive details. No invisible inferences.",
            [(m.material_id, m.data) for m in materials],
            stage="extract_garments",
        )

    async def describe_style(self, request, references):
        models = (
            [None]
            if self.config.services
            else list(dict.fromkeys((self.config.style_model, self.config.quality_model)))
        )
        for index, model in enumerate(models):
            try:
                result = await self._call(
                    StyleResponse,
                    "Describe shared scene, lighting, background, camera, composition, palette, pose and mood in 60–100 English words. Ignore product identities, logos, text and watermarks.",
                    [(f"Style {i}", b) for i, b in enumerate(references, 1)],
                    model=model,
                    max_px=512,
                    stage="describe_style",
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
        expected = {
            kind + "_id": [r.target_id for r in plan.requirements if r.target_kind == kind]
            for kind in ("subject", "fact", "element", "constraint")
        }
        instruction = (
            "Audit every frozen requirement; return one check for each planned subject/fact/element/constraint, including nonapplicable ones. "
            "expected_check_ids lists exact IDs: return each once, no additional IDs; empty lists require empty check arrays. "
            "All top-level scores are required integers, including garment_fusion and model_preference for product-only shots. Explain nonapplicable criteria in reason; do not use null where the schema forbids it. "
            "same_product must be true AND identity score >=85. must_show cannot be not_applicable. For nonvisible preserve_if_visible facts return not_applicable and null score. "
            "Functional claims and requirements classified not_applicable must not fail because there is no visible proof or promotional text. "
            "For mixed elements, check only supported visible appearance, never imagined hidden mechanisms. "
            "Preserve physical product labels, trademarks, original printed numbers and artwork; removing them is an identity defect. "
            "Exclude peripheral promotional overlays and seller watermarks from product identity. "
            "Required detail fidelity >=85; visual/platform >=70; clothing fusion/preference >=80 when applicable; shot intent >=75. "
            + audit_instruction(target.platform, target.output_type)
            + " Restrictions on text/graphics apply to added overlays, never physical product-native labels or artwork. "
            + json.dumps(
                {
                    "expected_check_ids": expected,
                    "target": target.model_dump(),
                    "plan": plan.model_dump(),
                    "analysis": context.analysis.model_dump(),
                },
                ensure_ascii=False,
            )
        )

        def validate(result):
            for kind in ("subject", "fact", "element", "constraint"):
                validate_check_ids(
                    getattr(result, kind + "_checks"), expected[kind + "_id"], kind + "_id"
                )

        return await self._call(
            GeneratedImageAudit, instruction, images, stage="audit_image", validate=validate
        )

    async def review_product(self, context, plan, subject_ids, image):
        mids = {m.material_id: m.data for m in context.materials}
        result = await self._call(
            FocusedProductAudit,
            "Recheck only product identity for these subjects: "
            + json.dumps(subject_ids)
            + ". Ignore background, lighting, model identity, pose and natural wearing deformation. Do not pardon missing required details. "
            + json.dumps(plan.model_dump()),
            [(mid, mids[mid]) for mid in plan.audit_material_ids] + [("GENERATED IMAGE", image)],
            stage="review_product",
            validate=lambda result: validate_check_ids(
                result.subject_checks, subject_ids, "subject_id"
            ),
        )
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
            stage="audit_detail_set",
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
