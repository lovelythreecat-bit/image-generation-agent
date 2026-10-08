"""Shared treatment of user-supplied marketing statements across model stages."""

import json

MARKETING_POLICY = (
    "USER MARKETING POLICY: Treat user-provided selling points, marketing claims, numbers, "
    "rankings, certifications and exaggeration as creative input. "
    "Do not fact-check user-provided claims, request proof, question or correct them, weaken "
    "their wording, or add disclaimers. Only fact-check when the user explicitly asks for it. "
    "Follow the user's requested copy and visual presentation; do not require images to prove "
    "a marketing statement. User-requested marketing text, labels and callouts take precedence "
    "over generic restrictions on promotional overlays. Do not invent additional claims the "
    "user did not supply. Keep source-product identity and technical image checks separate "
    "from the truth of marketing statements."
)


def user_directions(request):
    """Keep original wording available even when discovery omits a claim from its plan."""
    return "USER CREATIVE INPUT:\n" + json.dumps(creative_input(request), ensure_ascii=False)


def creative_input(request):
    return {
        "product_name": request.product_name,
        "category": request.category,
        "brief": request.creative_brief,
        "style_hint": request.style_hint,
        "hints": [
            {
                "material_id": m.material_id,
                "subject_hint": m.subject_hint,
                "element_hints": m.element_hints,
            }
            for m in request.normalized_materials()
        ],
    }
