from hashlib import sha256

import pytest

from image_agent.config import AgentConfig
from image_agent.contracts import result_to_bundle
from image_agent.errors import ProviderError
from image_agent.execution import ExecutionPolicy
from image_agent.pipeline import run_pipeline
from tests.test_pipeline import setup


@pytest.mark.parametrize('n', [0, 1, 2])
async def test_repair_limit_delivers_highest_candidate_without_faking_audit(request_data, n):
    scores = [{'identity': 96, 'same': False}, {'identity': 65, 'same': False},
              {'identity': 45, 'same': False}]
    request, deps, vision, gen = setup(request_data, scores={'main': scores})
    result = await run_pipeline(request, AgentConfig(), dependencies=deps,
                                policy=ExecutionPolicy(max_quality_repairs=n))
    assert result.status == 'succeeded'
    asset = result.assets[0]
    assert asset.delivery.candidate_id == 'c0001'
    assert not asset.quality.passed and asset.quality.critical_failed_checks
    assert asset.error_info is None and asset.stop_reason == 'quality_repair_limit'
    assert len(gen.calls) == 1 + 2 * n
    assert len(asset.candidates) == 1 + 2 * n
    assert next(c for c in asset.candidates if c.get('selected'))['candidate_id'] == 'c0001'
    assert asset.candidates[0]['status'] == 'quality_failed'
    bundle = result_to_bundle(result)
    assert bundle.dto.assets[0].delivery.sha256 == sha256(bundle.blobs[asset.asset_id]).hexdigest()
    assert bundle.dto.assets[0].quality.passed is False
    assert result.warnings


async def test_best_can_be_later_and_equal_scores_choose_earliest(request_data):
    request, deps, _, _ = setup(request_data, scores={'main': [
        {'identity': 40, 'same': False}, {'identity': 80, 'same': False},
        {'identity': 80, 'same': False, 'reason': 'new evidence'},
    ]})
    result = await run_pipeline(request, AgentConfig(), dependencies=deps,
                                policy=ExecutionPolicy(max_quality_repairs=2))
    assert result.status == 'succeeded'
    assert result.assets[0].delivery.candidate_id == 'c0003'
    assert result.assets[0].quality.subject_checks[0].score == 80


@pytest.mark.parametrize('error', [ProviderError('audit unavailable', kind='transport'),
                                 ProviderError('audit response malformed')])
async def test_unscored_image_delivered_after_audit_retries(request_data, error):
    request, deps, _, gen = setup(request_data, scores={'main': [error]})
    result = await run_pipeline(request, AgentConfig(), dependencies=deps,
                                policy=ExecutionPolicy(max_audit_retries=1))
    assert result.status == 'succeeded'
    assert len(gen.calls) == 1
    assert result.assets[0].delivery.score is None
    assert result.assets[0].quality is None
    assert result.assets[0].candidates[0]['error_info']


@pytest.mark.parametrize('code', ['budget_exhausted', 'generation_uncertain', 'provider_transport'])
async def test_repair_interruption_preserves_available_final_not_stage(request_data, code):
    request, deps, _, gen = setup(request_data, scores={'main': [{'identity': 60, 'same': False}]})
    generate = gen.generate

    async def interrupted(**kwargs):
        if len(gen.calls) == 2:
            raise ProviderError('repair stopped', code=code, kind='transport')
        return await generate(**kwargs)

    deps.generator.generate = interrupted
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == 'succeeded'
    asset = result.assets[0]
    assert asset.delivery.candidate_id == 'c0001'
    assert asset.candidates[-1]['status'] == 'stage'
    assert asset.delivery.stop_reason == code


async def test_no_generated_image_does_not_become_success(request_data):
    request, deps, _, _ = setup(request_data, error=ProviderError('no image', kind='transport'))
    result = await run_pipeline(request, AgentConfig(), dependencies=deps)
    assert result.status == 'failed'
    assert result.assets[0].image is None


async def test_detail_group_warning_does_not_block_complete_selected_images(request_data):
    from image_agent.models import DetailSetAudit

    request_data['output_types'] = ['detail_page']
    request, deps, vision, gen = setup(request_data, scores={
        key: [{'identity': 60, 'same': False}] for key in ('scene', 'feature', 'closeup')
    })

    async def group(*args):
        return DetailSetAudit(platform='taobao', passed=False, distinctiveness=50,
                              role_coverage=60, issues=['too similar'], reason='similar')

    vision.audit_detail_set = group
    result = await run_pipeline(request, AgentConfig(), dependencies=deps,
                                policy=ExecutionPolicy(max_quality_repairs=0))
    assert result.status == 'succeeded'
    assert len(gen.calls) == 3
    assert all(a.delivery and a.image for a in result.assets)
    assert not result.detail_set_audits[0].passed
