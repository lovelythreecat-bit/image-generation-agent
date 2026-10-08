"""Pure prompt construction; selection decisions belong to selection.py."""

import json
import re

from .appearance import PRODUCT_APPEARANCE_POLICY, SOURCE_VIEW_POLICY
from .marketing import MARKETING_POLICY, user_directions
from .models import AssetTarget
from .platforms import aspect_ratio_for

STRUCTURED_MARKER = "\nSTRUCTURED ELEMENT PLAN (IDs are labels, not image text):\n"
SCENE_KEYWORDS = (
    "场景",
    "外景",
    "户外",
    "街头",
    "街景",
    "街道",
    "巷子",
    "巷弄",
    "水乡",
    "江南",
    "古镇",
    "海边",
    "沙滩",
    "海滩",
    "山",
    "森林",
    "树林",
    "草原",
    "田野",
    "花园",
    "庭院",
    "公园",
    "湖",
    "河",
    "桥",
    "落叶",
    "咖啡馆",
    "咖啡店",
    "餐厅",
    "民宿",
    "客厅",
    "卧室",
    "书房",
    "雪景",
    "雪地",
    "樱花",
    "竹林",
    "落日",
    "黄昏",
    "清晨",
    "夜景",
    "霓虹",
    "lifestyle",
    "outdoor",
    "scene",
    "street",
    "garden",
    "beach",
    "forest",
    "cafe",
    "park",
)
_APPAREL_TERMS = (
    "服饰",
    "服装",
    "女装",
    "男装",
    "童装",
    "上衣",
    "衬衫",
    "t恤",
    "毛衣",
    "针织",
    "开衫",
    "卫衣",
    "夹克",
    "外套",
    "风衣",
    "大衣",
    "连衣裙",
    "半身裙",
    "裤",
    "牛仔",
    "短裤",
    "套装",
    "西装",
    "礼服",
    "apparel",
    "clothing",
    "garment",
    "dress",
    "shirt",
    "t-shirt",
    "tshirt",
    "tee",
    "top",
    "blouse",
    "sweater",
    "cardigan",
    "hoodie",
    "jacket",
    "coat",
    "jeans",
    "pants",
    "trousers",
    "skirt",
    "shorts",
    "suit",
    "leggings",
)
_CATEGORY_FAMILIES = {
    "top": (
        "t恤",
        "上衣",
        "衬衫",
        "针织",
        "毛衣",
        "背心",
        "tee",
        "shirt",
        "top",
        "blouse",
        "sweater",
        "cardigan",
        "tank",
    ),
    "bottom": (
        "裤",
        "牛仔裤",
        "半身裙",
        "短裤",
        "pants",
        "trousers",
        "jeans",
        "skirt",
        "shorts",
        "culottes",
        "leggings",
    ),
    "dress": ("连衣裙", "礼服", "dress", "gown"),
    "outerwear": ("夹克", "外套", "风衣", "大衣", "jacket", "coat", "hoodie", "blazer"),
}
SHOTS = {
    "hero": "Hero product shot, full product clearly visible and centered, studio-grade lighting with soft shadows, crisp focus on the product, uncluttered background, premium e-commerce look",
    "scene": "Lifestyle scene shot: show a wider contextual view of the established shooting environment, preserving its background, props, palette and lighting. Create variety through camera framing, composition and natural interaction. For model-wear apparel, use a natural three-quarter or full-body walking or interacting pose that preserves a source-supported product view while keeping the garment readable",
    "feature": "Selling-point feature shot, not a generic catalog portrait. Use a shoulder-to-hip or similarly tight editorial crop so the product occupies most of the frame. Emphasize the user's requested selling point through composition, pose and requested marketing copy. For a top, a natural hand gesture may reveal the hem, width or drape without stretching or redesigning it. Keep clean negative space for marketing copy, and include promotional text, labels or callouts when requested by the user; preserve product-native labels and artwork",
    "closeup": "Macro close-up detail shot: crop tightly into one detail visibly supported by the source image, such as neckline, stitching, texture or construction. Use sharp focus and soft side light; do not invent fabric texture, labels or components that cannot be verified",
}


def is_apparel(text):
    return any(term in text.lower() for term in _APPAREL_TERMS)


def is_placeholder(request):
    name = request.product_name.lower()
    return request.category.lower() in ("general", "unknown", "其他") and (
        name == "用户上传商品"
        or re.fullmatch(
            r"(?:clipboard|pasted|upload(?:ed)?(?:[-_]?product)?|image|img)[-_]?(?:\d+|[0-9a-f-]{8,})?",
            name,
        )
        is not None
        or re.fullmatch(r"sku[-_ ]?[a-z0-9]+(?:[-_][a-z0-9]+)*", name) is not None
    )


def strict_catalog(target):
    return target.output_type == "pdd_white_background" or (
        target.platform == "amazon" and target.output_type == "main_image"
    )


def scene_hint(request):
    return any(k in (request.style_hint or "").lower() for k in SCENE_KEYWORDS)


def resolve_presentation(request, observed_product=""):
    if request.output_types == ["pdd_white_background"]:
        return "product_only"
    if request.presentation_mode != "auto":
        return request.presentation_mode
    return (
        "model_wear"
        if request.model_preference != "auto"
        or is_apparel(f"{request.product_name} {request.category} {observed_product}")
        else "product_only"
    )


def build_targets(request, presentation_mode):
    targets = []
    for output in ("main_image", "detail_page", "pdd_white_background"):
        if output not in request.output_types:
            continue
        white = output == "pdd_white_background"
        for platform in ["pinduoduo"] if white else request.platforms:
            for variant in ["scene", "feature", "closeup"] if output == "detail_page" else [None]:
                targets.append(
                    AssetTarget(
                        platform=platform,
                        output_type=output,
                        variant=variant,
                        aspect_ratio="1:1"
                        if white
                        else request.aspect_ratio
                        if request.aspect_ratio != "auto"
                        else aspect_ratio_for(platform, output),
                        presentation_mode="product_only" if white else presentation_mode,
                        model_preference="auto"
                        if white or presentation_mode == "product_only"
                        else request.model_preference,
                    )
                )
    return targets


def build_shoot_prompt(request, target, context, plan):
    if "detail_page" not in request.output_types or strict_catalog(target):
        return ""
    direction = (
        "\nSHARED SHOOT DIRECTION:\n"
        "Create this image as part of one coherent commercial photo shoot. Keep the same "
        "environment, background materials, recurring props, lighting direction, time of day, "
        "palette and color grading across the set. For model-wear images, keep the same model "
        "identity, hairstyle, makeup and styling. Vary camera distance, crop, composition and "
        "pose to fulfill this shot's role; a macro crop need not show the full setting or face. "
        "User directions, selected source references and platform rules take precedence. "
        "Shared suggestions only fill unspecified details.\n"
        + json.dumps(getattr(context, "shoot_plan", {}), ensure_ascii=False)
    )
    anchor = next((b for b in plan.reference_bindings if b.role == "shoot_continuity"), None)
    if anchor:
        direction += (
            f"\nReference Image {anchor.index} ({anchor.stage_id}) is the approved shoot anchor. "
            "Match its environment, lighting, palette and model identity when visible. "
            "Use it only for shoot continuity; original source images remain the sole authority "
            "for product identity and physical details. Do not copy its framing or marketing text."
        )
    else:
        direction += (
            "\nEstablish a coherent setting from these shared directions for subsequent images. "
            "Keep all shared scene, lighting and palette choices stable."
        )
    return direction


def build_prompt(request, target, context, plan, mode="standard"):
    parts = [
        f"Create a high-quality e-commerce photo of the selected source products. The declared product is {request.product_name}, category {request.category}. Do not change category, silhouette, construction, color, material, pattern, proportions or distinctive details. Never substitute the products from scene/style references.",
        PRODUCT_APPEARANCE_POLICY,
        SOURCE_VIEW_POLICY,
        MARKETING_POLICY,
        user_directions(request),
    ]
    if target.presentation_mode == "model_wear":
        parts.append(
            "Photorealistic model naturally wearing selected clothing, with realistic folds, draping, fabric tension, body contact and occlusion. Do not paste flat garments onto the body; accessories remain accessories."
        )
        if target.variant is None:
            parts.append("Show the model head-to-toe with every sellable garment fully visible.")
        if target.model_preference == "no_face":
            parts.append("The model's face must not be visible; honor the requested shot crop.")
        elif target.model_preference != "auto":
            parts.append(f"Use a {target.model_preference} model.")
    else:
        parts.append("Present only the selected products. No model or person.")
    shot = SHOTS.get(target.variant or "hero", "")
    if (
        "detail_page" in request.output_types
        and target.variant is None
        and not strict_catalog(target)
    ):
        shot = (
            "Hero product shot, full product clearly visible and dominant, crisp product focus. "
            "Establish the shared shooting environment with clean commercial composition."
        )
    if scene_hint(request) and not strict_catalog(target):
        for phrase in (
            "uncluttered background",
            "clean modern background",
            "clean background",
            "studio-grade lighting with soft shadows",
            "studio lighting",
            "blurred surroundings",
            "premium e-commerce look",
            "premium magazine feel",
        ):
            shot = shot.replace(phrase, "")
    parts.append(shot)
    if target.variant == "closeup" and plan.focus_element_id is None:
        focus_ids = {
            r.target_id
            for r in plan.requirements
            if r.target_kind == "fact"
            and r.origin == "shot_rule"
            and r.applicability == "must_show"
        }
        parts.append(
            "SOURCE-SUPPORTED CLOSE-UP FOCUS: Crop tightly into the following visible "
            "source-product appearance. Keep other identity details only where visible in "
            "the crop; do not reveal unsupported surfaces or invent finer detail.\n"
            + json.dumps(
                [f.model_dump() for f in context.analysis.facts if f.fact_id in focus_ids],
                ensure_ascii=False,
            )
        )
    if not strict_catalog(target):
        if request.style_hint:
            parts.append(f"USER SCENE AND STYLE DIRECTION: {request.style_hint}")
        if context.style_prompt:
            parts.append(f"Supplement unspecified visual details only: {context.style_prompt}")
    if context.analysis.creative_plan:
        suggestions = [
            s.model_dump()
            for s in context.analysis.creative_plan.suggestions
            if not strict_catalog(target) or s.aspect == "lighting"
        ]
        if suggestions:
            parts.append(
                "MODEL CREATIVE SUGGESTIONS (optional visual guidance, not user requirements): "
                "Use only to fill unspecified details. User directions, source-product identity, "
                "selected subjects/accessories, platform rules, presentation mode and this image's "
                "shot/crop rules take precedence. Selected style references also take precedence. "
                "Adapt to this image's role; never replace a macro close-up with a full-product shot. "
                "Ignore incompatible suggestions. Do not invent claims, copy or product details.\n"
                + json.dumps(suggestions, ensure_ascii=False)
            )
    if context.product_attributes:
        parts.append(
            "VISIBLE GARMENT FACTS: " + json.dumps(context.product_attributes, ensure_ascii=False)
        )
    if mode == "strict":
        parts.append(
            "Correction: prioritize exact identity, frozen requirements and platform background. Preserve requested framing."
        )
        parts.append(
            {
                "scene": "Show a credible environment.",
                "feature": "Use tighter framing, composition and copy to emphasize the selling point within a source-supported product view.",
                "closeup": "Use true macro framing, not a full body view.",
            }.get(target.variant, "Keep complete products visible.")
        )
    elif mode == "staged":
        parts.append(
            "Fuse source products into the final scene with natural contact, folds, proportions and occlusion; preserve every required detail."
        )
    allowed = set(plan.required_element_ids + plan.preferred_element_ids)
    facts = set(plan.required_fact_ids)
    parts.append(build_shoot_prompt(request, target, context, plan))
    parts.append(
        STRUCTURED_MARKER
        + json.dumps(
            {
                "subjects": plan.subject_ids,
                "subject_presentations": plan.subject_presentations,
                "requirements": [x.model_dump() for x in plan.requirements],
                "constraints": [c.model_dump() for c in plan.constraints],
                "elements": [
                    e.model_dump() for e in context.analysis.elements if e.element_id in allowed
                ],
                "facts": [f.model_dump() for f in context.analysis.facts if f.fact_id in facts],
                "reference_bindings": [b.model_dump() for b in plan.reference_bindings],
            },
            ensure_ascii=False,
        )
    )
    return "\n".join(parts)


def build_stage_prompt(request, target, plan, final_prompt, *, shoot_direction=""):
    return (
        "Create only a scene and natural pose template. Leave product regions empty for later fusion. "
        "Do not draw or copy product identities. Reserve space for the required subjects. "
        "Keep product-slot orientation flexible so final fusion can adapt the pose to a "
        "source-supported product view, including supported side or rear views. Do not require "
        "a turn or new product angle merely for creative variety. "
        f"Shot: {target.variant or 'hero'}. Style: {request.style_hint or ''}. "
        + shoot_direction
        + STRUCTURED_MARKER
        + json.dumps(
            {"subject_slots": plan.subject_ids, "presentation": plan.subject_presentations}
        )
    )


REPAIR_MARKER = "\nTARGETED REPAIR FEEDBACK:\n"


def build_repair_prompt(request, target, context, plan, quality, *, previous_prompt=""):
    """Repair only failures of applicable frozen requirements using their audit evidence."""
    failures = []
    preserve = []
    for requirement in plan.requirements:
        if requirement.applicability == "not_applicable":
            continue
        kind = requirement.target_kind
        checks = getattr(quality, kind + "_checks")
        check = next(c for c in checks if getattr(c, kind + "_id") == requirement.target_id)
        if kind == "subject":
            failed = not check.same_product or check.score < 85
            action = (
                "Restore the exact source silhouette, proportions, markings and construction for this subject. "
                "Remove clearly unsupported added product details identified by the audit; "
                "if the angle exposes unsupported surfaces, return to a source-supported view "
                "instead of guessing their construction. Retain natural folds, lighting and separate scene decorations."
            )
        elif kind in ("fact", "element"):
            failed = requirement.origin != "preferred" and (
                (requirement.applicability == "must_show" and check.presence != "present")
                or (check.presence == "present" and check.fidelity_score < 85)
            )
            action = "Restore this verified visible detail from the original evidence without inventing components."
        else:
            failed = requirement.applicability == "must_show" and not check.satisfied
            marketing_copy = any(
                c.constraint_id == requirement.target_id and c.kind == "marketing_claim"
                for c in plan.constraints
            )
            action = (
                "Restore the user-requested marketing copy and presentation exactly without fact-checking, weakening or disclaimers."
                if marketing_copy
                else "Implement the specified placement, appearance or exclusion while preserving product identity."
            )
        row = {"kind": kind, "id": requirement.target_id, "check": check.model_dump()}
        if failed:
            row.update(action=action, requirement=requirement.model_dump())
            failures.append(row)
        elif kind not in ("fact", "element") or check.presence == "present":
            preserve.append(row)
    metrics = [
        (
            "visual_quality",
            70,
            "Correct blur, lighting, artifacts or composition identified by the audit.",
        ),
        (
            "platform_compliance",
            70,
            "Correct the specified platform background, framing or promotional overlay defect.",
        ),
        (
            "output_intent",
            75,
            "Restore the requested shot role and framing without changing product details.",
        ),
    ]
    if target.presentation_mode == "model_wear":
        metrics.append(
            (
                "garment_fusion",
                80,
                "Correct garment contact, folds and occlusion while preserving source construction.",
            )
        )
        if target.model_preference != "auto":
            metrics.append(
                (
                    "model_preference",
                    80,
                    "Honor the requested model preference while preserving the garment and framing.",
                )
            )
    for name, threshold, action in metrics:
        score = getattr(quality, name)
        if score < threshold:
            failures.append(
                {
                    "metric": name,
                    "score": score,
                    "threshold": threshold,
                    "reason": quality.reason,
                    "action": action,
                }
            )
    if not quality.deterministic_checks.passed:
        failures.append(
            {
                "metric": "pixels",
                "check": quality.deterministic_checks.model_dump(),
                "action": "Correct the reported dimensions, aspect ratio or white border without redesigning the product.",
            }
        )
    base = build_prompt(request, target, context, plan)
    # A previous structured prompt has obsolete image indices and already includes
    # the request directions. Rebuild it; retain only standalone caller guidance.
    if previous_prompt and STRUCTURED_MARKER not in previous_prompt:
        guidance = previous_prompt.partition(REPAIR_MARKER)[0].strip()
        if guidance:
            base += "\nPrevious composition guidance: " + guidance
    return (
        base
        + REPAIR_MARKER
        + (
            "Use original source materials as the sole authority for product identity. "
            "Use the previous candidate only to locate the reported errors; it must never replace original identity evidence. "
            "If the previous candidate is unavailable as an image, use this written audit feedback only. "
            "Preserve already-correct product details, native labels/artwork, layout and other passing requirements. "
            "Preserve the user's marketing statements and requested promotional text without fact-checking them. "
            "Change only the listed failed objects/metrics and retain the requested shot.\n"
            + json.dumps(
                {"failures": failures, "preserve": preserve, "model_reason": quality.model_reason},
                ensure_ascii=False,
            )
        )
    )
