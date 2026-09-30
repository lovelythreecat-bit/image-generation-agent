"""Image wire formats; deployment chooses a protocol, not a hard-coded provider."""

import base64
import math

from .errors import ProviderError


def image_endpoint(service, has_references):
    if service and service.protocol == "openai_images":
        if has_references:
            return service.edit_path or "/images/edits", "multipart"
        return service.generation_path or "/images/generations", "json"
    return (service.generation_path if service else None) or "/images", "json"


def openai_canvas(aspect_ratio):
    a, b = map(int, aspect_ratio.split(":"))
    if min(a, b) <= 0 or max(a, b) / min(a, b) > 3:
        raise ProviderError(
            "openai_images supports aspect ratios up to 3:1",
            kind="capability",
            code="unsupported_aspect_ratio",
        )
    # Exact requested ratio, 16-pixel grid, around a 1024-pixel long edge.
    divisor = math.gcd(a, b)
    a, b = a // divisor, b // divisor
    unit = max(math.ceil(1024 / (16 * max(a, b))), math.ceil(math.sqrt(655360 / (256 * a * b))))
    width, height = 16 * unit * a, 16 * unit * b
    if max(width, height) > 3840 or width * height > 8294400:
        raise ProviderError(
            "aspect ratio cannot fit the image canvas limits",
            kind="capability",
            code="unsupported_aspect_ratio",
        )
    return f"{width}x{height}"


async def generate_image(transport, service, *, model, prompt, size, aspect_ratio, images):
    protocol = service.protocol if service else "openrouter_images"
    path, _ = image_endpoint(service, bool(images))
    payload = dict(model=model, prompt=prompt, n=1)
    if protocol == "openai_images":
        payload.update(
            quality={"1K": "low", "2K": "medium", "4K": "high"}[size],
            size=openai_canvas(aspect_ratio),
        )
        if images:
            files = [
                ("image[]", (f"reference-{i}.jpg", data, "image/jpeg"))
                for i, data in enumerate(images, 1)
            ]
            return await transport.post_multipart(
                path,
                {key: str(value) for key, value in payload.items()},
                files,
            )
        return await transport.post_json(path, payload)
    payload["aspect_ratio"] = aspect_ratio
    if images:
        payload["input_references"] = [
            {
                "type": "image_url",
                "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(data).decode()},
            }
            for data in images
        ]
    if model in {"openai/gpt-image-2", "gpt-image-2"}:
        payload["quality"] = {"1K": "low", "2K": "medium", "4K": "high"}[size]
    else:
        payload["resolution"] = size
    return await transport.post_json(path, payload)
