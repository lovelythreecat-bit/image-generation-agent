import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from image_agent.errors import AgentError
from image_agent.long_image_contracts import (
    LongImagePlanDTO,
    LongImagePlanningResultDTO,
    LongImageRequestDTO,
    LongImageResultDTO,
    long_planning_to_dto,
    long_request_from_dto,
    long_result_to_bundle,
)
from image_agent.long_image_models import LongImagePlanningResult, LongImageResult
from image_agent.models import ImageSource
from tests.test_images import picture


def test_request_media_and_font_bindings_are_explicit():
    dto = LongImageRequestDTO(
        scene="tutorial",
        title="教程",
        content_brief="操作",
        materials=[{"material_id": "m", "media_id": "upload"}],
        options={"font_id": "font"},
    )
    with pytest.raises(AgentError):
        long_request_from_dto(dto, {})
    with pytest.raises(AgentError):
        long_request_from_dto(dto, {"upload": ImageSource(data=b"material")})
    request = long_request_from_dto(
        dto,
        {"upload": ImageSource(data=b"material")},
        font_bindings={"font": "C:/allowed/font.ttf"},
    )
    assert request.materials[0].source.data == b"material"
    assert request.options.font_path.as_posix() == "C:/allowed/font.ttf"
    for change in [
        {"output_dir": "private"},
        {"options": {"font_path": "private"}},
        {"run_dir": "private"},
    ]:
        with pytest.raises(ValidationError):
            LongImageRequestDTO(scene="tutorial", title="教程", content_brief="操作", **change)


def test_bundle_contains_no_local_paths():
    result = LongImageResult(
        run_id="run",
        run_dir="C:/private/run",
        status="failed",
        warnings=["input missing C:/private/material.png"],
        output_errors=[
            {"code": "output", "kind": "output", "message": "save /home/private/file.png failed"}
        ],
        image_assets=[
            {
                "slot_id": "pic",
                "source": "generate",
                "status": "pending_audit",
                "file_path": "C:/private/pic.png",
                "candidates": [
                    {
                        "candidate_id": "c1",
                        "status": "pending_audit",
                        "image": picture(),
                        "file_path": "C:/private/candidate.png",
                        "context": {"ledger_path": "C:/private/ledger"},
                    }
                ],
            }
        ],
        page={
            "artifact_id": "page",
            "kind": "page",
            "status": "failed",
            "availability": "absent",
            "layout": {
                "nested": {
                    "font_path": "C:/private/font.ttf",
                    "error": "open C:\\private\\font.ttf",
                }
            },
        },
    )
    bundle = long_result_to_bundle(result)
    encoded = bundle.dto.model_dump_json()
    assert "private" not in encoded and "file_path" not in encoded and "ledger_path" not in encoded
    candidate = bundle.dto.image_assets[0].candidates[0]
    assert candidate.status == "pending_audit" and candidate.blob_id in bundle.blobs
    assert candidate.sha256 and candidate.byte_length == len(bundle.blobs[candidate.blob_id])
    assert LongImageResultDTO.model_validate_json(encoded) == bundle.dto


def test_stale_page_is_not_current_blob():
    result = LongImageResult(
        run_id="run",
        status="failed",
        delivery_revision=3,
        page={
            "artifact_id": "page",
            "kind": "page",
            "status": "succeeded",
            "availability": "stale",
            "image": picture(),
        },
    )
    bundle = long_result_to_bundle(result)
    assert bundle.dto.page.availability == "stale" and bundle.dto.page.blob_id is None
    assert not bundle.blobs


def test_current_candidate_page_preserves_unaudited_status():
    result = LongImageResult(
        run_id="run",
        status="pending_audit",
        delivery_revision=3,
        page={
            "artifact_id": "page",
            "kind": "page",
            "status": "pending_audit",
            "availability": "current",
            "image": picture(),
        },
    )
    bundle = long_result_to_bundle(result)
    assert bundle.dto.page.status == "pending_audit"
    assert bundle.dto.page.blob_id == "long_image/run/3/page"


def test_planning_dto_exposes_only_opaque_record_and_unavailable_usage():
    result = LongImagePlanningResult(
        status="needs_input",
        planning_id="p",
        record={
            "record_id": "r",
            "planning_id": "p",
            "request_hash": "a",
            "plan_hash": "b",
            "local_record_ref": "C:/private/record.json",
            "ledger_ref": "C:/private/ledger.sqlite",
        },
    )
    dto = long_planning_to_dto(result)
    assert dto.record_id == "r" and dto.usage is None
    assert "private" not in dto.model_dump_json()
    with pytest.raises(ValidationError):
        LongImagePlanningResultDTO(status="ready", planning_id="p", trusted=True)


def test_four_schemas_have_resolvable_references():
    for model in [
        LongImageRequestDTO,
        LongImagePlanDTO,
        LongImagePlanningResultDTO,
        LongImageResultDTO,
    ]:
        schema = model.model_json_schema()
        assert schema["additionalProperties"] is False
        assert schema["properties"]["task_type"]["const"] == "long_image"

        def walk(value):
            if isinstance(value, dict):
                if "$ref" in value:
                    node = schema
                    for part in value["$ref"].removeprefix("#/").split("/"):
                        node = node[part]
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(json.loads(json.dumps(schema)))


def test_recursive_error_scrubbing_preserves_https_references():
    result = LongImageResult(
        status="failed",
        page_checks=[
            {
                "message": "could not open /var/private/image.png",
                "reference": "https://example.com/help/recovery",
                "context": {"nested": [{"config_path": "C:/private/config.json"}]},
            }
        ],
    )
    data = long_result_to_bundle(result).dto.page_checks[0]
    assert "private" not in str(data)
    assert data["reference"] == "https://example.com/help/recovery"


def test_schema_export_preserves_old_contracts_and_loads_four_new_schemas():
    root = Path(__file__).resolve().parents[1]
    old = {
        name: (root / "schema" / name).read_bytes()
        for name in (
            "creation-request-v1.json",
            "material-analysis-v1.json",
            "creation-result-v2.json",
            "creation-result-v1.json",
            "error-v1.json",
        )
    }
    subprocess.run(
        [sys.executable, "-X", "utf8", str(root / "scripts" / "export_schema.py")],
        cwd=root,
        check=True,
    )
    for name, data in old.items():
        assert (root / "schema" / name).read_bytes() == data
    for name in ("request", "plan", "planning-result", "result"):
        schema = json.loads(
            (root / "schema" / f"long-image-{name}-v1.json").read_text(encoding="utf-8")
        )
        assert schema["properties"]["schema_version"]["const"] == "1.0"
        assert schema["properties"]["task_type"]["const"] == "long_image"


def test_duplicate_blob_ids_are_rejected():
    result = LongImageResult(
        status="pending_audit",
        image_assets=[
            {
                "slot_id": "pic",
                "source": "generate",
                "status": "pending_audit",
                "candidates": [
                    {"candidate_id": "same", "image": picture()},
                    {"candidate_id": "same", "image": picture()},
                ],
            }
        ],
    )
    with pytest.raises(AgentError, match="duplicate"):
        long_result_to_bundle(result)


def test_candidate_selection_comes_from_asset_choice():
    result = LongImageResult(
        status="succeeded",
        image_assets=[
            {
                "slot_id": "pic",
                "source": "generate",
                "status": "succeeded",
                "selected_candidate_id": "c2",
                "candidates": [
                    {"candidate_id": "c1", "selected": True},
                    {"candidate_id": "c2", "selected": False},
                ],
            }
        ],
    )
    assert [c.selected for c in long_result_to_bundle(result).dto.image_assets[0].candidates] == [
        False,
        True,
    ]
    result.image_assets[0].selected_candidate_id = None
    assert not any(c.selected for c in long_result_to_bundle(result).dto.image_assets[0].candidates)


@pytest.mark.asyncio
@pytest.mark.parametrize("persisted", [False, True], ids=["memory", "disk"])
async def test_repaired_run_delivers_verified_candidate_history(tmp_path, persisted):
    from image_agent import AgentConfig, ExecutionPolicy
    from image_agent.long_image_runtime import accept_long_image_candidate, execute_long_image
    from image_agent.long_image_store import LongImageRunStore
    from tests.test_long_image_pipeline import Transport, deps_for, png, request_plan

    request, plan = request_plan()
    store = LongImageRunStore(tmp_path if persisted else None)

    class RepairTransport(Transport):
        async def post_json(self, path, payload):
            if path == "/images" and self.posts:
                self.image = png(800)
            return await super().post_json(path, payload)

    transport = RepairTransport(audits=[False, True])
    result = await execute_long_image(
        request,
        AgentConfig(),
        plan=plan,
        dependencies=deps_for(transport),
        policy=ExecutionPolicy(max_quality_repairs=1),
        store=store,
    )
    assert result.status == "succeeded"
    posts = len(transport.posts)
    assert posts == 4
    expected = [png(), png(800)]

    def check_bundle(value, selected):
        bundle = long_result_to_bundle(value)
        candidates = bundle.dto.image_assets[0].candidates
        assert len(candidates) == 2
        assert all(c.blob_id in bundle.blobs for c in candidates)
        assert len({c.blob_id for c in candidates}) == 2
        assert [c.selected for c in candidates] == selected
        for candidate, image in zip(candidates, expected, strict=True):
            assert bundle.blobs[candidate.blob_id] == image
            assert candidate.mime_type == "image/png"
            assert candidate.byte_length == len(image)
            assert candidate.sha256 == hashlib.sha256(image).hexdigest()
        encoded = bundle.dto.model_dump_json()
        assert "file_path" not in encoded and "candidate_payload" not in encoded
        assert str(tmp_path) not in encoded and "b64" not in encoded
        assert LongImageResultDTO.model_validate_json(encoded) == bundle.dto
        return candidates

    check_bundle(result, [False, True])
    loaded = (LongImageRunStore(tmp_path) if persisted else store).hydrate_result()
    check_bundle(loaded, [False, True])
    check_bundle(loaded.model_copy(deep=True), [False, True])
    # Local payloads never enter checkpoints/manifests or ordinary model dumps.
    for value in (
        result.model_dump(),
        result.model_dump(mode="json"),
        store.json("state.json"),
        store.json("result.json"),
    ):
        encoded = json.dumps(value)
        assert "candidate_payload" not in encoded and "b64" not in encoded
        assert '"image"' not in encoded and '"data"' not in encoded
    if persisted:
        accepted = await accept_long_image_candidate(
            tmp_path, "illustration", "c0001", reason="reviewed history"
        )
        candidates = check_bundle(accepted, [True, False])
        assert accepted.status == "accepted" and candidates[0].status == "accepted"
        assert candidates[0].acceptance["reason"] == "reviewed history"
        assert candidates[0].acceptance["at"]
        check_bundle(LongImageRunStore(tmp_path).hydrate_result(), [True, False])
    # A corrupt historical file invalidates the snapshot, including all cached
    # candidate and selected payloads; local hydration/export never submits POSTs.
    store.write(result.image_assets[0].candidates[1]["file_path"], b"corrupt history")
    damaged = store.hydrate_result()
    assert damaged.status == "failed" and damaged.output_errors
    assert damaged.page.availability == "stale"
    assert not long_result_to_bundle(damaged).blobs
    assert len(transport.posts) == posts
