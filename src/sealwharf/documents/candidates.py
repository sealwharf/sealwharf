"""List the empty places where a signature could go, each with the text of its own block.

The places come from the PDF itself: the empty space after a "Signature" label,
above a short drawn line or above a run of underscores. Each one is checked to
be empty. The text around a place is the content of the table cell or bordered
section it is in or, outside a table, the lines just above and below it. A
reader, the code or the local model, then says which place is meant for a
person without ever giving coordinates of its own.
"""

import math
from dataclasses import dataclass, replace
from io import BytesIO

import pypdfium2 as pdfium
from pypdf import PdfReader

from .contextual_position import _holds_role, _inside, _joined, _role_and_name, _text_lines
from .names import contains_name, name_words
from .visual_position import _annotations_clear, _overlaps, _page_objects_clear, _tokens

MAX_PAGES = 20
MAX_PLACES = 26
LABELS = {"assinatura", "signature", "firma"}
# Words of a label that say nothing about whose signature it is.
FILLER = {"do", "da", "de", "dos", "das", "a", "o", "of", "the", "del", "la", "el", "por"}
WIDTH, HEIGHT = 200.0, 42.0
ABOVE, BELOW = 120.0, 45.0
LONG_LINE = 14  # words; longer lines are prose, left out of what the model reads


@dataclass(frozen=True)
class Place:
    letter: str
    page: int
    box: tuple[float, float, float, float]
    label: str
    context: tuple[str, ...]
    # Every line of the document, for the roles it gives to people, such as "Witness: Jane Example".
    document: tuple[str, ...] = ()


def _clear(page, source, box, media) -> bool:
    return (0 <= box[0] < box[2] <= media[2] and 0 <= box[1] < box[3] <= media[3]
            and _page_objects_clear(page, box) and _annotations_clear(source, box))


def _label_places(line, media) -> list[tuple[float, float, float, float]]:
    """Room after a label, then above the line it is on (for "Signature: ____"), then under it."""
    x1, y1, x2, y2 = line.box
    right = (x2 + 6, y1 - 30, min(x2 + 6 + WIDTH, media[2] - 30), y2 + 5)
    above = (x1, y2 + 2, min(x1 + WIDTH, media[2] - 30), y2 + 2 + HEIGHT)
    below = (x1, y1 - 6 - HEIGHT, min(x1 + WIDTH, media[2] - 30), y1 - 6)
    return [box for box in (right, above, below) if box[2] - box[0] >= 60]


def _rule_places(bounds) -> list[tuple[float, float, float, float]]:
    """Room above a short drawn line or a run of underscores."""
    x1, _, x2, y2 = bounds
    width = min(x2 - x1, 300.0)
    left = x1 + max(0.0, (x2 - x1 - width) / 2)
    return [(left, y2 + 2, left + width, y2 + 2 + HEIGHT)]


def _sections(paths, media) -> list[tuple[float, float, float, float]]:
    """Boxes bounded by a pair of vertical borders of the same height, as tables and framed forms have."""
    vertical = [b for b in _joined([b for b in paths if b[2] - b[0] <= 3]) if b[3] - b[1] >= 40]
    found = []
    for left in vertical:
        for right in vertical:
            if right[0] - left[2] >= 150 and abs(left[1] - right[1]) <= 4 and abs(left[3] - right[3]) <= 4:
                found.append((max(left[2], 0), max(left[1], right[1]), min(right[0], media[2]), min(left[3], right[3])))
    # The smallest section around a place is the one that speaks about it.
    return sorted(found, key=lambda s: (s[2] - s[0]) * (s[3] - s[1]))


def signature_places(document: bytes) -> list[Place]:
    """Return every empty place to sign, lettered A, B... in reading order.

    An empty list means there is nothing to choose from: a page without text,
    such as a scan, or no empty place at all.
    """
    reader = PdfReader(BytesIO(document), strict=True)
    if reader.is_encrypted or len(reader.pages) > MAX_PAGES:
        return []
    places: list[Place] = []
    any_text = False
    everything: list[str] = []
    with pdfium.PdfDocument(document) as pdf:
        for number, source in enumerate(reader.pages):
            media = tuple(float(v) for v in source.mediabox)
            if int(source.rotation or 0) % 360 or media[:2] != (0.0, 0.0) or not all(math.isfinite(v) for v in media):
                return []
            page = pdf[number]
            try:
                lines = _text_lines(page)
                if lines is None:
                    return []
                any_text = any_text or bool(lines)
                everything.extend(line.text for line in lines)
                paths = [tuple(float(v) for v in obj.get_bounds()) for obj in page.get_objects(max_depth=1) if obj.type == 2]
                sections = _sections(paths, media)
                # Each place, its label, and the line it belongs to: one place per label line at most.
                found: list[tuple[tuple[float, float, float, float], str, int | None]] = []
                for index, line in enumerate(lines):
                    words = _tokens(line.text)
                    if words[:1] and words[0] in LABELS:
                        found.extend((box, line.text, index) for box in _label_places(line, media))
                    elif "_____" in line.text and not words:
                        found.extend((box, "", index) for box in _rule_places(line.box))
                for bounds in paths:
                    if len(bounds) == 4 and 100 <= bounds[2] - bounds[0] <= 360 and bounds[3] - bounds[1] <= 3:
                        found.extend((box, "", None) for box in _rule_places(bounds))
                taken: list[tuple[float, float, float, float]] = []
                lines_used: set[int] = set()
                for box, label, owner in found:
                    box = tuple(round(v, 2) for v in box)
                    if owner in lines_used or any(_overlaps(box, other) for other in taken):
                        continue
                    if not _clear(page, source, box, media):
                        continue
                    if len(places) >= MAX_PLACES:
                        return []
                    section = next((s for s in sections if _inside(box, s)), None)
                    if section is not None:
                        context = [line.text for line in lines if _inside(line.box, section)]
                    elif label:
                        # A label outside a table: the label and the lines just above it, such as "Name: ...".
                        context = [line.text for line in lines if box[1] <= line.box[1] and line.box[3] <= box[3] + ABOVE]
                    else:
                        # A line to sign on: what is written right under it, where the signer's name usually is.
                        context = [line.text for line in lines if box[1] - BELOW <= line.box[1] and line.box[3] <= box[1]]
                    places.append(Place(chr(ord("A") + len(places)), number, box, label, tuple(context)))
                    taken.append(box)
                    if owner is not None:
                        lines_used.add(owner)
            finally:
                page.close()
    # A document with no text at all, such as a scan, is left to the reading of its picture.
    return [replace(place, document=tuple(everything)) for place in places] if any_text else []


def visible_lines(document: bytes) -> list[str]:
    """Every line of text a reader can see in the document; none for a document with no visible text."""
    reader = PdfReader(BytesIO(document), strict=True)
    if reader.is_encrypted or len(reader.pages) > MAX_PAGES:
        return []
    found: list[str] = []
    with pdfium.PdfDocument(document) as pdf:
        for number in range(len(pdf)):
            page = pdf[number]
            try:
                found.extend(line.text for line in _text_lines(page) or [])
            finally:
                page.close()
    return found


def free_places_at_end(document: bytes, count: int) -> list[tuple[int, tuple[float, float, float, float]]]:
    """Empty places low on the last page, one per signer, side by side when they fit; none if there is no room."""
    reader = PdfReader(BytesIO(document), strict=True)
    if reader.is_encrypted or not reader.pages or count < 1:
        return []
    number = len(reader.pages) - 1
    source = reader.pages[number]
    media = tuple(float(v) for v in source.mediabox)
    if int(source.rotation or 0) % 360 or media[:2] != (0.0, 0.0):
        return []
    chosen: list[tuple[float, float, float, float]] = []
    with pdfium.PdfDocument(document) as pdf:
        page = pdf[number]
        try:
            y = 50.0
            while len(chosen) < count and y + HEIGHT <= media[3] / 2:
                for x in (72.0, media[2] - 72.0 - WIDTH):
                    box = (x, y, x + WIDTH, y + HEIGHT)
                    if len(chosen) < count and not any(_overlaps(box, other) for other in chosen) and _clear(page, source, box, media):
                        chosen.append(box)
                y += 12.0
        finally:
            page.close()
    return [(number, box) for box in chosen] if len(chosen) == count else []


def _role(label: str) -> tuple[str, ...]:
    return tuple(word for word in _tokens(label)[1:] if word not in FILLER)


def _short(line: str, identity: tuple[str, ...]) -> bool:
    # A name inside a sentence is who the text is about, not who signs: only a short line, such as a field, counts.
    return len(name_words(line)) <= len(identity) + 5 and contains_name(name_words(line), identity)


def roles_of(place: Place, name: str) -> list[tuple[str, ...]]:
    """The roles the document gives to the person anywhere, as in "Witness: Jane Example"."""
    identity = name_words(name)
    found = [tuple(word for word in _role_and_name(line, identity) if word not in FILLER)
             for line in place.document if _short(line, identity)]
    return [role for role in found if role]


def contradicts(place: Place, name: str) -> bool:
    """Whether the label asks for a role and the document gives the person other roles, never that one."""
    role, written = _role(place.label), roles_of(place, name)
    return bool(role and written) and not any(_holds_role(other, role) for other in written)


def holds_name(place: Place, name: str) -> bool:
    """Whether the place is the person's: named in its own block in the role its label asks for, if any,
    or given that role anywhere in the document."""
    identity = name_words(name)
    role = _role(place.label)
    if role and any(_holds_role(written, role) for written in roles_of(place, name)):
        return True
    for line in place.context:
        if not _short(line, identity):
            continue
        if not role:
            return True
        written = tuple(word for word in _role_and_name(line, identity) if word not in FILLER)
        if _holds_role(written, role):
            return True
    return False


def place_by_name(places: list[Place], name: str) -> Place | None:
    """The one place whose own block names the person; None when there is none or more than one."""
    holders = [place for place in places if holds_name(place, name)]
    return holders[0] if len(holders) == 1 else None


def describe_places(places: list[Place]) -> str:
    """The places as the model reads them: each label and the short lines of its block, long prose left out."""
    parts = []
    for place in places:
        lines = [line[:100] for line in place.context if len(line.split()) <= LONG_LINE][:12]
        label = place.label or "a signature line"
        parts.append(f"[{place.letter}] page {place.page + 1}, label: {label}\n" + "\n".join(f"    {line}" for line in lines))
    return "\n".join(parts)
