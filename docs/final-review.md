# Final independent review — 2026-09-29

Reviewed head: `7ebc7fe794faa1cdcd8e3af8142d8d165935957b`; whole new runtime including initial contracts. A separate read-only reviewer read all runtime files and focused tests. Verdict: **with fixes**; no confirmed Critical findings, six Important findings, no separately deferred Minor findings.

| Finding | Reproduction / fix evidence |
| --- | --- |
| Staged fusion kept original source numbering after a stage image displaced an optional source | Capacity 2, m1 identity + m2 scene: final payload m1/stage versus old plan m1/m2. Rebind source/stage IDs and final prompt while retaining requirements. Regression checks payload, bindings and prompt. |
| Focused identity recheck demanded exempt subjects | Two subjects at 82, only focus applicable. One shared applicability helper now selects both requested and expected review IDs. |
| Primary subject could be the second identity reference | Proposal primary=s2 with subject_ids=[s1,s2]. Asset subject/reference order now begins with primary. |
| Malformed platform/output lists leaked TypeError/AttributeError | Null/scalar/non-string entries now raise validation errors in runtime and DTO. |
| Placeholder regex diverged from source | Restored complete source regex; clipboard/img/upload/SKU positives and real-name negatives tested. |
| Model diagnostic messages bypassed sanitization | Shared diagnostic-field validation redacts credentials/data URLs/paths and limits messages to 500 characters. Facts, IDs and verbatim source_quote are not rewritten. |

All six were reproduced before implementation fixes: 14 failures, one existing passing case. A further applicability regression confirmed that an exempt subject could select an unnecessary staged repair; repair selection now respects the same frozen plan. Full-suite and packaging outcomes are recorded in implementation-progress.md; no second reviewer pass is claimed.

Review boundaries and executor rulings:

- Live multi-image effect, scoring reliability and actual supplier capacity remain unverified; paid calls are outside this execution's authorization.
- HTTP service/auth/queues/workbench/cloud storage remain outside the approved package scope.
- No-face/model framing was considered but no concrete failure was shown; keep the crop-aware no-face instruction and validate visually during live acceptance.
- The reviewer did not rerun the full suite or read every test line; the executor's subsequent tests are the verification gate. No claim of independent test execution is made.
