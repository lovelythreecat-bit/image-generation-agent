"""Shared, tolerant product appearance rules for discovery, generation and review."""

PRODUCT_APPEARANCE_POLICY = (
    "PRODUCT APPEARANCE POLICY: Original source-product images are the sole authority for physical "
    "product appearance. Preserve actual shape, proportions, construction, intrinsic color, "
    "pattern, markings and distinctive components. This takes precedence over user directions, "
    "product descriptions, style references and creative suggestions. User marketing wording "
    "never authorizes a physical product change; keep requested copy separate from product design. "
    "Do not translate selling points into invented components, cutaways, textures or accessories. "
    "Do not invent detail to sharpen an unclear source region. Allow normal lighting and shadows, "
    "small perspective changes, natural wearing folds, drape and occlusion that preserve identity. "
    "Scene decorations are allowed where platform rules permit, but must remain separate from "
    "the product, not become new product markings, parts or included accessories. "
)

SOURCE_VIEW_POLICY = (
    "SOURCE VIEW POLICY: Alternate views are allowed when supported by source-product images. "
    "If only a front view is supported, keep a front-facing product view; do not guess distinctive "
    "side, rear or internal details. When a requested angle or pose would reveal unsupported "
    "product surfaces, use the closest source-supported view instead. This takes precedence over "
    "shot and platform suggestions for alternate angles. Create variety through scene, lighting, "
    "composition, crop and copy without requiring a new product view. Use a close-up of a readable "
    "source detail instead of completing a blurred or hidden area. Fall back without requesting "
    "extra evidence merely for creative variety. "
)

PRODUCT_APPEARANCE_AUDIT = (
    "PRODUCT APPEARANCE AUDIT: Inspect the whole visible product, including additions absent from "
    "the frozen fact list. Check for clearly invented or altered pockets, buttons, ports, closures, "
    "patterns, markings, accessories and distinctive side/rear/internal construction without "
    "supporting source views. For a clear, meaningful physical discrepancy, set same_product=false "
    "for the affected subject even if its overall similarity score is high; describe the specific "
    "detail and source evidence in reason. Do not create extra check IDs. Do not assume a detail "
    "is invented merely because the extracted fact list omitted it; compare the source images. "
    "Unclear regions alone are not grounds for rejection. Do not reject minor rendering differences, "
    "natural lighting, perspective, folds or occlusion, separate scene decorations, or user "
    "marketing wording. A source-supported view fallback must not lower output_intent or "
    "platform_compliance merely because an alternate angle was not shown. "
)
