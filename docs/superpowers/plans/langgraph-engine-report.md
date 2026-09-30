# LangGraph engine implementation report

Scope: Task 2 of 2026-09-30-langgraph-refactor.md. Existing transport sanitization, remote image download safeguards, output atomic writes and business preparation behavior were retained. No credentials were read; no live requests or commits were made.

## Public interfaces

- `pipeline.create_images(request, config=None, *, analysis=None, policy=None, on_progress=None)` async.
- `pipeline.resume_images(run_dir, config=None, *, on_progress=None)` async.
- `pipeline.accept_candidate(run_dir, asset_id, candidate_id, *, reason)` async.
- Existing `run_pipeline(request, config, *, dependencies, analysis=None)` accepts optional policy, on_progress, store.
- Internal test recovery API: `graph_runtime.resume_pipeline(run_dir, config, *, dependencies, on_progress=None)`.
- `ExecutionPolicy` defaults: max_quality_repairs=2, max_audit_retries=2, max_image_calls=100, max_vision_calls=200.
- Coordinator clarified that quality limits remain configurable: max_quality_repairs range 0..10; default is 2. Policy is persisted and immutable when reopening its ledger.
- Optional shared cumulative accounting: `ExecutionPolicy(shared_ledger_path=Path(...), shared_max_image_calls=10, shared_max_vision_calls=30)`; every run using that path shares the limits. Actual POSTs are counted immediately before transport submission, including HTTP retries and vision format-correction requests. Standalone analyze_materials has no policy parameter; callers needing its accounting can use `call_scope(ledger, 'vision')` around it.
- Progress callback receives stage, asset_id, attempt, message, run_dir and may be sync/async. Stage names: prepare, prepare_asset, generate, save_candidate, audit, repair, finish_asset, group_audit, finish.

## Graph and durability

Real StateGraph nodes independently run preparation, asset preparation, generation, candidate persistence, audit, repair, asset completion, group audit, finalization. Staged repairs traverse generation/persistence separately for scene and fusion. No old production asset loop remains.

With output_dir, run.json contains copied input file references/hashes, sanitized analysis and request, policy, and a non-secret execution configuration fingerprint. state.json is atomically updated after each node; LangGraph AsyncSqliteSaver independently checkpoints each stage into checkpoints.sqlite. Recovery dispatches the durable phase, consulting the operation journal before any generation. Runtime clients, configuration credentials, and image bytes are not in graph state/checkpoints. LangSmith tracing is explicitly disabled.

Generation intent is recorded before calling a generator; actual submissions record submitted status at the transport boundary. Returned bytes are synchronously atomically staged before any subsequent await. If interrupted without staged bytes, recovery reports generation_uncertain and does not re-submit. A timeout during an image POST also reports generation_uncertain immediately. Explicit retriable HTTP responses retain the bounded retry behavior and consume reservations.

OS locks exclude overlapping run/recovery/accept operations and release on process death. Copied inputs and candidate hashes are verified before resume. Routing/model/reference-capacity changes are rejected; secret rotation and timeout/proxy changes are permitted. Runtime execution config is copied.

Saved candidates are audited before any next generation. Audit protocol/transport errors receive their own bounded retries; permanent HTTP authorization errors do not retry. Terminal quality_failed/succeeded/accepted/generation_uncertain runs do not automatically regenerate on resume. Completed audit_error runs re-audit the same candidate. Cancellation propagates as asyncio.CancelledError while durable progress remains.

## Artifacts and acceptance

Failed/pending candidates: candidates/<asset_id>/<candidate_id>.<ext>. Successful images are promoted to approved/<platform>/<output_type>/<variant-or-default>.<ext>. Manually accepted images are promoted to accepted/<asset_id>/<candidate_id>.<ext>. Promotion writes the destination, persists metadata/manifest, then removes the source. Every candidate retains independent metadata, quality/error audit history, image hash, index and repair prompt. Scene intermediates are retained with status stage and cannot be accepted as final output.

Acceptance requires nonempty reason and terminated workflow, preserves failed quality reports, records timestamp/reason, and marks exactly one candidate selected. Accepting an older candidate remains stable on future reads/resume. Candidate.selected marks the chosen preview. Acceptance never makes status succeeded. Memory-only runs retain nonserialized private candidate payloads so later save_result exports full candidate history; they still cannot resume across processes.

Detail sets retain explicit incomplete/group-error/failed states; all individually approved images plus a failing group audit result in partial, not succeeded. Partial outputs remain available.

## Evidence

Initial TDD command: `.venv/Scripts/python -m pytest tests/test_graph_recovery.py -q` -> 5 failures before implementation (missing execution module and candidate absent before audit).

Additional fault tests first failed for Windows lock conflict normalization, approved-file promotion, accepted-file promotion, and memory-run history export; corresponding implementations made each pass.

Latest full owned suite: `.venv/Scripts/python -m pytest tests/test_graph_recovery.py tests/test_pipeline.py tests/test_output.py tests/test_configured_pipeline.py tests/test_acceptance.py tests/test_review_regressions.py tests/test_transport.py tests/test_public_api.py -q` -> **86 passed in 18.05s**. Log: .superpowers/engine-tests.log.

Coverage includes: same-candidate audit resume with zero additional generation; persistence before audit; repair history; input corruption; route/capacity mismatch; actual POST retries consuming durable budget; shared budget atomic reservations across runs; limits cannot reset; generation interruption pauses uncertain; cancellation after save resumes audit; concurrent run lock; image timeout never silently replays; success promotion; manual older-candidate selection; memory-run full history export; existing configured API routing and per-service credential isolation; public cancellation preserving recovery files.

Scoped Ruff check/format performed. Coordinator owns global suite/build/live validation and independent review. No claim of live acceptance made by this worker.

## Independent review fixes and final engine gate

- P1 budget integrity: resuming requires an existing nonempty ledger. Missing tables/rows, counter reductions inconsistent with reservation receipts, changed ceilings, and missing initialized shared ledgers fail closed before any POST. Persistent `.initialized` sidecars distinguish prior ledgers from new ones. Added five regression cases; all observed RED then GREEN.
- P1 uncertain-operation integrity: each actual image reservation stores its operation ID; operations.json records submission count. Before resume, every image reservation group must have a consistent journal status and count. A missing journal or `{}` cannot erase an uncertain submission. Both regressions observed RED then GREEN. Corrupt journal recovery raises `generation_uncertain` with nonretryable error metadata, preserving artifacts.
- P2 repair evidence: identical scores with genuinely different normalized audit evidence may use the next allowed repair. Signature includes QualityReport.model_reason and relevant failed-check evidence; exact repeated evidence still stops early. This conservatively treats changed provider instructions as a potentially new action instead of guessing semantic equivalence. Changed-angle/then-cropped-handle regression observed RED then GREEN.
- P2 error metadata: generation_uncertain has retryable=False, while generic audit transport errors remain retryable=True. Observed RED then GREEN.
- Added independent fresh-process recovery coverage from the domain worker: process exit after candidate save, new Python process resumes without generation, input source may be removed because copied input remains.

Final stable engine gate: `.venv/Scripts/python -m pytest tests/test_graph_recovery.py tests/test_pipeline.py tests/test_output.py tests/test_configured_pipeline.py tests/test_acceptance.py tests/test_review_regressions.py tests/test_transport.py tests/test_public_api.py tests/test_process_recovery.py -q` -> **96 passed in 20.90s**. Scoped Ruff check -> **All checks passed**. No product/test edits after coordinator's stable global/build/live handoff. Independent reviewer notified for bounded verification.
