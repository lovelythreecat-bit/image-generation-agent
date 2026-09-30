# Independent LangGraph refactor review

Date: 2026-09-30. Reviewer: independent_review. Final scoped verdict: no remaining actionable findings in the reviewed source after the six corrections below. No open billing/recovery P1 from this review.

## Scope and authority

Reviewed the confirmed design and implementation plan (including Global Constraints and Review Focus), implementation reports, domain/entrypoint diff packages against `.superpowers/langgraph-baseline-20260930`, and current domain, graph, storage, transport, CLI, DTO and desktop source. The working-tree baseline, rather than git HEAD, was used to separate this refactor from pre-existing user changes.

Product source was read-only for this reviewer. The coordinator authorized this report and ignored `.superpowers/langgraph-review/` reproduction artifacts. No credentials, real APIs, commits, or historical user outputs were accessed. Existing passing whole-project suites were not repeated.

## Findings and final disposition

All locations below refer to the final reviewed source; reproduction history records the behavior before correction.

1. **P1 — Missing budget ledger reset cumulative usage. Resolved.** `src/image_agent/execution.py:25`, `src/image_agent/graph_runtime.py:164`. An audit consumed the sole allowed vision submission and failed. Removing that temporary run's budget.sqlite allowed resume to submit a second audit while reporting vision_calls=1. Resume now requires an existing ledger, validates its two counters and ceilings against reservation receipts, and rejects missing, empty, incomplete or lowered counters. An initialization marker also prevents silently recreating a missing shared ledger. Independent replay now raises ConfigurationError with exactly one audit submission.

2. **P2 — Returning optional reference crashed the second repair. Resolved.** `src/image_agent/selection.py:657`. With capacity two, m1 identity plus m2 optional style, an identity failure caused scene/fusion generation to drop m2 from the active binding subset. A subsequent strict repair selected m2 again but indexed the prior subset and raised KeyError('m2') after three generations. Bindings now derive from frozen source analysis and actual selected references. The same independent sequence finishes succeeded with four generations and correct source references.

3. **P2 — Functional-only claims still blocked detail closeups. Resolved.** `src/image_agent/selection.py:289`, `src/image_agent/selection.py:362`. A required advertised benefit was correctly not_applicable in scene/feature requirements but remained in the pre-compilation closeup feasibility set, causing requirement_conflict. Functional-only facts/elements are now excluded from visual focus, required-subject and crop feasibility calculations. The independent scene/feature/closeup compilation succeeds with the functional fact and element not_applicable; actual visible detail remains the closeup focus.

4. **P2 — Equal scores discarded new actionable repair evidence. Resolved.** `src/image_agent/graph_nodes.py:439`, `src/image_agent/quality.py:153`, `src/image_agent/models.py:633`. Two output_intent scores of 60 stopped after one repair although the second audit explicitly said the angle was fixed and the crop must widen. The signature now includes normalized model reason and failed-check evidence. The independent sequence uses the remaining permitted repair and succeeds with three generations. The raw audit reason is retained separately from the synthesized summary.

5. **P2 — Uncertain paid generation advertised automatic retry. Resolved; originally found by coordinator.** `src/image_agent/errors.py:63`. generation_uncertain is now retryable=False, while ordinary audit transport failures remain retryable=True. The specific regression test passes and the source condition was independently inspected.

6. **P1 — Missing or empty submission journal replayed an uncertain image call. Resolved.** `src/image_agent/run_store.py:186`, `src/image_agent/execution.py:141`, `src/image_agent/graph_runtime.py:164`. After a counted image submission was cancelled before saving its response, deleting operations.json or replacing it with {} let resume submit the same generation again. Image reservation receipts now retain operation IDs; resume cross-checks journal submission counts/status against those receipts before invoking the graph. Both independent corruption variants now raise non-retryable generation_uncertain with exactly one generator call.

## Fresh independent verification

Ran once after owners signalled all fixes ready:

- `.venv/Scripts/python.exe -X utf8 -c "import runpy; runpy.run_path('.superpowers/langgraph-review/independent_repros.py', run_name='__main__')"` — exit 0. Binding sequence succeeded (4 calls); missing ledger refused before another audit; all three functional-claim shots compiled; changed-strategy sequence succeeded (3 calls); missing and empty-object journals refused with one generation call each.
- `.venv/Scripts/python.exe -m pytest -q tests/test_graph_recovery.py -k 'damaged_budget or shared_ledger_cannot or same_scores or uncertain_generation_metadata or uncertain_submission_cannot'` — **9 passed, 13 deselected in 3.34s**.
- Inspected the final implementations for the same six changes after those runs. No broader speculative fault expansion was performed after the coordinator bounded final review scope.

Reproduction source: `.superpowers/langgraph-review/independent_repros.py`. Before-fix observations: `independent-baseline-results.txt`. Final output: `independent-final-results.txt` in the same ignored directory.

## Spec compliance verdict

Pass for the reviewed implementation boundaries. Production execution uses actual StateGraph nodes; generation and audit are separate; atomic phase state and SQLite checkpoints are saved; inputs and candidates use hashed file references rather than serialized image bytes. Generation submission uncertainty is distinct from quality failure, and resume of an already saved candidate follows audit without generating again. Persistent actual-POST budgets include retries and share the configured external ledger. Source references remain authoritative during repair, functional claims do not require invented visible mechanisms, required appearance remains enforced, and product-native graphics/numbers are preserved by prompt/compliance rules.

Candidates, approved outputs and manual acceptance retain distinct states. Manual acceptance records an explicit reason and preserves failed audit evidence; selected older candidates remain stable during hydration and reopening. Detail group failures prevent aggregate succeeded. Public result DTOs whitelist candidate metadata and exclude local storage paths; only succeeded image bytes enter automatic-success blobs. Desktop progress crosses the queue and Tk updates occur in the main thread; task changes clear stale candidate bindings and old v1 manifests remain viewable.

## Code quality verdict and limits

Approve for coordinator integration and the separately budgeted live acceptance. No remaining actionable correctness finding was found within the reviewed and re-reviewed scope. Module boundaries are understandable, lower-level transport/image/evidence code is reused, and the replaced production asset loop is not retained as a competing fallback.

This is not a claim that fake-provider tests prove real model fidelity, aesthetic quality, prompt compliance, or exactly-once provider charging. Recovery refuses uncertain generation instead of assuming provider idempotency. Whole-project tests, clean installation/build, live provider availability and the explicitly authorized real-image regression remain coordinator acceptance evidence; this reviewer did not rerun or claim those results. The review verifies detected accidental corruption boundaries, not tamper-proof storage against coordinated rewriting of every local record.

## Follow-up: live-discovered discovery correction boundary

After the coordinator's first real acceptance attempt exposed an invalid source_quote that bypassed protocol correction, performed the explicitly requested narrow follow-up review only. No real call, credential access, or full-suite rerun was performed by this reviewer.

**Verdict: approve; no actionable finding in this follow-up patch.** Reviewed `vision.py` discovery instructions and callback, the strict `MaterialAnalysis.validate_intent_source` implementation, the permanent-invalid-response fixture update, and the new five offline cases. The exact substring validator remains unchanged. For source=brief it still checks only creative_brief; for source=hint it still checks the specified material's subject_hint or individual element_hints. The new instructions explain these same boundaries and forbid paraphrasing, joining separate hints, or substituting product name/category/OCR.

Material ID/order validation and source_quote validation now execute inside `validate_discovery`, passed to the existing bounded `_call` correction loop. ProviderError causes the existing single corrective request with the same images; a second invalid response still fails with analyze_materials and attempt=2/2 context. Source SHA binding happens only after callback validation succeeds. No quality threshold, provenance requirement, or source validation was relaxed, and requests still traverse the budgeted transport boundary.

Fresh independent verification: `.venv/Scripts/python.exe -m pytest -q tests/test_vision_resilience.py -k 'discovery_source_quote or discovery_material_ids'` -> **5 passed, 11 deselected in 0.90s**. These cover brief/hint correction success, brief/hint persistent invalidity, material-ID correction, unchanged image payloads, and actual mock-HTTP vision usage of two submissions. This confirms protocol behavior and accounting offline; it does not claim a later real provider response will satisfy the protocol.
