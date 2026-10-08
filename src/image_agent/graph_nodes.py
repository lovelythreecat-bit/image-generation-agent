"""Individually checkpointed graph stages. Runtime clients and image bytes stay outside state."""

import copy
import inspect

from pydantic import ValidationError

from .compliance import apply_compliance
from .errors import AgentError, ProviderError, make_error_info
from .execution import call_scope
from .images import generated_image, image_format
from .models import (
    Asset,
    AssetTarget,
    CreationResult,
    DetailSetAudit,
    GenerationAttempt,
    GenerationReference,
    LoadedMaterial,
    PreparedContext,
)
from .prompt import (
    build_prompt,
    build_repair_prompt,
    build_shoot_prompt,
    build_stage_prompt,
    build_targets,
    strict_catalog,
)
from .quality import (
    check_pixels,
    choose_retry,
    evaluate_quality,
    export_white_background,
    review_subject_ids,
)
from .selection import bind_generation_references, compile_element_plan, select_references
from .transport import compute

STAGES = (
    "prepare",
    "prepare_asset",
    "generate",
    "save_candidate",
    "audit",
    "repair",
    "finish_asset",
    "group_audit",
    "finish",
)


def aggregate(result):
    if not result.assets:
        return
    statuses = [asset.status for asset in result.assets]
    if all(s == "succeeded" for s in statuses):
        result.status = "succeeded"
    elif any(s in ("succeeded", "accepted") for s in statuses):
        result.status = "accepted" if all(s == "accepted" for s in statuses) else "partial"
    else:
        result.status = next(
            (
                s
                for s in (
                    "generation_uncertain",
                    "budget_exhausted",
                    "audit_error",
                    "pending_audit",
                    "needs_input",
                    "quality_failed",
                )
                if s in statuses
            ),
            "failed",
        )


class GraphNodes:
    def __init__(
        self,
        request,
        config,
        dependencies,
        store,
        policy,
        ledger,
        *,
        analysis=None,
        on_progress=None,
    ):
        self.request, self.config, self.dependencies = request, config, dependencies
        self.store, self.policy, self.ledger = store, policy, ledger
        self.analysis, self.on_progress = analysis, on_progress
        self.materials = store.materials()

    def context(self, state):
        data = dict(state["context"])
        data["materials"] = self.materials
        return PreparedContext.model_validate(data)

    def result(self, state):
        return CreationResult.model_validate(state["result"])

    def asset(self, state):
        return Asset.model_validate(state["result"]["assets"][state["index"]])

    def target(self, state):
        return AssetTarget.model_validate(state["targets"][state["index"]])

    def update_asset(self, state, asset):
        state["result"]["assets"][state["index"]] = asset.model_dump(mode="json")

    def candidate_bytes(self, candidate):
        return self.store.read({"path": candidate["file_path"], "sha256": candidate["sha256"]})

    async def emit(self, stage, state):
        if not self.on_progress:
            return
        assets = state["result"]["assets"]
        index = state.get("index", 0)
        asset = assets[index] if index < len(assets) else None
        event = {
            "stage": stage,
            "asset_id": asset["asset_id"] if asset else None,
            "attempt": len(asset["attempts"]) if asset else 0,
            "message": stage,
            "run_dir": str(self.store.directory) if self.store.directory else None,
        }
        pending = self.on_progress(event)
        if inspect.isawaitable(pending):
            await pending

    def node(self, stage):
        async def run(graph_state):
            state = copy.deepcopy(graph_state["data"])
            await self.emit(stage, state)
            try:
                with call_scope(self.ledger, "vision"):
                    await getattr(self, stage)(state)
            except (AgentError, ValidationError) as error:
                if getattr(error, "code", None) == "stale_analysis":
                    raise
                info = make_error_info(error)
                status = (
                    "budget_exhausted"
                    if info.code == "budget_exhausted"
                    else "generation_uncertain"
                    if info.code == "generation_uncertain"
                    else "failed"
                )
                if stage == "prepare" or not state["result"]["assets"]:
                    state["result"]["error_info"] = info.model_dump(mode="json")
                    state["result"]["status"] = status
                    state["phase"] = "finish"
                else:
                    asset = self.asset(state)
                    asset.status, asset.stop_reason = status, info.code
                    asset.error_info, asset.error = info, info.message
                    if asset.attempts:
                        asset.attempts[-1].error_info = info
                    self.update_asset(state, asset)
                    state["phase"] = "finish_asset"
            state["result"]["usage"] = self.ledger.usage()
            self.store.json("state.json", state)
            await self.persist_result(state)
            for obsolete in state.get("cleanup", []):
                self.store.remove(obsolete)
            return {"data": state}

        return run

    async def persist_result(self, state):
        if self.store.directory:
            from .output import write_manifest

            write_manifest(self.result(state), self.store.directory)

    async def prepare(self, state):
        from .pipeline import prepare_context

        context, result = await prepare_context(
            self.request,
            self.config,
            dependencies=self.dependencies,
            analysis=self.analysis,
            loaded_materials=self.materials,
        )
        result.run_dir = str(self.store.directory) if self.store.directory else None
        state["result"] = result.model_dump(mode="json")
        if context is None:
            state["phase"] = "finish"
            return
        state["context"] = context.model_dump(mode="json")
        state["targets"] = [
            target.model_dump(mode="json")
            for target in build_targets(self.request, context.presentation_mode)
        ]
        state["phase"] = "prepare_asset"

    async def prepare_asset(self, state):
        target, context = self.target(state), self.context(state)
        asset = Asset(
            asset_id=target.target_key,
            platform=target.platform,
            output_type=target.output_type,
            variant=target.variant,
            model=self.config.model_name(self.request.image_model),
            status="pending_audit",
        )
        state["result"]["assets"].append(asset.model_dump(mode="json"))
        plan = compile_element_plan(self.request, target, context, self.config)
        asset.element_plan, asset.reference_bindings = plan, plan.reference_bindings
        state["result"]["warnings"].extend(issue.message for issue in plan.issues)
        asset.prompt = build_prompt(self.request, target, context, plan, "standard")
        self.update_asset(state, asset)
        state.update(
            phase="generate",
            stage="standard",
            repairs=0,
            audit_attempts=0,
            scene=None,
            raw=None,
            previous_failures=None,
            shoot_anchor=state.get("shoot_anchors", {}).get(target.platform)
            if target.output_type == "detail_page"
            else None,
        )

    def references(self, state, asset, context):
        plan = asset.element_plan
        refs = select_references(
            plan,
            context.analysis,
            context.materials,
            limit=self.config.generation_reference_limit,
            reserve_stage=state["stage"] == "staged_fusion",
        )
        if state["stage"] == "staged_scene":
            refs = tuple(ref for ref in refs if ref.role in ("scene", "style"))
        anchor = state.get("shoot_anchor")
        if anchor:
            if state["stage"] != "staged_scene":
                try:
                    refs = select_references(
                        plan,
                        context.analysis,
                        context.materials,
                        limit=self.config.generation_reference_limit - 1,
                        reserve_stage=state["stage"] == "staged_fusion",
                    )
                except ProviderError as error:
                    if error.code != "reference_capacity_exceeded":
                        raise
            room = self.config.generation_reference_limit - int(state["stage"] == "staged_fusion")
            if len(refs) < room:
                refs += (
                    GenerationReference(
                        stage_id=anchor["stage_id"],
                        data=self.store.read(anchor),
                        role="shoot_continuity",
                    ),
                )
            else:
                warning = (
                    asset.asset_id
                    + ": reference capacity retains required evidence; shoot continuity uses shared text only"
                )
                if warning not in state["result"]["warnings"]:
                    state["result"]["warnings"].append(warning)
        if state["stage"] == "staged_scene":
            return refs
        if state["stage"] == "staged_fusion":
            refs += (
                GenerationReference(
                    stage_id="stage", data=self.store.read(state["scene"]), role="scene"
                ),
            )
        previous = next((c for c in reversed(asset.candidates) if c["status"] != "stage"), None)
        if state["repairs"] and previous:
            if len(refs) < self.config.generation_reference_limit:
                refs += (
                    GenerationReference(
                        stage_id="previous-candidate",
                        data=self.candidate_bytes(previous),
                        role="repair_evidence",
                    ),
                )
            else:
                warning = (
                    asset.asset_id
                    + ": reference capacity retains original evidence; previous candidate supplied as textual feedback only"
                )
                if warning not in state["result"]["warnings"]:
                    state["result"]["warnings"].append(warning)
        return refs

    async def generate(self, state):
        asset, target, context = self.asset(state), self.target(state), self.context(state)
        operation = f"{asset.asset_id}-{len(asset.attempts) + 1}"
        old = self.store.operation(operation)
        if old and old["status"] == "saved":
            state.update(raw=old["raw"], operation=operation, phase="save_candidate")
            if "asset_snapshot" in old:
                self.update_asset(state, Asset.model_validate(old["asset_snapshot"]))
            elif "attempt" in old:
                asset.attempts.append(GenerationAttempt.model_validate(old["attempt"]))
                self.update_asset(state, asset)
            return
        if old and old["status"] in ("submitted", "started"):
            raise ProviderError(
                "previous generation may have been charged; response was not recorded",
                code="generation_uncertain",
                kind="transport",
            )
        refs = self.references(state, asset, context)
        plan = bind_generation_references(asset.element_plan, context.analysis, refs)
        if state["stage"] != "staged_scene":
            asset.element_plan, asset.reference_bindings = plan, plan.reference_bindings
        if state["stage"] != "staged_scene":
            base_prompt = build_prompt(self.request, target, context, plan, asset.generation_mode)
            asset.prompt = (
                build_repair_prompt(
                    self.request, target, context, plan, asset.quality, previous_prompt=base_prompt
                )
                if state["repairs"]
                else base_prompt
            )
        prompt = (
            build_stage_prompt(
                self.request,
                target,
                plan,
                asset.prompt,
                shoot_direction=build_shoot_prompt(self.request, target, context, plan),
            )
            if state["stage"] == "staged_scene"
            else asset.prompt
        )
        compliance = apply_compliance(self.request, target, prompt)
        state["result"]["warnings"].extend(compliance.warnings)
        if compliance.blocked:
            raise AgentError("提示词触发合规拦截", kind="compliance", code="compliance_blocked")
        attempt = GenerationAttempt(
            stage=state["stage"],
            model=asset.model,
            reference_ids=[r.material_id or r.stage_id for r in refs],
            prompt=compliance.prompt,
            outcome="failed",
        )
        asset.attempts.append(attempt)
        self.update_asset(state, asset)
        self.store.mark_operation(operation, "started")
        with call_scope(self.ledger, "image", self.store, operation):
            try:
                data = await self.dependencies.generator.generate(
                    prompt=compliance.prompt,
                    model=asset.model,
                    size=self.request.image_size,
                    aspect_ratio=target.aspect_ratio,
                    references=refs,
                )
            except AgentError as error:
                if error.code != "generation_uncertain":
                    self.store.mark_operation(operation, "failed")
                raise
        raw = self.store.write(f"raw/{operation}.bin", data)
        attempt.outcome = "succeeded"
        self.update_asset(state, asset)
        self.store.mark_operation(
            operation,
            "saved",
            raw=raw,
            attempt=attempt.model_dump(mode="json"),
            asset_snapshot=asset.model_dump(mode="json"),
        )
        state.update(raw=raw, operation=operation, phase="save_candidate")

    async def save_candidate(self, state):
        asset, target = self.asset(state), self.target(state)
        data = self.store.read(state["raw"])
        await compute(generated_image, data)
        if target.output_type == "pdd_white_background" and state["stage"] != "staged_scene":
            data = await compute(export_white_background, data)
        candidate_id = f"c{len(asset.candidates) + 1:04d}"
        ref = self.store.write(
            f"candidates/{asset.asset_id}/{candidate_id}{image_format(data)[0]}", data
        )
        candidate = {
            "candidate_id": candidate_id,
            "index": len(asset.candidates) + 1,
            "status": "stage" if state["stage"] == "staged_scene" else "pending_audit",
            "file_path": ref["path"],
            "sha256": ref["sha256"],
            "quality": None,
            "error_info": None,
            "repair_prompt": asset.prompt if state["repairs"] else "",
            "audits": [],
        }
        asset.candidates.append(candidate)
        if state["stage"] == "staged_scene":
            state.update(scene=ref, stage="staged_fusion", phase="generate")
        else:
            asset.status, asset.file_path = "pending_audit", ref["path"]
            asset.quality, asset.error_info, asset.error, asset.stop_reason = None, None, None, None
            state.update(phase="audit", audit_attempts=0)
        self.update_asset(state, asset)
        state["raw"] = None

    async def audit(self, state):
        asset, target, context = self.asset(state), self.target(state), self.context(state)
        candidate = asset.candidates[-1]
        data = self.candidate_bytes(candidate)
        try:
            pixels = await compute(check_pixels, data, target, self.request.image_size, asset.model)
            audit = await self.dependencies.vision.audit_image(
                self.request, target, context, asset.element_plan, data
            )
            quality = evaluate_quality(audit, pixels, target, asset.element_plan)
            pending = review_subject_ids(audit, asset.element_plan)
            if pending:
                review = await self.dependencies.vision.review_product(
                    context, asset.element_plan, pending, data
                )
                quality = evaluate_quality(audit, pixels, target, asset.element_plan, review=review)
        except (AgentError, ValidationError) as error:
            info = make_error_info(error)
            candidate["error_info"] = info.model_dump(mode="json")
            candidate["audits"].append({"error_info": candidate["error_info"]})
            candidate["status"] = asset.status = (
                "budget_exhausted" if info.code == "budget_exhausted" else "audit_error"
            )
            asset.error_info, asset.error, asset.stop_reason = info, info.message, info.code
            state["audit_attempts"] += 1
            retryable = info.retryable or info.kind in ("protocol", "validation")
            state["phase"] = (
                "audit"
                if asset.status == "audit_error"
                and retryable
                and state["audit_attempts"] <= self.policy.max_audit_retries
                else "finish_asset"
            )
        else:
            asset.quality, asset.element_checks = quality, quality.element_checks
            candidate["quality"] = quality.model_dump(mode="json")
            candidate["audits"].append({"quality": candidate["quality"]})
            asset.error_info, asset.error, asset.stop_reason = None, None, None
            candidate["error_info"] = None
            candidate["status"] = asset.status = "succeeded" if quality.passed else "quality_failed"
            if quality.passed:
                state["phase"] = "finish_asset"
            else:
                info = make_error_info(
                    AgentError(quality.reason, code="quality_failed", kind="quality")
                )
                candidate["error_info"] = info.model_dump(mode="json")
                asset.error_info, asset.error = info, info.message
                asset.attempts[-1].outcome, asset.attempts[-1].error_info = "failed", info
                state["phase"] = "repair"
        self.update_asset(state, asset)

    async def repair(self, state):
        asset, target, context = self.asset(state), self.target(state), self.context(state)
        quality = asset.quality.model_dump(mode="json")
        failures = {
            key: value
            for key, value in quality.items()
            if key
            in (
                "visual_quality",
                "platform_compliance",
                "garment_fusion",
                "model_preference",
                "output_intent",
            )
        }
        for key in ("subject_checks", "fact_checks", "element_checks", "constraint_checks"):
            failures[key] = [
                {k: v for k, v in row.items() if k != "reason"} for row in quality[key]
            ]
        failures["pixels"] = (
            {"passed": True}
            if quality["deterministic_checks"]["passed"]
            else {
                key: value
                for key, value in quality["deterministic_checks"].items()
                if key != "reason"
            }
        )
        # Distinct audit evidence can prescribe a new repair even at the same score.
        # Normalize presentation-only differences; never infer that changed instructions
        # are equivalent without evidence, as that would prematurely discard a strategy.
        failures["audit_evidence"] = " ".join(
            (quality.get("model_reason") or quality.get("reason") or "").casefold().split()
        )
        failed_text = " ".join(quality["failed_checks"])
        for kind in ("subject", "fact", "element", "constraint"):
            failures[kind + "_evidence"] = {
                row[kind + "_id"]: " ".join(row["reason"].casefold().split())
                for row in quality[kind + "_checks"]
                if row[kind + "_id"] in failed_text
            }
        if state["repairs"] >= self.policy.max_quality_repairs or failures == state.get(
            "previous_failures"
        ):
            asset.stop_reason = (
                "quality_repair_limit"
                if state["repairs"] >= self.policy.max_quality_repairs
                else "no_quality_improvement"
            )
            state["phase"] = "finish_asset"
        else:
            mode = choose_retry(target, asset.quality, asset.element_plan)
            if mode == "staged":
                try:
                    select_references(
                        asset.element_plan,
                        context.analysis,
                        context.materials,
                        limit=self.config.generation_reference_limit,
                        reserve_stage=True,
                    )
                except ProviderError as error:
                    if error.code != "reference_capacity_exceeded":
                        raise
                    mode = "strict"
                    state["result"]["warnings"].append(
                        asset.asset_id + ": staged预留后证据容量不足，改用strict"
                    )
            asset.generation_mode = mode
            asset.prompt = build_repair_prompt(
                self.request,
                target,
                context,
                asset.element_plan,
                asset.quality,
                previous_prompt=asset.prompt,
            )
            state.update(
                phase="generate",
                stage="staged_scene" if mode == "staged" else mode,
                repairs=state["repairs"] + 1,
                previous_failures=failures,
            )
        self.update_asset(state, asset)

    async def finish_asset(self, state):
        asset = self.asset(state)
        if asset.status == "succeeded":
            candidate = next(c for c in reversed(asset.candidates) if c["status"] == "succeeded")
            data = self.candidate_bytes(candidate)
            ref = self.store.write(
                f"approved/{asset.platform}/{asset.output_type}/{asset.variant or 'default'}{image_format(data)[0]}",
                data,
            )
            state.setdefault("cleanup", []).append(candidate["file_path"])
            asset.file_path = candidate["file_path"] = ref["path"]
            candidate["selected"] = True
            target = self.target(state)
            if "detail_page" in self.request.output_types and (
                (target.output_type == "main_image" and not strict_catalog(target))
                or (target.output_type == "detail_page" and target.variant == "scene")
            ):
                state.setdefault("shoot_anchors", {}).setdefault(
                    target.platform,
                    ref | {"stage_id": f"shoot-{target.platform}-{target.variant or 'main'}"},
                )
        elif asset.candidates:
            warning_only = (
                asset.quality is not None
                and asset.quality.critical_failed_checks == []
                and asset.status == "quality_failed"
            )
            state["result"]["warnings"].append(
                asset.asset_id
                + (
                    ": 主观指标未达标，已交付候选图及审核警告，未标记审核通过"
                    if warning_only
                    else ": 图片已保留为候选图，未通过审核，不属于成功交付"
                )
            )
        self.update_asset(state, asset)
        state.setdefault("asset_runtime", {})[str(state["index"])] = {
            key: state.get(key)
            for key in ("repairs", "stage", "scene", "previous_failures", "shoot_anchor")
        }
        queue = state.get("resume_queue")
        if queue:
            state["index"] = queue.pop(0)
            state.update(state.get("asset_runtime", {}).get(str(state["index"]), {}))
            state.update(phase="audit", audit_attempts=0)
        elif queue is not None:
            state["phase"] = "group_audit"
        else:
            state["index"] += 1
            state["phase"] = (
                "prepare_asset" if state["index"] < len(state["targets"]) else "group_audit"
            )

    async def group_audit(self, state):
        result, context = self.result(state), self.context(state)
        existing = {audit.platform: audit for audit in result.detail_set_audits}
        for platform in self.request.platforms:
            detail = [
                asset
                for asset in result.assets
                if asset.platform == platform and asset.output_type == "detail_page"
            ]
            if not detail:
                continue
            if (
                platform in existing
                and existing[platform].error is None
                and existing[platform].passed
            ):
                continue
            if len(detail) != 3 or not all(a.status == "succeeded" for a in detail):
                audit = DetailSetAudit(
                    platform=platform,
                    passed=False,
                    distinctiveness=None,
                    role_coverage=None,
                    issues=["detail set incomplete or contains unapproved images"],
                    reason="成组审核未执行：组图未全部通过",
                )
            else:
                try:
                    audit_context = context.model_copy(update={"shoot_reference": None})
                    anchor = state.get("shoot_anchors", {}).get(platform)
                    if anchor and anchor["stage_id"].endswith("-main"):
                        source_ids = {
                            mid for asset in detail for mid in asset.element_plan.audit_material_ids
                        }
                        if len(source_ids) + 4 <= self.config.vision_image_limit:
                            audit_context.shoot_reference = LoadedMaterial(
                                material_id=anchor["stage_id"],
                                data=self.store.read(anchor),
                                sha256=anchor["sha256"],
                                order=0,
                                role_hint="style",
                            )
                        else:
                            result.warnings.append(
                                platform
                                + ": vision capacity retains product evidence and details; "
                                "main-to-detail shoot continuity audit omitted"
                            )
                    audit = await self.dependencies.vision.audit_detail_set(
                        platform,
                        audit_context,
                        tuple(a.element_plan for a in detail),
                        tuple(
                            self.candidate_bytes(
                                next(
                                    c for c in reversed(a.candidates) if c["status"] == "succeeded"
                                )
                            )
                            for a in detail
                        ),
                    )
                except (AgentError, ValidationError) as error:
                    info = make_error_info(error)
                    audit = DetailSetAudit(
                        platform=platform,
                        passed=False,
                        distinctiveness=None,
                        role_coverage=None,
                        issues=[],
                        reason="成组审核服务失败",
                        error=info.message,
                        error_info=info,
                    )
            existing[platform] = audit
            if not audit.passed:
                result.warnings.append(
                    platform + ": 详情组图审核警告（不影响有效单图）：" + audit.reason
                )
            result.detail_set_audits = list(existing.values())
            state["result"] = result.model_dump(mode="json")
            self.store.json("state.json", state)
        state["phase"] = "finish"

    async def finish(self, state):
        result = self.result(state)
        aggregate(result)
        result.warnings = list(dict.fromkeys(result.warnings))
        state["result"] = result.model_dump(mode="json")
        state["phase"] = "done"
