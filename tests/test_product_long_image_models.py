import pytest
from pydantic import ValidationError

from image_agent.contracts import CreationRequestDTO, request_from_dto
from image_agent.models import CreationRequest
from image_agent.prompt import build_targets


def test_long_output_defaults_and_mixed_targets(request_data):
    request = CreationRequest(**(request_data | {"output_types": ["long_image"]}))
    assert request.long_image.model_dump() == dict(
        width=750, segment_count=6, segment_aspect_ratio="3:4", known_info="", required_copy=[]
    )
    assert build_targets(request, "product_only")[0].target_key == "taobao.long_image.default"
    mixed = CreationRequest(
        **(
            request_data
            | {
                "platforms": ["taobao", "pinduoduo"],
                "output_types": ["main_image", "detail_page", "pdd_white_background", "long_image"],
                "aspect_ratio": "1:1",
            }
        )
    )
    targets = build_targets(mixed, "product_only")
    assert len(targets) == 11
    assert [t.aspect_ratio for t in targets if t.output_type == "long_image"] == ["3:4", "3:4"]


@pytest.mark.parametrize(
    "options",
    [
        {"width": 399},
        {"width": 2401},
        {"width": True},
        {"segment_count": 2},
        {"segment_count": 13},
        {"segment_count": False},
        {"segment_aspect_ratio": "1:1"},
        {"known_info": "x" * 12001},
        {"required_copy": ["x"] * 31},
        {"required_copy": ["  \n"]},
        {"required_copy": ["x" * 501]},
    ],
)
def test_long_options_reject_invalid(request_data, options):
    with pytest.raises(ValidationError):
        CreationRequest(**(request_data | {"output_types": ["long_image"], "long_image": options}))


def test_long_copy_preserves_codepoints_and_endpoints(request_data):
    text = "  超强99.9%\n原文  "
    for width, count in [(400, 3), (2400, 12)]:
        request = CreationRequest(
            **(
                request_data
                | {
                    "output_types": ["long_image"],
                    "long_image": dict(width=width, segment_count=count, required_copy=[text]),
                }
            )
        )
        assert request.long_image.required_copy == [text]


def test_long_dto_binding_and_legacy_requests(request_data):
    source = CreationRequest(**request_data).materials[0].source
    data = request_data | {"materials": [{"material_id": "m1", "media_id": "photo"}]}
    assert request_from_dto(CreationRequestDTO(**data), {"photo": source}).long_image is None
    dto = CreationRequestDTO(
        **(data | {"output_types": ["long_image"], "long_image": {"known_info": "蓝色"}})
    )
    assert dto.long_image.width == 750
    assert request_from_dto(dto, {"photo": source}).long_image.known_info == "蓝色"
