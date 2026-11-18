"""Detect a single unoccupied signature-line candidate using local heuristics.

This is not AI or identity verification. Scanned pages and complex transforms
may not be detected. A matching label does not prove the signer's intent.
"""

import ctypes
import math
import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO

import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_raw
from pypdf import PdfReader

from .names import matches_name


@dataclass(frozen=True)
class VisualCandidate:
    page: int
    box: tuple[float, float, float, float]
    reason: str
    context_box: tuple[float, float, float, float] | None = None


def _point(matrix: list, x: float, y: float) -> tuple[float, float]:
    a, b, c, d, e, f = map(float, matrix)
    return a * x + c * y + e, b * x + d * y + f


def _quarter_turn(matrix: list) -> bool:
    """Accept transforms that keep horizontal and vertical lines on the axes."""
    a, b, c, d = (abs(float(value)) for value in matrix[:4])
    return (b < 0.0001 and c < 0.0001) or (a < 0.0001 and d < 0.0001)


def _text_scale(cm: list, tm: list) -> float | None:
    """Return the horizontal scale of upright text, or None for any other orientation."""
    ta, tb, tc, td = (float(value) for value in tm[:4])
    ca, cb, cc, cd = (float(value) for value in cm[:4])
    a, b, c, d = ta * ca + tb * cc, ta * cb + tb * cd, tc * ca + td * cc, tc * cb + td * cd
    if abs(b) >= 0.0001 or abs(c) >= 0.0001 or a <= 0 or d <= 0:
        return None
    return a


def _tokens(value: str) -> tuple[str, ...]:
    text = unicodedata.normalize("NFKD", value.casefold())
    text = "".join(char for char in text if not unicodedata.combining(char))
    return tuple(re.findall(r"[^\W\d_]+", text, flags=re.UNICODE))


def _matches_identity(label: str, identity: tuple[str, ...]) -> bool:
    """Match a complete name or initialed middle names, optionally after a known field label."""
    if "\n" in label or "\r" in label:
        return False
    words = _tokens(label)
    prefixes = ((), ("nome",), ("nome", "completo"), ("name",), ("full", "name"), ("assinante",),
                ("assinatura",), ("signer",), ("signature",))
    return any(words[:len(prefix)] == prefix and matches_name(words[len(prefix):], identity) for prefix in prefixes)


def _overlaps(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> bool:
    """Treat touching edges and zero-height paths as collisions."""
    return not (first[2] < second[0] or second[2] < first[0]
                or first[3] < second[1] or second[3] < first[1])


def _annotations_clear(page, box: tuple[float, float, float, float]) -> bool:
    try:
        for ref in page.get("/Annots", []):
            rect = tuple(float(x) for x in ref.get_object()["/Rect"])
            if len(rect) != 4 or not all(math.isfinite(x) for x in rect):
                return False
            bounds = (min(rect[0], rect[2]), min(rect[1], rect[3]), max(rect[0], rect[2]), max(rect[1], rect[3]))
            if _overlaps(box, bounds):
                return False
    except Exception:
        return False
    return True


def _page_objects_clear(page, box: tuple[float, float, float, float]) -> bool:
    """Fail closed on any text, vector, image, shading, form or unknown object.

    Form XObjects are treated as a whole; a form with a large bounding box may
    prevent an otherwise legitimate suggestion. This cannot detect content
    missing from a malformed PDF or reliably establish semantic intent.
    """
    try:
        for index, obj in enumerate(page.get_objects(max_depth=1)):
            bounds = tuple(float(x) for x in obj.get_bounds())
            if len(bounds) != 4 or not all(math.isfinite(x) for x in bounds):
                return False
            # PDFium reports empty or whitespace text runs as a point, with no ink.
            if obj.type == 1 and bounds[0] == bounds[2] and bounds[1] == bounds[3]:
                continue
            if index == 0 and _is_white_page_background(page, obj, bounds):
                continue
            if bounds[0] > bounds[2] or bounds[1] > bounds[3] or _overlaps(box, bounds):
                return False
    except Exception:
        return False
    return True


def _is_white_page_background(page, obj, bounds: tuple[float, float, float, float]) -> bool:
    """Ignore only an initial opaque white page-sized rectangular fill."""
    if obj.type != 2:
        return False
    width, height = page.get_size()
    if not (bounds[0] <= 0 and bounds[1] <= 0 and bounds[2] >= width - 1 and bounds[3] >= height - 1):
        return False
    fill = ctypes.c_int()
    stroke = ctypes.c_int()
    rgba = [ctypes.c_uint() for _ in range(4)]
    return bool(
        pdfium_raw.FPDFPath_GetDrawMode(obj.raw, ctypes.byref(fill), ctypes.byref(stroke))
        and fill.value != 0 and stroke.value == 0
        and pdfium_raw.FPDFPath_CountSegments(obj.raw) == 5
        and pdfium_raw.FPDFPageObj_GetFillColor(obj.raw, *(ctypes.byref(value) for value in rgba))
        and [value.value for value in rgba] == [255, 255, 255, 255]
    )


def _is_bordered_graphic(page, box: tuple[float, float, float, float], peers: list[VisualCandidate], *, multiple: bool) -> bool:
    """Exclude candidate lines that are demonstrably borders of a rectangle.

    LibreOffice frequently emits table boundaries as separate vector paths or
    as a single rectangular path. These are not free-standing signature lines.
    Unknown or malformed objects do not count as evidence of a table.
    """
    left, baseline, right = box[0], box[1] - 2, box[2]
    # A lone rectangle could itself contain a signature, so require repeated,
    # aligned candidate borders before classifying anything as a table.
    if multiple and not any(abs(other.box[0] - left) <= 4 and abs(other.box[2] - right) <= 4
                            and 6 <= abs((other.box[1] - 2) - baseline) <= 80 for other in peers):
        return False
    left_edge = right_edge = False
    try:
        for obj in page.get_objects(max_depth=1):
            if obj.type != 2:
                continue
            bounds = tuple(float(x) for x in obj.get_bounds())
            if len(bounds) != 4 or not all(math.isfinite(x) for x in bounds):
                continue
            x1, y1, x2, y2 = bounds
            width, height = x2 - x1, y2 - y1
            if width >= right - left - 4 and height >= 5:
                if x1 <= left + 2 and x2 >= right - 2 and min(abs(y1 - baseline), abs(y2 - baseline)) <= 2:
                    return True
            if width <= 3 and height >= 5 and y1 - 2 <= baseline <= y2 + 2:
                left_edge |= x1 - 2 <= left <= x2 + 2
                right_edge |= x1 - 2 <= right <= x2 + 2
        return left_edge and right_edge
    except Exception:
        # Without evidence, retain the candidate so ambiguity remains visible.
        return False


def suggest_visual_positions(document: bytes, *, signer_name: str | None = None,
                             max_candidates: int = 20) -> list[VisualCandidate]:
    """Return estimated boxes above visible lines with nearby text below them.

    Exclude proven table borders and refuse multiple remaining candidates even
    if only one passes collision checks. Estimates use approximate font metrics.
    When a signer name is supplied, require a nearby matching complete name;
    do not use partial names, a generic signature label, or competing labels.
    No document text or signer name is returned, and visual cues do not
    guarantee the line's semantic meaning.
    """
    if max_candidates < 1:
        raise ValueError("max_candidates must be positive")
    identity = _tokens(signer_name) if signer_name is not None else None
    if identity is not None and len(identity) < 2:
        return []
    reader = PdfReader(BytesIO(document), strict=True)
    if reader.is_encrypted:
        return []
    candidates: list[VisualCandidate] = []
    matching: set[VisualCandidate] = set()
    for page_index, page in enumerate(reader.pages):
        if int(page.rotation or 0) % 360:
            continue
        crop = tuple(float(x) for x in page.cropbox)
        media = tuple(float(x) for x in page.mediabox)
        if not all(math.isfinite(x) for x in (*crop, *media)):
            continue
        lines: list[tuple[float, float, float]] = []
        labels: list[tuple[float, float, str]] = []
        path_start: tuple[float, float] | None = None
        pending: list[tuple[float, float, float]] = []

        def operand(operator, operands, cm, tm, lines=lines):
            nonlocal path_start, pending
            if not _quarter_turn(cm):
                path_start = None
                pending = []
                return
            if operator == b"m" and len(operands) == 2:
                path_start = _point(cm, float(operands[0]), float(operands[1]))
            elif operator == b"l" and len(operands) == 2 and path_start is not None:
                end = _point(cm, float(operands[0]), float(operands[1]))
                if abs(end[1] - path_start[1]) <= 0.5:
                    left, right = sorted((path_start[0], end[0]))
                    if 60 <= right - left <= 300:
                        pending.append((left, right, (path_start[1] + end[1]) / 2))
                path_start = end
            elif operator in (b"S", b"s"):
                lines.extend(pending)
                path_start = None
                pending = []
            elif operator in (b"n", b"f", b"F", b"f*", b"B", b"B*", b"b", b"b*"):
                path_start = None
                pending = []

        def text(value, cm, tm, font, font_size, lines=lines, labels=labels):
            scale = _text_scale(cm, tm)
            if scale is None:
                return
            clean = value.strip()
            if not clean:
                return
            x, y = _point(cm, float(tm[4]), float(tm[5]))
            if not all(math.isfinite(v) for v in (x, y, float(font_size))):
                return
            # Underscores are a common Word export of a drawn signing line.
            if re.fullmatch(r"_{6,}", clean):
                char_width = abs(float(font_size) * scale) * 0.46
                width = min(300.0, len(clean) * char_width)
                if 60 <= width <= 300:
                    lines.append((x, x + width, y))
            elif 2 <= len(clean) <= 150:
                words = re.findall(r"[^\W\d_]+", clean, re.UNICODE)
                if len(words) >= 2 or (len(words) == 1 and words[0].casefold() in {"assinatura", "signature"}):
                    labels.append((x, y, clean))

        page.extract_text(visitor_operand_before=operand, visitor_text=text)
        for left, right, baseline in lines:
            nearby = [label for x, y, label in labels
                      if left - 15 <= x <= right + 15 and 2 <= baseline - y <= 28]
            if not nearby:
                continue
            box = (round(left, 2), round(baseline + 2, 2), round(right, 2), round(baseline + 42, 2))
            if not all(bounds[0] <= box[0] < box[2] <= bounds[2] and bounds[1] <= box[1] < box[3] <= bounds[3] for bounds in (crop, media)):
                continue
            candidate = VisualCandidate(page_index, box, "line_with_label")
            if candidate not in candidates:
                candidates.append(candidate)
            if identity is not None:
                matched = any(_matches_identity(label, identity) for label in nearby)
                conflicts = any(not _matches_identity(label, identity)
                                and _tokens(label) not in {("assinatura",), ("signature",)}
                                for label in nearby)
                if matched and not conflicts:
                    matching.add(candidate)
            if len(candidates) > max_candidates:
                raise ValueError("Too many visual candidates to review safely")
    if not candidates:
        return []
    unbordered = []
    with pdfium.PdfDocument(document) as pdf:
        for candidate in candidates:
            rendered_page = pdf[candidate.page]
            try:
                peers = [other for other in candidates if other.page == candidate.page]
                if not _is_bordered_graphic(rendered_page, candidate.box, peers, multiple=len(candidates) > 1):
                    unbordered.append(candidate)
            finally:
                rendered_page.close()
        eligible = unbordered if identity is None else [candidate for candidate in unbordered if candidate in matching]
        if len(eligible) != 1:
            return []
        candidate = eligible[0]
        rendered_page = pdf[candidate.page]
        try:
            if (_annotations_clear(reader.pages[candidate.page], candidate.box)
                    and _page_objects_clear(rendered_page, candidate.box)):
                return [candidate]
        finally:
            rendered_page.close()
    return []
