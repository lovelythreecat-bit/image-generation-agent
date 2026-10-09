"""Independent bounded planning and inline preparation for the long-image runtime."""

import re
from dataclasses import dataclass
from uuid import uuid4

from .config import AgentConfig
from .errors import AgentError, make_error_info
from .execution import ExecutionPolicy, call_scope
from .images import encode_jpeg, load_image
from .long_image_fonts import preflight_long_image, resolve_fonts
from .long_image_models import LongImageIssue, LongImagePlanningResult
from .long_image_product import prepare_long_product, product_from_plan
from .long_image_requirements import collect_text_requirements, validate_long_image_plan
from .long_image_store import LongImageRunStore
from .long_image_vision import (
    LongImageVisionClient,
    validate_section_count,
    validate_slot_copy_isolation,
)
from .output import reserve_output
from .pipeline import _load
from .transport import HttpTransport, compute


@dataclass(frozen=True)
class LongImageDependencies:
    vision: object
    load: object
    generator: object | None = None


def long_image_dependencies(transport, config, image_alias=None):
    from .generate import ImageGenerator

    async def load(source):
        return await load_image(source, client=transport.client, config=config)

    return LongImageDependencies(
        LongImageVisionClient(transport, config),
        load,
        ImageGenerator(transport, config, encoder=encode_jpeg, alias=image_alias),
    )


def planning_service_names(request, config):
    stages = ["plan_long_image"]
    if request.scene == "product_detail":
        stages.extend(["analyze_materials", "validate_evidence"])
    return {route.service for stage in stages if (route := config.vision_route(stage)) is not None}


def missing_input_issues(request):
    text = request.content_brief
    if request.scene == "promotion" and re.search(
        r"(?:活动日期|活动时间|开售日期|event date)", text, re.I
    ):
        known_date = re.search(
            r"\d{1,4}\s*(?:年|月|日|[-/])|(?:今天|明天|今晚|即日起|长期|随时|每天|每周|每月)", text
        )
        unspecified = re.search(
            r"(?:待定|未定|尚未|未确定|未知|待补充|to be determined|tbd)", text, re.I
        )
        if not known_date or unspecified:
            return [
                LongImageIssue(
                    issue_id="activity-date",
                    code="missing_activity_date",
                    message="请补充必须展示的活动日期。",
                    resolution="input",
                    field="content_brief",
                )
            ]
    return []


def _issues(issues):
    return [
        LongImageIssue(
            **i.model_dump(exclude={"fact_ids", "options"}),
            options=[o.model_dump(mode="json") for o in i.options],
        )
        for i in issues
    ]


async def prepare_long_plan(
    request, config, dependencies, store, ledger, *, plan=None, analysis=None, planning_record=None
):
    """Use immutable original request; renderer overlays belong to T5 only.

    Store must be initialized, and caller must hold its lock for disk persistence.
    Every vision scope here spans actual POSTs, retries and corrective JSON calls.
    """
    materials = tuple(store.materials())
    hashes = {m.material_id: m.sha256 for m in materials}
    planning_id = uuid4().hex
    if planning_record is not None and plan is None:
        raise AgentError("planning record requires its matching plan", code="plan_invalid")
    if plan is not None:
        validate_long_image_plan(request, plan, material_hashes=hashes, analysis=analysis)
        validate_section_count(request, plan)
        validate_slot_copy_isolation(plan, analysis)
        if planning_record is not None:
            verified = LongImageRunStore.resolve_planning_record(
                planning_record, request=request, plan=plan, analysis=analysis
            )
            await compute(preflight_long_image, request)
            await _preflight_plan_fonts(request, plan)
            if request.scene == "product_detail":
                product_from_plan(request, materials, plan, analysis, config)
            return LongImagePlanningResult(
                planning_id=verified.planning_id,
                status="ready",
                plan=plan,
                analysis=analysis,
                usage=verified.usage,
                record=verified,
            )
    await compute(preflight_long_image, request)
    issues = missing_input_issues(request)
    if issues:
        return LongImagePlanningResult(
            planning_id=planning_id, status="needs_input", issues=issues, usage=ledger.usage()
        )
    try:
        if request.scene == "product_detail":
            with call_scope(ledger, "vision", operation=planning_id + "/product"):
                product = await prepare_long_product(
                    request, materials, analysis=analysis, vision=dependencies.vision, config=config
                )
            analysis = product.analysis
            if product.status != "ready":
                return LongImagePlanningResult(
                    planning_id=planning_id,
                    status=product.status,
                    analysis=analysis,
                    issues=_issues(product.issues),
                    error_info=product.error_info,
                    usage=ledger.usage(),
                )
            if plan is not None and plan.resolved_selection != product.context.selection:
                raise AgentError(
                    "unbound plan selection differs from verified evidence", code="plan_invalid"
                )
        if plan is None:
            with call_scope(ledger, "vision", operation=planning_id + "/plan"):
                result = await dependencies.vision.plan(
                    request, materials, collect_text_requirements(request), analysis=analysis
                )
            result.planning_id = planning_id
        else:
            result = LongImagePlanningResult(
                planning_id=planning_id,
                status="ready",
                plan=plan,
                analysis=analysis,
                warnings=["外部规划未绑定历史用量；本次证据验证计入当前任务。"],
            )
        result.usage = ledger.usage() if plan is None else None
        if result.status == "ready":
            validate_long_image_plan(
                request, result.plan, material_hashes=hashes, analysis=result.analysis
            )
            validate_section_count(request, result.plan)
            validate_slot_copy_isolation(result.plan, result.analysis)
            if request.scene == "product_detail":
                product_from_plan(request, materials, result.plan, result.analysis, config)
            await _preflight_plan_fonts(request, result.plan)
            # New inline planning can be bound to this execution ledger. Unbound external
            # plans never acquire fabricated historical usage or a zero-cost certificate.
            if plan is None:
                record = store.save_planning_result(result, request, materials, ledger.policy)
                result.record = record
                result.usage = record.usage
        return result
    except AgentError as error:
        if error.code in {"plan_invalid", "stale_analysis"} and plan is not None:
            raise
        return LongImagePlanningResult(
            planning_id=planning_id,
            status="failed",
            analysis=analysis,
            error_info=make_error_info(error),
            usage=ledger.usage(),
        )


async def _preflight_plan_fonts(request, plan):
    texts = [
        u.text
        for section in plan.sections
        for u in [*section.text_units, *(u for row in section.table_rows for u in row)]
    ]
    await compute(resolve_fonts, texts, request.options.font_path)


async def analyze_long_image(request, config=None, *, analysis=None, policy=None, on_progress=None):
    """Standalone planning uses its own run, policy, real ledger and opaque record."""
    config = config or AgentConfig.from_env()
    policy = policy or ExecutionPolicy()
    await compute(preflight_long_image, request)
    issues = missing_input_issues(request)
    if issues:
        # No HTTP client/credentials are required for deterministic missing-input checks.
        return LongImagePlanningResult(
            planning_id=uuid4().hex,
            status="needs_input",
            issues=issues,
            usage={"image_calls": 0, "vision_calls": 0},
        )
    store = LongImageRunStore(reserve_output(request))
    try:
        with store.lock():
            if store.exists("run.json"):
                raise AgentError(
                    "independent planning requires a new output directory", code="plan_invalid"
                )
            async with HttpTransport(
                config, services=planning_service_names(request, config)
            ) as transport:
                dependencies = long_image_dependencies(transport, config)
                materials = await _load(request, dependencies)
                store.initialize(request, materials, config, policy)
                if on_progress:
                    on_progress(
                        {
                            "stage": "plan",
                            "message": "正在规划图文长图",
                            "run_dir": str(store.directory) if store.directory else None,
                        }
                    )
                with store.budget() as ledger:
                    result = await prepare_long_plan(
                        request, config, dependencies, store, ledger, analysis=analysis
                    )
                store.json("planning-result.json", result.model_dump(mode="json"))
                return result
    finally:
        store.close()
