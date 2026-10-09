"""Measured local text/image layout, bounded raster memory and lossless PNG delivery."""

import hashlib
import io
import json
import math
import struct
import zlib

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError
from pydantic import ValidationError

from .errors import AgentError
from .long_image_fonts import FontSet, resolve_fonts
from .long_image_models import (
    LongImageArtifact,
    LongImageOptions,
    LongImagePlan,
    LongSlotAsset,
    RenderedLongImage,
    section_text_units,
)

RENDERER_VERSION = "1.0"
MAX_HEIGHT = 16000
MAX_PIXELS = 38400000
_PALETTES = {
    "clean": {
        "background": "#ffffff",
        "foreground": "#202a35",
        "accent": "#174d3e",
        "table_background": "#f1f4f6",
        "border": "#d4dce2",
    },
    "warm": {
        "background": "#fff7ed",
        "foreground": "#392619",
        "accent": "#873b16",
        "table_background": "#f7e7d4",
        "border": "#dbc4ab",
    },
    "premium": {
        "background": "#151a22",
        "foreground": "#f5f0e8",
        "accent": "#e9bd71",
        "table_background": "#242c38",
        "border": "#626a78",
    },
}


def _theme(plan: LongImagePlan, options: LongImageOptions) -> dict:
    name = options.theme if options.theme != "auto" else plan.theme
    if name == "auto":
        direction = plan.visual_direction.lower()
        if any(word in direction for word in ("premium", "高级", "黑金")):
            name = "premium"
        elif any(word in direction for word in ("warm", "温暖", "暖色")):
            name = "warm"
        else:
            name = "warm" if plan.scene in {"brand_intro", "promotion"} else "clean"
    scale = options.width / 750
    return {
        "name": name,
        **_PALETTES[name],
        "body_size": max(14, round(26 * scale)),
        "heading_size": max(22, round(40 * scale)),
        "hero_size": max(28, round(54 * scale)),
        "margin": max(18, round(36 * scale)),
        "padding": max(8, round(16 * scale)),
        "line_gap": max(4, round(8 * scale)),
        "unit_gap": max(8, round(18 * scale)),
        "section_gap": max(12, round(36 * scale)),
    }


def render_fingerprint(
    plan: LongImagePlan,
    assets: dict[str, LongSlotAsset],
    options: LongImageOptions,
    fonts: FontSet,
) -> str:
    data = {
        "renderer_version": RENDERER_VERSION,
        "plan": plan.model_dump(mode="json"),
        "assets": {
            slot.slot_id: hashlib.sha256(assets[slot.slot_id].image).hexdigest()
            for slot in plan.image_slots
            if slot.slot_id in assets and assets[slot.slot_id].image
        },
        "theme": _theme(plan, options),
        "width": options.width,
        "slice_height": options.slice_height,
        "fonts": fonts.fingerprint_data(),
    }
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class _Typography:
    def __init__(self, fonts: FontSet, line_gap: int):
        self.fonts = fonts
        self.line_gap = line_gap
        self.cache = {}

    def glyph(self, char, size):
        key = char, size
        if key not in self.cache:
            text = "    " if char == "\t" else char
            face = self.fonts.face_for(" " if char == "\t" else char)
            font = self.fonts.font(face, size)
            left, top, right, bottom = font.getbbox(text, anchor="ls")
            shift = max(0, -left)
            advance = max(float(font.getlength(text)) + shift, right + shift)
            self.cache[key] = (face, text, advance, shift, top, bottom)
        return self.cache[key]

    def wrap(self, text, size, width):
        if width <= 0:
            raise AgentError("Text has no available width", code="layout_table_width")
        lines = []
        start = position = 0
        used = 0.0
        while position < len(text):
            char = text[position]
            if char in "\r\n":
                end = position + (2 if text[position : position + 2] == "\r\n" else 1)
                lines.append((start, end, used))
                start = position = end
                used = 0.0
                continue
            advance = self.glyph(char, size)[2]
            if advance > width:
                raise AgentError(
                    "A character exceeds the available column width", code="layout_table_width"
                )
            if used + advance > width and position > start:
                lines.append((start, position, used))
                start = position
                used = 0.0
            used += advance
            position += 1
        # A trailing explicit newline retains the final empty line.
        if start < len(text) or not lines or text.endswith(("\r", "\n")):
            lines.append((start, len(text), used))
        return lines

    def text_record(self, unit, size, x, y, width, section_id):
        lines = []
        cursor = y
        for start, end, used in self.wrap(unit.text, size, width):
            chars = [self.glyph(char, size) for char in unit.text[start:end] if char not in "\r\n"]
            ascent = max([size, *(-glyph[4] for glyph in chars)])
            descent = max([math.ceil(size * 0.25), *(glyph[5] for glyph in chars)])
            line_height = ascent + descent
            lines.append(
                {
                    "start": start,
                    "end": end,
                    "text": unit.text[start:end],
                    "bbox": [x, cursor, x + math.ceil(used), cursor + line_height],
                    "baseline": cursor + ascent,
                }
            )
            cursor += line_height + self.line_gap
        return {
            "unit_id": unit.unit_id,
            "section_id": section_id,
            "text": unit.text,
            "source": unit.source.model_dump(),
            "requirement_ids": list(unit.requirement_ids),
            "font_size": size,
            "bbox": [x, y, x + width, cursor - self.line_gap],
            "lines": lines,
        }

    def draw(self, draw, record, color):
        size = record["font_size"]
        for line in record["lines"]:
            x = line["bbox"][0]
            for char in line["text"]:
                if char in "\r\n":
                    continue
                face, text, advance, shift, _, _ = self.glyph(char, size)
                draw.text(
                    (x + shift, line["baseline"]),
                    text,
                    font=self.fonts.font(face, size),
                    fill=color,
                    anchor="ls",
                )
                x += advance


def _image_size(data):
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if image.getexif().get(274) in (5, 6, 7, 8):
                width, height = height, width
        # getexif() may load a PNG and close its stream; verification needs a
        # newly opened handle. Verification is independent of the metadata read.
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        return width, height
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError):
        raise AgentError("Required image cannot be decoded", code="layout_invalid_image") from None


def _validate_requirements(plan, records):
    by_requirement = {}
    for index, record in enumerate(records):
        if len(record["requirement_ids"]) > 1:
            raise AgentError(
                "One text unit cannot satisfy several occurrences", code="plan_invalid"
            )
        for requirement_id in record["requirement_ids"]:
            by_requirement.setdefault(requirement_id, []).append((index, record))
    ordered = []
    for requirement in plan.requirements:
        if requirement.kind == "slot_visual":
            continue
        assigned = by_requirement.get(requirement.requirement_id, [])
        if requirement.required and len(assigned) != requirement.occurrences:
            raise AgentError("Required text occurrences were not drawn", code="plan_invalid")
        for index, record in assigned:
            if (
                record["text"] != requirement.text
                or record["source"] != requirement.source.model_dump()
            ):
                raise AgentError(
                    "Rendered copy differs from the required source", code="plan_invalid"
                )
            if requirement.section_ids and record["section_id"] not in requirement.section_ids:
                raise AgentError("Required copy appears in the wrong section", code="plan_invalid")
            if requirement.order is not None:
                ordered.append((index, requirement.order))
    if [order for _, order in sorted(ordered)] != sorted(order for _, order in ordered):
        raise AgentError("Required copy reading order is invalid", code="plan_invalid")


def _measure(plan, assets, options, theme, typography):
    margin = theme["margin"]
    content_width = options.width - 2 * margin
    y = 0
    sections, records, images, rows, markers = [], [], [], [], []
    for section in plan.sections:
        if not section.text_units and not section.table_rows and not section.slot_ids:
            raise AgentError("Cannot render an empty section", code="layout_empty_section")
        top = y
        y += theme["section_gap"]
        if section.role == "hero":
            y += theme["padding"]
        step = 0
        for unit in section.text_units:
            size = theme["body_size"]
            if unit.kind == "heading":
                size = theme["hero_size"] if section.role == "hero" else theme["heading_size"]
            indent = 0
            marker = None
            if unit.kind != "heading" and (
                unit.kind == "bullet" or section.role in {"bullets", "steps"}
            ):
                step += 1
                marker = f"{step}." if section.role == "steps" else "•"
                marker_width = sum(typography.glyph(char, size)[2] for char in marker)
                indent = math.ceil(marker_width) + theme["padding"]
            record = typography.text_record(
                unit, size, margin + indent, y, content_width - indent, section.section_id
            )
            records.append(record)
            if marker:
                markers.append(
                    {
                        "text": marker,
                        "font_size": size,
                        "x": margin,
                        "baseline": record["lines"][0]["baseline"],
                    }
                )
            y = record["bbox"][3] + theme["unit_gap"]
        if section.table_rows:
            columns = len(section.table_rows[0])
            edges = [margin + content_width * column // columns for column in range(columns + 1)]
            padding = theme["padding"]
            if min(right - left for left, right in zip(edges, edges[1:])) <= 2 * padding:
                raise AgentError(
                    "Table columns cannot fit readable text", code="layout_table_width"
                )
            for row_index, row in enumerate(section.table_rows):
                row_records = [
                    typography.text_record(
                        cell,
                        theme["body_size"],
                        edges[column] + padding,
                        y + padding,
                        edges[column + 1] - edges[column] - 2 * padding,
                        section.section_id,
                    )
                    for column, cell in enumerate(row)
                ]
                bottom = max(record["bbox"][3] for record in row_records) + padding
                rows.append(
                    {
                        "bbox": [margin, y, options.width - margin, bottom],
                        "edges": edges,
                        "header": row_index == 0,
                    }
                )
                records.extend(row_records)
                y = bottom
            y += theme["unit_gap"]
        for slot_id in section.slot_ids:
            slot = next(slot for slot in plan.image_slots if slot.slot_id == slot_id)
            asset = assets.get(slot_id)
            if (
                asset is None
                or not asset.image
                or asset.slot_id != slot_id
                or asset.source != slot.source
            ):
                raise AgentError("Required image is missing", code="layout_missing_image")
            if asset.source == "source" and asset.source_status != "available":
                raise AgentError("Source image is unavailable", code="layout_missing_image")
            native_width, native_height = _image_size(asset.image)
            # Source pixels preserve their native ratio; generated aspect ratios are
            # validated by the generation pipeline, and rendering never crops either.
            height = max(1, round(content_width * native_height / native_width))
            images.append(
                {
                    "slot_id": slot_id,
                    "section_id": section.section_id,
                    "native_size": [native_width, native_height],
                    "bbox": [margin, y, options.width - margin, y + height],
                }
            )
            y += height + theme["unit_gap"]
        y += theme["section_gap"] - theme["unit_gap"]
        if y > MAX_HEIGHT:
            raise AgentError(
                f"Page requires {y} pixels; maximum is {MAX_HEIGHT}", code="layout_height_limit"
            )
        if options.width * y > MAX_PIXELS:
            raise AgentError("Page exceeds the total pixel limit", code="layout_pixel_limit")
        sections.append(
            {
                "section_id": section.section_id,
                "role": section.role,
                "slot_ids": list(section.slot_ids),
                "bbox": [0, top, options.width, y],
            }
        )
    _validate_requirements(plan, records)
    return {
        "width": options.width,
        "height": y,
        "theme": theme,
        "sections": sections,
        "text_units": records,
        "images": images,
        "table_rows": rows,
        "markers": markers,
    }


def _chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _layout_checks(plan, layout):
    width, height = layout["width"], layout["height"]
    expected = [unit.unit_id for section in plan.sections for unit in section_text_units(section)]
    records = layout["text_units"]
    if [record["unit_id"] for record in records] != expected:
        raise AgentError(
            "Text units are missing or out of reading order", code="layout_text_coverage"
        )
    boxes = []
    for record in records:
        x0, y0, x1, y1 = record["bbox"]
        if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
            raise AgentError("Text region is outside the page", code="layout_text_bounds")
        position = 0
        for line in record["lines"]:
            a, b, c, d = line["bbox"]
            if not (x0 <= a <= c <= x1 and y0 <= b < d <= y1):
                raise AgentError("Text line is outside its region", code="layout_text_bounds")
            if (
                line["start"] != position
                or record["text"][line["start"] : line["end"]] != line["text"]
            ):
                raise AgentError("Text line lost source codepoints", code="layout_text_coverage")
            position = line["end"]
        if position != len(record["text"]):
            raise AgentError("Text was truncated", code="layout_text_coverage")
        boxes.append(record["bbox"])
    for image in layout["images"]:
        a, b, c, d = image["bbox"]
        if not (0 <= a < c <= width and 0 <= b < d <= height):
            raise AgentError("Image region is outside the page", code="layout_image_bounds")
        boxes.append(image["bbox"])
    for index, (a, b, c, d) in enumerate(boxes):
        for x0, y0, x1, y1 in boxes[index + 1 :]:
            if a < x1 and x0 < c and b < y1 and y0 < d:
                raise AgentError("Text and image regions overlap", code="layout_overlap")
    return [
        {"kind": "text_coverage", "passed": True, "count": len(records)},
        {"kind": "text_bounds", "passed": True, "count": sum(len(r["lines"]) for r in records)},
        {"kind": "region_overlap", "passed": True, "count": len(boxes)},
        {"kind": "nonempty_sections", "passed": True, "count": len(layout["sections"])},
        {"kind": "required_images", "passed": True, "count": len(layout["images"])},
        {"kind": "table_columns", "passed": True, "count": len(layout["table_rows"])},
        {"kind": "page_dimensions", "passed": True, "width": width, "height": height},
    ]


def _png_region(page, top, bottom):
    """Encode a vertical region using <=64 raster rows, never a whole-page copy."""
    output = io.BytesIO()
    output.write(b"\x89PNG\r\n\x1a\n")
    output.write(_chunk(b"IHDR", struct.pack(">IIBBBBB", page.width, bottom - top, 8, 2, 0, 0, 0)))
    compressor = zlib.compressobj()
    stride = page.width * 3
    for y in range(top, bottom, 64):
        with page.crop((0, y, page.width, min(y + 64, bottom))) as stripe:
            pixels = stripe.tobytes()
        for start in range(0, len(pixels), stride):
            compressed = compressor.compress(b"\x00" + pixels[start : start + stride])
            if compressed:
                output.write(_chunk(b"IDAT", compressed))
    output.write(_chunk(b"IDAT", compressor.flush()))
    output.write(_chunk(b"IEND", b""))
    return output.getvalue()


def _artifact(identifier, kind, image, width, height, *, dependencies=(), layout=None):
    # Delivery status intentionally stays unaggregated; T5 derives it from assets/checks.
    return LongImageArtifact(
        artifact_id=identifier,
        kind=kind,
        image=image,
        width=width,
        height=height,
        sha256=hashlib.sha256(image).hexdigest(),
        dependencies=list(dependencies),
        layout=layout or {},
    )


def _paint_image(page, item, data):
    x0, y0, x1, y1 = item["bbox"]
    width, height = x1 - x0, y1 - y0
    try:
        with Image.open(io.BytesIO(data)) as source:
            orientation = source.getexif().get(274)
            target = (height, width) if orientation in (5, 6, 7, 8) else (width, height)
            # JPEG draft decoding may reduce the source raster before loading it.
            source.draft("RGB", target)
            ImageOps.exif_transpose(source, in_place=True)
            for top in range(0, height, 64):
                bottom = min(height, top + 64)
                source_box = (
                    0,
                    top * source.height / height,
                    source.width,
                    bottom * source.height / height,
                )
                # Resample from the complete source coordinate system into a
                # small output stripe, without a full-size RGBA/resize copy.
                with source.resize(
                    (width, bottom - top), Image.Resampling.LANCZOS, box=source_box
                ) as stripe:
                    if stripe.mode == "RGB":
                        page.paste(stripe, (x0, y0 + top))
                    else:
                        with stripe.convert("RGBA") as rgba:
                            page.paste(rgba, (x0, y0 + top), rgba)
    except (OSError, ValueError, Image.DecompressionBombError):
        raise AgentError("Required image cannot be rendered", code="layout_invalid_image") from None


def render_long_layout(
    plan: LongImagePlan,
    selected_assets: dict[str, LongSlotAsset],
    options: LongImageOptions,
) -> RenderedLongImage:
    try:
        options = LongImageOptions.model_validate(options.model_dump())
        plan = LongImagePlan.model_validate(plan.model_dump())
    except ValidationError:
        raise AgentError("Invalid local plan or render options", code="plan_invalid") from None
    texts = [unit.text for section in plan.sections for unit in section_text_units(section)]
    fonts = resolve_fonts([*texts, "•0123456789. "], options.font_path)
    theme = _theme(plan, options)
    typography = _Typography(fonts, theme["line_gap"])
    layout = _measure(plan, selected_assets, options, theme, typography)
    checks = _layout_checks(plan, layout)
    fingerprint = render_fingerprint(plan, selected_assets, options, fonts)
    width, height = options.width, layout["height"]
    sections, slices = [], []
    with Image.new("RGB", (width, height), theme["background"]) as page:
        draw = ImageDraw.Draw(page)
        for section in layout["sections"]:
            if section["role"] in {"hero", "closing"}:
                x = theme["margin"]
                y = section["bbox"][1] + theme["padding"]
                draw.rectangle(
                    (x, y, width - x - 1, y + max(2, theme["padding"] // 4)), fill=theme["accent"]
                )
        for row in layout["table_rows"]:
            x0, y0, x1, y1 = row["bbox"]
            draw.rectangle(
                (x0, y0, x1 - 1, y1 - 1),
                fill=theme["table_background"] if row["header"] else theme["background"],
                outline=theme["border"],
            )
            for edge in row["edges"][1:-1]:
                draw.line((edge, y0, edge, y1 - 1), fill=theme["border"])
        for record in layout["text_units"]:
            typography.draw(draw, record, theme["foreground"])
        for marker in layout["markers"]:
            x = marker["x"]
            for char in marker["text"]:
                face, text, advance, shift, _, _ = typography.glyph(char, marker["font_size"])
                draw.text(
                    (x + shift, marker["baseline"]),
                    text,
                    font=fonts.font(face, marker["font_size"]),
                    anchor="ls",
                    fill=theme["accent"],
                )
                x += advance
        for item in layout["images"]:
            _paint_image(page, item, selected_assets[item["slot_id"]].image)
        for section in layout["sections"]:
            top, bottom = section["bbox"][1], section["bbox"][3]
            sections.append(
                _artifact(
                    section["section_id"],
                    "section",
                    _png_region(page, top, bottom),
                    width,
                    bottom - top,
                    dependencies=section["slot_ids"],
                    layout={
                        **section,
                        "text_units": [
                            record
                            for record in layout["text_units"]
                            if record["section_id"] == section["section_id"]
                        ],
                        "images": [
                            record
                            for record in layout["images"]
                            if record["section_id"] == section["section_id"]
                        ],
                    },
                )
            )
        page_artifact = _artifact(
            "page",
            "page",
            _png_region(page, 0, height),
            width,
            height,
            dependencies=[slot.slot_id for slot in plan.image_slots],
            layout=layout,
        )
        for index, top in enumerate(range(0, height, options.slice_height)):
            bottom = min(height, top + options.slice_height)
            slices.append(
                _artifact(
                    f"slice_{index + 1}",
                    "slice",
                    _png_region(page, top, bottom),
                    width,
                    bottom - top,
                    dependencies=[slot.slot_id for slot in plan.image_slots],
                    layout={"bbox": [0, top, width, bottom], "index": index},
                )
            )
    if sum(artifact.height for artifact in slices) != height:
        raise AgentError("Slices do not cover the entire page", code="layout_slice_coverage")
    checks.append({"kind": "slices_contiguous", "passed": True, "count": len(slices)})
    return RenderedLongImage(
        sections=sections,
        page=page_artifact,
        slices=slices,
        layout=layout,
        checks=checks,
        render_fingerprint=fingerprint,
    )
