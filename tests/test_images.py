import io

import pytest
from PIL import Image

from image_agent.errors import InputImageError
from image_agent.models import ImageSource


def picture(size=(800, 800), color="white", mode="RGB", fmt="PNG"):
    out = io.BytesIO()
    Image.new(mode, size, color).save(out, format=fmt)
    return out.getvalue()


def test_encode_no_upscale_and_alpha_white():
    from image_agent.images import encode_jpeg

    data = encode_jpeg(picture((20, 10), (0, 0, 0, 0), "RGBA"), 768)
    im = Image.open(io.BytesIO(data))
    assert im.size == (20, 10)
    assert im.getpixel((0, 0)) == (255, 255, 255)


async def test_load_preserves_bytes_and_rejects_bad(tmp_path):
    from image_agent.images import load_image

    data = picture(fmt="JPEG")
    p = tmp_path / "a.jpg"
    p.write_bytes(data)
    assert await load_image(ImageSource(path=p)) == data
    assert await load_image(ImageSource(data=data)) == data
    for source in [ImageSource(data=b"bad"), ImageSource(path=tmp_path / "missing")]:
        with pytest.raises(InputImageError):
            await load_image(source)
