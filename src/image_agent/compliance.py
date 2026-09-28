from .models import ComplianceResult
from .platforms import generation_instruction
from .prompt import STRUCTURED_MARKER, scene_hint, strict_catalog

REF_WATERMARK_SUFFIX = " Do not copy, reproduce, or recreate any text, watermark, logo, icon, badge, price tag, or graphic overlay visible in the reference image. Generate clean original imagery with no embedded text."


def apply_compliance(request, target, prompt):
    lowered = prompt.lower()
    if any(k in lowered for k in ("bomb", "weapon", "drug", "counterfeit", "fake", "replica")):
        return ComplianceResult(blocked=True, prompt=prompt)
    natural, marker, structured = prompt.partition(STRUCTURED_MARKER)
    for old, new in {"CN": {"4": "6"}, "VN": {"4": "6"}, "PH": {"13": "12"}}.get(
        request.market, {}
    ).items():
        natural = natural.replace(old, new)
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
