from io import BytesIO

import pytest
from PIL import Image

from image_agent.errors import OutputError
from tests.test_images import picture


def test_pixels_and_lossless_slices():
    from image_agent.product_long_image_render import compose_product_long_image

    full, slices, boundaries = compose_product_long_image([picture((750, 1000))] * 6, width=750)
    image = Image.open(BytesIO(full))
    assert image.size == (750, 6000)
    assert len(slices) == 4
    assert boundaries[-1] == {"y": 5000, "width": 750, "height": 1000}
    restored = Image.new("RGB", image.size)
    y = 0
    for data in slices:
        part = Image.open(BytesIO(data))
        restored.paste(part, (0, y))
        y += part.height
    assert restored.tobytes() == image.tobytes()


def test_proportional_normalization_keeps_entire_frame_and_last_slice():
    from image_agent.product_long_image_render import compose_product_long_image

    image = Image.new("RGB", (400, 800), "red")
    image.paste("blue", (0, 0, 400, 10))
    image.paste("green", (0, 790, 400, 800))
    stream = BytesIO()
    image.save(stream, "PNG")
    full, slices, boundaries = compose_product_long_image([stream.getvalue()], width=750)
    output = Image.open(BytesIO(full))
    assert output.size == (750, 1500)
    assert output.getpixel((375, 0)) == (0, 0, 255)
    assert output.getpixel((375, 1499)) == (0, 128, 0)
    assert boundaries == [{"y": 0, "width": 750, "height": 1500}]
    _, slices, _ = compose_product_long_image([picture((750, 1000))] * 5, width=750)
    assert Image.open(BytesIO(slices[-1])).height == 500


@pytest.mark.parametrize("width,count,size", [(750, 61, (10, 14)), (2400, 27, (10, 4))])
def test_render_limits_fail_before_allocating_full_canvas(width, count, size):
    from image_agent.product_long_image_render import compose_product_long_image

    with pytest.raises(OutputError):
        compose_product_long_image([picture(size)] * count, width=width)
