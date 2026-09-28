"""Single orchestrator for discovery, selection, per-asset generation and repair."""

import hashlib
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from pydantic import ValidationError

from .compliance import apply_compliance
from .config import AgentConfig
from .errors import AgentError, ProviderError, StaleAnalysisError, make_error_info
from .generate import ImageGenerator
from .images import decode_image, encode_jpeg, load_image
from .models import (
    Asset,
    CreationResult,
    DetailSetAudit,
    ErrorInfo,
    GenerationAttempt,
    GenerationReference,
    ImageSource,
    LoadedMaterial,
    MaterialAnalysis,
    PreparedAnalysis,
    PreparedContext,
    ProductInputAudit,
)
from .prompt import (
    build_prompt,
    build_stage_prompt,
    build_targets,
    is_apparel,
    resolve_presentation,
)
from .quality import (
    check_pixels,
    choose_retry,
    evaluate_quality,
    export_white_background,
    review_subject_ids,
    validate_check_ids,
)
from .selection import (
    analysis_fingerprint,
    bind_generation_references,
    compile_element_plan,
    finalize_selection,
    resolve_selection,
    select_references,
)
from .transport import HttpTransport, compute
from .vision import VisionClient, selected_fact_ids


class VisionProtocol(Protocol):
    async def analyze_materials(self, request, materials): ...
    async def validate_evidence(self, request, materials, analysis, draft): ...
    async def extract_garments(self, request, materials, subject_id): ...
    async def describe_style(self, request, references): ...
    async def audit_image(self, request, target, context, plan, image): ...
    async def review_product(self, context, plan, subject_ids, image): ...
    async def audit_detail_set(self, platform, context, plans, images): ...


class GeneratorProtocol(Protocol):
    async def generate(self, *, prompt, model, size, aspect_ratio, references) -> bytes: ...


@dataclass(frozen=True)
class Dependencies:
    vision: VisionProtocol
    generator: GeneratorProtocol
    load: Callable[[ImageSource], Awaitable[bytes]]


async def _load(request, dependencies):
    materials = []
    for index, material in enumerate(request.normalized_materials()):
        data = await dependencies.load(material.source)
        await compute(decode_image, data)
        materials.append(
            LoadedMaterial(
                material_id=material.material_id,
                data=data,
                sha256=hashlib.sha256(data).hexdigest(),
                order=index,
                role_hint=material.role_hint,
            )
        )
    return tuple(materials)


def _failed_analysis(fingerprint, error):
    return MaterialAnalysis(
        analysis_id=uuid.uuid4().hex,
        fingerprint=fingerprint,
        status="failed",
        materials=[],
        subjects=[],
        facts=[],
        elements=[],
        intent=None,
        error_info=make_error_info(error),
    )


async def prepare_analysis(request, config, dependencies):
    if request.selection.mode != "auto":
        raise AgentError("analyze_materials only accepts auto selection")
    materials = await _load(request, dependencies)
    fingerprint = analysis_fingerprint(request, materials)
    try:
        if len(materials) > config.vision_image_limit:
            raise ProviderError(
                "discovery exceeds vision capacity",
                kind="capability",
                code="vision_capacity_exceeded",
            )
        analysis = await dependencies.vision.analyze_materials(request, materials)
        analysis = MaterialAnalysis.model_validate(analysis.model_dump())
        analysis.validate_intent_source(request)
        if [m.material_id for m in analysis.materials] != [m.material_id for m in materials]:
            raise ProviderError("discovery material IDs do not match inputs")
        analysis.fingerprint = fingerprint
        for observed, loaded in zip(analysis.materials, materials):
            observed.sha256 = loaded.sha256
        if analysis.status == "failed" and analysis.error_info is None:
            analysis.error_info = ErrorInfo(
                code="provider_protocol",
                kind="protocol",
                message="discovery returned failed without usable evidence",
            )
    except (ProviderError, ValidationError) as error:
        if isinstance(error, ValidationError):
            error = ProviderError("discovery schema invalid")
        analysis = _failed_analysis(fingerprint, error)
    return PreparedAnalysis(materials=materials, analysis=analysis)


def _result(request, **kwargs):
    return CreationResult(
        request_id=request.request_id,
        input_check=ProductInputAudit(matches=False, observed_product="", reason="核对服务失败"),
        status="failed",
        **kwargs,
    )


async def run_pipeline(request, config, *, dependencies, analysis=None):
    if request.selection.mode == "explicit" and analysis is None:
        raise AgentError("explicit selection requires the same analysis snapshot")
    result = _result(request)
    if analysis is None:
        prepared = await prepare_analysis(request, config, dependencies)
    else:
        materials = await _load(request, dependencies)
        analysis = MaterialAnalysis.model_validate(
            analysis.model_dump() if isinstance(analysis, MaterialAnalysis) else analysis
        )
        if analysis.fingerprint != analysis_fingerprint(request, materials):
            raise StaleAnalysisError("analysis does not match current materials, hints or brief")
        prepared = PreparedAnalysis(materials=materials, analysis=analysis)
    snapshot = analysis is not None
    result.analysis = prepared.analysis
    result.warnings.extend(prepared.analysis.warnings)
    if prepared.analysis.status == "failed" and prepared.analysis.intent is None:
        result.error_info = prepared.analysis.error_info or ErrorInfo(
            code="provider_protocol",
            kind="protocol",
            message="analysis has no usable intent; reanalyze",
        )
        return result
    try:
        draft = resolve_selection(request, prepared.analysis)
        evidence = None
        if draft.candidate and not draft.blocking_issues and (snapshot or draft.pending_issues):
            if len(prepared.materials) > config.vision_image_limit:
                raise ProviderError(
                    "evidence review exceeds vision capacity",
                    kind="capability",
                    code="vision_capacity_exceeded",
                )
            evidence = await dependencies.vision.validate_evidence(
                request, prepared.materials, prepared.analysis, draft
            )
            validate_check_ids(evidence.subject_checks, draft.candidate.subject_ids, "subject_id")
            validate_check_ids(
                evidence.fact_checks,
                selected_fact_ids(prepared.analysis, draft.candidate),
                "fact_id",
            )
        resolution = finalize_selection(draft, evidence)
        if resolution.status != "ready":
            result.status = "needs_input" if resolution.status == "needs_input" else "failed"
            result.issues = resolution.issues
            result.error_info = resolution.error_info
            result.input_check.reason = (
                "待澄清" if result.status == "needs_input" else "所选商品不符"
            )
            return result
        selection = resolution.selection
        selected = [s for s in prepared.analysis.subjects if s.subject_id in selection.subject_ids]
        if evidence is None and any(not s.matches_product for s in selected):
            result.error_info = ErrorInfo(
                code="input_mismatch", kind="input", message="所选商品与声明不符"
            )
            result.input_check.reason = result.error_info.message
            return result
        observed = "; ".join(
            s.subject_id
            + ": "
            + ", ".join(
                f.description for f in prepared.analysis.facts if f.subject_id == s.subject_id
            )
            for s in selected
        )
        presentation = resolve_presentation(request, observed)
        input_check = ProductInputAudit(
            matches=True, observed_product=observed, reason="所选商品核对通过"
        )
        context = PreparedContext(
            materials=prepared.materials,
            analysis=prepared.analysis,
            selection=selection,
            input_check=input_check,
            presentation_mode=presentation,
            product_attributes={
                "subjects": {
                    s.subject_id: {
                        f.fact_id: f.description
                        for f in prepared.analysis.facts
                        if f.subject_id == s.subject_id
                    }
                    for s in selected
                }
            },
            warnings=result.warnings,
        )
        if presentation == "model_wear":
            for s in selected:
                text = " ".join(context.product_attributes["subjects"][s.subject_id].values())
                if is_apparel(text) or (
                    len(selected) == 1 and is_apparel(request.category + " " + request.product_name)
                ):
                    try:
                        garments = await dependencies.vision.extract_garments(
                            request,
                            tuple(m for m in prepared.materials if m.material_id in s.material_ids),
                            s.subject_id,
                        )
                        context.product_attributes["subjects"][s.subject_id]["garments"] = (
                            garments.model_dump()
                        )
                    except ProviderError as error:
                        context.warnings.append("服装补充分析降级: " + error.message)
        style_ids = {
            e.material_id
            for element in prepared.analysis.elements
            if element.kind == "style"
            and element.element_id
            in selection.required_element_ids + selection.preferred_element_ids
            for f in prepared.analysis.facts
            if f.fact_id in element.fact_ids
            for e in f.evidence
        }
        if not request.materials:
            style_ids.update(m.material_id for m in prepared.materials if m.role_hint == "style")
        if style_ids:
            try:
                context.style_prompt = await dependencies.vision.describe_style(
                    request, tuple(m.data for m in prepared.materials if m.material_id in style_ids)
                )
            except ProviderError as error:
                context.warnings.append("风格分析降级: " + error.message)
        result.input_check = input_check
        result.presentation_mode = presentation
        result.product_attributes = context.product_attributes
        result.style_prompt = context.style_prompt
        result.warnings = context.warnings
    except ProviderError as error:
        result.error_info = make_error_info(error)
        return result
    for target in build_targets(request, presentation):
        result.assets.append(await _create_asset(request, target, context, config, dependencies))
    for platform in request.platforms:
        detail = [
            a for a in result.assets if a.platform == platform and a.output_type == "detail_page"
        ]
        if len(detail) == 3 and all(a.status == "succeeded" for a in detail):
            try:
                audit = await dependencies.vision.audit_detail_set(
                    platform,
                    context,
                    tuple(a.element_plan for a in detail),
                    tuple(a.image for a in detail),
                )
            except ProviderError as error:
                audit = DetailSetAudit(
                    platform=platform,
                    passed=False,
                    distinctiveness=None,
                    role_coverage=None,
                    issues=[],
                    reason="成组审核服务失败",
                    error=error.message,
                    error_info=make_error_info(error),
                )
            result.detail_set_audits.append(audit)
    succeeded = sum(a.status == "succeeded" for a in result.assets)
    result.status = (
        "succeeded"
        if succeeded == len(result.assets) and succeeded
        else "partial"
        if succeeded
        else "failed"
    )
    result.warnings = list(dict.fromkeys(context.warnings))
    return result


async def _create_asset(request, target, context, config, dependencies):
    model = config.model_name(request.image_model)
    asset = Asset(
        asset_id=target.target_key,
        platform=target.platform,
        output_type=target.output_type,
        variant=target.variant,
        status="failed",
        model=model,
    )

    async def generate(stage, prompt, refs):
        compliance = apply_compliance(request, target, prompt)
        context.warnings.extend(compliance.warnings)
        asset.prompt = compliance.prompt
        attempt = GenerationAttempt(
            stage=stage,
            model=model,
            reference_ids=[r.material_id or r.stage_id for r in refs],
            prompt=compliance.prompt,
            outcome="failed",
        )
        asset.attempts.append(attempt)
        try:
            if compliance.blocked:
                raise AgentError("提示词触发合规拦截", kind="compliance", code="compliance_blocked")
            data = await dependencies.generator.generate(
                prompt=compliance.prompt,
                model=model,
                size=request.image_size,
                aspect_ratio=target.aspect_ratio,
                references=refs,
            )
            attempt.outcome = "succeeded"
            return data
        except AgentError as error:
            attempt.error_info = make_error_info(error)
            raise

    try:
        plan = compile_element_plan(request, target, context, config)
        asset.element_plan = plan
        asset.reference_bindings = plan.reference_bindings
        refs = select_references(
            plan, context.analysis, context.materials, limit=config.generation_reference_limit
        )
        mode = "standard"
        for iteration in range(2):
            asset.generation_mode = mode
            prompt = build_prompt(request, target, context, plan, mode)
            if mode == "staged":
                try:
                    fusion = select_references(
                        plan,
                        context.analysis,
                        context.materials,
                        limit=config.generation_reference_limit,
                        reserve_stage=True,
                    )
                except ProviderError as error:
                    if error.code != "reference_capacity_exceeded":
                        raise
                    mode = asset.generation_mode = "strict"
                    context.warnings.append(
                        target.target_key + ": staged预留后证据容量不足，改用strict"
                    )
                    prompt = build_prompt(request, target, context, plan, "strict")
                else:
                    scene_refs = tuple(r for r in refs if r.role in ("scene", "style"))
                    stage = await generate(
                        "staged_scene",
                        build_stage_prompt(request, target, plan, prompt),
                        scene_refs,
                    )
                    from .images import generated_image

                    await compute(generated_image, stage)
                    fusion_references = fusion + (
                        GenerationReference(
                            stage_id="stage",
                            data=stage,
                            role="scene",
                            element_ids=list(
                                dict.fromkeys(eid for ref in scene_refs for eid in ref.element_ids)
                            ),
                        ),
                    )
                    plan = bind_generation_references(plan, context.analysis, fusion_references)
                    asset.element_plan = plan
                    asset.reference_bindings = plan.reference_bindings
                    prompt = build_prompt(request, target, context, plan, "staged")
                    data = await generate(
                        "staged_fusion",
                        prompt,
                        fusion_references,
                    )
            if mode != "staged":
                data = await generate(mode, prompt, refs)
            if target.output_type == "pdd_white_background":
                data = await compute(export_white_background, data)
            pixels = await compute(check_pixels, data, target, request.image_size, model)
            audit = await dependencies.vision.audit_image(request, target, context, plan, data)
            quality = evaluate_quality(audit, pixels, target, plan)
            asset.quality = quality
            pending = review_subject_ids(audit, plan)
            if pending:
                review = await dependencies.vision.review_product(context, plan, pending, data)
                quality = evaluate_quality(audit, pixels, target, plan, review=review)
                asset.quality = quality
            asset.element_checks = quality.element_checks
            if quality.passed:
                asset.status = "succeeded"
                asset.image = data
                return asset
            info = ErrorInfo(code="quality_failed", kind="quality", message=quality.reason)
            asset.attempts[-1].outcome = "failed"
            asset.attempts[-1].error_info = info
            if iteration == 1:
                raise AgentError(quality.reason, code="quality_failed", kind="quality")
            mode = choose_retry(target, quality, plan)
    except AgentError as error:
        asset.error_info = make_error_info(error)
        asset.error = asset.error_info.message
        asset.image = asset.file_path = None
        if asset.attempts:
            asset.attempts[-1].outcome = "failed"
            asset.attempts[-1].error_info = asset.error_info
    return asset


def _dependencies(transport, config):
    async def load(source):
        return await load_image(source, client=transport.client, config=config)

    return Dependencies(
        vision=VisionClient(transport, config),
        generator=ImageGenerator(transport, config, encoder=encode_jpeg),
        load=load,
    )


async def analyze_materials(request, config=None):
    if request.selection.mode != "auto":
        raise AgentError("analyze_materials only accepts auto selection")
    config = config or AgentConfig.from_env()
    async with HttpTransport(config) as transport:
        return (await prepare_analysis(request, config, _dependencies(transport, config))).analysis


async def create_images(request, config=None, *, analysis=None):
    if request.selection.mode == "explicit" and analysis is None:
        raise AgentError("explicit selection requires an analysis snapshot")
    config = config or AgentConfig.from_env()
    # Output lifecycle is attached by output.py; no directory means no filesystem writes.
    directory = None
    if request.output_dir is not None:
        from .output import reserve_output

        directory = reserve_output(request)
    async with HttpTransport(config) as transport:
        result = await run_pipeline(
            request, config, dependencies=_dependencies(transport, config), analysis=analysis
        )
    if directory is not None:
        import asyncio

        from .output import save_result
        from .transport import settle

        result = await settle(asyncio.create_task(save_result(result, directory)))
    return result
