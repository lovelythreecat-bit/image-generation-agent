import importlib.util
import io
from pathlib import Path

import pytest
from PIL import Image, ImageChops

from image_agent.errors import AgentError
from image_agent.long_image_models import LongImageOptions, LongImagePlan, LongSlotAsset

OPTIONS = LongImageOptions(font_path=Path("C:/Windows/Fonts/msyh.ttc"), slice_height=500)


def renderer():
    assert importlib.util.find_spec("image_agent.long_image_render"), "local renderer is missing"
    from image_agent import long_image_render

    return long_image_render


def unit(identifier, text, kind="body"):
    return {"unit_id": identifier, "text": text, "kind": kind}


def plan(sections=None, **changes):
    return LongImagePlan(
        scene="tutorial",
        input_fingerprint="offline",
        sections=sections
        or [
            {
                "section_id": "intro",
                "role": "hero",
                "text_units": [unit("title", "中文教程", "heading")],
            },
            {
                "section_id": "end",
                "role": "closing",
                "text_units": [unit("last", "完成后请保存。")],
            },
        ],
        **changes,
    )


def decode(data):
    with Image.open(io.BytesIO(data)) as image:
        image.load()
        return image.copy()


def intersects(a, b):
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def test_slices_reconstruct_page_pixels():
    rendered = renderer().render_long_layout(plan(), {}, OPTIONS)
    page = decode(rendered.page.image)
    rebuilt = Image.new("RGB", page.size)
    y = 0
    for artifact in rendered.slices:
        with decode(artifact.image) as image:
            assert image.height <= 500
            rebuilt.paste(image, (0, y))
            y += image.height
    assert y == page.height
    assert ImageChops.difference(page, rebuilt).getbbox() is None
    y = 0
    for artifact in rendered.sections:
        with decode(artifact.image) as image:
            assert (
                ImageChops.difference(
                    page.crop((0, y, page.width, y + image.height)), image
                ).getbbox()
                is None
            )
            y += image.height
    assert y == page.height


def test_table_and_step_text_not_clipped():
    sections = [
        {
            "section_id": "steps",
            "role": "steps",
            "text_units": [
                unit("step1", "第一步：打开设置，填写\n０９:30 和 １２ ml。"),
                unit("step2", "第二步：保存 " + "UnbrokenLongEnglishWord" * 8),
            ],
        },
        {
            "section_id": "table",
            "role": "table",
            "table_rows": [
                [
                    unit("a", "维度", "cell"),
                    unit("b", "对象甲", "cell"),
                    unit("c", "对象乙", "cell"),
                ],
                [
                    unit("d", "续航", "cell"),
                    unit("e", "１２ 小时\n完全保留", "cell"),
                    unit("f", "１８ 小时", "cell"),
                ],
            ],
        },
    ]
    rendered = renderer().render_long_layout(plan(sections), {}, OPTIONS)
    records = rendered.layout["text_units"]
    assert [r["unit_id"] for r in records] == ["step1", "step2", "a", "b", "c", "d", "e", "f"]
    for index, record in enumerate(records):
        x0, y0, x1, y1 = record["bbox"]
        assert 0 <= x0 < x1 <= 750
        assert 0 <= y0 < y1 <= rendered.page.height
        assert record["font_size"] >= 26
        for line in record["lines"]:
            box = line["bbox"]
            assert x0 <= box[0] <= box[2] <= x1
            assert y0 <= box[1] < box[3] <= y1
        for other in records[index + 1 :]:
            assert not intersects(record["bbox"], other["bbox"])


@pytest.mark.parametrize("role", ["hero", "image_text", "bullets", "steps", "table", "closing"])
def test_six_roles_and_zero_generated_slots_render_successfully(role):
    page_plan = plan(
        [
            {
                "section_id": "first",
                "role": role,
                "text_units": [unit("first", "文字内容")],
                **(
                    {"table_rows": [[unit("left", "参数", "cell"), unit("right", "数值", "cell")]]}
                    if role == "table"
                    else {}
                ),
            },
            {"section_id": "last", "role": "closing", "text_units": [unit("last", "结束")]},
        ]
    )
    rendered = renderer().render_long_layout(page_plan, {}, OPTIONS)
    assert rendered.page.image.startswith(b"\x89PNG")
    assert all(check["passed"] for check in rendered.checks)
    assert len(rendered.sections) == 2


def test_multilingual_and_newlines_preserve_source():
    text = "  发布 🚀 ０９:30\n\n１２ ml " + "ABCDEFGHIJKLMNOPQRSTUVWXYZ" * 3 + "\r\n尾行\n"
    page_plan = plan(
        [
            {"section_id": "first", "role": "hero", "text_units": [unit("original", text)]},
            {"section_id": "last", "role": "closing", "text_units": [unit("last", "完")]},
        ]
    )
    rendered = renderer().render_long_layout(page_plan, {}, OPTIONS)
    record = rendered.layout["text_units"][0]
    assert record["text"] == text
    offsets = [(line["start"], line["end"]) for line in record["lines"]]
    assert offsets[0][0] == 0 and offsets[-1][1] == len(text)
    assert all(left[1] == right[0] for left, right in zip(offsets, offsets[1:]))
    assert "".join(text[start:end] for start, end in offsets) == text
    assert len(record["lines"]) >= 6


def test_height_limit_does_not_truncate(monkeypatch):
    page_plan = plan(
        [
            {"section_id": "big", "role": "hero", "text_units": [unit("big", "正文\n" * 600)]},
            {"section_id": "end", "role": "closing", "text_units": [unit("end", "结束")]},
        ]
    )
    allocations = []
    original = Image.new

    def allocate(mode, size, *args, **kwargs):
        allocations.append(size)
        return original(mode, size, *args, **kwargs)

    monkeypatch.setattr(Image, "new", allocate)
    with pytest.raises(AgentError) as caught:
        renderer().render_long_layout(page_plan, {}, OPTIONS)
    assert caught.value.code == "layout_height_limit"
    assert not any(height > 16000 for _, height in allocations)


def test_source_slot_preserves_entire_native_image_ratio():
    image = Image.new("RGB", (100, 200), "red")
    image.putpixel((0, 0), (0, 255, 0))
    output = io.BytesIO()
    image.save(output, "PNG")
    page_plan = plan(
        [
            {"section_id": "first", "role": "image_text", "slot_ids": ["source"]},
            {"section_id": "last", "role": "closing", "text_units": [unit("last", "完")]},
        ],
        image_slots=[
            {
                "slot_id": "source",
                "source": "source",
                "material_ids": ["material"],
                "role": "原图",
                "section_ids": ["first"],
            }
        ],
    )
    rendered = renderer().render_long_layout(
        page_plan,
        {
            "source": LongSlotAsset(
                slot_id="source",
                source="source",
                source_status="available",
                image=output.getvalue(),
            )
        },
        OPTIONS,
    )
    box = rendered.layout["images"][0]["bbox"]
    assert (box[2] - box[0]) * 2 == box[3] - box[1]
    with decode(rendered.page.image) as page:
        assert page.getpixel((box[0], box[1])) == (0, 255, 0)


def test_missing_or_invalid_required_image_blocks_render():
    page_plan = plan(
        [
            {"section_id": "first", "role": "image_text", "slot_ids": ["source"]},
            {"section_id": "last", "role": "closing", "text_units": [unit("last", "完")]},
        ],
        image_slots=[
            {
                "slot_id": "source",
                "source": "source",
                "material_ids": ["material"],
                "role": "原图",
                "section_ids": ["first"],
            }
        ],
    )
    for assets in [
        {},
        {
            "source": LongSlotAsset(
                slot_id="source", source="source", source_status="available", image=b"bad"
            )
        },
    ]:
        with pytest.raises(AgentError):
            renderer().render_long_layout(page_plan, assets, OPTIONS)


def test_empty_section_and_impossibly_narrow_table_fail():
    for page_plan in [
        plan([{"section_id": "a", "role": "hero"}, {"section_id": "b", "role": "closing"}]),
        plan(
            [
                {
                    "section_id": "a",
                    "role": "table",
                    "table_rows": [[unit(f"c{i}", "字", "cell") for i in range(30)]],
                },
                {"section_id": "b", "role": "closing", "text_units": [unit("end", "完")]},
            ]
        ),
    ]:
        with pytest.raises(AgentError) as caught:
            renderer().render_long_layout(page_plan, {}, OPTIONS)
        assert caught.value.code in {"layout_empty_section", "layout_table_width"}


@pytest.mark.parametrize("theme", ["auto", "clean", "warm", "premium"])
def test_theme_readable_and_fingerprint_changes_with_metrics(theme):
    page_plan = plan()
    rendered = renderer().render_long_layout(
        page_plan, {}, OPTIONS.model_copy(update={"theme": theme})
    )
    metrics = rendered.layout["theme"]

    def luminance(hex_color):
        channels = [int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        linear = [v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4 for v in channels]
        return sum(a * b for a, b in zip(linear, [0.2126, 0.7152, 0.0722]))

    values = sorted([luminance(metrics["foreground"]), luminance(metrics["background"])])
    assert (values[1] + 0.05) / (values[0] + 0.05) >= 4.5
    changed = renderer().render_long_layout(
        page_plan, {}, OPTIONS.model_copy(update={"width": 800, "theme": theme})
    )
    assert changed.render_fingerprint != rendered.render_fingerprint


def test_maximum_width_and_near_limit_height_use_one_large_raster(monkeypatch):
    page_plan = plan(
        [
            {"section_id": "big", "role": "hero", "text_units": [unit("big", "正文\n" * 59)]},
            {"section_id": "end", "role": "closing", "text_units": [unit("end", "结束\n" * 58)]},
        ]
    )
    large_allocations = []
    crop_heights = []
    original_new, original_crop = Image.new, Image.Image.crop

    def allocate(mode, size, *args, **kwargs):
        if size[0] * size[1] > 2400 * 4000:
            large_allocations.append(size)
        return original_new(mode, size, *args, **kwargs)

    def crop(image, box=None):
        crop_heights.append(box[3] - box[1])
        return original_crop(image, box)

    monkeypatch.setattr(Image, "new", allocate)
    monkeypatch.setattr(Image.Image, "crop", crop)
    rendered = renderer().render_long_layout(
        page_plan, {}, OPTIONS.model_copy(update={"width": 2400})
    )
    assert rendered.page.width == 2400
    assert 15000 <= rendered.page.height <= 16000
    assert large_allocations == [(2400, rendered.page.height)]
    assert max(crop_heights) <= 64


def test_required_copy_ids_are_verified_after_layout():
    page_plan = plan()
    page_plan.requirements = [
        {
            "requirement_id": "title",
            "kind": "page_text",
            "text": "中文教程",
            "source": {"pointer": "/title"},
            "order": 0,
        },
    ]
    page_plan = LongImagePlan.model_validate(page_plan.model_dump())
    with pytest.raises(AgentError) as caught:
        renderer().render_long_layout(page_plan, {}, OPTIONS)
    assert caught.value.code == "plan_invalid"


def test_fingerprint_uses_actual_font_bytes(tmp_path):
    from fontTools.ttLib import TTFont

    from image_agent.long_image_fonts import resolve_fonts

    selected = tmp_path / "font.ttf"
    with TTFont("C:/Windows/Fonts/msyh.ttc", fontNumber=0) as font:
        font.save(selected)
    page_plan = plan()
    options = OPTIONS.model_copy(update={"font_path": selected})
    first = renderer().render_fingerprint(page_plan, {}, options, resolve_fonts(["中文"], selected))
    with TTFont(selected) as font:
        font["head"].fontRevision += 0.01
        font.save(selected)
    second = renderer().render_fingerprint(
        page_plan, {}, options, resolve_fonts(["中文"], selected)
    )
    assert first != second


def test_source_image_scaling_uses_bounded_output_stripes(monkeypatch):
    data = io.BytesIO()
    with Image.new("RGB", (800, 900), "red") as image:
        image.save(data, "PNG")
    page_plan = plan(
        [
            {"section_id": "first", "role": "image_text", "slot_ids": ["source"]},
            {"section_id": "last", "role": "closing", "text_units": [unit("last", "完")]},
        ],
        image_slots=[
            {
                "slot_id": "source",
                "source": "source",
                "material_ids": ["material"],
                "role": "原图",
                "section_ids": ["first"],
            }
        ],
    )
    resized_heights = []
    original = Image.Image.resize

    def resize(image, size, *args, **kwargs):
        resized_heights.append(size[1])
        return original(image, size, *args, **kwargs)

    monkeypatch.setattr(Image.Image, "resize", resize)
    rendered = renderer().render_long_layout(
        page_plan,
        {
            "source": LongSlotAsset(
                slot_id="source", source="source", source_status="available", image=data.getvalue()
            ),
        },
        OPTIONS,
    )
    assert rendered.page.image
    assert resized_heights and max(resized_heights) <= 64


def test_layout_guard_rejects_out_of_bounds_measurement(monkeypatch):
    module = renderer()
    original = module._Typography.text_record

    def bad_measurement(typography, *args, **kwargs):
        record = original(typography, *args, **kwargs)
        record["lines"][0]["bbox"][2] = 751
        return record

    monkeypatch.setattr(module._Typography, "text_record", bad_measurement)
    with pytest.raises(AgentError) as caught:
        module.render_long_layout(plan(), {}, OPTIONS)
    assert caught.value.code == "layout_text_bounds"


def test_artifacts_carry_layout_records_for_persisted_delivery():
    rendered = renderer().render_long_layout(plan(), {}, OPTIONS)
    assert [record["unit_id"] for record in rendered.page.layout["text_units"]] == ["title", "last"]
    assert [record["unit_id"] for record in rendered.sections[0].layout["text_units"]] == ["title"]
    assert rendered.page.layout["text_units"] == rendered.layout["text_units"]
