import asyncio
import json

import pytest

from image_agent.errors import OutputError
from image_agent.models import Asset, CreationRequest, CreationResult, ProductInputAudit
from tests.test_images import picture


def result():
    return CreationResult(
        status="succeeded",
        input_check=ProductInputAudit(matches=True, observed_product="cup", reason="ok"),
        assets=[
            Asset(
                asset_id="taobao.main_image.default",
                platform="taobao",
                output_type="main_image",
                status="succeeded",
                model="test",
                image=picture(fmt="JPEG"),
            )
        ],
    )


async def test_original_jpeg_relative_manifest_and_exclusive_directory(request_data, tmp_path):
    from image_agent.output import reserve_output, save_result

    r = CreationRequest(**(request_data | dict(output_dir=tmp_path, request_id="run")))
    directory = reserve_output(r)
    with pytest.raises(OutputError):
        reserve_output(r)
    original = result()
    saved = await save_result(original, directory)
    assert saved.assets[0].file_path.endswith(".jpg")
    assert (directory / "taobao/main_image.jpg").read_bytes() == original.assets[0].image
    manifest = json.loads((directory / "result.json").read_text("utf-8"))
    assert (
        manifest["assets"][0]["image"]
        == manifest["assets"][0]["file_path"]
        == "taobao/main_image.jpg"
    )


async def test_image_write_error_keeps_bytes_and_structured_error(tmp_path, monkeypatch):
    from image_agent import output

    real = output.atomic_write

    def write(path, data):
        if path.suffix == ".jpg":
            raise PermissionError("denied")
        real(path, data)

    monkeypatch.setattr(output, "atomic_write", write)
    saved = await output.save_result(result(), tmp_path)
    assert saved.assets[0].status == "succeeded" and saved.assets[0].image
    assert saved.assets[0].file_path is None and saved.assets[0].error_info is None
    assert saved.output_errors[0].code == "output"
    assert json.loads((tmp_path / "result.json").read_text("utf-8"))["assets"][0]["image"] is None


async def test_manifest_error_carries_result(tmp_path, monkeypatch):
    from image_agent import output

    def write(path, data):
        raise PermissionError("denied")

    monkeypatch.setattr(output, "atomic_write", write)
    with pytest.raises(OutputError) as error:
        await output.save_result(result(), tmp_path)
    assert error.value.result.assets[0].image


async def test_repeat_cancellation_waits_for_owned_save():
    from image_agent.transport import settle

    entered, release, finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def save():
        entered.set()
        await release.wait()
        finished.set()

    async def owner():
        await settle(asyncio.create_task(save()))

    task = asyncio.create_task(owner())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert finished.is_set()
