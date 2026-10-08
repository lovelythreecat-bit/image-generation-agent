"""Single orchestrator for discovery, selection, per-asset generation and repair."""

import hashlib
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from pydantic import ValidationError

from .config import AgentConfig
from .errors import AgentError, ProviderError, StaleAnalysisError, make_error_info
from .generate import ImageGenerator
from .images import decode_image, encode_jpeg, load_image
from .marketing import creative_input
from .models import (
    CreationResult,
    ErrorInfo,
    ImageSource,
    LoadedMaterial,
    MaterialAnalysis,
    PreparedAnalysis,
    PreparedContext,
    ProductInputAudit,
)
from .prompt import (
    is_apparel,
    resolve_presentation,
)
from .quality import (
    normalize_audit_checks,
)
from .selection import (
    analysis_fingerprint,
    finalize_selection,
    normalize_discovery_attribution,
    resolve_selection,
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


async def prepare_analysis(request, config, dependencies, *, loaded_materials=None):
    if request.selection.mode != "auto":
        raise AgentError("analyze_materials only accepts auto selection")
    materials = (
        loaded_materials if loaded_materials is not None else await _load(request, dependencies)
    )
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
        analysis = normalize_discovery_attribution(analysis, request)
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


async def prepare_context(request, config, *, dependencies, analysis=None, loaded_materials=None):
    if request.selection.mode == "explicit" and analysis is None:
        raise AgentError("explicit selection requires the same analysis snapshot")
    result = _result(request)
    if analysis is None:
        prepared = await prepare_analysis(
            request, config, dependencies, loaded_materials=loaded_materials
        )
    else:
        materials = (
            loaded_materials if loaded_materials is not None else await _load(request, dependencies)
        )
        analysis = MaterialAnalysis.model_validate(
            analysis.model_dump() if isinstance(analysis, MaterialAnalysis) else analysis
        )
        if analysis.fingerprint != analysis_fingerprint(request, materials):
            raise StaleAnalysisError("analysis does not match current materials, hints or brief")
        prepared = PreparedAnalysis(materials=materials, analysis=analysis)
    result.analysis = prepared.analysis
    result.warnings.extend(prepared.analysis.warnings)
    if prepared.analysis.status == "failed" and prepared.analysis.intent is None:
        result.error_info = prepared.analysis.error_info or ErrorInfo(
            code="provider_protocol",
            kind="protocol",
            message="analysis has no usable intent; reanalyze",
        )
        return None, result
    try:
        draft = resolve_selection(request, prepared.analysis)
        warnings_only = bool(draft.issue_resolutions) and all(
            resolution.status == "irrelevant" for resolution in draft.issue_resolutions
        )
        if warnings_only:
            result.warnings.extend(resolution.reason for resolution in draft.issue_resolutions)
        evidence = None
        if (
            draft.candidate
            and not draft.blocking_issues
            and (
                draft.pending_issues or (prepared.analysis.status != "ready" and not warnings_only)
            )
        ):
            if len(prepared.materials) > config.vision_image_limit:
                raise ProviderError(
                    "evidence review exceeds vision capacity",
                    kind="capability",
                    code="vision_capacity_exceeded",
                )
            evidence = await dependencies.vision.validate_evidence(
                request, prepared.materials, prepared.analysis, draft
            )
            evidence = normalize_audit_checks(
                evidence,
                {
                    "subject_id": draft.candidate.subject_ids,
                    "fact_id": selected_fact_ids(prepared.analysis, draft.candidate),
                    "issue_id": [i.issue_id for i in draft.pending_issues],
                },
            )
        resolution = finalize_selection(draft, evidence)
        if resolution.status != "ready":
            result.status = "needs_input" if resolution.status == "needs_input" else "failed"
            result.issues = resolution.issues
            result.error_info = resolution.error_info
            result.input_check.reason = (
                "待澄清" if result.status == "needs_input" else "所选商品不符"
            )
            return None, result
        selection = resolution.selection
        selected = [s for s in prepared.analysis.subjects if s.subject_id in selection.subject_ids]
        if evidence is None and any(not s.matches_product for s in selected):
            result.error_info = ErrorInfo(
                code="input_mismatch", kind="input", message="所选商品与声明不符"
            )
            result.input_check.reason = result.error_info.message
            return None, result
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
            user_input=creative_input(request),
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
        if "detail_page" in request.output_types:
            context.shoot_plan = {
                "user_scene_and_style": request.style_hint,
                "style_reference_summary": context.style_prompt,
                "shared_visual_suggestions": {
                    s.aspect: s.instruction
                    for s in (
                        prepared.analysis.creative_plan.suggestions
                        if prepared.analysis.creative_plan
                        else []
                    )
                    if s.aspect in ("scene", "lighting", "palette", "mood")
                },
            }
        result.input_check = input_check
        result.presentation_mode = presentation
        result.product_attributes = context.product_attributes
        result.style_prompt = context.style_prompt
        result.warnings = context.warnings
    except ProviderError as error:
        result.error_info = make_error_info(error)
        return None, result
    return context, result


def _dependencies(transport, config, image_alias=None):
    async def load(source):
        return await load_image(source, client=transport.client, config=config)

    return Dependencies(
        vision=VisionClient(transport, config),
        generator=ImageGenerator(transport, config, encoder=encode_jpeg, alias=image_alias),
        load=load,
    )


async def analyze_materials(request, config=None):
    if request.selection.mode != "auto":
        raise AgentError("analyze_materials only accepts auto selection")
    config = config or AgentConfig.from_env()
    async with HttpTransport(config, services=config.service_names()) as transport:
        return (await prepare_analysis(request, config, _dependencies(transport, config))).analysis


async def run_pipeline(
    request, config, *, dependencies, analysis=None, policy=None, on_progress=None, store=None
):
    from .graph_runtime import execute_pipeline

    return await execute_pipeline(
        request,
        config,
        dependencies=dependencies,
        analysis=analysis,
        policy=policy,
        on_progress=on_progress,
        store=store,
    )


async def create_images(request, config=None, *, analysis=None, policy=None, on_progress=None):
    if request.selection.mode == "explicit" and analysis is None:
        raise AgentError("explicit selection requires an analysis snapshot")
    config = config or AgentConfig.from_env()
    async with HttpTransport(
        config, services=config.service_names(request.image_model)
    ) as transport:
        return await run_pipeline(
            request,
            config,
            dependencies=_dependencies(transport, config, request.image_model),
            analysis=analysis,
            policy=policy,
            on_progress=on_progress,
        )


async def resume_images(run_dir, config=None, *, on_progress=None):
    from .graph_runtime import resume_pipeline
    from .run_store import RunStore

    config = config or AgentConfig.from_env()
    store = RunStore(run_dir)
    request = store.request()
    async with HttpTransport(
        config, services=config.service_names(request.image_model)
    ) as transport:
        return await resume_pipeline(
            run_dir,
            config,
            dependencies=_dependencies(transport, config, request.image_model),
            on_progress=on_progress,
        )


async def accept_candidate(run_dir, asset_id, candidate_id, *, reason):
    from .graph_runtime import accept_saved_candidate

    return await accept_saved_candidate(run_dir, asset_id, candidate_id, reason=reason)
