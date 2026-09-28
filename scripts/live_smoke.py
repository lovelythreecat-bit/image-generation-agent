"""Opt-in billable smoke test. Never run as part of pytest."""

import argparse
import asyncio
from pathlib import Path

from image_agent import CreationRequest, ImageSource, MaterialInput, create_images


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Live provider acceptance: requires --live and incurs model charges"
    )
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--material", action="append", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--category")
    parser.add_argument("--brief")
    parser.add_argument("--platform", default="taobao")
    parser.add_argument("--output", default="main_image")
    parser.add_argument("--model", default="fast", choices=["pro", "fast", "base"])
    parser.add_argument("--out", type=Path, default=Path("out"))
    parser.add_argument("--request-id", default="live-smoke")
    args = parser.parse_args(argv)
    if not args.live:
        parser.error("No model called. Add --live only after choosing acceptance scope.")
    if not args.material or not args.name or not args.category:
        parser.error("--material, --name and --category are required")
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
    result = asyncio.run(create_images(request))
    print(result.status)
    for asset in result.assets:
        print(asset.asset_id, asset.status, asset.file_path or "", asset.error or "")
    print("Inspect every saved image manually; model scores are not acceptance proof.")
    return 0 if result.status == "succeeded" and not result.output_errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
