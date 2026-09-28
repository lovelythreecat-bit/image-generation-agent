"""Pure prompt construction; selection decisions belong to selection.py."""

import json
import re

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
    "scene": "Lifestyle scene shot, visibly different from the main catalog image: show the product in a credible furnished environment with at least two readable contextual elements, not a plain seamless studio wall. For model-wear apparel, use a natural three-quarter or full-body walking, turning or interacting pose from a different camera angle while keeping the garment readable. Use soft natural light, depth and an aspirational mood",
    "feature": "Selling-point feature shot, not a generic catalog portrait. Use a shoulder-to-hip or similarly tight editorial crop so the product occupies most of the frame. Emphasize one source-supported benefit such as fit, silhouette, drape or construction through the model pose and camera angle. For a top, a natural hand gesture may reveal the hem, width or drape without stretching or redesigning it. Keep clean negative space for later marketing copy but generate no text, labels or invented callouts",
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


def build_prompt(request, target, context, plan, mode="standard"):
    parts = [
        f"Create a high-quality e-commerce photo of the selected source products. The declared product is {request.product_name}, category {request.category}. Do not change category, silhouette, construction, color, material, pattern, proportions or distinctive details. Never substitute the products from scene/style references."
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
    if not strict_catalog(target):
        if request.style_hint:
            parts.append(f"USER SCENE AND STYLE DIRECTION: {request.style_hint}")
        if context.style_prompt:
            parts.append(f"Supplement unspecified visual details only: {context.style_prompt}")
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
                "feature": "Do not repeat a front catalog portrait.",
                "closeup": "Use true macro framing, not a full body view.",
            }.get(target.variant, "Keep complete products visible.")
        )
    elif mode == "staged":
        parts.append(
            "Fuse source products into the final scene with natural contact, folds, proportions and occlusion; preserve every required detail."
        )
    allowed = set(plan.required_element_ids + plan.preferred_element_ids)
    facts = set(plan.required_fact_ids)
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


def build_stage_prompt(request, target, plan, final_prompt):
    return (
        "Create only a scene and natural pose template. Leave product regions empty for later fusion. "
        "Do not draw or copy product identities. Reserve space for the required subjects. "
        f"Shot: {target.variant or 'hero'}. Style: {request.style_hint or ''}. "
        + STRUCTURED_MARKER
        + json.dumps(
            {"subject_slots": plan.subject_ids, "presentation": plan.subject_presentations}
        )
    )
