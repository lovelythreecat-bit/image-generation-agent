import pytest
from pydantic import BaseModel, ValidationError, model_validator

from image_agent.desktop_runtime import build_request
from image_agent.errors import make_error_info


def test_validation_error_explains_incompatible_white_background(tmp_path):
    with pytest.raises(ValidationError) as caught:
        build_request(
            [tmp_path / "private.png"],
            product_name="cup",
            category="home",
            platforms=["taobao"],
            output_types=["detail_page", "pdd_white_background"],
        )
    info = make_error_info(caught.value)
    assert "white background requires pinduoduo" in info.message
    assert not info.message.startswith(":")
    assert "private.png" not in info.message
    assert info.kind == "validation" and not info.retryable


def test_validation_details_are_redacted_without_input_or_context():
    class Invalid(BaseModel):
        value: str

        @model_validator(mode="after")
        def invalid(self):
            raise ValueError("request rejected: token=private-secret C:/private/input.png")

    with pytest.raises(ValidationError) as caught:
        Invalid(value="private-input-value")
    message = make_error_info(caught.value).message
    assert "request rejected" in message
    assert "private-secret" not in message
    assert "C:/private" not in message
    assert "private-input-value" not in message


def test_field_validation_preserves_location_and_reason():
    class Invalid(BaseModel):
        count: int

    with pytest.raises(ValidationError) as caught:
        Invalid(count="private-invalid-input")
    message = make_error_info(caught.value).message
    assert "count:" in message and "valid integer" in message
    assert "private-invalid-input" not in message
