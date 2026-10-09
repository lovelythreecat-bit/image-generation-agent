"""Serial, journaled long-image LangGraph stages (no HTTP protocol duplication)."""

import asyncio
from dataclasses import replace

from pydantic import ValidationError

from .errors import AgentError, make_error_info
from .execution import call_scope
from .images import generated_image, image_format
from .long_image_models import LongImageResult, LongImageUsage, LongSlotAsset
from .long_image_planning import prepare_long_plan
from .long_image_product import (
    compile_long_generic_slot,
    compile_long_product_slot,
    product_from_plan,
)
from .models import GenerationReference, MaterialAnalysis
from .prompt import build_repair_prompt
from .quality import check_pixels
from .selection import bind_generation_references
from .transport import compute, settle

STAGES = (
    "prepare",
    "plan",
    "prepare_slot",
    "generate",
    "save_candidate",
    "audit",
    "repair",
    "finish_slot",
    "render",
    "compose",
    "finish",
)


def usage_for(state, ledger):
    return LongImageUsage(
        execution=ledger.usage(),
        linked_planning=state.get("linked_planning", {}),
        planning_id=state.get("planning_id"),
    )


class LongImageNodes:
    def __init__(self, request, config, dependencies, store, ledger, *, on_progress=None):
        self.request, self.config, self.dependencies = request, config, dependencies
        self.store, self.ledger, self.policy = store, ledger, ledger.policy
        self.on_progress = on_progress
        self.prepared = None
        self.rendered = None

    def result(self, state):
        result = LongImageResult.model_validate(state["result"])
        for asset in result.image_assets:
            self.store._hydrate_asset(asset)
        return result

    def node(self, stage):
        async def run(graph_state):
            state = dict(graph_state["data"])
            if self.on_progress:
                self.on_progress(
                    {
                        "stage": stage,
                        "message": f"长图：{stage}",
                        "run_dir": str(self.store.directory) if self.store.directory else None,
                        "slot_id": state.get("slot_id"),
                        "attempt": state.get("runtime", {}).get("attempt", 1),
                    }
                )
            try:
                await getattr(self, stage)(state)
            except (AgentError, ValidationError) as error:
                result = self.result(state)
                info = make_error_info(error)
                if (
                    stage in {"generate", "save_candidate", "prepare_slot", "repair"}
                    and result.image_assets
                ):
                    asset = result.image_assets[state["slot_index"]]
                    asset.status = (
                        info.code
                        if info.code in {"generation_uncertain", "budget_exhausted", "needs_input"}
                        else "failed"
                    )
                    asset.error_info = info
                    state["phase"] = "finish_slot"
                else:
                    result.status = "needs_input" if info.code == "needs_input" else "failed"
                    result.error_info = info
                    state["phase"] = "finish"
                state["result"] = result.model_dump(mode="json")
            result = self.result(state)
            result.usage = usage_for(state, self.ledger)
            state["result"] = result.model_dump(mode="json")
            self.store.save_checkpoint(result, **{k: v for k, v in state.items() if k != "result"})
            return {"data": state}

        return run

    async def prepare(self, state):
        state["phase"] = "plan"

    async def plan(self, state):
        from .long_image_models import LongImagePlan, LongPlanningRecord

        planning = await prepare_long_plan(
            self.request,
            self.config,
            self.dependencies,
            self.store,
            self.ledger,
            plan=LongImagePlan.model_validate(state["supplied_plan"])
            if state.get("supplied_plan")
            else None,
            analysis=MaterialAnalysis.model_validate(state["analysis"])
            if state.get("analysis")
            else None,
            planning_record=LongPlanningRecord.model_validate(state["planning_record"])
            if state.get("planning_record")
            else None,
        )
        result = self.result(state)
        result.plan, result.issues, result.error_info = (
            planning.plan,
            planning.issues,
            planning.error_info,
        )
        result.warnings.extend(planning.warnings)
        state["analysis"] = planning.analysis.model_dump(mode="json") if planning.analysis else None
        state["planning_id"] = planning.planning_id
        if state.get("supplied_plan"):
            state["linked_planning"] = {planning.planning_id: planning.usage}
        if planning.status != "ready":
            result.status = planning.status
            state["phase"] = "finish"
        else:
            result.image_assets = [
                LongSlotAsset(slot_id=s.slot_id, source=s.source) for s in planning.plan.image_slots
            ]
            state.update(phase="prepare_slot", slot_index=0)
        state["result"] = result.model_dump(mode="json")

    def compile(self, state, result):
        slot = result.plan.image_slots[state["slot_index"]]
        materials = self.store.materials()
        if self.request.scene == "product_detail":
            analysis = MaterialAnalysis.model_validate(state["analysis"])
            product = product_from_plan(self.request, materials, result.plan, analysis, self.config)
            prepared = compile_long_product_slot(self.request, slot, result.plan, product)
        else:
            prepared = compile_long_generic_slot(
                self.request, slot, result.plan, materials, self.config
            )
        continuity = next(
            (
                a
                for a in result.image_assets[: state["slot_index"]]
                if a.source == "generate" and a.status == "succeeded" and a.image
            ),
            None,
        )
        if continuity:
            if len(prepared.references) < self.config.generation_reference_limit:
                refs = (
                    *prepared.references,
                    GenerationReference(
                        stage_id="style-" + continuity.slot_id,
                        data=continuity.image,
                        role="style continuity",
                    ),
                )
                element_plan = prepared.element_plan
                if element_plan:
                    element_plan = bind_generation_references(
                        element_plan, prepared.context.analysis, refs
                    )
                prepared = replace(prepared, references=refs, element_plan=element_plan)
            elif "参考容量已满，使用文字保持风格连续。" not in result.warnings:
                result.warnings.append("参考容量已满，使用文字保持风格连续。")
        self.prepared = prepared
        return prepared

    async def prepare_slot(self, state):
        result = self.result(state)
        if state["slot_index"] >= len(result.image_assets):
            state["phase"] = "render"
            return
        asset = result.image_assets[state["slot_index"]]
        slot = result.plan.image_slots[state["slot_index"]]
        state["slot_id"] = slot.slot_id
        state["runtime"] = state.get("slot_runtime", {}).get(
            slot.slot_id, {"attempt": 1, "repairs": 0, "audit_attempts": 0}
        )
        if slot.source == "source":
            ref = next(
                r
                for r in self.store.load_metadata()["inputs"]
                if r["material_id"] == slot.material_ids[0]
            )
            asset.file_path, asset.sha256, asset.image = (
                ref["path"],
                ref["sha256"],
                self.store.read(ref),
            )
            asset.source_status = "available"
            state["phase"] = "finish_slot"
        elif asset.status in {
            "succeeded",
            "accepted",
            "quality_failed",
            "generation_uncertain",
            "budget_exhausted",
            "failed",
        }:
            state["phase"] = "finish_slot"
        else:
            self.compile(state, result)
            state["phase"] = "audit" if asset.selected_candidate_id else "generate"
        state["result"] = result.model_dump(mode="json")

    async def generate(self, state):
        state["local_delivery_current"] = False
        result = self.result(state)
        asset = result.image_assets[state["slot_index"]]
        runtime = state["runtime"]
        operation = f"{result.run_id}/{asset.slot_id}/{runtime['attempt']}"
        state["operation"] = operation
        old = self.store.operation(operation)
        if old and old["status"] == "saved":
            self.store.read(old["raw"])
            state.update(raw=old["raw"], phase="save_candidate")
            return
        if old:
            raise AgentError(
                "previous generation has no persistent response; refusing another POST",
                code="generation_uncertain",
                kind="transport",
            )
        prepared = self.compile(state, result)
        prompt = runtime.get("repair_prompt") or prepared.prompt
        self.store.mark_operation(
            operation, "started", slot_id=asset.slot_id, attempt=runtime["attempt"], prompt=prompt
        )

        async def submit_and_save():
            with call_scope(self.ledger, "image", self.store, operation):
                try:
                    data = await self.dependencies.generator.generate(
                        prompt=prompt,
                        model=self.config.model_name(self.request.image_model),
                        size=self.request.image_size,
                        aspect_ratio=prepared.target.aspect_ratio,
                        references=prepared.references,
                    )
                except AgentError as error:
                    if error.code == "budget_exhausted" and not self.store.operation(operation).get(
                        "submissions"
                    ):
                        self.store.mark_operation(operation, "failed", error_code=error.code)
                        raise
                    raise AgentError(
                        "generation response was not persisted; submission may have been charged",
                        code="generation_uncertain",
                        kind="transport",
                    ) from error
            raw = self.store.write(f"raw/{asset.slot_id}/{runtime['attempt']}.bin", data)
            self.store.mark_operation(
                operation, "saved", raw=raw, slot_id=asset.slot_id, attempt=runtime["attempt"]
            )
            return raw

        state["raw"] = await settle(asyncio.create_task(submit_and_save()))
        state["phase"] = "save_candidate"

    async def save_candidate(self, state):
        result = self.result(state)
        asset = result.image_assets[state["slot_index"]]
        raw = state.get("raw") or self.store.operation(state["operation"])["raw"]
        data = self.store.read(raw)
        await compute(generated_image, data)
        cid = f"c{state['runtime']['attempt']:04d}"
        if not any(c["candidate_id"] == cid for c in asset.candidates):
            ref = self.store.write(f"candidates/{asset.slot_id}/{cid}{image_format(data)[0]}", data)
            asset.candidates.append(
                {
                    "candidate_id": cid,
                    "file_path": ref["path"],
                    "sha256": ref["sha256"],
                    "status": "pending_audit",
                    "audit": None,
                    "audits": [],
                    "operation_id": state["operation"],
                }
            )
        chosen = next(c for c in asset.candidates if c["candidate_id"] == cid)
        asset.selected_candidate_id = cid
        asset.file_path, asset.sha256, asset.image = chosen["file_path"], chosen["sha256"], data
        asset.status, asset.audit, asset.error_info = "pending_audit", None, None
        state["runtime"]["audit_attempts"] = 0
        result = await self.refresh_existing_delivery(state, result)
        state.update(result=result.model_dump(mode="json"), phase="audit", raw=None)

    async def refresh_existing_delivery(self, state, result):
        """Never checkpoint new candidate metadata alongside an older current page."""
        if self.store.exists("delivery-current.json"):
            from .long_image_runtime import deliver_local

            result.usage = usage_for(state, self.ledger)
            result = await deliver_local(self.store, result)
            state["local_delivery_current"] = True
        return result

    async def audit(self, state):
        result = self.result(state)
        asset = result.image_assets[state["slot_index"]]
        slot = result.plan.image_slots[state["slot_index"]]
        candidate = next(
            c for c in asset.candidates if c["candidate_id"] == asset.selected_candidate_id
        )
        prepared = self.compile(state, result)
        runtime = state["runtime"]
        runtime["audit_attempts"] += 1
        try:
            pixels = await compute(
                check_pixels,
                asset.image,
                prepared.target,
                self.request.image_size,
                self.config.model_name(self.request.image_model),
            )
            candidate["pixels"] = pixels.model_dump(mode="json")
            if not pixels.passed:
                asset.status = "quality_failed"
                asset.error_info = make_error_info(
                    AgentError(pixels.reason, code="quality_failed", kind="quality")
                )
                candidate["status"] = asset.status
                result = await self.refresh_existing_delivery(state, result)
                state.update(result=result.model_dump(mode="json"), phase="finish_slot")
                return
            with call_scope(
                self.ledger,
                "vision",
                operation=f"{result.run_id}/{slot.slot_id}/{candidate['candidate_id']}/audit/{runtime['audit_attempts']}",
            ):
                if self.request.scene == "product_detail":
                    audit = await self.dependencies.vision.audit_product_slot(
                        self.request,
                        slot,
                        prepared,
                        asset.image,
                        size=self.request.image_size,
                        model=self.config.model_name(self.request.image_model),
                    )
                else:
                    audit = await self.dependencies.vision.audit(
                        slot,
                        result.plan,
                        asset.image,
                        candidate_id=candidate["candidate_id"],
                        materials=self.store.materials(),
                    )
            asset.audit, asset.error_info = audit, None
            candidate["error_info"] = None
            candidate["audit"] = audit.model_dump(mode="json")
            candidate["audits"].append({"audit": candidate["audit"]})
            asset.status = candidate["status"] = "succeeded" if audit.passed else "quality_failed"
            state["phase"] = "finish_slot" if audit.passed else "repair"
        except (AgentError, ValidationError) as error:
            info = make_error_info(error)
            asset.error_info = info
            asset.status = candidate["status"] = (
                "budget_exhausted" if info.code == "budget_exhausted" else "audit_error"
            )
            candidate["error_info"] = info.model_dump(mode="json")
            candidate["audits"].append({"error_info": candidate["error_info"]})
            retryable = info.retryable or info.kind in {"protocol", "validation"}
            state["phase"] = (
                "audit"
                if asset.status == "audit_error"
                and retryable
                and runtime["audit_attempts"] <= self.policy.max_audit_retries
                else "finish_slot"
            )
        result = await self.refresh_existing_delivery(state, result)
        state["result"] = result.model_dump(mode="json")

    async def repair(self, state):
        result = self.result(state)
        asset = result.image_assets[state["slot_index"]]
        audit = asset.audit
        runtime = state["runtime"]
        if (
            runtime["repairs"] >= self.policy.max_quality_repairs
            or audit is None
            or (self.request.scene != "product_detail" and not audit.repairable)
        ):
            state["phase"] = "finish_slot"
            return
        prepared = self.compile(state, result)
        if self.request.scene == "product_detail":
            prompt = build_repair_prompt(
                prepared.evidence_request,
                prepared.target,
                prepared.context,
                prepared.element_plan,
                audit,
                previous_prompt=prepared.prompt,
            )
        else:
            prompt = prepared.prompt + "\n\n" + audit.repair_prompt
        runtime.update(
            repairs=runtime["repairs"] + 1, attempt=runtime["attempt"] + 1, repair_prompt=prompt
        )
        state["phase"] = "generate"

    async def finish_slot(self, state):
        state.setdefault("slot_runtime", {})[state["slot_id"]] = dict(state["runtime"])
        state.update(slot_index=state["slot_index"] + 1, phase="prepare_slot")

    async def render(self, state):
        # Delivery intent precedes pixels and all metadata changes.
        from .long_image_runtime import deliver_local

        result = self.result(state)
        result.usage = usage_for(state, self.ledger)
        if not state.get("local_delivery_current"):
            result = await deliver_local(self.store, result)
        state.update(result=result.model_dump(mode="json"), phase="compose")

    async def compose(self, state):
        # T2 composes sections and contiguous slices in the same local pass.
        state["phase"] = "finish"

    async def finish(self, state):
        state["phase"] = "done"
