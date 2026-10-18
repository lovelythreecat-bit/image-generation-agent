"""Choose a readable final candidate without rewriting its audit history."""

from statistics import fmean

from .errors import AgentError
from .images import generated_image
from .models import BestAvailableDelivery, QualityReport


def candidate_score(candidate, target, plan=None):
    segments = (candidate.get('metadata') or {}).get('segments')
    if target.output_type == 'long_image' and isinstance(segments, list) and segments:
        scores = [candidate_score({'quality': s.get('quality')}, target, plan) for s in segments]
        scored = [score for score in scores if score is not None]
        return round(fmean(scored), 6) if scored else None
    value = candidate.get('quality')
    if not value:
        return None
    quality = QualityReport.model_validate(value)
    requirements = {(r.target_kind, r.target_id): r for r in plan.requirements} if plan else {}

    def applicable(kind, identifier):
        if plan is None:
            return True
        r = requirements.get((kind, identifier))
        return r is not None and r.origin != 'preferred' and r.applicability != 'not_applicable'

    scores = [s.score for s in quality.subject_checks if applicable('subject', s.subject_id)]
    for element in quality.element_checks:
        if not applicable('element', element.element_id):
            continue
        r = requirements.get(('element', element.element_id))
        if r and r.applicability == 'preserve_if_visible' and element.presence != 'present':
            continue
        scores.append(element.fidelity_score if element.fidelity_score is not None else 0)
    scores.extend(100 if c.satisfied else 0 for c in quality.constraint_checks
                  if applicable('constraint', c.constraint_id))
    scores.extend((quality.visual_quality, quality.platform_compliance, quality.output_intent))
    if target.presentation_mode != 'product_only':
        scores.append(quality.garment_fusion)
        if target.model_preference != 'auto':
            scores.append(quality.model_preference)
    return round(fmean(scores), 6)


def select_best_available(asset, target, read):
    """Return selected bytes; stage-only and corrupt images can never win."""
    ranked = []
    for order, candidate in enumerate(asset.candidates):
        if candidate['status'] == 'stage':
            continue
        score = candidate_score(candidate, target, asset.element_plan)
        ranked.append(((score is not None, score if score is not None else 0, -order),
                       candidate, score))
    for _, candidate, score in sorted(ranked, key=lambda row: row[0], reverse=True):
        try:
            data = read(candidate)
            generated_image(data)
        except (AgentError, OSError, ValueError):
            continue
        asset.delivery = BestAvailableDelivery(
            candidate_id=candidate['candidate_id'], score=score,
            sha256=candidate['sha256'], stop_reason=asset.stop_reason or asset.status,
        )
        asset.status = 'succeeded'
        asset.file_path = candidate['file_path']
        asset.quality = QualityReport.model_validate(candidate['quality']) if candidate.get('quality') else None
        asset.element_checks = asset.quality.element_checks if asset.quality else []
        asset.error = asset.error_info = None
        for row in asset.candidates:
            row['selected'] = row['candidate_id'] == candidate['candidate_id']
        return data
    return None


def delivery_warning(asset):
    delivery = asset.delivery
    score = f'{delivery.score:g}' if delivery.score is not None else '未能取得评分'
    reason = asset.quality.reason if asset.quality else '审核服务未能完成评估'
    if asset.output_type == 'long_image':
        selected = next(c for c in asset.candidates if c['candidate_id'] == delivery.candidate_id)
        reasons = []
        for section in selected['metadata']['segments']:
            quality, copy = section.get('quality'), section.get('copy_audit')
            issues = []
            if quality and not quality['passed']:
                issues.append('商品/画面审核未通过')
            if copy and not copy['passed']:
                issues.append('文案审核未通过')
            if quality is None or copy is None:
                issues.append('审核未完成')
            if issues:
                reasons.append(section['section_id'] + '：' + '；'.join(issues))
        reason = '；'.join(reasons) or '逐段审核已完成'
    return f'{asset.asset_id}: 已交付最佳可用候选 {delivery.candidate_id}（综合分：{score}）；{reason}'
