from image_agent.models import AssetTarget, CreationRequest


def test_danger_market_and_platform_order(request_data):
    from image_agent.compliance import apply_compliance

    r = CreationRequest(**request_data)
    t = AssetTarget(
        platform="pinduoduo",
        output_type="main_image",
        aspect_ratio="3:4",
        presentation_mode="product_only",
    )
    assert apply_compliance(r, t, "weapon").blocked
    result = apply_compliance(r, t, "item 4")
    assert not result.blocked
    assert result.prompt.startswith("item 6")
    assert "fake platform badge" in result.prompt
    assert result.prompt.endswith("Target aspect ratio: 3:4.")


def test_structured_ids_not_market_rewritten(request_data):
    from image_agent.compliance import apply_compliance
    from image_agent.prompt import STRUCTURED_MARKER

    r = CreationRequest(**request_data)
    t = AssetTarget(
        platform="taobao",
        output_type="main_image",
        aspect_ratio="1:1",
        presentation_mode="product_only",
    )
    out = apply_compliance(
        r, t, "item 4" + STRUCTURED_MARKER + '{"material_id":"m4","element_id":"e4"}'
    )
    assert '"m4"' in out.prompt and '"e4"' in out.prompt
    assert out.prompt.startswith("item 6")
