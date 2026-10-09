"""Public long-image execution and credential-free local delivery revisions."""

import asyncio
from datetime import datetime, timezone
from typing import TypedDict

from langgraph.errors import NodeCancelledError
from langgraph.graph import END, START, StateGraph
from pydantic import ValidationError

from .config import AgentConfig
from .errors import AgentError, OutputError, make_error_info
from .execution import ExecutionPolicy
from .graph_runtime import checkpoint
from .images import load_image
from .long_image_fonts import preflight_long_image
from .long_image_models import (
    LongImageOptions,
    LongImageResult,
    LongImageUsage,
)
from .long_image_nodes import STAGES, LongImageNodes
from .long_image_planning import LongImageDependencies, long_image_dependencies
from .long_image_render import render_long_layout
from .long_image_store import LongImageRunStore
from .output import reserve_output
from .pipeline import _load
from .sanitization import redact
from .transport import HttpTransport, compute

SEVERITY = (
    "generation_uncertain",
    "budget_exhausted",
    "audit_error",
    "pending_audit",
    "needs_input",
    "quality_failed",
    "failed",
)


class LongGraphState(TypedDict):
    # data holds phase, slot_index, result, runtime, planning_record and delivery_intent.
    data: dict


def _aggregate_slots(slots, assets):
    statuses = []
    for slot in slots:
        asset = assets.get(slot.slot_id)
        if asset is None:
            statuses.append("failed")
        elif slot.source == "source":
            if asset.source_status != "available" or not asset.image:
                statuses.append("failed")
        elif not asset.image:
            statuses.append(asset.status if asset.status in SEVERITY else "failed")
        else:
            statuses.append(asset.status or "pending_audit")
    for status in SEVERITY:
        if status in statuses:
            return status
    return "accepted" if "accepted" in statuses else "succeeded"


def aggregate_long_image(plan, assets, *, checks, output_errors):
    """Output/layout failures precede generated-candidate severity."""
    if plan is None or output_errors or any(c.get("passed") is False for c in checks):
        return "failed"
    if not isinstance(assets, dict):
        assets = {a.slot_id: a for a in assets}
    return _aggregate_slots(plan.image_slots, assets)


def _statuses(result):
    assets = {a.slot_id: a for a in result.image_assets}
    result.status = aggregate_long_image(
        result.plan, assets, checks=result.page_checks, output_errors=result.output_errors
    )
    current = (
        not result.output_errors
        and all(c.get("passed") is not False for c in result.page_checks)
        and all(assets.get(s.slot_id) and assets[s.slot_id].image for s in result.plan.image_slots)
    )
    for artifact in [*result.sections, *([result.page] if result.page else []), *result.slices]:
        artifact.status = result.status
        if artifact.kind == "section" and current:
            artifact.status = _aggregate_slots(
                [s for s in result.plan.image_slots if s.slot_id in artifact.dependencies], assets
            )
        artifact.availability = (
            "current" if current and artifact.image else "stale" if artifact.file_path else "absent"
        )
    return result


def _options(value, base=None):
    if value is None:
        return base or LongImageOptions()
    if isinstance(value, LongImageOptions):
        value = value.model_dump(mode="python", exclude_unset=True)
    if not isinstance(value, dict) or set(value) - {"width", "theme", "slice_height", "font_path"}:
        raise AgentError(
            "render_options only allow width, theme, slice_height and font_path; content changes require replanning"
        )
    try:
        return LongImageOptions.model_validate(
            (base or LongImageOptions()).model_dump(mode="python") | value
        )
    except ValidationError as error:
        raise AgentError(make_error_info(error).message) from None


def render_long_image_memory(plan, selected_assets, *, render_options=None):
    options = _options(render_options)
    result = LongImageResult(
        status="failed", plan=plan, image_assets=list(selected_assets.values())
    )
    if not all(
        selected_assets.get(s.slot_id) and selected_assets[s.slot_id].image
        for s in plan.image_slots
    ):
        result.status = aggregate_long_image(plan, selected_assets, checks=[], output_errors=[])
        return result
    try:
        rendered = render_long_layout(plan, selected_assets, options)
        result.sections, result.page, result.slices, result.page_checks = (
            rendered.sections,
            rendered.page,
            rendered.slices,
            rendered.checks,
        )
        return _statuses(result)
    except AgentError as error:
        result.error_info = make_error_info(error)
        result.output_errors = [result.error_info]
        return result


def current_options(store):
    pending = store.pending_delivery_revision()
    if pending:
        return LongImageOptions.model_validate(pending["options"])
    if store.exists("delivery-current.json"):
        manifest = store.json("delivery-current.json")
        import json

        return LongImageOptions.model_validate(json.loads(store.read(manifest))["options"])
    return store.request().options


async def deliver_local(store, result, *, render_options=None):
    """Caller owns lock. Intent is durable before drawing or metadata replacement."""
    if result.plan is None:
        return result
    # A paid audit may have finished just before an interrupted delivery intent.
    # Never replay the older manifest's accounting snapshot during local recovery.
    with store.budget() as ledger:
        result.usage = LongImageUsage(
            execution=ledger.usage(),
            linked_planning=result.usage.linked_planning,
            planning_id=result.usage.planning_id,
        )
    options = _options(render_options, current_options(store))
    assets = {a.slot_id: a for a in result.image_assets}
    if not all(assets.get(s.slot_id) and assets[s.slot_id].image for s in result.plan.image_slots):
        result.status = aggregate_long_image(
            result.plan, assets, checks=[], output_errors=result.output_errors
        )
        store.save_checkpoint(result)
        return result
    choices = {a.slot_id: a.selected_candidate_id for a in result.image_assets}
    statuses = {
        a.slot_id: {
            k: v
            for k, v in a.model_dump(mode="json").items()
            if k in {"status", "source_status", "audit", "error_info", "candidates"}
        }
        for a in result.image_assets
    }
    try:
        store.begin_delivery_revision(choices=choices, statuses=statuses, options=options)
        # begin reads the preceding checkpoint; new status metadata is already in intent.
        result.output_errors, result.error_info = [], None
        rendered = await compute(render_long_layout, result.plan, assets, options)
        result.sections, result.page, result.slices, result.page_checks = (
            rendered.sections,
            rendered.page,
            rendered.slices,
            rendered.checks,
        )
        _statuses(result)
        # T3 copies rendered artifacts; supply the aggregated metadata with those pixels.
        store.commit_delivery_revision(result, rendered)
        return store.hydrate_result()
    except (AgentError, OSError) as error:
        info = make_error_info(
            error
            if isinstance(error, AgentError)
            else OutputError("local output could not be saved")
        )
        result.status, result.error_info = "failed", info
        result.output_errors = [info]
        for artifact in [*result.sections, *([result.page] if result.page else []), *result.slices]:
            artifact.status = "failed"
            artifact.availability = "stale" if artifact.file_path else "absent"
        # A render failure can publish a failed manifest; an I/O failure retains intent.
        try:
            if store.pending_delivery_revision():
                store.commit_delivery_revision(result)
                return store.hydrate_result()
        except (AgentError, OSError):
            pass
        return result


def build_long_graph(nodes, saver):
    graph = StateGraph(LongGraphState)
    for stage in STAGES:
        graph.add_node(stage, nodes.node(stage))
    routes = {stage: stage for stage in STAGES} | {"done": END}
    graph.add_conditional_edges(START, lambda state: state["data"]["phase"], routes)
    for stage in STAGES:
        graph.add_conditional_edges(stage, lambda state: state["data"]["phase"], routes)
    return graph.compile(checkpointer=saver)


async def _invoke(state, request, config, dependencies, store, *, on_progress=None):
    from langsmith import tracing_context

    with store.budget() as ledger:
        nodes = LongImageNodes(
            request, config, dependencies, store, ledger, on_progress=on_progress
        )
        with tracing_context(enabled=False):
            async with checkpoint(store) as saver:
                graph = build_long_graph(nodes, saver)
                try:
                    await graph.ainvoke(
                        {"data": state},
                        {
                            "configurable": {"thread_id": "long-image"},
                            "recursion_limit": 10000,
                            "callbacks": [],
                        },
                    )
                except NodeCancelledError:
                    raise asyncio.CancelledError from None
        result = store.hydrate_result()
        # The manifest has the delivered usage; finish checkpoints may contain terminal errors.
        if store.pending_delivery_revision():
            result.status = "failed"
        return result


class _LazyClient:
    """Open only the service needed by the next actual stage."""

    def __init__(self, config, request, *, image=False):
        self.config, self.request, self.image = config, request, image

    def __getattr__(self, name):
        async def call(*args, **kwargs):
            if self.image:
                services = (
                    {self.config.image_models[self.request.image_model].service}
                    if self.config.services
                    else None
                )
            else:
                stage = {
                    "plan": "plan_long_image",
                    "audit": "audit_long_image_asset",
                    "audit_product_slot": "audit_long_image_asset",
                }.get(name, name)
                route = self.config.vision_route(stage)
                services = {route.service} if route else None
            async with HttpTransport(self.config, services=services) as transport:
                deps = long_image_dependencies(
                    transport, self.config, image_alias=self.request.image_model
                )
                return await getattr(deps.generator if self.image else deps.vision, name)(
                    *args, **kwargs
                )

        return call


def _lazy_dependencies(request, config):
    async def load(source):
        return await load_image(source, config=config)

    return LongImageDependencies(
        _LazyClient(config, request), load, _LazyClient(config, request, image=True)
    )


async def execute_long_image(
    request,
    config,
    *,
    dependencies,
    plan=None,
    analysis=None,
    planning_record=None,
    policy=None,
    on_progress=None,
    store=None,
):
    """Injectable offline/runtime seam; public users normally call create_long_image."""
    await compute(preflight_long_image, request)
    policy = policy or ExecutionPolicy()
    store = store or LongImageRunStore(reserve_output(request))
    try:
        with store.lock():
            if store.exists("run.json"):
                raise AgentError("run already initialized; use resume_long_image")
            materials = await _load(request, dependencies)
            store.initialize(request, materials, config, policy)
            meta = store.load_metadata()
            result = LongImageResult(
                status="failed",
                run_id=meta["run_id"],
                run_dir=str(store.directory) if store.directory else None,
            )
            state = {
                "phase": "prepare",
                "slot_index": 0,
                "runtime": {},
                "delivery_intent": None,
                "result": result.model_dump(mode="json"),
                "supplied_plan": plan.model_dump(mode="json") if plan else None,
                "analysis": analysis.model_dump(mode="json") if analysis else None,
                "planning_record": planning_record.model_dump(mode="json")
                if planning_record
                else None,
                "linked_planning": {},
            }
            store.save_checkpoint(result, **{k: v for k, v in state.items() if k != "result"})
            return await _invoke(
                state, request, config, dependencies, store, on_progress=on_progress
            )
    finally:
        store.close()


async def create_long_image(
    request,
    config=None,
    *,
    plan=None,
    analysis=None,
    planning_record=None,
    policy=None,
    on_progress=None,
):
    config = config or AgentConfig.from_env()
    return await execute_long_image(
        request,
        config,
        dependencies=_lazy_dependencies(request, config),
        plan=plan,
        analysis=analysis,
        planning_record=planning_record,
        policy=policy,
        on_progress=on_progress,
    )


async def _render_saved(store, *, render_options=None, on_progress=None):
    try:
        store.load_metadata()
    except OutputError as error:
        return _integrity_failure(store, error)
    if render_options is not None:
        _options(render_options, current_options(store))
    if store.pending_delivery_revision():
        if store.recover_delivery_revision():
            if render_options is None:
                return store.hydrate_result()
        elif render_options is not None:
            # Finish the persisted intent before accepting a different layout request.
            pending_result = store.hydrate_result()
            await deliver_local(store, pending_result)
    result = store.hydrate_result()
    try:
        store.validate_inputs_and_candidates()
    except (AgentError, ValueError, KeyError):
        return result
    if on_progress:
        on_progress(
            {"stage": "render", "message": "正在本地重新排版", "run_dir": str(store.directory)}
        )
    result = await deliver_local(store, result, render_options=render_options)
    store.save_checkpoint(result)
    return result


async def render_long_image(run_dir, *, render_options=None, on_progress=None):
    store = LongImageRunStore(run_dir)
    try:
        with store.lock():
            return await _render_saved(
                store, render_options=render_options, on_progress=on_progress
            )
    finally:
        store.close()


async def accept_long_image_candidate(run_dir, slot_id, candidate_id, *, reason):
    if not isinstance(reason, str) or not reason.strip():
        raise AgentError("acceptance reason is required")
    store = LongImageRunStore(run_dir)
    try:
        with store.lock():
            store.load_metadata()
            if store.pending_delivery_revision():
                await _render_saved(store)
            result = store.hydrate_result()
            store.validate_inputs_and_candidates()
            asset = next(
                (a for a in result.image_assets if a.slot_id == slot_id and a.source == "generate"),
                None,
            )
            candidate = (
                next((c for c in asset.candidates if c["candidate_id"] == candidate_id), None)
                if asset
                else None
            )
            if candidate is None:
                raise AgentError("generated candidate not found")
            candidate["acceptance"] = {
                "reason": redact(reason.strip()),
                "at": datetime.now(timezone.utc).isoformat(),
            }
            candidate["status"] = "accepted"
            asset.selected_candidate_id, asset.status = candidate_id, "accepted"
            asset.file_path, asset.sha256 = candidate["file_path"], candidate["sha256"]
            asset.image = store.read({"path": asset.file_path, "sha256": asset.sha256})
            asset.audit = candidate.get("audit")
            # Validate the union after choosing an older candidate's audit.
            from .long_image_models import LongSlotAsset

            replacement = LongSlotAsset.model_validate(asset.model_dump(mode="python"))
            replacement.image = asset.image
            result.image_assets[result.image_assets.index(asset)] = replacement
            result = await deliver_local(store, result)
            state = store.json("state.json")
            # The human decision supersedes any paused audit/repair for this slot.
            # Start the serial scan again so accepted slots are skipped while
            # any other unfinished slots can still continue normally.
            updates = {}
            if state.get("phase") != "done":
                updates = {"phase": "prepare_slot", "slot_index": 0}
            store.save_checkpoint(result, **updates)
            return result
    finally:
        store.close()


async def resume_long_pipeline(run_dir, config=None, *, dependencies=None, on_progress=None):
    store = LongImageRunStore(run_dir)
    try:
        with store.lock():
            try:
                store.load_metadata()
            except OutputError as error:
                return _integrity_failure(store, error)
            # Local repair has precedence over any environment/configuration access.
            if store.pending_delivery_revision():
                return await _render_saved(store, on_progress=on_progress)
            result = store.hydrate_result()
            try:
                store.validate_inputs_and_candidates()
            except (AgentError, ValueError, KeyError):
                return result
            state = store.json("state.json")
            with store.budget() as ledger:
                try:
                    store.validate_operations(ledger)
                except AgentError as error:
                    result.status, result.error_info = (
                        "generation_uncertain",
                        make_error_info(error),
                    )
                    for asset in result.image_assets:
                        if asset.source == "generate" and asset.status != "accepted":
                            asset.status, asset.error_info = (
                                "generation_uncertain",
                                result.error_info,
                            )
                    result.usage = LongImageUsage(
                        execution=ledger.usage(),
                        linked_planning=state.get("linked_planning", {}),
                        planning_id=state.get("planning_id"),
                    )
                    result = await deliver_local(store, result)
                    store.save_checkpoint(result, phase="done")
                    return result
            if state["phase"] == "done":
                pending = [
                    i
                    for i, a in enumerate(result.image_assets)
                    if a.status in {"pending_audit", "audit_error"}
                ]
                if pending:
                    state.update(phase="prepare_slot", slot_index=pending[0])
                    for i in pending:
                        state.setdefault("slot_runtime", {}).setdefault(
                            result.image_assets[i].slot_id,
                            {"attempt": len(result.image_assets[i].candidates), "repairs": 0},
                        )["audit_attempts"] = 0
                elif result.plan and (result.output_errors or result.page is None):
                    return await _render_saved(store, on_progress=on_progress)
                else:
                    return result
            if state["phase"] in {"render", "compose", "finish"}:
                return await _render_saved(store, on_progress=on_progress)
            config = config or AgentConfig.from_env()
            store.load_metadata(config)
            request = store.request()
            state["result"] = result.model_dump(mode="json")
            return await _invoke(
                state,
                request,
                config,
                dependencies or _lazy_dependencies(request, config),
                store,
                on_progress=on_progress,
            )
    finally:
        store.close()


async def resume_long_image(run_dir, config=None, *, on_progress=None):
    return await resume_long_pipeline(run_dir, config, on_progress=on_progress)


def _integrity_failure(store, error):
    """Return metadata for inspection without hydrating any unverified payload."""
    try:
        data = store.json("state.json")["result"]
        result = LongImageResult.model_validate(data)
    except (AgentError, KeyError, ValueError):
        result = LongImageResult(status="failed", run_dir=str(store.directory))
    result.status, result.error_info = "failed", make_error_info(error)
    result.output_errors = [result.error_info]
    for asset in result.image_assets:
        asset.image = None
    for artifact in [*result.sections, *([result.page] if result.page else []), *result.slices]:
        artifact.status, artifact.image = "failed", None
        artifact.availability = "stale" if artifact.file_path else "absent"
    return result
