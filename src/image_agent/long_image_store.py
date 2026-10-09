"""Long-image storage and local delivery revisions on the existing run primitives.

Callers hold ``lock()`` across read/modify/write operations. Paths in persisted
results are root-relative. The original request is immutable; render overrides
belong to delivery intents. A planning descriptor is only a locator: resolve it
against its original store before using its plan, analysis or usage.
"""

import io
import json
from contextlib import contextmanager
from pathlib import Path, PurePosixPath, PureWindowsPath
from uuid import uuid4

from PIL import Image

from .errors import AgentError, ConfigurationError, OutputError, make_error_info
from .execution import BudgetLedger, ExecutionPolicy
from .long_image_models import (
    LongImageOptions,
    LongImagePlan,
    LongImageRequest,
    LongImageResult,
    LongPlanningRecord,
    LongSlotAsset,
)
from .long_image_requirements import planning_fingerprint, validate_long_image_plan
from .models import MaterialAnalysis
from .run_store import RunStore, config_fingerprint, digest


def content_hash(value):
    return digest(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _invalid(message):
    raise AgentError(message, code="plan_invalid")


class LongImageRunStore(RunStore):
    # In-process records keep their original memory store alive. They cannot be
    # imported from JSON, nor restored after a process restart without a directory.
    _memory_records = {}

    def __init__(self, directory=None):
        super().__init__(directory)
        self._ledger = None

    def path(self, relative):
        value = str(relative)
        windows = PureWindowsPath(value)
        if (
            not value
            or windows.drive
            or windows.root
            or "\\" in value
            or PurePosixPath(value).is_absolute()
            or any(p in ("", ".", "..") for p in value.split("/"))
            or ":" in value
        ):
            raise OutputError("persisted path must be a safe root-relative path")
        return super().path(value)

    def write(self, relative, data):
        self.path(relative)  # Memory stores must enforce the same path contract.
        return super().write(relative, data)

    def read(self, ref):
        self.path(ref["path"])
        return super().read(ref)

    def json(self, name, value=None):
        self.path(name)
        return super().json(name, value)

    def initialize(self, request, materials, config, policy, *, planning_result=None):
        incoming = request.model_dump(mode="python")
        for item, material in zip(incoming["materials"], request.materials, strict=True):
            if material.source.data is not None:
                item["source"] = {"data": material.source.data}
        request = LongImageRequest.model_validate(incoming)
        materials = tuple(materials)
        hashes = self._material_hashes(request, materials)
        fingerprint = planning_fingerprint(request, hashes)
        if self.exists("run.json"):
            meta = self.load_metadata(config)
            if meta["request_fingerprint"] != fingerprint or meta["policy"] != policy.model_dump(
                mode="json"
            ):
                raise ConfigurationError("persisted request or policy cannot change on resume")
            ledger = self.open_ledger()
            if self.directory:
                ledger.close()
            return
        binding = None
        if planning_result is not None:
            if planning_result.status != "ready" or planning_result.plan is None:
                _invalid("only ready planning results can initialize generation")
            if planning_result.record is not None:
                binding = self.resolve_planning_record(
                    planning_result.record,
                    request=request,
                    plan=planning_result.plan,
                    analysis=planning_result.analysis,
                )
            validate_long_image_plan(
                request,
                planning_result.plan,
                material_hashes=hashes,
                analysis=planning_result.analysis,
            )
        refs = []
        payload = request.model_dump(mode="json")
        for material in materials:
            ref = self.write(
                f"inputs/{material.order:02d}-{material.material_id}.bin", material.data
            )
            refs.append(material.model_dump(mode="json") | ref)
        for item, ref in zip(payload["materials"], refs, strict=True):
            item["source"] = {"path": ref["path"]}
        # Initialize accounting before the run header. A crash never resets it.
        ledger = BudgetLedger(self.path("budget.sqlite"), policy)
        if self.directory:
            ledger.close()
        else:
            self._ledger = ledger
        self.json(
            "run.json",
            {
                "version": 2,
                "task_type": "long_image",
                "run_id": str(uuid4()),
                "request": payload,
                "request_hash": content_hash(payload),
                "request_fingerprint": fingerprint,
                "inputs": refs,
                "policy": policy.model_dump(mode="json"),
                "policy_fingerprint": content_hash(policy.model_dump(mode="json")),
                "config_fingerprint": config_fingerprint(config),
                "planning_record": binding.model_dump(mode="json") if binding else None,
                "planning_result": planning_result.model_dump(mode="json")
                if planning_result
                else None,
            },
        )

    @staticmethod
    def _material_hashes(request, materials):
        if (
            [m.material_id for m in materials] != [m.material_id for m in request.materials]
            or any(digest(m.data) != m.sha256 for m in materials)
            or len({m.order for m in materials}) != len(materials)
        ):
            raise OutputError("material bindings or hashes differ from request")
        return {m.material_id: m.sha256 for m in materials}

    def load_metadata(self, config=None):
        meta = self.json("run.json")
        if meta.get("version") != 2 or meta.get("task_type") != "long_image":
            raise AgentError("only version 2 long_image runs are supported")
        if config is not None and config_fingerprint(config) != meta["config_fingerprint"]:
            raise ConfigurationError("execution configuration changed")
        if (
            content_hash(meta["request"]) != meta["request_hash"]
            or content_hash(meta["policy"]) != meta["policy_fingerprint"]
        ):
            raise OutputError("run request or policy integrity validation failed")
        request = LongImageRequest.model_validate(meta["request"])
        refs = meta["inputs"]
        if [r["material_id"] for r in refs] != [m.material_id for m in request.materials]:
            raise OutputError("input bindings differ from persisted request")
        for material, ref in zip(request.materials, refs, strict=True):
            if material.source.path is None or material.source.path.as_posix() != ref["path"]:
                raise OutputError("input source does not match persisted binding")
            self.read(ref)
        if (
            planning_fingerprint(request, {r["material_id"]: r["sha256"] for r in refs})
            != meta["request_fingerprint"]
        ):
            raise OutputError("request fingerprint integrity validation failed")
        return meta

    def request(self):
        meta = self.load_metadata()
        payload = meta["request"]
        for material, ref in zip(payload["materials"], meta["inputs"], strict=True):
            # Memory requests bind bytes; disk requests never revisit original sources.
            material["source"] = (
                {"path": str(self.path(ref["path"]))}
                if self.directory
                else {"data": self.read(ref)}
            )
        return LongImageRequest.model_validate(payload)

    def open_ledger(self):
        """Disk callers close returned ledgers; the memory ledger is store-owned."""
        policy = ExecutionPolicy.model_validate(self.load_metadata()["policy"])
        if not self.directory:
            if self._ledger is None:
                raise ConfigurationError("memory budget ledger unavailable")
            return self._ledger
        return BudgetLedger(self.path("budget.sqlite"), policy, require_existing=True)

    @contextmanager
    def budget(self):
        """Uniform ledger ownership: use ``with store.budget() as ledger``.

        Disk connections are closed on exit. A memory ledger remains store-owned
        so that a returned planning record can still be verified in this process.
        """
        ledger = self.open_ledger()
        try:
            yield ledger
        finally:
            if self.directory:
                ledger.close()

    def close(self):
        """Release unregistered memory accounting; published records retain it.

        Registered memory planning capabilities have process lifetime. Close does
        not revoke a returned record. Disk ledgers are owned by ``budget()``.
        """
        if self._ledger is not None and self not in self._memory_records.values():
            self._ledger.close()
            self._ledger = None

    def _usage_snapshot(self, count=None):
        ledger = self.open_ledger()
        try:
            rows = ledger.db.execute(
                "SELECT kind, operation FROM reservations ORDER BY rowid"
            ).fetchall()
            if count is not None:
                if type(count) is not int or count < 0 or count > len(rows):
                    _invalid("planning record reservation snapshot is invalid")
                rows = rows[:count]
            usage = {"image_calls": 0, "vision_calls": 0}
            for kind, _ in rows:
                usage[kind + "_calls"] += 1
            return usage, {"count": len(rows), "hash": content_hash(rows)}
        finally:
            if self.directory:
                ledger.close()

    def save_planning_result(self, result, request, materials, policy):
        meta = self.load_metadata()
        hashes = self._material_hashes(request, tuple(materials))
        if (
            planning_fingerprint(request, hashes) != meta["request_fingerprint"]
            or policy.model_dump(mode="json") != meta["policy"]
        ):
            _invalid("planning request or policy differs from original run")
        if (
            result.status != "ready"
            or result.plan is None
            or any(issue.required for issue in result.issues)
        ):
            _invalid("only ready, non-blocked plans can be recorded")
        validate_long_image_plan(
            request, result.plan, material_hashes=hashes, analysis=result.analysis
        )
        usage, proof = self._usage_snapshot()
        rid = str(uuid4())
        relative = f"records/{rid}.json"
        selection = (
            result.plan.resolved_selection.model_dump(mode="json")
            if result.plan.resolved_selection
            else {}
        )
        record = LongPlanningRecord(
            record_id=rid,
            planning_id=result.planning_id,
            request_hash=meta["request_fingerprint"],
            plan_hash=content_hash(result.plan.model_dump(mode="json")),
            analysis_hash=(
                content_hash(result.analysis.model_dump(mode="json")) if result.analysis else None
            ),
            selection_summary=selection,
            usage=usage,
            local_record_ref=str(self.path(relative)) if self.directory else None,
            ledger_ref=str(self.path("budget.sqlite")) if self.directory else None,
        )
        self.json(
            relative,
            {
                "record": record.model_dump(mode="json"),
                "request": meta["request"],
                "material_hashes": hashes,
                "plan": result.plan.model_dump(mode="json"),
                "analysis": result.analysis.model_dump(mode="json") if result.analysis else None,
                "reservations": proof,
            },
        )
        if not self.directory:
            self._memory_records[rid] = self
        return record

    def load_planning_record(self, record_id, *, request, plan, analysis=None):
        try:
            meta = self.load_metadata()
            data = self.json(f"records/{record_id}.json")
            record = LongPlanningRecord.model_validate(data["record"])
            persisted_request = LongImageRequest.model_validate(data["request"])
            persisted_plan = LongImagePlan.model_validate(data["plan"])
            persisted_analysis = (
                MaterialAnalysis.model_validate(data["analysis"]) if data["analysis"] else None
            )
            hashes = {m.material_id: m.sha256 for m in self.materials()}
            validate_long_image_plan(
                persisted_request,
                persisted_plan,
                material_hashes=hashes,
                analysis=persisted_analysis,
            )
            validate_long_image_plan(request, plan, material_hashes=hashes, analysis=analysis)
            selection = (
                plan.resolved_selection.model_dump(mode="json") if (plan.resolved_selection) else {}
            )
            usage, proof = self._usage_snapshot(data["reservations"]["count"])
            if (
                record.record_id != record_id
                or record.request_hash != meta["request_fingerprint"]
                or record.request_hash != planning_fingerprint(persisted_request, hashes)
                or record.request_hash != planning_fingerprint(request, hashes)
                or record.plan_hash != content_hash(data["plan"])
                or record.plan_hash != content_hash(plan.model_dump(mode="json"))
                or record.analysis_hash
                != (content_hash(data["analysis"]) if data["analysis"] else None)
                or record.analysis_hash
                != (content_hash(analysis.model_dump(mode="json")) if analysis else None)
                or record.selection_summary != selection
                or data["material_hashes"] != hashes
                or proof != data["reservations"]
                or usage != record.usage
            ):
                _invalid("planning record content or usage does not match persisted binding")
            relative = f"records/{record_id}.json"
            if record.local_record_ref != (
                str(self.path(relative)) if self.directory else None
            ) or record.ledger_ref != (str(self.path("budget.sqlite")) if self.directory else None):
                _invalid("planning record locator differs from original store")
            return record
        except (KeyError, TypeError, ValueError, OutputError) as error:
            raise AgentError(
                "planning record missing, corrupt or unbound", code="plan_invalid"
            ) from error

    @classmethod
    def resolve_planning_record(cls, descriptor, *, request, plan, analysis=None):
        """Resolve a local Python descriptor; external DTO record IDs need an adapter.

        Disk locators name ``<run>/records/<record_id>.json``. Memory locators
        resolve only in this process to the original registered store.
        """
        if descriptor.local_record_ref:
            locator = Path(descriptor.local_record_ref)
            if (
                not locator.is_absolute()
                or locator.parent.name != "records"
                or locator.name != f"{descriptor.record_id}.json"
                or not locator.is_file()
            ):
                _invalid("planning record locator is not bound")
            store = cls(locator.parent.parent)
        else:
            store = cls._memory_records.get(descriptor.record_id)
            if store is None:
                _invalid("memory planning record is unavailable or unbound")
        verified = store.load_planning_record(
            descriptor.record_id, request=request, plan=plan, analysis=analysis
        )
        if verified != descriptor:
            _invalid("planning record descriptor differs from original binding")
        return verified

    @staticmethod
    def _artifacts(result):
        return [*result.sections, *([result.page] if result.page else []), *result.slices]

    @staticmethod
    def _stale(result):
        result.status = "failed"
        for artifact in LongImageRunStore._artifacts(result):
            artifact.availability = "stale" if artifact.file_path or artifact.image else "absent"
            artifact.status = "failed"
        return result

    def _current_manifest(self):
        if not self.exists("delivery-current.json"):
            return None
        pointer = self.json("delivery-current.json")
        manifest = json.loads(self.read(pointer))
        if manifest["result"]["delivery_revision"] != pointer["revision"]:
            raise OutputError("delivery pointer revision does not match manifest")
        return manifest

    def _saved_result(self):
        manifest = self._current_manifest()
        state = self.json("state.json") if self.exists("state.json") else None
        if manifest:
            result = LongImageResult.model_validate(manifest["result"])
            # A later graph checkpoint may include new candidates. Never hide
            # those from integrity validation simply because the page is older.
            if state and state["result"].get("delivery_revision", 0) >= result.delivery_revision:
                result.image_assets = [
                    LongSlotAsset.model_validate(a) for a in state["result"]["image_assets"]
                ]
            return result
        if state:
            return LongImageResult.model_validate(state["result"])
        if self.exists("result.json"):
            return LongImageResult.model_validate(self.json("result.json"))
        meta = self.load_metadata()
        planning = meta.get("planning_result")
        return LongImageResult(
            status="failed",
            run_id=meta["run_id"],
            run_dir=str(self.directory) if self.directory else None,
            plan=planning.get("plan") if planning else None,
        )

    def save_slot_assets(self, assets):
        """Persist slot metadata only after candidate/raw bytes are safely written."""
        rows = []
        for asset in assets:
            self._hydrate_asset(asset.model_copy(deep=True))
            rows.append(asset.model_dump(mode="json"))
        self.json("slots.json", rows)

    def save_checkpoint(self, result, **state_updates):
        """Mirror a graph checkpoint without resetting phase/runtime or accounting.

        The runtime may also use inherited ``json('state.json', state)``. Store
        commits preserve all existing state keys and replace only ``result`` and
        ``delivery_intent``; LangGraph's own SQLite checkpoint remains its owner.
        """
        self.save_slot_assets(result.image_assets)
        state = self.json("state.json") if self.exists("state.json") else {}
        state.update(state_updates)
        state["result"] = result.model_dump(mode="json")
        self.json("state.json", state)

    def pending_delivery_revision(self):
        if not self.exists("delivery-intent.json"):
            return None
        intent = self.json("delivery-intent.json")
        if type(intent.get("revision")) is not int or intent["revision"] < 1:
            raise OutputError("delivery intent revision is corrupt")
        LongImageOptions.model_validate(intent["options"])
        if not isinstance(intent.get("choices"), dict) or not isinstance(
            intent.get("statuses"), dict
        ):
            raise OutputError("delivery intent choices or statuses are corrupt")
        return intent

    def begin_delivery_revision(self, *, choices, statuses, options):
        """Write intent first; even a crash before stale.json cannot expose old output.

        ``choices`` maps slot IDs to candidate IDs (None for source slots).
        ``statuses`` maps slot IDs to a status string or a dict containing status,
        source_status, audit, error_info and/or candidates (including acceptance
        reason/time). Pass complete metadata here to preserve it before slot save.
        An identical pending intent is idempotent; a different one must be finished.
        """
        options = LongImageOptions.model_validate(
            options.model_dump(mode="python") if isinstance(options, LongImageOptions) else options
        ).model_dump(mode="json")
        target = {"choices": choices, "statuses": statuses, "options": options}
        # Normalize/copy caller-owned dictionaries before durable writes.
        target = json.loads(json.dumps(target, ensure_ascii=False))
        pending = self.pending_delivery_revision()
        if pending:
            if any(pending[k] != target[k] for k in target):
                raise OutputError("finish the pending delivery revision before changing its intent")
            return pending["revision"]
        old = self._saved_result()
        revision = old.delivery_revision + 1
        current = self._current_manifest()
        # Compare with the last delivered metadata, not an audit checkpoint that
        # may already carry the new status but has not yet revised the delivery.
        base_assets = current["result"]["image_assets"] if current else []
        intent = target | {"revision": revision, "base_assets": base_assets}
        # Validate target metadata while leaving input/candidate integrity checks
        # until commit; a missing candidate must still invalidate old delivery.
        self._apply_intent(old.model_copy(deep=True), intent)
        self.json("delivery-intent.json", intent)
        self.json("result.json", self._stale(old).model_dump(mode="json"))
        return revision

    def _apply_intent(self, result, intent):
        base = {a["slot_id"]: a for a in intent.get("base_assets", [])}
        allowed = {"status", "source_status", "audit", "error_info", "candidates"}
        for asset in result.image_assets:
            old_id = asset.selected_candidate_id
            update = intent["statuses"].get(asset.slot_id, {})
            if isinstance(update, str):
                update = {"status" if asset.source == "generate" else "source_status": update}
            if not isinstance(update, dict) or not set(update) <= allowed:
                raise OutputError("delivery status metadata contains unsupported fields")
            data = asset.model_dump(mode="python") | update
            if asset.slot_id in intent["choices"]:
                data["selected_candidate_id"] = intent["choices"][asset.slot_id]
            changed_choice = data["selected_candidate_id"] != old_id
            if changed_choice:
                data.update(file_path=None, sha256=None)
            updated = LongSlotAsset.model_validate(data)
            updated.image = None if changed_choice else asset.image
            previous = base.get(asset.slot_id)
            relevant = (
                "selected_candidate_id",
                "status",
                "source_status",
                "audit",
                "sha256",
                "candidates",
            )
            changed = previous is None or any(previous.get(k) != data.get(k) for k in relevant)
            updated.asset_revision = (previous or {}).get("asset_revision", 0) + int(changed)
            result.image_assets[result.image_assets.index(asset)] = updated
        return result

    def _hydrate_asset(self, asset):
        # Publish bytes only after the entire asset validates. Rehydrating a
        # previously valid object must not retain its cache on any failure.
        asset.image = None
        asset._candidate_payloads = {}
        candidates = {}
        payloads = {}
        for candidate in asset.candidates:
            cid = candidate.get("candidate_id")
            if not cid or cid in candidates:
                raise OutputError("candidate IDs are missing or duplicated")
            data = self.read({"path": candidate["file_path"], "sha256": candidate["sha256"]})
            payloads[cid] = (candidate["file_path"], candidate["sha256"], data)
            candidates[cid] = candidate
        if (
            asset.source == "generate"
            and asset.status in ("succeeded", "accepted")
            and not asset.selected_candidate_id
        ):
            raise OutputError("successful generated asset requires a selected candidate")
        if asset.source == "generate" and asset.selected_candidate_id:
            selected = candidates.get(asset.selected_candidate_id)
            if selected is None:
                raise OutputError("selected candidate missing from candidate history")
            if (
                asset.file_path is not None
                and asset.file_path != selected["file_path"]
                or asset.sha256 is not None
                and asset.sha256 != selected["sha256"]
            ):
                raise OutputError("selected asset differs from selected candidate")
            asset.file_path, asset.sha256 = selected["file_path"], selected["sha256"]
        image = None
        if asset.file_path or asset.sha256:
            if not asset.file_path or not asset.sha256:
                raise OutputError("selected asset has an incomplete file binding")
            image = self.read({"path": asset.file_path, "sha256": asset.sha256})
            if asset.source == "source" and not any(
                ref["path"] == asset.file_path and ref["sha256"] == asset.sha256
                for ref in self.load_metadata()["inputs"]
            ):
                raise OutputError("source asset does not match a persisted material")
        elif asset.source == "source" and asset.source_status == "available":
            raise OutputError("source asset marked available without persisted bytes")
        elif asset.source == "generate" and asset.status in ("succeeded", "accepted"):
            raise OutputError("successful generated asset has no selected candidate")
        asset.image = image
        asset._candidate_payloads = payloads
        return asset

    def validate_inputs_and_candidates(self):
        self.load_metadata()
        result = self._saved_result()
        if result.delivery_revision and self.exists("delivery-current.json"):
            if (
                self.json(f"deliveries/{result.delivery_revision}/checks.json")
                != result.page_checks
            ):
                raise OutputError("delivery checks differ from manifest")
        intent = self.pending_delivery_revision()
        if self.exists("slots.json"):
            for row in self.json("slots.json"):
                self._hydrate_asset(LongSlotAsset.model_validate(row))
        if intent:
            self._apply_intent(result, intent)
        for asset in result.image_assets:
            self._hydrate_asset(asset)
        self._validate_source_bindings(result)
        for artifact in self._artifacts(result):
            if artifact.file_path:
                data = self.read({"path": artifact.file_path, "sha256": artifact.sha256})
                self._validate_png(data, artifact)
        if self.exists("operations.json"):
            for operation in self.json("operations.json").values():
                if operation.get("raw"):
                    self.read(operation["raw"])

    @staticmethod
    def _validate_png(data, artifact):
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.format != "PNG" or image.size != (artifact.width, artifact.height):
                    raise OutputError("derived PNG dimensions or format differ from metadata")
                image.verify()
        except (OSError, ValueError) as error:
            raise OutputError("derived PNG is corrupt") from error

    def hydrate_result(self):
        """Hydrate verified bytes; integrity failures remove current qualification."""
        result = self._saved_result()
        intent = self.pending_delivery_revision()
        if intent:
            if self.exists("slots.json"):
                result.image_assets = [
                    LongSlotAsset.model_validate(row) for row in self.json("slots.json")
                ]
            self._apply_intent(result, intent)
            self._stale(result)
        try:
            self.validate_inputs_and_candidates()
            for asset in result.image_assets:
                self._hydrate_asset(asset)
            for artifact in self._artifacts(result):
                if artifact.file_path:
                    artifact.image = self.read(
                        {"path": artifact.file_path, "sha256": artifact.sha256}
                    )
            return result
        except (OutputError, KeyError, ValueError) as error:
            self._stale(result)
            result.output_errors.append(
                make_error_info(
                    error
                    if isinstance(error, OutputError)
                    else OutputError("persisted assets corrupt")
                )
            )
            # Never return unverified bytes from a corrupt snapshot.
            for asset in result.image_assets:
                asset.image = None
                asset._candidate_payloads = {}
            for artifact in self._artifacts(result):
                artifact.image = None
            return result

    def commit_delivery_revision(self, result, rendered=None):
        """Complete the pending local revision; re-entry is safe after every write.

        Call with pixels from the local renderer, or an already hydrated result to
        reuse identical pixels. No network clients are constructed. The manifest
        binds all files before the pointer changes; state/result follow the pointer
        and the intent is removed last. Caller holds the run lock and supplies
        freshly aggregated section statuses, including when reusing pixels.
        """
        intent = self.pending_delivery_revision()
        if intent is None:
            raise OutputError("begin a delivery revision before committing")
        self.load_metadata()
        result = result.model_copy(deep=True)
        self._apply_intent(result, intent)
        for asset in result.image_assets:
            self._hydrate_asset(asset)
        previous = self._current_manifest()
        if rendered is None and result.status != "failed" and not result.output_errors:
            if previous is None or not self._same_pixel_inputs(result, intent, previous):
                raise OutputError("changed layout or selected pixels require local rendering")
        self.save_slot_assets(result.image_assets)
        revision = intent["revision"]
        meta = self.load_metadata()
        result.run_id = meta["run_id"]
        result.run_dir = str(self.directory) if self.directory else None
        result.delivery_revision = revision
        if rendered is not None:
            section_statuses = {a.artifact_id: a.status for a in result.sections}
            result.sections = [a.model_copy(deep=True) for a in rendered.sections]
            # Rendering describes pixels; runtime aggregation describes each
            # semantic section's own dependencies, independently of page status.
            for section in result.sections:
                if section.artifact_id in section_statuses:
                    section.status = section_statuses[section.artifact_id]
            result.page = rendered.page.model_copy(deep=True)
            result.slices = [a.model_copy(deep=True) for a in rendered.slices]
            result.page_checks = rendered.checks
        failed = result.status == "failed" or bool(result.output_errors)
        if not failed:
            self._validate_complete_delivery(result, intent)
        for artifact in self._artifacts(result):
            if failed:
                artifact.status = "failed"
            elif artifact.kind != "section":
                artifact.status = result.status
            artifact.delivery_revision = revision
            if failed:
                artifact.availability = "stale" if artifact.file_path else "absent"
                continue
            if artifact.image is None:
                data = self.read({"path": artifact.file_path, "sha256": artifact.sha256})
            else:
                data = artifact.image
                if artifact.sha256 and digest(data) != artifact.sha256:
                    raise OutputError("derived bytes differ from their hash")
            self._validate_png(data, artifact)
            ref = self.write(
                f"deliveries/{revision}/{artifact.kind}/{artifact.artifact_id}.png", data
            )
            artifact.file_path, artifact.sha256 = ref["path"], ref["sha256"]
            artifact.availability = "current"
        if failed:
            result.status = "failed"
        self.json(f"deliveries/{revision}/checks.json", result.page_checks)
        manifest = {
            "result": result.model_dump(mode="json"),
            "options": intent["options"],
            "render_fingerprint": (
                rendered.render_fingerprint
                if rendered
                else (previous or {}).get("render_fingerprint")
            ),
            "layout": rendered.layout if rendered else (previous or {}).get("layout", {}),
        }
        name = f"deliveries/{revision}/manifest.json"
        self.json(name, manifest)
        self._publish_delivery(manifest, name)

    @staticmethod
    def _same_pixel_inputs(result, intent, manifest):
        old = manifest["result"]
        return (
            intent["options"] == manifest["options"]
            and result.plan.model_dump(mode="json") == old["plan"]
            and {a.slot_id: a.sha256 for a in result.image_assets}
            == {a["slot_id"]: a["sha256"] for a in old["image_assets"]}
        )

    def _validate_complete_delivery(self, result, intent):
        if result.plan is None or result.page is None or not result.slices:
            raise OutputError("delivery is missing its plan, page or required slices")
        if [a.artifact_id for a in result.sections] != [s.section_id for s in result.plan.sections]:
            raise OutputError("delivery is missing or reorders required sections")
        assets = {a.slot_id: a for a in result.image_assets}
        if (
            len(assets) != len(result.image_assets)
            or set(assets) != {s.slot_id for s in result.plan.image_slots}
            or not set(intent["choices"]) <= assets.keys()
            or not set(intent["statuses"]) <= assets.keys()
        ):
            raise OutputError("delivery slot assets do not match plan and intent")
        if (
            result.page.kind != "page"
            or any(a.kind != "section" for a in result.sections)
            or any(a.kind != "slice" for a in result.slices)
            or len({a.artifact_id for a in result.slices}) != len(result.slices)
        ):
            raise OutputError("delivery artifact identity or kind is inconsistent")
        self._validate_source_bindings(result)

    def _validate_source_bindings(self, result):
        if result.plan is None:
            return
        slots = {s.slot_id: s for s in result.plan.image_slots}
        inputs = {r["material_id"]: r for r in self.load_metadata()["inputs"]}
        for asset in result.image_assets:
            slot = slots.get(asset.slot_id)
            if slot is None or slot.source != asset.source:
                raise OutputError("asset source differs from its planned slot")
            if asset.source == "source" and asset.file_path:
                ref = inputs.get(slot.material_ids[0])
                if ref is None or (asset.file_path, asset.sha256) != (ref["path"], ref["sha256"]):
                    raise OutputError("source slot does not match its planned material")

    def _publish_delivery(self, manifest, name):
        result = manifest["result"]
        encoded = self.path(name).read_bytes() if self.directory else self.memory[name]
        self.json(
            "delivery-current.json",
            {"path": name, "sha256": digest(encoded), "revision": result["delivery_revision"]},
        )
        state = self.json("state.json") if self.exists("state.json") else {}
        state.update(result=result, delivery_intent=None)
        self.json("state.json", state)
        self.json("result.json", result)
        self.remove("delivery-intent.json")

    def recover_delivery_revision(self):
        """Publish a fully saved pending manifest without rendering or model calls.

        Return False if local rendering/commit is still needed (no complete
        manifest); True when no intent remains. Earlier interruptions expose the
        intent via ``pending_delivery_revision`` for the runtime's local renderer.
        """
        intent = self.pending_delivery_revision()
        if intent is None:
            return True
        self.load_metadata()
        name = f"deliveries/{intent['revision']}/manifest.json"
        if not self.exists(name):
            return False
        manifest = self.json(name)
        result = LongImageResult.model_validate(manifest["result"])
        if (
            result.delivery_revision != intent["revision"]
            or manifest["options"] != intent["options"]
        ):
            raise OutputError("pending delivery manifest differs from intent")
        updated = self._apply_intent(result.model_copy(deep=True), intent)
        if updated.model_dump(mode="json") != result.model_dump(mode="json"):
            raise OutputError("pending delivery slot metadata differs from intent")
        if result.status != "failed" and not result.output_errors:
            self._validate_complete_delivery(result, intent)
        for asset in result.image_assets:
            self._hydrate_asset(asset)
        for artifact in self._artifacts(result):
            if artifact.file_path:
                data = self.read({"path": artifact.file_path, "sha256": artifact.sha256})
                self._validate_png(data, artifact)
        if self.json(f"deliveries/{intent['revision']}/checks.json") != result.page_checks:
            raise OutputError("pending delivery checks differ from manifest")
        self._publish_delivery(manifest, name)
        return True
