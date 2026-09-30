"""LangGraph entry, durable checkpoint lifecycle and explicit recovery/acceptance."""

import asyncio
from contextlib import asynccontextmanager
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.errors import NodeCancelledError
from langgraph.graph import END, START, StateGraph

from .errors import AgentError
from .execution import BudgetLedger, ExecutionPolicy
from .graph_nodes import STAGES, GraphNodes, aggregate
from .models import CreationResult, MaterialAnalysis
from .output import reserve_output, write_manifest
from .run_store import RunStore


class GraphState(TypedDict):
    data: dict


def build_graph(nodes, checkpointer):
    graph = StateGraph(GraphState)
    for stage in STAGES:
        graph.add_node(stage, nodes.node(stage))
    routes = {stage: stage for stage in STAGES} | {"done": END}
    graph.add_conditional_edges(START, lambda state: state["data"]["phase"], routes)
    for stage in STAGES:
        graph.add_conditional_edges(stage, lambda state: state["data"]["phase"], routes)
    return graph.compile(checkpointer=checkpointer)


@asynccontextmanager
async def checkpoint(store):
    if store.directory:
        async with AsyncSqliteSaver.from_conn_string(
            str(store.directory / "checkpoints.sqlite")
        ) as saver:
            yield saver
    else:
        yield InMemorySaver()


def hydrate_result(state, store):
    result = CreationResult.model_validate(state["result"])
    for asset in result.assets:
        chosen = next((c for c in asset.candidates if c.get("selected")), None)
        chosen = chosen or next(
            (c for c in reversed(asset.candidates) if c["status"] != "stage"), None
        )
        if chosen:
            asset.image = store.read({"path": chosen["file_path"], "sha256": chosen["sha256"]})
        if asset.file_path and store.directory:
            asset.file_path = str(store.path(asset.file_path))
        elif not store.directory:
            asset.file_path = None
    if not store.directory:
        # Ephemeral payloads are deliberately outside Pydantic serialization and graph state.
        object.__setattr__(
            result,
            "_candidate_images",
            {
                candidate["file_path"]: store.read(
                    {"path": candidate["file_path"], "sha256": candidate["sha256"]}
                )
                for asset in result.assets
                for candidate in asset.candidates
            },
        )
    return result


async def invoke(
    state,
    request,
    config,
    dependencies,
    store,
    policy,
    *,
    analysis=None,
    on_progress=None,
    reopening=False,
):
    ledger = BudgetLedger(
        store.directory / "budget.sqlite" if store.directory else None,
        policy,
        require_existing=reopening,
    )
    try:
        nodes = GraphNodes(
            request,
            config,
            dependencies,
            store,
            policy,
            ledger,
            analysis=analysis,
            on_progress=on_progress,
        )
        # Disable automatic environment-based tracing: task inputs never leave via telemetry.
        from langsmith import tracing_context

        with tracing_context(enabled=False):
            async with checkpoint(store) as saver:
                graph = build_graph(nodes, saver)
                graph_config = {
                    "configurable": {"thread_id": "run"},
                    "recursion_limit": 10000,
                    "callbacks": [],
                }
                try:
                    await graph.ainvoke({"data": state}, graph_config)
                except NodeCancelledError:
                    raise asyncio.CancelledError from None
        return hydrate_result(store.json("state.json"), store)
    finally:
        ledger.close()


async def execute_pipeline(
    request, config, *, dependencies, analysis=None, policy=None, on_progress=None, store=None
):
    from .pipeline import _load, _result

    if request.selection.mode == "explicit" and analysis is None:
        raise AgentError("explicit selection requires the same analysis snapshot")
    config = config.model_copy(deep=True)
    policy = policy or ExecutionPolicy()
    store = store or RunStore(reserve_output(request))
    with store.lock():
        if store.exists("run.json"):
            raise AgentError("run already initialized; use resume_images")
        materials = await _load(request, dependencies)
        if analysis is not None:
            analysis = MaterialAnalysis.model_validate(
                analysis.model_dump() if isinstance(analysis, MaterialAnalysis) else analysis
            )
        store.save_inputs(request, materials, config, policy, analysis)
        result = _result(request)
        result.run_dir = str(store.directory) if store.directory else None
        state = {
            "phase": "prepare",
            "index": 0,
            "result": result.model_dump(mode="json"),
            "targets": [],
            "context": None,
        }
        store.json("state.json", state)
        return await invoke(
            state,
            request,
            config,
            dependencies,
            store,
            policy,
            analysis=analysis,
            on_progress=on_progress,
        )


async def resume_pipeline(run_dir, config, *, dependencies, on_progress=None):
    config = config.model_copy(deep=True)
    store = RunStore(run_dir)
    with store.lock():
        metadata = store.load_metadata(config)
        state = store.json("state.json")
        store.validate_candidates(state)
        policy = ExecutionPolicy.model_validate(metadata["policy"])
        check = BudgetLedger(store.directory / "budget.sqlite", policy, require_existing=True)
        try:
            store.validate_operations(check)
        finally:
            check.close()
        request = store.request()
        analysis = (
            MaterialAnalysis.model_validate(metadata["analysis"]) if metadata["analysis"] else None
        )
        # Terminal quality failures and human decisions never trigger paid regeneration.
        if state["phase"] == "done":
            pending = [
                i
                for i, asset in enumerate(state["result"]["assets"])
                if asset["status"] in ("audit_error", "pending_audit")
            ]
            if pending:
                state.update(
                    index=pending[0], resume_queue=pending[1:], phase="audit", audit_attempts=0
                )
                state.update(state.get("asset_runtime", {}).get(str(pending[0]), {}))
            elif any(a.get("error") for a in state["result"]["detail_set_audits"]):
                state["phase"] = "group_audit"
            else:
                return hydrate_result(state, store)
        return await invoke(
            state,
            request,
            config,
            dependencies,
            store,
            policy,
            analysis=analysis,
            on_progress=on_progress,
            reopening=True,
        )


async def accept_saved_candidate(run_dir, asset_id, candidate_id, *, reason):
    if not isinstance(reason, str) or not reason.strip():
        raise AgentError("acceptance reason is required")
    store = RunStore(run_dir)
    with store.lock():
        store.load_metadata()
        state = store.json("state.json")
        store.validate_candidates(state)
        if state["phase"] != "done":
            raise AgentError("finish or resume the paused workflow before accepting a candidate")
        result = CreationResult.model_validate(state["result"])
        asset = next((a for a in result.assets if a.asset_id == asset_id), None)
        candidate = (
            next((c for c in asset.candidates if c["candidate_id"] == candidate_id), None)
            if asset
            else None
        )
        if candidate is None or candidate["status"] == "stage":
            raise AgentError("final candidate not found")
        if asset.status == "succeeded":
            raise AgentError("automatically approved asset does not need manual acceptance")
        from datetime import datetime, timezone

        from .images import image_format
        from .sanitization import redact

        data = store.read({"path": candidate["file_path"], "sha256": candidate["sha256"]})
        ref = store.write(f"accepted/{asset.asset_id}/{candidate_id}{image_format(data)[0]}", data)
        asset.status, asset.file_path, asset.stop_reason = (
            "accepted",
            ref["path"],
            "manually_accepted",
        )
        candidate["acceptance"] = {
            "reason": redact(reason.strip()),
            "at": datetime.now(timezone.utc).isoformat(),
        }
        candidate["status"] = "accepted"
        obsolete = candidate["file_path"]
        candidate["file_path"] = ref["path"]
        for row in asset.candidates:
            row["selected"] = row["candidate_id"] == candidate_id
        # The chosen candidate's failed audit remains visible and does not become passed.
        asset.quality = candidate["quality"]
        asset.error_info = candidate["error_info"]
        asset.error = asset.error_info.message if asset.error_info else None
        asset.element_checks = asset.quality.element_checks if asset.quality else []
        aggregate(result)
        state["result"] = result.model_dump(mode="json")
        store.json("state.json", state)
        write_manifest(result, store.directory)
        if obsolete != ref["path"]:
            store.remove(obsolete)
        hydrated = hydrate_result(state, store)
        next(a for a in hydrated.assets if a.asset_id == asset_id).image = data
        return hydrated
