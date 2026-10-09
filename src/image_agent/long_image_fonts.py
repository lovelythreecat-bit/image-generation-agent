"""Installed-font coverage and deterministic codepoint fallback, without networking."""

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path

from fontTools.ttLib import TTCollection, TTFont, TTLibError
from PIL import ImageFont

from .errors import AgentError
from .long_image_models import LongImageRequest

LAYOUT_CONTROLS = frozenset("\n\r\t")


@dataclass(frozen=True)
class FontFace:
    path: Path
    index: int
    sha256: str
    cmap: frozenset[int]


@dataclass
class FontSet:
    faces: tuple[FontFace, ...]
    _characters: dict[str, FontFace] = field(default_factory=dict, repr=False)
    _sizes: dict[tuple[str, int, int], ImageFont.FreeTypeFont] = field(
        default_factory=dict, repr=False
    )

    def face_for(self, char: str) -> FontFace:
        if char not in self._characters:
            face = next((face for face in self.faces if ord(char) in face.cmap), None)
            if face is None:
                raise AgentError(
                    f"No installed font covers U+{ord(char):04X}", code="font_missing_glyph"
                )
            self._characters[char] = face
        return self._characters[char]

    def font(self, face: FontFace, size: int) -> ImageFont.FreeTypeFont:
        key = (face.sha256, face.index, size)
        if key not in self._sizes:
            try:
                self._sizes[key] = ImageFont.truetype(
                    str(face.path), size, index=face.index, layout_engine=ImageFont.Layout.BASIC
                )
            except (OSError, ValueError):
                raise AgentError("Installed font cannot be rendered", code="font_invalid") from None
        return self._sizes[key]

    def fingerprint_data(self) -> list[dict]:
        # Sequence, file contents and TTC index, never merely a pathname.
        return [{"sha256": face.sha256, "index": face.index} for face in self.faces]


def _system_paths() -> list[Path]:
    if os.name == "nt":
        directory = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        return [
            directory / name
            for name in (
                "msyh.ttc",
                "simhei.ttf",
                "simsun.ttc",
                "seguiemj.ttf",
                "seguisym.ttf",
                "arial.ttf",
            )
        ]
    return [
        Path(name)
        for name in (
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
            "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
            "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
            "/System/Library/Fonts/PingFang.ttc",
            "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
        )
    ]


def _read_faces(path: Path) -> list[FontFace]:
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
        stream.seek(0)
        collection = stream.read(4) == b"ttcf"
    count = 1
    if collection:
        with TTCollection(path, lazy=True) as fonts:
            count = len(fonts.fonts)
    result = []
    for index in range(count):
        with TTFont(path, fontNumber=index, lazy=True) as font:
            cmap = font.getBestCmap() or {}
            coverage = frozenset(code for code, glyph in cmap.items() if glyph != ".notdef")
            result.append(FontFace(path, index, digest, coverage))
    return result


def resolve_fonts(texts: list[str], font_path: Path | None = None) -> FontSet:
    paths = ([Path(font_path)] if font_path is not None else []) + _system_paths()
    faces = []
    seen = set()
    for position, path in enumerate(paths):
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        explicit = font_path is not None and position == 0
        if not path.is_file():
            if explicit:
                raise AgentError("Specified font file is missing", code="font_not_found")
            continue
        try:
            faces.extend(_read_faces(path))
        except (OSError, TTLibError, ValueError, KeyError):
            if explicit:
                raise AgentError("Specified font file is invalid", code="font_invalid") from None
    if not faces:
        raise AgentError("No usable installed fonts found", code="font_not_found")
    fonts = FontSet(tuple(faces))
    missing = []
    for char in dict.fromkeys("".join(texts)):
        if char in LAYOUT_CONTROLS:
            continue
        try:
            face = fonts.face_for(char)
            # Verify FreeType accepts the exact face whose cmap was inspected.
            fonts.font(face, 26)
        except AgentError as error:
            if error.code != "font_missing_glyph":
                raise
            missing.append(f"U+{ord(char):04X}")
    if missing:
        raise AgentError(
            "No installed font covers " + ", ".join(missing), code="font_missing_glyph"
        )
    return fonts


def preflight_long_image(request: LongImageRequest) -> None:
    texts = [request.title, request.content_brief]
    texts.extend(copy.text for copy in request.required_copy)
    texts.extend(request.service_notes)
    for spec in request.product_specs:
        texts.extend((spec.name, spec.value))
    resolve_fonts(texts, request.options.font_path)
