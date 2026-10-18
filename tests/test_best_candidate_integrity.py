from hashlib import sha256

from image_agent.config import AgentConfig
from image_agent.contracts import CreationResultDTO, result_to_bundle
from image_agent.delivery import select_best_available
from image_agent.execution import ExecutionPolicy
from image_agent.models import Asset, AssetTarget
from image_agent.pipeline import run_pipeline
from image_agent.server.task_service import sanitize_public_model
from tests.test_images import picture
from tests.test_pipeline import setup


def test_corrupt_and_stage_candidates_are_not_delivered():
    valid = picture()
    candidates = [
        dict(candidate_id='stage', status='stage', file_path='stage', sha256=sha256(valid).hexdigest()),
        dict(candidate_id='broken', status='audit_error', file_path='broken', sha256=sha256(b'broken').hexdigest()),
        dict(candidate_id='final', status='audit_error', file_path='final', sha256=sha256(valid).hexdigest()),
    ]
    asset = Asset(asset_id='taobao.main_image.default', platform='taobao', output_type='main_image',
                  status='audit_error', model='test', candidates=candidates)
    target = AssetTarget(platform='taobao', output_type='main_image', aspect_ratio='1:1',
                         presentation_mode='product_only')
    assert select_best_available(asset, target, lambda c: b'broken' if c['file_path']=='broken' else valid) == valid
    assert asset.delivery.candidate_id == 'final'


def test_missing_required_element_is_scored_zero_not_omitted():
    from image_agent.delivery import candidate_score
    from image_agent.models import AssetElementPlan, PixelChecks, Requirement
    from image_agent.quality import evaluate_quality
    from tests.test_quality import audit, plan, target

    required = Requirement(target_kind='element', target_id='e1', origin='user_required',
                           applicability='must_show', reason='show handle')
    element_plan = plan()
    element_plan.requirements.append(required)
    missing = audit()
    missing.element_checks = [dict(element_id='e1', presence='absent', fidelity_score=None, reason='missing')]
    shown = audit(score=89)
    shown.element_checks = [dict(element_id='e1', presence='present', fidelity_score=89, reason='visible')]
    pixels = PixelChecks(passed=True, width=800, height=800, reason='ok')
    candidate_a = {'quality': evaluate_quality(missing,pixels,target(),element_plan).model_dump(mode='json')}
    candidate_b = {'quality': evaluate_quality(shown,pixels,target(),element_plan).model_dump(mode='json')}
    assert candidate_score(candidate_a,target(),element_plan) < candidate_score(candidate_b,target(),element_plan)

    preferred = AssetElementPlan.model_validate(element_plan.model_dump())
    preferred.requirements[-1].origin = 'preferred'
    assert candidate_score(candidate_a,target(),preferred) == 90


async def test_published_best_candidate_digest_remains_bound_to_actual_blob(request_data, tmp_path):
    request, deps, _, _ = setup(request_data, scores={'main': [{'identity': 70, 'same': False}]})
    result = await run_pipeline(request, AgentConfig(), dependencies=deps,
                                policy=ExecutionPolicy(max_quality_repairs=0))
    bundle = result_to_bundle(result)
    hashes = {sha256(data).hexdigest() for data in bundle.blobs.values()}
    dto = sanitize_public_model(CreationResultDTO, bundle.dto, ['credential'], tmp_path,
                                trusted_hashes=hashes, identity_key='fixed')
    assert dto.assets[0].delivery.sha256 in hashes
    assert dto.assets[0].delivery.candidate_id == dto.assets[0].candidates[0].candidate_id


async def test_explicit_legacy_recovery_selects_original_candidate_without_calls(request_data, tmp_path):
    from image_agent.graph_runtime import recover_best_available
    from image_agent.run_store import RunStore

    request_data['output_dir'] = tmp_path
    request, deps, vision, gen = setup(request_data, scores={'main': [{'identity': 60, 'same': False}]})
    result = await run_pipeline(request, AgentConfig(), dependencies=deps,
                                policy=ExecutionPolicy(max_quality_repairs=0))
    store = RunStore(result.run_dir)
    state = store.json('state.json')
    state['result']['status'] = state['result']['assets'][0]['status'] = 'quality_failed'
    state['result']['assets'][0].pop('delivery')
    store.json('state.json', state)
    calls = (len(gen.calls), len(vision.calls))
    repaired = recover_best_available(result.run_dir)
    again = recover_best_available(result.run_dir)
    assert repaired.status == again.status == 'succeeded'
    assert repaired.assets[0].delivery == again.assets[0].delivery
    assert repaired.assets[0].image == result.assets[0].image
    assert (len(gen.calls), len(vision.calls)) == calls


async def test_audit_uses_actual_source_images_without_inferred_appearance_text(request_data):
    from image_agent.pipeline import prepare_context
    from image_agent.prompt import build_targets
    from image_agent.selection import compile_element_plan
    from image_agent.vision import VisionClient
    from tests.test_vision import ScriptTransport

    request, deps, _, _ = setup(request_data)
    context, _ = await prepare_context(request, AgentConfig(), dependencies=deps)
    context.analysis.facts[0].description = 'invented plain yellow dress'
    target = build_targets(request, 'product_only')[0]
    plan = compile_element_plan(request, target, context, AgentConfig())
    response = (await deps.vision.audit_image(request, target, context, plan, picture())).model_dump()
    transport = ScriptTransport([response])
    await VisionClient(transport, AgentConfig()).audit_image(request, target, context, plan, picture())
    blocks = transport.calls[0][1]['messages'][0]['content']
    assert sum(b['type'] == 'image_url' for b in blocks) == 2
    assert 'invented plain yellow dress' not in blocks[0]['text']
