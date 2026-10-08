import io
import json
from collections import Counter

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
            768
            if model in {"openai/gpt-image-2", "gpt-image-2"}
            else {"1K": 768, "2K": 1536, "4K": 3000}[size]
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
        details = {
            "missing": sorted(set(expected) - set(ids)),
            "duplicated": sorted(i for i, count in Counter(ids).items() if count > 1),
            "unknown": sorted(set(ids) - set(expected)),
        }
        raise ProviderError(
            f"audit {key} IDs are missing, duplicated or unknown: "
            + "; ".join(f"{name}={json.dumps(values)}" for name, values in details.items())
        )


def normalize_audit_checks(audit, expected):
    """Keep expected checks on a copy; missing/conflicting evidence is still invalid."""
    result = audit.model_copy(deep=True)
    for key, required in expected.items():
        field = "issue_resolutions" if key == "issue_id" else key.removesuffix("_id") + "_checks"
        rows = {}
        for check in getattr(result, field):
            identity = getattr(check, key)
            if identity not in required:
                continue
            if identity in rows and rows[identity] != check:
                raise ProviderError(f"audit {key} contradictory duplicated check: {identity}")
            rows[identity] = check
        checks = [rows[i] for i in dict.fromkeys(required) if i in rows]
        validate_check_ids(checks, required, key)
        setattr(result, field, checks)
    return result


def review_subject_ids(audit, plan):
    applicable = {
        r.target_id
        for r in plan.requirements
        if r.target_kind == "subject" and r.applicability != "not_applicable"
    }
    return tuple(
        s.subject_id
        for s in audit.subject_checks
        if s.subject_id in applicable and s.same_product and 80 <= s.score <= 84
    )


def evaluate_quality(audit, pixels, target, plan, *, review=None):
    requirements = {
        kind: [r for r in plan.requirements if r.target_kind == kind]
        for kind in ("subject", "fact", "element", "constraint")
    }
    audit = normalize_audit_checks(
        audit, {f"{kind}_id": [r.target_id for r in rows] for kind, rows in requirements.items()}
    )
    original = audit.subject_checks
    subjects = list(original)
    reviewed = []
    if review is not None:
        ids = review_subject_ids(audit, plan)
        review = normalize_audit_checks(review, {"subject_id": ids})
        reviewed = review.subject_checks
        replacements = {s.subject_id: s for s in reviewed if not s.same_product or s.score >= 85}
        subjects = [replacements.get(s.subject_id, s) for s in subjects]
    failed = []
    critical = []
    lookup = {s.subject_id: s for s in subjects}
    for r in requirements["subject"]:
        s = lookup[r.target_id]
        if r.applicability != "not_applicable" and (s.score < 85 or not s.same_product):
            failed.append(f"商品一致性 {s.subject_id} {s.score}<85或身份不符")
            if not s.same_product:
                critical.append(failed[-1])
    for kind in ("fact", "element"):
        lookup = {getattr(c, f"{kind}_id"): c for c in getattr(audit, f"{kind}_checks")}
        for r in requirements[kind]:
            c = lookup[r.target_id]
            if r.origin == "preferred" or r.applicability == "not_applicable":
                continue
            if r.applicability == "must_show" and c.presence != "present":
                failed.append(f"必需要素 {r.target_id} 未呈现")
                critical.append(failed[-1])
            elif c.presence == "present" and c.fidelity_score < 85:
                failed.append(f"要素保真 {r.target_id} {c.fidelity_score}<85")
                # Clear physical changes must be reported as same_product=false;
                # a fidelity score and prose alone do not establish that finding.
    checks = {c.constraint_id: c for c in audit.constraint_checks}
    for r in requirements["constraint"]:
        if r.applicability == "must_show" and not checks[r.target_id].satisfied:
            failed.append(f"必需约束 {r.target_id} 未满足")
            critical.append(failed[-1])
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
        critical.append(failed[-1])
    return QualityReport(
        **scores,
        subject_checks=subjects,
        original_subject_checks=original,
        reviewed_subject_checks=reviewed,
        passed=not failed,
        model_passed=audit.passed,
        model_reason=audit.reason,
        deterministic_checks=pixels,
        failed_checks=failed,
        critical_failed_checks=critical,
        reason=("未通过指标：" + "；".join(failed) + "；审核依据：" + audit.reason)
        if failed
        else "通过",
    )


def choose_retry(target, quality, plan=None):
    if strict_catalog(target):
        return "strict"
    applicable = (
        {
            r.target_id
            for r in plan.requirements
            if r.target_kind == "subject" and r.applicability != "not_applicable"
        }
        if plan
        else {s.subject_id for s in quality.subject_checks}
    )
    if any(
        (s.score < 85 or not s.same_product)
        for s in quality.subject_checks
        if s.subject_id in applicable
    ) or (target.presentation_mode == "model_wear" and quality.garment_fusion < 80):
        return "staged"
    return "strict"
