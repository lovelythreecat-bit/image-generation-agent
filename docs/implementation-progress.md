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
2. In progress: platform/prompt/compliance.
3. Pending: transport/generation.
4. Pending: image I/O/quality.
5. Pending: vision.
5A. Pending: selection.
6. Pending: pipeline.
7. Pending: output/CLI.
7A. Pending: JSON integration.
8. Pending: isolation, packaging, docs and final review.
