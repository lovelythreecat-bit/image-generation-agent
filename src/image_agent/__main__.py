import argparse
import asyncio
import json
import sys
from pathlib import Path, PureWindowsPath

from pydantic import ValidationError

from .config import AgentConfig
from .errors import AgentError, OutputError, make_error_info
from .models import CreationRequest, ImageSource, MaterialAnalysis, MaterialInput
from .output import atomic_write
from .pipeline import accept_candidate, analyze_materials, create_images, resume_images

EXIT_CODES = {
    "succeeded": 0,
    "failed": 1,
    "quality_failed": 1,
    "partial": 2,
    "needs_input": 3,
    "accepted": 4,
    "pending_audit": 5,
    "audit_error": 5,
    "budget_exhausted": 6,
    "generation_uncertain": 7,
}


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise AgentError(message)


def parser():
    root = Parser(prog="image-agent", description="Standalone multi-image product creation")
    subs = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    for command in ("create", "analyze"):
        p = subs.add_parser(command)
        p.add_argument("--product")
        p.add_argument("--reference", action="append", default=[])
        p.add_argument("--material", action="append", default=[])
        p.add_argument("--request-json")
        p.add_argument("--config", help="Deployment API configuration JSON file")
        p.add_argument("--media-map")
        p.add_argument("--media-root")
        p.add_argument("--name")
        p.add_argument("--category")
        p.add_argument("--platform", action="append", default=[])
        p.add_argument("--output", action="append", default=[])
        p.add_argument("--brief")
        p.add_argument("--style")
        p.add_argument("--presentation", default="auto")
        p.add_argument("--model-preference", default="auto")
        p.add_argument("--model", default="pro")
        p.add_argument("--size", default="2K")
        p.add_argument("--aspect", default="auto")
        p.add_argument("--market")
        p.add_argument("--request-id")
        p.add_argument("--out", required=True)
        if command == "create":
            p.add_argument("--analysis")
            p.add_argument("--max-image-calls", type=int)
            p.add_argument("--max-vision-calls", type=int)
    resume = subs.add_parser("resume", help="Continue a persisted v2 task")
    resume.add_argument("run_dir", type=Path)
    resume.add_argument("--config", help="Current deployment configuration; keys are not persisted")
    accept = subs.add_parser(
        "accept", help="Explicitly accept a saved candidate without approving it"
    )
    accept.add_argument("run_dir", type=Path)
    accept.add_argument("--asset", required=True)
    accept.add_argument("--candidate", required=True)
    accept.add_argument("--reason", required=True)
    return root


def local_source(value):
    if "://" in value:
        raise AgentError("CLI images must be local paths")
    return ImageSource(path=Path(value))


def request_from_args(args):
    if args.request_json:
        from .contracts import CreationRequestDTO, request_from_dto

        if (
            args.material
            or args.product
            or args.reference
            or args.name
            or args.category
            or args.platform
            or args.output
            or args.brief
            or args.style
            or args.market
            or args.request_id
            or args.presentation != "auto"
            or args.model_preference != "auto"
            or args.model != "pro"
            or args.size != "2K"
            or args.aspect != "auto"
        ):
            raise AgentError("JSON request mode cannot mix creative/input CLI flags")
        if not args.media_map or not args.media_root:
            raise AgentError("JSON mode requires --media-map and --media-root")
        dto = CreationRequestDTO.model_validate_json(Path(args.request_json).read_text("utf-8"))
        mapping = json.loads(Path(args.media_map).read_text("utf-8"))
        if not isinstance(mapping, dict):
            raise AgentError("media map must be a JSON object")
        root = Path(args.media_root).resolve()
        sources = {}
        for media_id, value in mapping.items():
            if (
                not isinstance(value, str)
                or not value
                or Path(value).is_absolute()
                or PureWindowsPath(value).drive
                or PureWindowsPath(value).root
                or "://" in value
            ):
                raise AgentError("media map paths must be relative to media-root")
            path = (root / value).resolve()
            if not path.is_relative_to(root):
                raise AgentError("media map path escapes media-root")
            sources[media_id] = ImageSource(path=path)
        request = request_from_dto(dto, sources)
        if args.command == "create":
            request.output_dir = Path(args.out)
        return request
    if args.media_map or args.media_root:
        raise AgentError("media bindings require --request-json")
    fields = dict(
        product_name=args.name,
        category=args.category,
        platforms=args.platform,
        output_types=args.output,
        creative_brief=args.brief,
        style_hint=args.style,
        presentation_mode=args.presentation,
        model_preference=args.model_preference,
        image_model=args.model,
        image_size=args.size,
        aspect_ratio=args.aspect,
        market=args.market,
        request_id=args.request_id,
    )
    if args.material:
        if args.product or args.reference:
            raise AgentError("--material cannot be mixed with --product/--reference")
        fields["materials"] = [
            MaterialInput(material_id=f"m{i}", source=local_source(path))
            for i, path in enumerate(args.material, 1)
        ]
    else:
        fields["product_image"] = local_source(args.product) if args.product else None
        fields["reference_images"] = [local_source(p) for p in args.reference]
    if args.command == "create":
        fields["output_dir"] = Path(args.out)
    return CreationRequest(**fields)


def run_cli(argv=None):
    try:
        args = parser().parse_args(argv)
        if args.command == "accept":
            reason = args.reason.strip()
            if not reason:
                raise AgentError("人工接受必须填写原因")
            result = asyncio.run(
                accept_candidate(args.run_dir, args.asset, args.candidate, reason=reason)
            )
            return print_result(result)
        config_args = {"config": AgentConfig.from_file(args.config)} if args.config else {}
        if args.command == "resume":
            return print_result(asyncio.run(resume_images(args.run_dir, **config_args)))
        request = request_from_args(args)
        if args.command == "analyze":
            analysis = asyncio.run(analyze_materials(request, **config_args))
            directory = Path(args.out)
            directory.mkdir(parents=True, exist_ok=True)
            atomic_write(
                directory / "analysis.json", analysis.model_dump_json(indent=2).encode("utf-8")
            )
            print("analysis: " + analysis.status)
            for issue in analysis.issues:
                print(issue.code + ": " + issue.message)
            return {"ready": 0, "needs_input": 3, "failed": 1}[analysis.status]
        analysis = (
            MaterialAnalysis.model_validate_json(Path(args.analysis).read_text("utf-8"))
            if args.analysis
            else None
        )
        policy_fields = {
            key: getattr(args, key)
            for key in ("max_image_calls", "max_vision_calls")
            if getattr(args, key) is not None
        }
        if policy_fields:
            from .execution import ExecutionPolicy

            config_args["policy"] = ExecutionPolicy(**policy_fields)
        return print_result(asyncio.run(create_images(request, analysis=analysis, **config_args)))
    except SystemExit as error:
        return 0 if error.code == 0 else 1
    except KeyboardInterrupt:
        return 130
    except (AgentError, ValidationError) as error:
        print(make_error_info(error).message, file=sys.stderr)
        return 1
    except (OSError, json.JSONDecodeError):
        print(make_error_info(OutputError("无法读取或写入 CLI 文件")).message, file=sys.stderr)
        return 1


def print_result(result):
    print("result: " + result.status)
    if result.run_dir:
        print("run_dir: " + str(result.run_dir))
    for asset in result.assets:
        print(
            f"{asset.platform} {asset.output_type} {asset.variant or '-'} {asset.status} {asset.file_path or ''}"
        )
        if asset.stop_reason:
            print("stop_reason: " + asset.stop_reason)
        if asset.error:
            print(asset.error)
        for candidate in asset.candidates:
            print(
                f"  candidate {candidate['candidate_id']} attempt={candidate.get('index', '-')} {candidate.get('status', '')}"
            )
    for issue in result.issues:
        print(issue.code + ": " + issue.message)
        for option in issue.options:
            print(f"  {option.id}: {option.label}")
    if result.error_info:
        print(result.error_info.message)
    return 1 if result.output_errors else EXIT_CODES[result.status]


def main():
    sys.exit(run_cli())


if __name__ == "__main__":
    main()
