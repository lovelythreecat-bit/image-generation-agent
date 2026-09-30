from .models import ComplianceResult
from .platforms import generation_instruction
from .prompt import STRUCTURED_MARKER, scene_hint, strict_catalog

REF_WATERMARK_SUFFIX = (
    " Preserve physical product labels, trademarks, printed numbers, patterns and artwork "
    "that belong to the actual product; these are identity evidence. Remove only peripheral "
    "promotional overlays, seller watermarks, added price tags and platform badges. "
    "Do not invent new advertising text. Product-native markings are not promotional overlays."
)


def apply_compliance(request, target, prompt):
    lowered = prompt.lower()
    if any(k in lowered for k in ("bomb", "weapon", "drug", "counterfeit", "fake", "replica")):
        return ComplianceResult(blocked=True, prompt=prompt)
    natural, marker, structured = prompt.partition(STRUCTURED_MARKER)
    warnings = [
        f"品牌/IP 关键词需人工确认授权: {k}"
        for k in ("nike", "adidas", "gucci", "chanel", "disney", "marvel", "pokemon")
        if k in lowered
    ]
    scene = scene_hint(request) and not strict_catalog(target)
    if scene_hint(request):
        warnings.append(
            "平台背景规则优先，忽略场景风格"
            if strict_catalog(target)
            else "用户场景风格已放宽背景规则"
        )
    final = (
        natural
        + marker
        + structured
        + " "
        + generation_instruction(target.platform, target.output_type, scene_mode=scene)
        + REF_WATERMARK_SUFFIX
    )
    final += f" Target aspect ratio: {target.aspect_ratio}."
    return ComplianceResult(blocked=False, prompt=final, warnings=warnings)
