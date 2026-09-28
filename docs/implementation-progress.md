# SDD ledger — plan: docs/superpowers/plans/2026-09-28-standalone-image-agent.md

## Setup

- Read revision r2, original spec, feasibility supplements and all task interfaces.
- All 12 source-manifest SHA-256 hashes match on 2026-09-28. Source files are read-only inputs.
- Ruling: initialize the dedicated empty project on feat/standalone-image-agent rather than create a linked worktree — Task 1 explicitly requires initialization here; no existing code or branch to isolate.
- Ruling: use this tracked ledger and native PowerShell task verification rather than Bash-only bookkeeping helpers — Windows workspace; retain equivalent briefs, test evidence and commits.
- Pre-flight 1→2/3/4/5/5A/6/7A: shared Pydantic contracts in models; no conflicting signatures.
- Pre-flight 3→4→6: generator encoder injected; image processing provides real encoder later.
- Pre-flight 5→5A→6: draft precedes evidence validation, only finalized ready permits generation.
- Pre-flight 6→7→7A: in-memory pipeline, optional output and external DTO remain separate.
- Pre-flight 1/4/5A/6: r2 fingerprint excludes targets and selection, includes all material content/hints and brief.

## Tasks

1. Core contracts implemented: RED 35 collected failures (missing models/config); GREEN `.venv/Scripts/python.exe -m pytest tests/test_models.py tests/test_config.py -q`: 35 passed. Additional source-intent validation exercised with vision in Task 5.
2. Core rules complete: RED 5 collected failures; GREEN full suite 40 passed. Platform file copied unchanged; prompt text updated for multi-source plans and frozen requirements.
3. Core transport complete: RED 14 failures after fixing Windows socketpair test guard; GREEN full suite 54 passed. Shared permits use a thread lock and cancellable polling, no loop-bound global semaphore. Supplier protocol remains unverified pending documentation checkpoint.
4. Core image I/O/quality complete: RED 10 failures; GREEN full suite 64 passed. Original bytes preserved; white export, decoding, local thresholds and audit IDs checked.
5. Core vision complete: RED 6 failures; fixed proposal/intent constraint ownership after 2 test failures; GREEN 6 vision tests passed (68 other cases passed in preceding run).
5A. Core selection complete: RED 5 failures; GREEN full suite 75 passed. OR/AND evidence, capacity, white optional/required conflict, pending recovery and fingerprint tested.
6. Core pipeline complete: RED 9 failures; GREEN 9 pipeline tests passed. Four-asset order, per-asset repair, staged, high-score false, recheck, snapshot, cancellation, white export covered.
7. Core output/CLI complete: RED 12 failures (existing cancellation utility already passed); GREEN full suite 97 passed. JPEG bytes, atomic writes, per-file/manifest failures, exit codes and repeat cancellation covered.
7A. Core JSON integration complete: RED 7 failures and 9 existing passes; GREEN full suite 104 passed. DTO round trips, media binding, safe CLI paths and exported schemas.
8. Installation/docs implemented; final review pending. Full suite 127 passed. `python -m build --no-isolation` built wheel+sdist; runtime-only second environment installed wheel offline. From external TEMP directory, public API with MockTransport, module CLI and console CLI passed. Wheel metadata has exactly httpx, pydantic, Pillow runtime requirements; pytest absent.

## Follow-up validation before final completion

- Broaden branch-boundary coverage against the full plan; passing core tests does not yet establish all task acceptance bullets.
- Protocol documentation checkpoint completed 2026-09-29; see docs/provider-protocol.md. No paid supplier calls authorized or performed.
- Ruling: staged cancellation helper is shared in transport.py for pure CPU tasks and final save tasks — one repeat-cancellation mechanism avoids incompatible lifecycle behavior.
- Ruling: tasks 2–7A share an integration checkpoint commit after interruption and API wiring, followed by acceptance fixes — preserves a runnable public import instead of commits referencing not-yet-added modules. Per-task RED/GREEN evidence remains recorded above.
- Acceptance sweep: 17 new cases exposed 3 omissions (ready primary, low-confidence selection evidence, DTO enums). All fixed RED→GREEN; suite 121 passed.
- Public API sweep: 6 cases exposed legacy first-reference omission and stale exported schema. Fixed RED→GREEN; suite 127 passed.
- Ruling: build with --no-isolation using installed hatchling/build — avoids another network dependency install while producing the same PEP 517 wheel/sdist; separate clean runtime environment validates independence.
