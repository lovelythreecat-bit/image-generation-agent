"""Opt-in billable smoke test. Never run as part of pytest."""

import argparse
import asyncio
import sys
from pathlib import Path

from pydantic import ValidationError

from image_agent import AgentConfig, CreationRequest, ImageSource, MaterialInput, create_images
from image_agent.errors import AgentError, make_error_info


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Live provider acceptance: requires --live and incurs model charges"
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--config", type=Path, help="Deployment API configuration JSON file")
    parser.add_argument("--material", action="append", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--category")
    parser.add_argument("--brief")
    parser.add_argument("--platform", default="taobao")
    parser.add_argument("--output", default="main_image")
    parser.add_argument("--model", default="fast", choices=["pro", "fast", "base"])
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument("--request-id", help="Optional output folder name; generated when omitted")
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("No model called. Add --live only after choosing acceptance scope.")
    if not args.material or not args.name or not args.category:
        parser.error("--material, --name and --category are required")
    try:
        request = CreationRequest(
            product_name=args.name,
            category=args.category,
            materials=[
                MaterialInput(material_id=f"m{i}", source=ImageSource(path=p))
                for i, p in enumerate(args.material, 1)
            ],
            creative_brief=args.brief,
            platforms=[args.platform],
            output_types=[args.output],
            image_model=args.model,
            output_dir=args.out,
            request_id=args.request_id,
        )
        config_args = {"config": AgentConfig.from_file(args.config)} if args.config else {}
        result = asyncio.run(create_images(request, **config_args))
    except (AgentError, ValidationError) as error:
        print(make_error_info(error).message, file=sys.stderr)
        return 1
    print(result.status)
    if result.error_info:
        print(result.error_info.message, file=sys.stderr)
    for asset in result.assets:
        print(asset.asset_id, asset.status, asset.file_path or "", asset.error or "")
    if any(asset.file_path for asset in result.assets):
        print("Inspect every saved image manually; model scores are not acceptance proof.")
    return 0 if result.status == "succeeded" and not result.output_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
