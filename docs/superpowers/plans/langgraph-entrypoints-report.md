# Task 3 — public entrypoints, desktop, contracts

Plan: `docs/superpowers/plans/2026-09-30-langgraph-refactor.md`
Date: 2026-09-30

## Delivered

- Public exports preserve `create_images`/`analyze_materials` and add `resume_images`, `accept_candidate`, `ExecutionPolicy`.
- CLI retains request parameters; adds `resume RUN_DIR --config ...`, `accept RUN_DIR --asset ... --candidate ... --reason ...`, and new-task `--max-image-calls` / `--max-vision-calls`.
- CLI statuses use explicit exits: succeeded=0; failed/quality_failed/output or argument error=1; partial=2; needs_input=3; accepted=4; pending_audit/audit_error=5; budget_exhausted=6; generation_uncertain=7; interrupted=130. Mixed aggregate partial remains 2; asset states and stop reasons are printed.
- Result DTO uses v2 statuses and candidate review metadata plus usage; removes local run_dir, asset paths, candidate paths, hashes and storage-only metadata. Only succeeded image bytes enter automatic-success blobs. Accepted images remain excluded. Request and error DTO versions remain v1.
- `creation-result-v2.json` exported; historical `creation-result-v1.json` remains unchanged. Analysis-v1 schema refreshed for domain's new optional evidence/provenance fields. Export script no longer overwrites result-v1.
- Desktop opens a task directory for review, reads old v1 and new v2 result manifests, validates local paths remain inside that directory, and resumes using current process credentials. Candidate history is selectable, old candidates remain previewable, stage/attempt/stop reasons are shown, and manual acceptance requires a separate explicit button action with a nonempty reason.
- Progress callbacks enqueue copied dictionaries in BackgroundJob; only Tk's polling thread mutates the UI. Terminal handling still waits for worker exit. Current form inputs are preserved on errors and resume does not require current form materials.
- Task changes clear stale candidate bindings; corrupt manifests cannot attach the old task's candidate selection to a new directory. Accepted older candidate stays selected; engine selected=true is authoritative; acceptance timestamp is a compatibility fallback for older manifests.
- README explains persistent directories, independent audit retries/quality repairs/HTTP budget, v1 viewing vs v2 recovery, lack of recovery for memory-only jobs, and uncertainty after API submission before response persistence. No exactly-once or restored-budget promise.

## Verification evidence

- Baseline assigned suites: 37 passed before changes.
- TDD red: 10 recovery/DTO/worker/manifest tests failed for absent behavior; implementation produced 10 passing tests.
- UI TDD red: progress was treated as terminal error, new statuses raised KeyError, and resume function absent. Tests passed after integration.
- Additional red→green guards: malformed task must not rebind old candidates; cancelled new task must not retain previous task candidate actions; accepted older candidate must stay selected while alternate history image preview changes actual pixels.
- Main assigned verification: `.venv/Scripts/python.exe -m pytest -q tests/test_cli.py tests/test_contracts.py tests/test_desktop.py tests/test_desktop_ui.py tests/test_entrypoint_recovery.py tests/test_isolation.py` → **56 passed in 9.13s** (before the final additional older-candidate regression).
- Final scoped verification after that change: `tests/test_desktop_ui.py -k "accepted_older or saved_candidates or opening_bad_task or start_new_task or open_v1"` → **5 passed, 17 deselected**.
- Ruff check and format --check passed for all nine edited source/test Python files; `resume --help` and `accept --help` passed.
- Schema-current and import-no-network/no-file-write tests passed in the assigned suite. Import boundary test now allows explicitly introduced langgraph; other forbidden imports remain prohibited.
- Existing real-pipeline desktop test updated only for intentional approved/<platform>/<output_type>/<variant>.png path change and passes with offline provider fakes.

## Scope and limits

No credentials read, no live API call, no commits, no out/ cleanup, no broad test duplication. Root owns final whole-project validation and independent review. Existing user changes in desktop/runtime/tests/README were preserved and extended.

Product-design-consistency skill applied to existing Tkinter controls and interaction patterns. Actual Tk widget behavior and preview pixel switching were exercised; no manual visual, screen-reader, high-DPI or keyboard-focus audit claimed. User-confirmed spec supplies authority for resume/manual-acceptance flows; no additional approval was required.

Ruling: External DTO candidate fields are explicitly whitelisted rather than serializing raw candidate dictionaries, preventing local storage references from crossing the integration boundary. Acceptance audit remains in the local result manifest; the external DTO exposes accepted status but not storage-only records.

Integration note: Engine owns accepted image hydration/current candidate persistence and provider-cost semantics. Desktop current-candidate selection follows the engine selected flag, with candidate status/acceptance timestamp as a compatibility fallback; candidate paths in manifests and runtime candidate metadata are relative to run_dir. UI can view v1 but cannot make old runs resumable.

Final interface update: Engine added selected flags and promotes accepted files to accepted/<asset_id>/<candidate_id>.<ext>. Repeated acceptance may preserve multiple accepted history entries; desktop now honors selected=true. Regression was re-run red→green, and final five affected UI tests passed again.
