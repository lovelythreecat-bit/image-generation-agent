"""Shared treatment of user-supplied marketing statements across model stages."""

import json

MARKETING_POLICY = (
    "USER MARKETING POLICY: Treat user-provided selling points, marketing claims, numbers, "
    "rankings, certifications and exaggeration as creative input. "
    "Do not fact-check user-provided claims, request proof, question or correct them, weaken "
    "their wording, or add disclaimers. Only fact-check when the user explicitly asks for it. "
    "Follow the user's requested copy and visual presentation while preserving source-product "
    "appearance; marketing text is not evidence for physical details. Do not require images to prove "
    "a marketing statement. User-requested marketing text, labels and callouts take precedence "
    "over generic restrictions on promotional overlays. Do not invent additional claims the "
    "user did not supply. Keep source-product identity and technical image checks separate "
    "from the truth of marketing statements. "
    "USER CREATIVE POLICY: Follow explicit surreal or physically impossible scene, motion and "
    "presentation requests without correcting them to realistic alternatives. Creative freedom "
    "applies to the setting, composition, lighting, layout and unchanged product's placement. "
    "Never add physical product components or change source-product appearance to enact a creative "
    "premise. Review and correction must preserve that premise and the user's supplied copy."
)


def user_directions(request):
    """Keep original wording available even when discovery omits a claim from its plan."""
    return "USER CREATIVE INPUT:\n" + json.dumps(creative_input(request), ensure_ascii=False)


def user_brief_sources(request):
    """All original text entered as product information or explicit copy."""
    sources = [request.creative_brief or ""]
    options = getattr(request, "long_image", None)
    if options is not None:
        sources.extend([options.known_info, *options.required_copy])
    return sources


def creative_input(request):
    inputs = {
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
    options = getattr(request, "long_image", None)
    if options is not None:
        inputs["long_image"] = {
            "known_info": options.known_info,
            "required_copy": options.required_copy,
        }
    return inputs
