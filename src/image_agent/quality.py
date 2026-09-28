import io

from PIL import Image

from .errors import ProviderError
from .images import generated_image
from .models import PixelChecks, QualityReport
from .prompt import strict_catalog


def export_white_background(data):
    image = generated_image(data)
    image.thumbnail((480, 480), Image.Resampling.LANCZOS)
    canvas = Image.new("RGB", (480, 480), "white")
    canvas.paste(image, ((480 - image.width) // 2, (480 - image.height) // 2))
    out = io.BytesIO()
    canvas.save(out, "PNG")
    return out.getvalue()


def check_pixels(data, target, size, model):
    image = generated_image(data)
    w, h = image.size
    reasons = []
    white = target.output_type == "pdd_white_background"
    if white:
        if (w, h) != (480, 480) or len(data) >= 3 * 1024 * 1024:
            reasons.append("白底必须480×480且小于3 MiB")
    else:
        minimum = (
            768 if model == "openai/gpt-image-2" else {"1K": 768, "2K": 1536, "4K": 3000}[size]
        )
        if max(w, h) < minimum:
            reasons.append(f"长边不足{minimum}")
    a, b = map(int, target.aspect_ratio.split(":"))
    if abs((w / h) / (a / b) - 1) > 0.06 + 1e-12:
        reasons.append("比例误差超过6%")
    ratio = None
    if strict_catalog(target):
        image.thumbnail((256, 256), Image.Resampling.LANCZOS)
        band = max(1, round(min(image.size) * 0.08))
        pixels = [
            image.getpixel((x, y))
            for y in range(image.height)
            for x in range(image.width)
            if x < band or y < band or x >= image.width - band or y >= image.height - band
        ]
        ratio = sum(min(p) >= 250 for p in pixels) / len(pixels)
        if ratio < 0.90:
            reasons.append("近白边比例不足90%")
    return PixelChecks(
        passed=not reasons,
        width=w,
        height=h,
        reason="；".join(reasons) or "通过",
        white_border_ratio=ratio,
        export_bytes=len(data) if white else None,
    )


def validate_check_ids(checks, expected, key):
    ids = [getattr(c, key) for c in checks]
    if len(ids) != len(set(ids)) or set(ids) != set(expected):
        raise ProviderError(f"audit {key} IDs are missing, duplicated or unknown")


def evaluate_quality(audit, pixels, target, plan, *, review=None):
    requirements = {
        kind: [r for r in plan.requirements if r.target_kind == kind]
        for kind in ("subject", "fact", "element", "constraint")
    }
    for kind, rows in requirements.items():
        validate_check_ids(
            getattr(audit, f"{kind}_checks"), [r.target_id for r in rows], f"{kind}_id"
        )
    original = audit.subject_checks
    subjects = list(original)
    reviewed = []
    if review is not None:
        ids = [s.subject_id for s in subjects if 80 <= s.score <= 84]
        validate_check_ids(review.subject_checks, ids, "subject_id")
        reviewed = review.subject_checks
        replacements = {s.subject_id: s for s in reviewed if s.same_product and s.score >= 85}
        subjects = [replacements.get(s.subject_id, s) for s in subjects]
    failed = []
    lookup = {s.subject_id: s for s in subjects}
    for r in requirements["subject"]:
        s = lookup[r.target_id]
        if r.applicability != "not_applicable" and (s.score < 85 or not s.same_product):
            failed.append(f"商品一致性 {s.subject_id} {s.score}<85或身份不符")
    for kind in ("fact", "element"):
        lookup = {getattr(c, f"{kind}_id"): c for c in getattr(audit, f"{kind}_checks")}
        for r in requirements[kind]:
            c = lookup[r.target_id]
            if r.origin == "preferred" or r.applicability == "not_applicable":
                continue
            if r.applicability == "must_show" and c.presence != "present":
                failed.append(f"必需要素 {r.target_id} 未呈现")
            elif c.presence == "present" and c.fidelity_score < 85:
                failed.append(f"要素保真 {r.target_id} {c.fidelity_score}<85")
    checks = {c.constraint_id: c for c in audit.constraint_checks}
    for r in requirements["constraint"]:
        if r.applicability == "must_show" and not checks[r.target_id].satisfied:
            failed.append(f"必需约束 {r.target_id} 未满足")
    scores = audit.model_dump(exclude={"passed", "reason", "subject_checks"})
    if target.presentation_mode == "product_only":
        scores["garment_fusion"] = scores["model_preference"] = 100
    elif target.model_preference == "auto":
        scores["model_preference"] = 100
    for name, threshold, label in (
        ("visual_quality", 70, "视觉质量"),
        ("platform_compliance", 70, "平台合规"),
        ("garment_fusion", 80, "服装融合"),
        ("model_preference", 80, "模特偏好"),
        ("output_intent", 75, "镜头意图"),
    ):
        if scores[name] < threshold:
            failed.append(f"{label} {scores[name]}<{threshold}")
    if not pixels.passed:
        failed.append("像素检查 " + pixels.reason)
    return QualityReport(
        **scores,
        subject_checks=subjects,
        original_subject_checks=original,
        reviewed_subject_checks=reviewed,
        passed=not failed,
        model_passed=audit.passed,
        deterministic_checks=pixels,
        failed_checks=failed,
        reason="未通过指标：" + "；".join(failed) if failed else "通过",
    )


def choose_retry(target, quality):
    if strict_catalog(target):
        return "strict"
    if (
        any(s.score < 85 or not s.same_product for s in quality.subject_checks)
        or quality.garment_fusion < 80
        or any(w in quality.reason for w in ("融合", "穿着", "人体", "贴合", "服装", "上身"))
    ):
        return "staged"
    return "strict"
