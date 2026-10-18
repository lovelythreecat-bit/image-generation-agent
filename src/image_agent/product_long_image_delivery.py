"""Select saved section candidates and compose a complete, auditable long image."""

from copy import deepcopy

from .delivery import candidate_score
from .errors import AgentError, make_error_info
from .images import generated_image
from .models import AssetElementPlan, BestAvailableDelivery


def select_sections(segments, target, store):
    selected, images = [], []
    for original in segments:
        section = deepcopy(original)
        ranked = []
        for order, candidate in enumerate(section['candidates']):
            plan = AssetElementPlan.model_validate(candidate['element_plan'])
            score = candidate_score(candidate, target, plan)
            ranked.append(((score is not None, score or 0, -order), candidate, score))
        # An approved section ends repair immediately; retain that approved frame.
        if section['status'] == 'succeeded':
            ranked = ranked[-1:]
        for _, candidate, score in sorted(ranked, key=lambda row: row[0], reverse=True):
            try:
                image = store.read(candidate['ref'])
                generated_image(image)
            except (AgentError, OSError, ValueError):
                continue
            quality, copy = candidate.get('quality'), candidate.get('copy_audit')
            section['quality'], section['copy_audit'] = quality, copy
            if quality is not None and copy is not None:
                section['status'] = ('succeeded' if quality['passed'] and copy['passed']
                                     else 'quality_failed')
            elif section['status'] == 'succeeded':
                section['status'] = 'pending_audit'
            section['selected_candidate_id'] = candidate['candidate_id']
            if original['status'] != 'succeeded':
                section['delivery'] = BestAvailableDelivery(
                    candidate_id=candidate['candidate_id'], score=score,
                    sha256=candidate['ref']['sha256'],
                    stop_reason=(original.get('error_info') or {}).get('code')
                    or ('quality_repair_limit' if original['status'] == 'quality_failed'
                        else original['status']),
                ).model_dump(mode='json')
            images.append(image)
            selected.append(section)
            break
        else:
            raise AgentError('missing readable product long-image section', kind='output',
                             code='output_error')
    return selected, images


def compose_best_sections(asset, target, runtime, store, width):
    """No provider clients or budget reservations; usable for explicit recovery."""
    from .product_long_image_render import compose_product_long_image

    segments, images = select_sections(runtime['segments'], target, store)
    full, slices, boundaries = compose_product_long_image(images, width=width)
    final_number = 1 + sum(c['status'] != 'stage' for c in asset.candidates)
    candidate_id = f'full{final_number:04d}'
    ref = store.write(f'candidates/{asset.asset_id}/{candidate_id}.png', full)
    slice_refs = [store.write(f'slices/{asset.asset_id}/{candidate_id}-{i + 1:03d}.png', data)
                  for i, data in enumerate(slices)]
    failed = [s for s in segments if s['status'] != 'succeeded']
    status = failed[0]['status'] if failed else 'succeeded'
    info = (make_error_info(AgentError(
        'one or more complete sections failed physical/copy review', kind='quality', code=status,
    )) if failed else None)
    metadata = {
        'plan': deepcopy(runtime['plan']), 'plan_ref': runtime['plan_ref'],
        'segments': segments, 'boundaries': boundaries, 'slices': slice_refs,
        'width': width, 'height': sum(b['height'] for b in boundaries),
        'audit_method': 'per-section physical and visual-model copy audits; not deterministic OCR',
    }
    asset.candidates.append(dict(
        candidate_id=candidate_id, index=len(asset.candidates) + 1, status=status,
        file_path=ref['path'], sha256=ref['sha256'], quality=None,
        error_info=info.model_dump(mode='json') if info else None,
        repair_prompt='', metadata=metadata,
    ))
    asset.status, asset.file_path, asset.quality = status, ref['path'], None
    asset.error_info, asset.error = info, info.message if info else None
    asset.stop_reason = 'quality_repair_limit' if status == 'quality_failed' else status if info else None
    return full
