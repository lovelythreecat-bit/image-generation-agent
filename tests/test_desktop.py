import asyncio
import os
import threading

import pytest
from pydantic import ValidationError

from image_agent import AgentConfig
from image_agent.desktop_runtime import BackgroundJob, build_request, with_credentials


def test_request_maps_materials_and_rejects_invalid_combinations(tmp_path):
    fields = dict(
        product_name="杯子",
        category="家居",
        platforms=["taobao"],
        output_types=["main_image"],
        output_dir=tmp_path,
    )
    request = build_request([tmp_path / "正面.png", tmp_path / "背面.png"], **fields)
    assert [m.material_id for m in request.materials] == ["m1", "m2"]
    assert request.materials[1].source.path.name == "背面.png"
    assert request.output_dir == tmp_path
    with pytest.raises(ValidationError):
        build_request([], **fields)
    with pytest.raises(ValidationError):
        build_request([tmp_path / "a.png"] * 9, **fields)
    with pytest.raises(ValidationError):
        build_request([tmp_path / "a.png"], **{**fields, "output_types": ["pdd_white_background"]})


def test_credentials_are_per_service_without_mutating_config_or_environment():
    config = AgentConfig.from_file("examples/config.openai.json")
    before = dict(os.environ)
    changed = with_credentials(config, {"official_images": "image-secret"})
    assert changed.services["official_images"].credential() == "image-secret"
    assert changed.services["official_images"].api_key_env is None
    assert changed.services["official_vision"].api_key_env == "OPENAI_API_KEY"
    assert config.services["official_images"].api_key is None
    assert dict(os.environ) == before


def test_worker_result_and_error_are_delivered_off_main_thread():
    main = threading.get_ident()
    job = BackgroundJob()

    async def work():
        return threading.get_ident()

    job.start(work)
    kind, value = job.events.get(timeout=3)
    assert kind == "result" and value != main
    job.thread.join(3)

    async def broken():
        raise ValueError("provider failed")

    job.start(broken)
    kind, value = job.events.get(timeout=3)
    assert kind == "error" and isinstance(value, ValueError)
    job.thread.join(3)


def test_cancel_is_idempotent_waits_for_cleanup_and_blocks_duplicate_start():
    job = BackgroundJob()
    started = threading.Event()
    cleaning = threading.Event()
    cleaned = threading.Event()

    async def work():
        started.set()
        try:
            await asyncio.sleep(60)
        finally:
            cleaning.set()
            await asyncio.sleep(0.04)
            cleaned.set()

    job.start(work)
    assert started.wait(3)
    with pytest.raises(RuntimeError):
        job.start(work)
    job.cancel()
    assert cleaning.wait(3)
    job.cancel()
    assert job.events.get(timeout=3) == ("cancelled", None)
    assert cleaned.is_set()
    job.thread.join(3)
    assert not job.running


def test_immediate_cancel_is_not_lost():
    job = BackgroundJob()

    async def work():
        await asyncio.sleep(60)

    job.start(work)
    job.cancel()
    assert job.events.get(timeout=3) == ("cancelled", None)
    job.thread.join(3)
