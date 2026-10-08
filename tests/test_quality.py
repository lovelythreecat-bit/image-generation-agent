import pytest

from image_agent.models import AssetElementPlan, AssetTarget, GeneratedImageAudit, PixelChecks
from tests.test_images import picture


def target(platform="taobao", output="main_image"):
    return AssetTarget(
        platform=platform, output_type=output, aspect_ratio="1:1", presentation_mode="product_only"
    )


def plan():
    return AssetElementPlan(
        target_key="taobao.main_image.default",
        subject_ids=["s1"],
        requirements=[
            dict(
                target_kind="subject",
                target_id="s1",
                origin="default_identity",
                applicability="must_show",
                reason="identity",
            )
        ],
    )


def audit(score=90, same=True):
    return GeneratedImageAudit(
        subject_checks=[dict(subject_id="s1", score=score, same_product=same, reason="ok")],
        fact_checks=[],
        element_checks=[],
        constraint_checks=[],
        visual_quality=90,
        platform_compliance=90,
        garment_fusion=90,
        model_preference=90,
        output_intent=90,
        passed=True,
        reason="ok",
    )


@pytest.mark.parametrize(
    "length,size,model,want",
    [
        (1536, "2K", "other", True),
        (1535, "2K", "other", False),
        (768, "4K", "openai/gpt-image-2", True),
    ],
)
def test_long_edge_boundaries(length, size, model, want):
    from image_agent.quality import check_pixels

    assert check_pixels(picture((length, length)), target(), size, model).passed is want


def test_white_export_exception_and_border_threshold():
    from image_agent.quality import check_pixels, export_white_background

    white = target("pinduoduo", "pdd_white_background")
    data = export_white_background(picture((1536, 1000)))
    check = check_pixels(data, white, "4K", "other")
    assert check.passed and (check.width, check.height) == (480, 480)
    assert check_pixels(picture(color=(250, 250, 250)), target("amazon"), "1K", "other").passed
    assert not check_pixels(picture(color=(249, 249, 249)), target("amazon"), "1K", "other").passed


@pytest.mark.parametrize(
    "score,same,want", [(85, True, True), (84, True, False), (95, False, False)]
)
def test_identity_not_score_alone(score, same, want):
    from image_agent.quality import choose_retry, evaluate_quality

    q = evaluate_quality(
        audit(score, same),
        PixelChecks(passed=True, width=800, height=800, reason="ok"),
        target(),
        plan(),
    )
    assert q.passed is want
    if not want:
        assert choose_retry(target(), q) == "staged"
        assert choose_retry(target("amazon"), q) == "strict"


def test_missing_and_unknown_required_checks_are_protocol():
    from image_agent.errors import ProviderError
    from image_agent.quality import evaluate_quality

    pixels = PixelChecks(passed=True, width=800, height=800, reason="ok")
    for checks in [
        [],
        [audit().subject_checks[0].model_copy(update={"subject_id": "s2"})],
    ]:
        with pytest.raises(ProviderError):
            evaluate_quality(
                audit().model_copy(update={"subject_checks": checks}), pixels, target(), plan()
            )
