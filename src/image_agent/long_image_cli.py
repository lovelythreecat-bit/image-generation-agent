"""Local CLI adapter for path-free long-image requests and verified planning records."""

import asyncio
import json
from pathlib import Path, PureWindowsPath

from .config import AgentConfig
from .errors import AgentError
from .execution import ExecutionPolicy
from .long_image_contracts import (
    LongImagePlanDTO,
    LongImageRequestDTO,
    long_planning_to_dto,
    long_request_from_dto,
)
from .long_image_models import LongImagePlan
from .long_image_planning import analyze_long_image
from .long_image_runtime import (
    accept_long_image_candidate,
    create_long_image,
    render_long_image,
    resume_long_image,
)
from .long_image_store import LongImageRunStore
from .models import ImageSource, MaterialAnalysis
from .output import atomic_write
from .sanitization import redact

EXIT_CODES = {
    "succeeded": 0,
    "failed": 1,
    "quality_failed": 1,
    "needs_input": 3,
    "accepted": 4,
    "pending_audit": 5,
    "audit_error": 5,
    "budget_exhausted": 6,
    "generation_uncertain": 7,
}


def add_long_image_parser(subparsers):
    parser = subparsers.add_parser("long-image", help="Plan, create or locally render a long image")
    commands = parser.add_subparsers(dest="long_command", required=True)
    for command in ("analyze", "create"):
        item = commands.add_parser(command)
        item.add_argument("--request-json", required=True, type=Path)
        item.add_argument("--media-map", type=Path)
        item.add_argument("--media-root", type=Path)
        item.add_argument("--font-path", type=Path)
        item.add_argument("--out", required=True, type=Path)
        item.add_argument("--analysis", type=Path)
        item.add_argument("--config", type=Path)
        item.add_argument("--max-image-calls", type=int)
        item.add_argument("--max-vision-calls", type=int)
        if command == "create":
            item.add_argument(
                "--plan", type=Path, help="Optional plan JSON; external plans are revalidated"
            )
            item.add_argument(
                "--plan-record", type=Path, help="Original <planning-run>/records/<record-id>.json"
            )
    resume = commands.add_parser("resume")
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--config", type=Path)
    accept = commands.add_parser("accept")
    accept.add_argument("run_dir", type=Path)
    accept.add_argument("--slot", required=True)
    accept.add_argument("--candidate", required=True)
    accept.add_argument("--reason", required=True)
    render = commands.add_parser("render")
    render.add_argument("run_dir", type=Path)
    render.add_argument("--width", type=int)
    render.add_argument("--theme", choices=("auto", "clean", "warm", "premium"))
    render.add_argument("--slice-height", type=int)
    render.add_argument("--font-path", type=Path)
    return parser


def _request(args):
    dto = LongImageRequestDTO.model_validate_json(args.request_json.read_text("utf-8"))
    if bool(args.media_map) != bool(args.media_root):
        raise AgentError("media binding requires both --media-map and --media-root")
    if dto.materials and not args.media_map:
        raise AgentError("materials require --media-map and --media-root")
    sources = {}
    if args.media_map:
        mapping = json.loads(args.media_map.read_text("utf-8"))
        if not isinstance(mapping, dict):
            raise AgentError("media map must be a JSON object")
        root = args.media_root.resolve()
        for media_id, value in mapping.items():
            if (
                not isinstance(value, str)
                or not value
                or Path(value).is_absolute()
                or PureWindowsPath(value).drive
                or PureWindowsPath(value).root
                or ".." in PureWindowsPath(value).parts
                or "://" in value
            ):
                raise AgentError("media map paths must be relative without parent traversal")
            path = (root / value).resolve()
            if not path.is_relative_to(root):
                raise AgentError("media map path escapes media-root")
            sources[media_id] = ImageSource(path=path)
    fonts = (
        {dto.options.font_id: args.font_path} if dto.options.font_id and args.font_path else None
    )
    request = long_request_from_dto(dto, sources, font_bindings=fonts)
    if args.font_path:
        request.options.font_path = args.font_path
    request.output_dir = args.out
    return request


def _planning(args, request, analysis):
    plan = (
        LongImagePlan.model_validate(
            LongImagePlanDTO.model_validate_json(args.plan.read_text("utf-8")).model_dump(
                exclude={"task_type"}
            )
        )
        if args.plan
        else None
    )
    if not args.plan_record:
        return plan, None
    locator = args.plan_record.resolve()
    if locator.parent.name != "records" or locator.suffix != ".json":
        raise AgentError("--plan-record must select an original records/<record-id>.json")
    # Only the chosen local locator is accepted; descriptors/usage from request JSON are forbidden.
    store = LongImageRunStore(locator.parent.parent)
    try:
        with store.lock():
            _validate_run(store)
            data = store.json(f"records/{locator.stem}.json")
            if not isinstance(data, dict):
                raise AgentError("planning record must be a JSON object", code="plan_invalid")
            if data.get("analysis") is not None and analysis is None:
                raise AgentError("planning record requires --analysis with the same saved snapshot")
            plan = plan or LongImagePlan.model_validate(data["plan"])
            record = store.load_planning_record(
                locator.stem, request=request, plan=plan, analysis=analysis
            )
            return plan, record
    except (KeyError, TypeError) as error:
        raise AgentError(
            "planning record is missing, corrupt or unbound", code="plan_invalid"
        ) from error
    finally:
        store.close()


def _validate_run(store):
    try:
        metadata = store.json("run.json")
        if not isinstance(metadata, dict):
            raise AgentError("run metadata must be a JSON object")
        return store.load_metadata()
    except (KeyError, TypeError, AttributeError) as error:
        raise AgentError("run metadata is malformed") from error


def _usage(name, value):
    print(f"{name}: " + ("unavailable" if value is None else json.dumps(value, ensure_ascii=False)))


def _issues(issues):
    for issue in issues:
        print(f"issue {issue.issue_id} {issue.code}: {redact(issue.message)}")
        print(
            f"  blocking={issue.required} field={issue.field or '-'} resolution={issue.resolution}"
        )
        for option in issue.options:
            print("  choice: " + redact(json.dumps(option, ensure_ascii=False)))


def _plan_summary(plan):
    if plan is None:
        return
    count = sum(slot.source == "generate" for slot in plan.image_slots)
    print(f"plan: scene={plan.scene} sections={len(plan.sections)} generated_slots={count}")
    print("planning, audit and repair calls are additional to generated_slots")
    for section in plan.sections:
        preview = redact(section.text_units[0].text)[:120] if section.text_units else ""
        print(
            f"  section {section.section_id} {section.role} slots={','.join(section.slot_ids) or '-'} {preview}"
        )


def print_long_result(result):
    print("result: " + result.status)
    if result.run_dir:
        print("run_dir: " + str(result.run_dir))
    _plan_summary(result.plan)
    _issues(result.issues)
    for asset in result.image_assets:
        print(f"slot {asset.slot_id} {asset.source} {asset.status or asset.source_status}")
        for candidate in asset.candidates:
            selected = candidate.get("candidate_id") == asset.selected_candidate_id
            print(
                f"  candidate {candidate.get('candidate_id')} {candidate.get('status')} selected={selected} "
                f"approval={'human accepted' if candidate.get('acceptance') else 'automatic passed' if candidate.get('status') == 'succeeded' else 'not approved'}"
            )
    for artifact in [*result.sections, *([result.page] if result.page else []), *result.slices]:
        print(
            f"{artifact.kind} {artifact.artifact_id} {artifact.status} {artifact.availability} "
            f"{artifact.width}x{artifact.height} {artifact.file_path or '-'}"
        )
    _usage("execution", result.usage.execution)
    for planning_id, usage in result.usage.linked_planning.items():
        _usage("linked_planning " + planning_id, usage)
    if not result.usage.linked_planning:
        print("linked_planning: {}")
    _usage("display_total", result.usage.display_total)
    for error in [*result.output_errors, *([result.error_info] if result.error_info else [])]:
        print(error.code + ": " + redact(error.message))
    for warning in result.warnings:
        print("warning: " + redact(warning))
    return 1 if result.output_errors else EXIT_CODES[result.status]


def run_long_image_cli(args):
    command = args.long_command
    if command in {"render", "accept", "resume"}:
        _validate_run(LongImageRunStore(args.run_dir))
    if command == "render":
        options = {
            key: getattr(args, key)
            for key in ("width", "theme", "slice_height", "font_path")
            if getattr(args, key) is not None
        }
        return print_long_result(
            asyncio.run(render_long_image(args.run_dir, render_options=options))
        )
    if command == "accept":
        reason = args.reason.strip()
        if not reason:
            raise AgentError("人工接受必须填写原因")
        return print_long_result(
            asyncio.run(
                accept_long_image_candidate(args.run_dir, args.slot, args.candidate, reason=reason)
            )
        )
    kwargs = {"config": AgentConfig.from_file(args.config)} if args.config else {}
    if command == "resume":
        return print_long_result(asyncio.run(resume_long_image(args.run_dir, **kwargs)))
    request = _request(args)
    analysis = (
        MaterialAnalysis.model_validate_json(args.analysis.read_text("utf-8"))
        if args.analysis
        else None
    )
    if request.selection.mode == "explicit" and analysis is None:
        raise AgentError("explicit selection requires --analysis with the same snapshot")
    policy = {
        key: getattr(args, key)
        for key in ("max_image_calls", "max_vision_calls")
        if getattr(args, key) is not None
    }
    if policy:
        kwargs["policy"] = ExecutionPolicy(**policy)
    if command == "create":
        plan, record = _planning(args, request, analysis)
        return print_long_result(
            asyncio.run(
                create_long_image(
                    request, plan=plan, analysis=analysis, planning_record=record, **kwargs
                )
            )
        )
    result = asyncio.run(analyze_long_image(request, analysis=analysis, **kwargs))
    args.out.mkdir(parents=True, exist_ok=True)
    atomic_write(
        args.out / "planning.json",
        long_planning_to_dto(result).model_dump_json(indent=2).encode("utf-8"),
    )
    if result.plan:
        atomic_write(
            args.out / "plan.json",
            long_planning_to_dto(result).plan.model_dump_json(indent=2).encode("utf-8"),
        )
    if result.analysis:
        atomic_write(
            args.out / "analysis.json", result.analysis.model_dump_json(indent=2).encode("utf-8")
        )
    print(f"planning: {result.status} planning_id={result.planning_id}")
    if result.record:
        print(f"plan_record: {result.record.local_record_ref}")
    _plan_summary(result.plan)
    _issues(result.issues)
    _usage("planning_usage", result.usage)
    if result.error_info:
        print(result.error_info.code + ": " + redact(result.error_info.message))
    for warning in result.warnings:
        print("warning: " + redact(warning))
    return {"ready": 0, "needs_input": 3, "failed": 1}[result.status]
