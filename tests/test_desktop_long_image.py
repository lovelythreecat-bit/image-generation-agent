import asyncio
import json

import pytest

from image_agent.desktop_runtime import load_saved_task
from image_agent.long_image_models import LongImageResult
from image_agent.long_image_runtime import create_long_image, render_long_image
from tests.test_long_image_pipeline import request_plan
from tests.test_output import result


def test_legacy_result_without_type_remains_product(tmp_path):
    saved = result()
    (tmp_path / "image.jpg").write_bytes(saved.assets[0].image)
    payload = saved.model_dump(mode="json")
    payload["assets"][0]["file_path"] = "image.jpg"
    (tmp_path / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    assert load_saved_task(tmp_path).assets[0].image == saved.assets[0].image


@pytest.mark.parametrize(
    "metadata",
    [
        {"version": 2, "task_type": "future"},
        {"version": 3},
        {"version": 1, "task_type": "long_image"},
    ],
)
def test_unknown_type_or_version_is_rejected(tmp_path, metadata):
    (tmp_path / "run.json").write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ValueError, match="类型|版本"):
        load_saved_task(tmp_path)


def test_long_saved_load_validates_and_hydrates_real_store(tmp_path):
    request, plan = request_plan(tmp_path, slots=False)
    saved = asyncio.run(create_long_image(request, plan=plan))
    saved = asyncio.run(render_long_image(saved.run_dir, render_options={"width": 820}))
    loaded = load_saved_task(saved.run_dir)
    assert isinstance(loaded, LongImageResult)
    assert loaded.page.width == 820 and loaded.page.image == saved.page.image
    assert loaded.usage == saved.usage


def test_missing_type_does_not_guess_long_image_fields(tmp_path):
    (tmp_path / "result.json").write_text(
        LongImageResult(status="failed").model_dump_json(), encoding="utf-8"
    )
    payload = json.loads((tmp_path / "result.json").read_text("utf-8"))
    del payload["task_type"]
    (tmp_path / "result.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ValueError):
        load_saved_task(tmp_path)
