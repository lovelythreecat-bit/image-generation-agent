import importlib.util
from pathlib import Path

import pytest
from PIL import Image, ImageChops, ImageDraw

from image_agent.errors import AgentError
from image_agent.long_image_models import LongImageRequest

YAH = Path("C:/Windows/Fonts/msyh.ttc")


def fonts_module():
    assert importlib.util.find_spec("image_agent.long_image_fonts"), "font resolution is missing"
    from image_agent import long_image_fonts

    return long_image_fonts


def test_real_ttc_chinese_and_deterministic_emoji_fallback():
    fonts = fonts_module().resolve_fonts(["中文 ０９:30 🚀"], YAH)
    assert fonts.face_for("中").path == YAH
    assert ord("中") in fonts.face_for("中").cmap
    assert fonts.face_for("🚀").path.name == "seguiemj.ttf"
    for char in "中🚀":
        face = fonts.face_for(char)
        canvas = Image.new("L", (100, 100))
        ImageDraw.Draw(canvas).text((10, 10), char, font=fonts.font(face, 32), fill=255)
        missing = Image.new("L", (100, 100))
        ImageDraw.Draw(missing).text((10, 10), "\U0010ffff", font=fonts.font(face, 32), fill=255)
        assert canvas.getbbox()
        assert ImageChops.difference(canvas, missing).getbbox()


def test_missing_glyph_before_paid_call():
    request = LongImageRequest(scene="tutorial", title="中文", content_brief="原样展示\U0010ffff")
    with pytest.raises(AgentError) as caught:
        fonts_module().preflight_long_image(request)
    assert caught.value.code == "font_missing_glyph"
    assert "U+10FFFF" in str(caught.value)


def test_explicit_invalid_font_is_not_silently_ignored(tmp_path):
    font = tmp_path / "bad.ttf"
    font.write_bytes(b"not a font")
    with pytest.raises(AgentError) as caught:
        fonts_module().resolve_fonts(["中文"], font)
    assert caught.value.code == "font_invalid"


def test_font_file_replacement_changes_fingerprint(tmp_path):
    from fontTools.ttLib import TTFont

    path = tmp_path / "chosen.ttf"
    with TTFont(YAH, fontNumber=0) as font:
        font.save(path)
    first = fonts_module().resolve_fonts(["中文"], path)
    with TTFont(path) as font:
        font["head"].fontRevision += 0.01
        font.save(path)
    second = fonts_module().resolve_fonts(["中文"], path)
    assert first.face_for("中").sha256 != second.face_for("中").sha256


def test_preflight_checks_specs_service_and_required_copy():
    for field in [
        {"product_specs": [{"name": "规格", "value": "\U0010ffff"}]},
        {"service_notes": ["\U0010ffff"]},
        {"required_copy": [{"requirement_id": "copy", "text": "\U0010ffff"}]},
    ]:
        request = LongImageRequest(scene="custom", title="中文", content_brief="说明", **field)
        with pytest.raises(AgentError, match="U\\+10FFFF"):
            fonts_module().preflight_long_image(request)
