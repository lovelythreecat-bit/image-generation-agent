from copy import deepcopy
from hashlib import sha256
from io import BytesIO

import pytest
from PIL import Image

from image_agent.config import AgentConfig
from image_agent.contracts import result_to_bundle
from image_agent.errors import ProviderError
from image_agent.execution import ExecutionPolicy
from image_agent.product_long_image_models import ProductLongImageCopyAudit
from tests.fakes import run_reviewed_pipeline as run_pipeline
from tests.test_product_long_image_pipeline import long_setup


@pytest.mark.parametrize('repairs', [0, 1, 2])
async def test_long_repair_limit_selects_best_each_section_and_preserves_failed_audits(
    request_data, repairs,
):
    script = [{'identity': score, 'same': False} for score in (96, 65, 30)[:repairs + 1]]
    request, deps, _, gen = long_setup(request_data, scores={'main': script * 6})
    result = await run_pipeline(request, AgentConfig(), dependencies=deps,
                                policy=ExecutionPolicy(max_quality_repairs=repairs))
    assert result.status == 'succeeded'
    asset = result.assets[0]
    assert len(gen.calls) == 6 * (repairs + 1)
    assert asset.delivery and asset.delivery.score is not None
    selected = next(c for c in asset.candidates if c.get('selected'))
    assert selected['status'] == 'quality_failed'
    segments = selected['metadata']['segments']
    for segment in segments:
        assert segment['delivery']['candidate_id'] == segment['candidates'][0]['candidate_id']
        assert segment['quality']['subject_checks'][0]['score'] == 96
        assert segment['quality']['passed'] is False
    bundle = result_to_bundle(result)
    public = bundle.dto.assets[0]
    assert public.delivery.sha256 == sha256(asset.image).hexdigest()
    assert all(s.delivery and not s.quality.passed for s in public.long_image_review.sections)
    assert result.warnings and Image.open(BytesIO(asset.image)).size == (750, 6000)


@pytest.mark.parametrize('kind', ['copy', 'audit'])
async def test_complete_long_delivers_with_copy_failure_or_unavailable_audit(request_data, kind):
    request, deps, vision, gen = long_setup(request_data)
    if kind == 'audit':
        async def fail(*args):
            raise ProviderError('audit unavailable', kind='transport')
        vision.audit_image = fail
    else:
        async def fail(*args):
            return ProductLongImageCopyAudit(
                passed=False, text_matches=False, no_invented_claims=False,
                layout_readable=True, issues=['copy mismatch'],
            )
        vision.audit_product_long_image_copy = fail
    result = await run_pipeline(request, AgentConfig(), dependencies=deps,
        policy=ExecutionPolicy(max_quality_repairs=0, max_audit_retries=0))
    assert result.status == 'succeeded' and len(gen.calls) == 6
    asset = result.assets[0]
    assert asset.delivery and result.warnings
    assert (asset.delivery.score is None) == (kind == 'audit')
    public = result_to_bundle(result).dto.assets[0]
    assert all(s.delivery for s in public.long_image_review.sections)
    if kind == 'copy':
        assert all(not s.copy_audit.passed for s in public.long_image_review.sections)


@pytest.mark.parametrize('failure', ['transport', 'invalid_image'])
async def test_long_repair_transport_error_uses_saved_section_and_continues(request_data, failure):
    request, deps, _, gen = long_setup(request_data, scores={'main': [
        {'identity': 60, 'same': False}, {}, {}, {}, {}, {},
    ]})
    generate = gen.generate
    interrupted = False
    async def repair_error(**kwargs):
        nonlocal interrupted
        if len(gen.calls) == 1 and not interrupted:
            interrupted = True
            if failure == 'invalid_image':
                return b'not an image'
            raise ProviderError('repair unavailable', kind='transport')
        return await generate(**kwargs)
    gen.generate = repair_error
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == 'succeeded' and len(gen.calls) == 6
    assert result.assets[0].delivery


async def test_recovery_recomposes_old_long_run_without_model_calls(request_data, tmp_path):
    import json

    from image_agent.graph_runtime import recover_best_available

    request, deps, _, gen = long_setup(request_data | {'output_dir': tmp_path},
        scores={'main': [{'identity': 95, 'same': False}, {'identity': 30, 'same': False}] * 6})
    result = await run_pipeline(request, AgentConfig(), dependencies=deps,
        policy=ExecutionPolicy(max_quality_repairs=1))
    from pathlib import Path
    run = Path(result.run_dir)
    state = json.loads((run / 'state.json').read_text('utf-8'))
    asset = state['result']['assets'][0]
    # Reproduce the old terminal shape: full image exists but quality failed.
    asset.update(status='quality_failed', delivery=None)
    for candidate in asset['candidates']:
        candidate.pop('selected', None)
    state['result']['status'] = 'quality_failed'
    (run / 'state.json').write_text(json.dumps(state), encoding='utf-8')
    before = deepcopy(result.usage)
    recovered = recover_best_available(run)
    assert recovered.status == 'succeeded' and recovered.usage == before
    assert len(gen.calls) == 12
    selected = next(c for c in recovered.assets[0].candidates if c.get('selected'))
    assert all(s['quality']['subject_checks'][0]['score'] == 95
               for s in selected['metadata']['segments'])
    again = recover_best_available(run)
    assert again.assets[0].delivery == recovered.assets[0].delivery
    assert len(again.assets[0].candidates) == len(recovered.assets[0].candidates)
    public = result_to_bundle(recovered).dto.assets[0]
    assert public.long_image_review.candidate_id == public.delivery.candidate_id
    assert public.long_image_review.sha256 == public.delivery.sha256


def test_section_ties_choose_earliest_and_corrupt_best_falls_back():
    from image_agent.models import PixelChecks
    from image_agent.product_long_image_delivery import select_sections
    from image_agent.quality import evaluate_quality
    from image_agent.run_store import RunStore
    from tests.test_images import picture
    from tests.test_quality import audit, plan, target

    store = RunStore()
    element_plan = plan()
    output = target().model_copy(update={'output_type': 'long_image'})
    quality = evaluate_quality(audit(), PixelChecks(passed=True, width=800, height=800,
        reason='ok'), output, element_plan).model_dump(mode='json')
    candidates = [dict(candidate_id=f'c{i}', ref=store.write(f'c{i}.png', picture()),
        element_plan=element_plan.model_dump(mode='json'), quality=quality,
        copy_audit={'passed': False}) for i in range(2)]
    sections = [dict(section_id='section', status='quality_failed', candidates=candidates)]
    selected, _ = select_sections(sections, output, store)
    assert selected[0]['selected_candidate_id'] == 'c0'
    store.write('c0.png', b'corrupt')
    selected, _ = select_sections(sections, output, store)
    assert selected[0]['selected_candidate_id'] == 'c1'


async def test_recovery_keeps_readable_full_image_if_old_section_is_missing(request_data, tmp_path):
    from pathlib import Path

    from image_agent.graph_runtime import recover_best_available
    from image_agent.run_store import RunStore

    request, deps, _, gen = long_setup(request_data | {'output_dir': tmp_path},
        scores={'main': [{'identity': 60, 'same': False}]})
    request.long_image.segment_count = 3
    result = await run_pipeline(request, AgentConfig(), dependencies=deps,
        policy=ExecutionPolicy(max_quality_repairs=0))
    store = RunStore(result.run_dir)
    state = store.json('state.json')
    asset = state['result']['assets'][0]
    asset.update(status='quality_failed', delivery=None)
    ref = state['product_long_images'][asset['asset_id']]['segments'][0]['candidates'][0]['ref']
    (Path(result.run_dir) / ref['path']).unlink()
    store.json('state.json', state)
    recovered = recover_best_available(result.run_dir)
    assert recovered.status == 'succeeded' and recovered.assets[0].image == result.assets[0].image
    assert len(gen.calls) == 3 and recovered.usage == result.usage
