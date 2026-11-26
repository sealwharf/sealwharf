"""Find bounded signature rows whose surrounding form identifies the signer.

Candidates are geometry-checked, but require separate semantic review before
signing. Document text is evidence only and is never treated as instructions.
"""

import ctypes
import math
from dataclasses import dataclass
from io import BytesIO

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
from pypdf import PdfReader

from .names import matches_name
from .visual_position import (
    INVISIBLE_MODES,
    MIN_TEXT_POINTS,
    VisualCandidate,
    _annotations_clear,
    _matches_identity,
    _page_objects_clear,
    _tokens,
)

MAX_PAGE_CHARACTERS = 100_000
MAX_PAGE_LINES = 5_000
MAX_PAGE_PATHS = 10_000
MAX_BORDER_PATHS = 500

@dataclass(frozen=True)
class _Line:
    text: str
    box: tuple[float, float, float, float]


# Characters that draw a line to sign on.
FLAT = frozenset("_-‐‑‒–—―‗¯")

def _visible(text_page, index: int) -> bool:
    """Whether a character can be read on the page: not invisible, not white, not tiny (as in TextVisibility)."""
    handle = text_page.raw
    obj = pdfium_c.FPDFText_GetTextObject(handle, index)
    if not obj or pdfium_c.FPDFTextObj_GetTextRenderMode(obj) in INVISIBLE_MODES:
        return False
    if pdfium_c.FPDFText_GetFontSize(handle, index) < MIN_TEXT_POINTS:
        return False
    red, green, blue, alpha = (ctypes.c_uint() for _ in range(4))
    if pdfium_c.FPDFText_GetFillColor(handle, index, red, green, blue, alpha):
        if alpha.value < 32 or min(red.value, green.value, blue.value) >= 235:
            return False
    left, bottom, right, top = text_page.get_charbox(index)
    # A line drawn with underscores or dashes is flat by nature: its width, not its height, shows it can be seen.
    if text_page.get_text_range(index, 1) in FLAT:
        return right - left >= MIN_TEXT_POINTS * 0.5
    return top - bottom >= MIN_TEXT_POINTS * 0.5


def _text_lines(page) -> list[_Line] | None:
    """The text lines of a page, built only from characters a reader can see."""
    text_page = page.get_textpage()
    try:
        text = text_page.get_text_range()
        if len(text) != text_page.count_chars() or len(text) > MAX_PAGE_CHARACTERS:
            return None
        result = []
        offset = 0
        for line in text.splitlines(keepends=True):
            if len(result) >= MAX_PAGE_LINES:
                return None
            seen = [char if char.isspace() or _visible(text_page, offset + i) else " " for i, char in enumerate(line)]
            boxes = [text_page.get_charbox(offset + i) for i, char in enumerate(seen) if not char.isspace()]
            offset += len(line)
            clean = " ".join("".join(seen).split())
            if not boxes or not clean or len(clean) > 500:
                continue
            box = (min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes))
            if all(math.isfinite(value) for value in box):
                result.append(_Line(clean, box))
        return result
    finally:
        text_page.close()


def _inside(inner, outer) -> bool:
    return outer[0] <= inner[0] < inner[2] <= outer[2] and outer[1] <= inner[1] < inner[3] <= outer[3]


def _joined(segments: list[tuple[float, float, float, float]]) -> list[tuple[float, float, float, float]]:
    """Join vertical border pieces that touch end to end, as tables drawn one row at a time have them."""
    joined: list[tuple[float, float, float, float]] = []
    for piece in sorted(segments, key=lambda b: (round(b[0]), b[1])):
        last = joined[-1] if joined else None
        if last is not None and abs(last[0] - piece[0]) <= 1.5 and abs(last[2] - piece[2]) <= 1.5 and piece[1] <= last[3] + 1.5:
            joined[-1] = (min(last[0], piece[0]), last[1], max(last[2], piece[2]), max(last[3], piece[3]))
        else:
            joined.append(piece)
    return joined


def _role_and_name(text: str, identity: tuple[str, ...]) -> tuple[str, ...]:
    """The role written before the signer's name, as in "Entity representative: Jane Example"; empty otherwise."""
    role, colon, name = text.rpartition(":")
    if not colon or "\n" in text or not matches_name(_tokens(name), identity):
        return ()
    return _tokens(role)


def _holds_role(written: tuple[str, ...], label: tuple[str, ...]) -> bool:
    """Whether every word of the written role appears, in order and together, in the signature label."""
    return bool(written) and any(label[i:i + len(written)] == written for i in range(len(label) - len(written) + 1))


def suggest_contextual_position(document: bytes, signer_name: str) -> VisualCandidate | None:
    """Find one empty, labeled row in a bounded section naming the signer."""
    identity = _tokens(signer_name)
    if len(identity) < 2:
        return None
    reader = PdfReader(BytesIO(document), strict=True)
    if reader.is_encrypted:
        return None
    candidates: list[VisualCandidate] = []
    with pdfium.PdfDocument(document) as pdf:
        for page_number, source_page in enumerate(reader.pages):
            if int(source_page.rotation or 0) % 360 or float(source_page.get('/UserUnit', 1)) != 1:
                return None
            crop = tuple(float(v) for v in source_page.cropbox)
            media = tuple(float(v) for v in source_page.mediabox)
            if (crop != media or media[:2] != (0.0, 0.0) or not all(math.isfinite(v) for v in media)
                    or not (0 < media[2] <= 5000 and 0 < media[3] <= 5000)):
                return None
            page = pdf[page_number]
            try:
                lines = _text_lines(page)
                if lines is None:
                    return None
                paths = []
                for obj in page.get_objects(max_depth=1):
                    if obj.type == 2:
                        bounds = tuple(float(v) for v in obj.get_bounds())
                        if len(bounds) == 4 and all(math.isfinite(v) for v in bounds):
                            paths.append(bounds)
                            if len(paths) > MAX_PAGE_PATHS:
                                return None
                vertical = [b for b in _joined([b for b in paths if b[2] - b[0] <= 3]) if b[3] - b[1] >= 60]
                horizontal = [b for b in paths if b[2] - b[0] >= 150 and b[3] - b[1] <= 3]
                if len(vertical) > 100 or len(horizontal) > MAX_BORDER_PATHS:
                    return None
                for left in vertical:
                    for right in vertical:
                        if not (right[0] - left[2] >= 150 and abs(left[1] - right[1]) <= 4 and abs(left[3] - right[3]) <= 4):
                            continue
                        section = (max(left[2], 0), max(left[1], right[1]), min(right[0], media[2]), min(left[3], right[3]))
                        if not _inside(section, media):
                            continue
                        section_lines = [line for line in lines if _inside(line.box, section)]
                        signature_lines = [line for line in section_lines if _tokens(line.text)[:1] in {('assinatura',), ('signature',)}]
                        if len(signature_lines) != 1:
                            continue
                        label = signature_lines[0]
                        role = _tokens(label.text)[1:]
                        if role:
                            # A label naming a role ("Signature of the entity representative") belongs to the
                            # one person written with that same role ("Entity representative: Jane Example").
                            matches = [line for line in section_lines if _holds_role(_role_and_name(line.text, identity), role)]
                        else:
                            name_lines = [line for line in section_lines if _tokens(line.text)[:1] in {('nome',), ('name',)}]
                            matches = [line for line in name_lines if _matches_identity(line.text, identity)] if len(name_lines) == 1 else []
                        if len(matches) != 1 or matches[0].box[1] <= label.box[3]:
                            continue
                        row_edges = [b for b in horizontal if abs(b[0] - left[0]) <= 5 and abs(b[2] - right[2]) <= 5]
                        above = [b for b in row_edges if b[1] > label.box[3]]
                        below = [b for b in row_edges if b[3] < label.box[1]]
                        if not above or not below:
                            continue
                        top = min(above, key=lambda b: b[1])[1]
                        bottom = max(below, key=lambda b: b[3])[3]
                        if not (25 <= top - bottom <= 120 and label.box[1] > (top + bottom) / 2):
                            continue
                        # Use the empty area to the right of the label, preserving
                        # the row height while leaving its text and borders clear.
                        start_x = max(section[0] + 4, label.box[2] + 6)
                        target_width = min(300, max(60, (top - bottom - 6) * 3.4))
                        box = (round(start_x, 2), round(bottom + 3, 2),
                               round(min(start_x + target_width, section[2] - 4), 2), round(top - 3, 2))
                        if box[2] - box[0] < 60 or box[3] - box[1] < 15 or not _inside(box, section):
                            continue
                        candidate = VisualCandidate(page_number, box, "contextual_signature_row", tuple(round(v, 2) for v in section))
                        if candidate not in candidates and _annotations_clear(source_page, box) and _page_objects_clear(page, box):
                            candidates.append(candidate)
            finally:
                page.close()
    return candidates[0] if len(candidates) == 1 else None
