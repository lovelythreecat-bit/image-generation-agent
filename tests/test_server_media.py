import importlib
import io

import pytest
from PIL import Image


def store(tmp_path, **kwargs):
    try:
        cls = importlib.import_module("image_agent.server.media").MediaStore
    except ModuleNotFoundError:
        pytest.fail("MediaStore is required to validate and confine uploaded images")
    result = cls(tmp_path, **kwargs)
    result.initialize()
    return result


def png(size=(10, 10)):
    stream = io.BytesIO()
    Image.new("RGB", size).save(stream, "PNG")
    return stream.getvalue()


def test_upload_size_boundary_and_content_validation(tmp_path):
    data = png()
    media = store(tmp_path, max_upload_bytes=len(data), max_image_pixels=100)
    ref = media.store(data)
    assert media.read(ref["storage_ref"]) == data
    assert ref["mime_type"] == "image/png" and ref["byte_size"] == len(data)
    for invalid in (data + b"x", b"pretend image"):
        with pytest.raises(Exception) as caught:
            media.store(invalid)
        assert caught.value.status_code in (413, 422)


def test_decoded_pixels_and_storage_boundary(tmp_path):
    media = store(tmp_path, max_image_pixels=99)
    with pytest.raises(Exception) as caught:
        media.store(png())
    assert caught.value.status_code == 413
    for ref in ("../private", "C:/private", "/private", "media/../../private"):
        with pytest.raises(Exception) as caught:
            media.read(ref)
        assert caught.value.status_code == 404


def test_default_twenty_mib_upload_limit_accepts_exact_boundary(tmp_path):
    media = store(tmp_path)
    exact = png() + bytes(media.max_upload_bytes - len(png()))
    assert media.store(exact)["byte_size"] == 20 * 1024 * 1024
    with pytest.raises(Exception) as caught:
        media.store(exact + b"x")
    assert caught.value.status_code == 413
