import json

import pytest
from pydantic import ValidationError

from image_agent.errors import AgentError, ProviderError
from image_agent.models import ErrorInfo, ImageSource, MaterialAnalysis
from tests.fakes import analysis_data
from tests.test_output import result


def dto_data():
    return dict(
        schema_version="1.0",
        product_name="cup",
        category="kitchen",
        platforms=["taobao"],
        output_types=["main_image"],
        materials=[dict(material_id="m1", media_id="upload1")],
    )


def test_request_binding_and_closed_schema():
    from image_agent.contracts import CreationRequestDTO, request_from_dto

    dto = CreationRequestDTO(**dto_data())
    request = request_from_dto(dto, {"upload1": ImageSource(data=b"abc")})
    assert request.materials[0].source.data == b"abc" and request.materials[0].material_id == "m1"
    with pytest.raises(AgentError):
        request_from_dto(dto, {})
    for change in [
        {"schema_version": "2.0"},
        {"output_dir": "C:/private"},
        {"product_image": {"path": "a"}},
    ]:
        with pytest.raises(ValidationError):
            CreationRequestDTO(**(dto_data() | change))
    schema = CreationRequestDTO.model_json_schema()
    assert schema["additionalProperties"] is False
    assert "materials" in schema["required"]


@pytest.mark.parametrize("status", ["succeeded", "partial", "failed", "needs_input"])
def test_result_blobs_roundtrip_no_paths(status):
    from image_agent.contracts import CreationResultDTO, result_to_bundle

    r = result()
    r.status = status
    r.assets[0].file_path = "C:/private/output.jpg"
    r.analysis = MaterialAnalysis(**analysis_data())
    if status in ("failed", "needs_input"):
        r.assets[0].status = "failed"
        r.assets[0].image = None
    bundle = result_to_bundle(r)
    encoded = json.dumps(bundle.dto.model_dump(mode="json"))
    assert "C:/private" not in encoded and "base64" not in encoded
    assert CreationResultDTO.model_validate_json(encoded) == bundle.dto
    if status in ("succeeded", "partial"):
        assert bundle.blobs["taobao.main_image.default"] == r.assets[0].image
        assert bundle.dto.assets[0].mime_type == "image/jpeg"
    else:
        assert not bundle.blobs and bundle.dto.assets[0].blob_id is None


def test_error_mapping_and_partial_preserved():
    from image_agent.contracts import CreationRequestDTO, error_to_dto, result_to_bundle

    error = error_to_dto(ProviderError("timeout", kind="transport"), "run")
    assert error.error.code == "provider_transport" and error.error.retryable
    r = result()
    r.status = "partial"
    r.assets[0].error_info = ErrorInfo(
        code="reference_capacity_exceeded", kind="capability", message="capacity"
    )
    r.output_errors = [ErrorInfo(code="output", kind="output", message="save")]
    assert result_to_bundle(r).dto.output_errors == r.output_errors
    assert result_to_bundle(r).dto.assets[0].error_info == r.assets[0].error_info
    try:
        CreationRequestDTO(**(dto_data() | {"schema_version": "secret-input-value"}))
    except ValidationError as e:
        text = error_to_dto(e).model_dump_json()
        assert "secret-input-value" not in text
