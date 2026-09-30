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
    assert result.prompt.startswith("item 4")
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
    assert out.prompt.startswith("item 4")


def test_compliance_preserves_original_numbers_and_product_artwork(request_data):
    from image_agent.compliance import apply_compliance
    from image_agent.prompt import build_targets

    for market in ("CN", "VN", "PH"):
        request = CreationRequest(**(request_data | {"market": market}))
        prompt = apply_compliance(
            request, build_targets(request, "product_only")[0], "Model 13, size 4, 48-hour label"
        ).prompt
        assert prompt.startswith("Model 13, size 4, 48-hour label")
        assert "Preserve physical product labels" in prompt
        assert "artwork" in prompt
        assert "promotional overlays" in prompt
        assert "any text" not in prompt
