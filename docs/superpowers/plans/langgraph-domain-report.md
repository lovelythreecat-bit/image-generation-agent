# LangGraph domain implementation report

Task 1 implementation is ready for independent review. No commits, live API requests, or credential reads were performed.

## Owned changes

- `src/image_agent/models.py`: Fact verifiability and evidence provenance; effective semantic classification; Requirement verifiability; Asset candidate history and stop reason; result v2 status/run/usage fields with v1 reading retained.
- `src/image_agent/selection.py`: claim-only facts/elements/constraints compile as not_applicable with informational `unverifiable_claim` issues; functional claims do not become required reference facts. Unknown required appearance blocks selection with `missing_evidence`. Physical product artwork classified as a subject style is retained under catalog backgrounds. Subject identity stays hard even when all fact claims are functional.
- `src/image_agent/quality.py`: preserve audit reasons in failed reports; ignore garment-fusion metrics when choosing repairs for product-only targets.
- `src/image_agent/prompt.py`: `build_repair_prompt(request, target, context, plan, quality, *, previous_prompt="")`; specific failed check IDs, scores, reasons, corrective actions and passing preservation requirements; original sources authoritative, previous candidate diagnostic only. Always rebuild current structured bindings; never reuse stale previous structured prompts. Feature shots preserve physical labels/artwork.
- `src/image_agent/vision.py`: discovery explicitly classifies every fact and evidence source, with missing/unknown fresh provenance corrected inside `_call`; instructions distinguish printed product labels/artwork from external promotional overlays and nonvisual claims. Focused identity check IDs are validated inside the same corrective protocol retry.
- `src/image_agent/compliance.py`: remove all CN/VN/PH numeric substitutions; preserve physical labels/trademarks/numbers/artwork; remove peripheral promotional overlays.
- Tests: new `tests/test_repair_semantics.py`; extended `tests/test_vision_resilience.py`, `tests/test_compliance.py`; narrow classification fields added to `tests/fakes.py`.

Existing uncommitted edits in owned files were preserved. The git diff includes those earlier edits and therefore is broader than the changes above.

## Interfaces and rulings

- `Evidence.source_type`: `visual | product_label | promotional_text | inference | unknown`; default `unknown` only for legacy analyses.
- `Fact.verifiability` and `Requirement.verifiability`: `visible_appearance | functional_claim | unverified`; default `visible_appearance` preserves historical required appearance semantics. `Fact.effective_verifiability` converts appearance supported only by promotional/inference provenance to unverified.
- Ruling agreed with coordinator: do not weaken all historical unknown facts. Legacy stored analyses keep their required appearance behavior; fresh discovery must explicitly classify provenance. Cost: old functional claims need fresh analysis to gain the corrected semantic exemption.
- Explicit functional claims are informational, not visual proof requirements. Unverified genuinely required appearance requires new evidence; it is not silently waived.
- Asset statuses add `pending_audit`, `audit_error`, `quality_failed`, `needs_input`, `budget_exhausted`, `accepted`, `generation_uncertain`; existing succeeded/failed remain. `candidates: list[dict]`, `stop_reason: str | None`.
- CreationResult accepts schema versions 1.0/2.0 and defaults to 2.0; same statuses plus partial; `run_dir: str | None`, `usage: dict[str, int]`.
- Analysis/request schemas stay 1.0. GenerationAttempt stage names unchanged.
- `AssetElementPlan.issues` with code `unverifiable_claim` is informational and must not block generation. Genuine missing evidence is handled by selection.

## Verification evidence

Tests were written and run red before the corresponding changes: v2/provenance fields (8 failures), claim/quality/repair/number/focused-audit behavior (11 failures initially; a Pydantic fixture mutation was corrected), catalog native artwork (2 failures), fresh provenance protocol (3 failures), stale repair reference bindings (1 failure).

Final targeted command:

`.venv/Scripts/python.exe -m pytest tests/test_repair_semantics.py tests/test_compliance.py tests/test_selection.py tests/test_prompt.py tests/test_quality.py tests/test_vision.py tests/test_vision_resilience.py tests/test_evidence_protocol.py tests/test_review_regressions.py::test_staged_final_plan_matches_actual_material_references -q --tb=short`

Result: **72 passed in 3.68s**. Lint and format checks of all 10 changed domain/test files pass.

One full shared-tree suite snapshot returned **289 passed, 18 failed in 22.62s** during engine/entrypoint integration. Full output is in ignored `.superpowers/langgraph-domain-suite.txt`. The stale reference-binding failure below was subsequently fixed and verified. The suite has not been rerun while the other agents are updating their owned modules/tests.

Remaining snapshot failures communicated to their owners:

- `tests/test_acceptance.py::test_group_error_does_not_downgrade_assets` — old succeeded aggregate vs partial.
- `tests/test_acceptance.py::test_quality_failure_partial_keeps_candidate_outside_approved_outputs` — old null file path vs retained candidate path.
- `tests/test_configured_pipeline.py::test_full_pipeline_routes_same_model_by_alias[valid-base64]`
- `tests/test_configured_pipeline.py::test_full_pipeline_routes_same_model_by_alias[valid-url]`
- `tests/test_configured_pipeline.py::test_full_pipeline_routes_same_model_by_alias[repair-base64]`
- `tests/test_configured_pipeline.py::test_full_pipeline_routes_same_model_by_alias[repair-url]` — old successful output layout.
- `tests/test_configured_pipeline.py::test_full_pipeline_routes_same_model_by_alias[fail-base64]`
- `tests/test_configured_pipeline.py::test_full_pipeline_routes_same_model_by_alias[fail-url]` — old failed vs audit_error state.
- `tests/test_desktop_ui.py::test_desktop_generates_and_saves_through_real_pipeline` — old output path.
- `tests/test_live_smoke.py::test_repeated_command_without_id_saves_both_runs[smoke]`
- `tests/test_live_smoke.py::test_repeated_command_without_id_saves_both_runs[cli]` — old manifest glob.
- `tests/test_output.py::test_original_jpeg_relative_manifest_and_exclusive_directory` — old output path.
- `tests/test_pipeline.py::test_identity_and_intent_repair_modes[scores0-3-staged]` — last reference now previous candidate instead of scene stage.
- `tests/test_pipeline.py::test_quality_service_failure_no_creative_retry` — old failed vs audit_error state.
- `tests/test_pipeline.py::test_audit_protocol_failure_preserves_candidate_and_saved_manifest` — old failed vs audit_error state.
- `tests/test_pipeline.py::test_ambiguous_no_generation_and_cancel_propagates` — LangGraph wraps CancelledError as NodeCancelledError; owner notified of real cancellation compatibility issue.
- `tests/test_public_api.py::test_public_cancel_generation_leaves_no_files` — old empty cancelled folder expectation conflicts with preserved inputs/checkpoint.
- Fixed after snapshot: `tests/test_review_regressions.py::test_staged_final_plan_matches_actual_material_references` — obsolete structured bindings in previous prompt; now fresh bindings, covered by added regression.

No remaining owned-file test failures are known. Build, whole-branch review and real service acceptance remain coordinator work.
