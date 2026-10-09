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
