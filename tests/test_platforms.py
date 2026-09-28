def test_platform_ratios_and_rules():
    from image_agent.platforms import SUPPORTED_PLATFORMS, aspect_ratio_for, generation_instruction

    assert len(SUPPORTED_PLATFORMS) == 9
    assert aspect_ratio_for("temu", "detail_page") == "1:1"
    assert aspect_ratio_for("shein", "detail_page") == "3:4"
    assert "pure white" in generation_instruction("amazon", "main_image")
