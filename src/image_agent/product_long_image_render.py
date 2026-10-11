"""Lossless delivery of fully designed model frames; no local typography/layout."""

from io import BytesIO
from typing import Sequence

from PIL import Image

from .errors import OutputError


def _png(image):
    stream = BytesIO()
    image.save(stream, "PNG")
    return stream.getvalue()


def compose_product_long_image(
    images: Sequence[bytes], *, width: int, slice_height: int = 1500
) -> tuple[bytes, list[bytes], list[dict[str, int]]]:
    if (
        not images
        or type(width) is not int
        or not 400 <= width <= 2400
        or type(slice_height) is not int
        or slice_height < 1
    ):
        raise OutputError("invalid product long-image composition options")
    sizes = []
    try:
        for data in images:
            with Image.open(BytesIO(data)) as image:
                sizes.append((width, max(1, round(image.height * width / image.width))))
    except (OSError, ValueError, Image.DecompressionBombError):
        raise OutputError("invalid complete section pixels") from None
    height = sum(h for _, h in sizes)
    if height > 60000 or width * height > 60000000:
        raise OutputError(f"product long-image dimensions exceed delivery limits: {width}x{height}")
    canvas = Image.new("RGB", (width, height))
    boundaries = []
    y = 0
    for data, size in zip(images, sizes):
        with Image.open(BytesIO(data)) as image:
            frame = image.convert("RGB")
            if frame.size != size:
                frame = frame.resize(size, Image.Resampling.LANCZOS)
            canvas.paste(frame, (0, y))
        boundaries.append({"y": y, "width": width, "height": size[1]})
        y += size[1]
    full = _png(canvas)
    slices = [
        _png(canvas.crop((0, y, width, min(y + slice_height, height))))
        for y in range(0, height, slice_height)
    ]
    return full, slices, boundaries
