# Configurable API Implementation Plan

> Execute inline with test-driven development; parent agent performs final review.

**Goal:** Configure service addresses, credentials, protocols, models and vision stages
without changing business code, including official GPT Image 2.

**Architecture:** Validated ServiceConfig and ModelRoute objects live in config.py.
HttpTransport binds services over its owned client; image protocol serialization moves
to image_protocols.py. Vision methods select stage routes. CLI/file/database mapping
entry points share validation.

**Tech Stack:** Existing Python, Pydantic, httpx, Pillow only.

**Spec:** ../specs/2026-09-29-configurable-api-design.md

## Constraints and review focus

No service/key fallback; explicit configs cannot inherit legacy environment settings.
Reject unknown routes, invalid files and missing referenced secrets before output.
Keep old AgentConfig/Python API and fake transports working. Preserve reference order,
error redaction and retry policy for multipart. Test official stage flow offline.

## Tasks

- [x] Write failing configuration tests for file/mapping equivalence, stage validation,
  key isolation, precedence and early credential errors; implement typed config loader.
- [x] Write failing wire tests for official edits/generations, capability limits and
  service credentials; implement binding and protocol serializers.
- [x] Write failing stage/full-pipeline tests; connect configured routes and CLI --config.
- [x] Add official and compatible service examples and docs; run full pytest and Ruff.

No commits or worktrees: the user authorized autonomous work in the shared testing folder.
