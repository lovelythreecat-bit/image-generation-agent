import io

import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

from .errors import InputImageError, ProviderError
from .transport import compute


def decode_image(data):
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            return ImageOps.exif_transpose(image).copy()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise InputImageError("image cannot be decoded") from None


def rgb_image(data):
    image = decode_image(data).convert("RGBA")
    background = Image.new("RGBA", image.size, "white")
    background.alpha_composite(image)
    return background.convert("RGB")


def encode_jpeg(data, max_px):
    image = rgb_image(data)
    image.thumbnail((max_px, max_px), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, "JPEG", quality=90)
    return output.getvalue()


def image_format(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    return ".img", "application/octet-stream"


async def load_image(source, *, client=None, config=None):
    try:
        if source.data is not None:
            data = source.data
        elif source.path is not None:
            data = await compute(source.path.read_bytes)
        elif client is not None:
            response = await client.get(source.url, follow_redirects=True)
            response.raise_for_status()
            data = response.content
        else:
            async with httpx.AsyncClient(
                trust_env=False,
                proxy=config.http_proxy if config else None,
                timeout=config.request_timeout_seconds if config else 180,
            ) as owned:
                return await load_image(source, client=owned, config=config)
        await compute(decode_image, data)
        return data
    except (OSError, httpx.HTTPError):
        raise InputImageError("unable to load input image") from None


def generated_image(data):
    try:
        return rgb_image(data)
    except InputImageError:
        raise ProviderError("provider image is not decodable") from None
