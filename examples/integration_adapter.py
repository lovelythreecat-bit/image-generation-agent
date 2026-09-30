"""No server required: callers own authentication, media storage and blob URLs.

Pass config=AgentConfig.from_file("examples/config.openai.json"), or read a deployment
dict from your own database and pass AgentConfig.from_mapping(deployment_record).
The DTO's image_model must select an alias provided by that configuration.
"""

import asyncio
from pathlib import Path

from image_agent import (
    CreationRequestDTO,
    ImageSource,
    SelectionSpec,
    analyze_materials,
    create_images,
    request_from_dto,
    result_to_bundle,
)


async def create_from_media(
    dto,
    media_bytes,
    *,
    selection=None,
    config=None,
    analyze=analyze_materials,
    create=create_images,
):
    request = request_from_dto(
        dto, {key: ImageSource(data=value) for key, value in media_bytes.items()}
    )
    analysis = await analyze(request, config)
    if analysis.status == "failed":
        return analysis
    # A UI can display analysis.elements and Issue.options[].selection before this step.
    if selection is not None:
        request.selection = selection
    elif analysis.status == "needs_input":
        return analysis
    else:
        proposal = analysis.intent.proposal
        request.selection = SelectionSpec(
            mode="explicit", **proposal.model_dump(exclude={"primary_subject_id"})
        )
    result = await create(request, config, analysis=analysis)
    return result_to_bundle(result)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Explicit live integration example (billable)")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--image", type=Path)
    parser.add_argument("--name")
    parser.add_argument("--category")
    args = parser.parse_args()
    if not args.live or not args.image or not args.name or not args.category:
        parser.error("--live, --image, --name and --category are required")
    dto = CreationRequestDTO(
        product_name=args.name,
        category=args.category,
        platforms=["taobao"],
        output_types=["main_image"],
        materials=[dict(material_id="m1", media_id="upload")],
    )
    result = asyncio.run(create_from_media(dto, {"upload": args.image.read_bytes()}))
    print(result.dto.status if hasattr(result, "dto") else result.status)


if __name__ == "__main__":
    main()
