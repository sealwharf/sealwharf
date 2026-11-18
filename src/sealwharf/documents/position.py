"""Conservative selection of an empty PDF signature field.

PDF coordinates returned here are unrotated user-space points. Documents that
cannot be interpreted without assumptions are deliberately left for review.
"""

import math
import re
import unicodedata
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from pypdf import PdfReader

from .names import matches_name


@dataclass(frozen=True)
class SignaturePosition:
    page: int  # zero-based
    box: tuple[float, float, float, float]
    field_name: str


def has_signature_widgets(document: bytes) -> bool:
    """Detect empty signature widgets before falling back to visual heuristics.

    Signatures that are already filled do not count, so another signer can
    still be added to a signed document.
    """
    reader = PdfReader(BytesIO(document), strict=True)
    for page in reader.pages:
        for reference in page.get("/Annots", []):
            widget = reference.get_object()
            if widget.get("/Subtype") != "/Widget":
                continue
            _, field_type, filled = _field_data(widget)
            if field_type == "/Sig" and not filled:
                return True
    return False


def _tokens(value: str) -> tuple[str, ...]:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    plain = "".join(c for c in decomposed if not unicodedata.combining(c))
    return tuple(re.findall(r"[^\W_]+", plain, flags=re.UNICODE))


def _field_data(widget: object) -> tuple[str, object, bool]:
    """Collect inherited field properties while rejecting cyclic parents."""
    names: list[str] = []
    field_type = None
    filled = False
    current = widget
    seen: set[int] = set()
    for _ in range(32):
        current = current.get_object()  # type: ignore[attr-defined]
        if id(current) in seen:
            raise ValueError("Cycle in field hierarchy")
        seen.add(id(current))
        name = current.get("/T")
        if name is not None:
            names.append(str(name))
        if field_type is None:
            field_type = current.get("/FT")
        filled = filled or current.get("/V") is not None
        current = current.get("/Parent")
        if current is None:
            return ".".join(reversed(names)), field_type, filled
    raise ValueError("Field hierarchy too deep")


def find_signature_position(pdf_path: str | Path | BytesIO, signer_name: str | None = None) -> SignaturePosition | None:
    """Return a uniquely identified field, or None when human review is needed.

    If a name is supplied, it must appear as consecutive words in the field
    name; middle names may be reduced to initials. An ambiguous match, rotation, invisible widget or invalid rectangle
    is never converted into an automatic placement. Caller must treat parsing
    errors as a failed document, not as an instruction to pick a default box.
    """
    identity = _tokens(signer_name) if signer_name is not None else ()
    if signer_name is not None and len(identity) < 2:
        return None
    reader = PdfReader(pdf_path, strict=True)
    if reader.is_encrypted:
        return None
    candidates: list[SignaturePosition] = []
    for page_number, page in enumerate(reader.pages):
        for reference in page.get("/Annots", []):
            widget = reference.get_object()
            if widget.get("/Subtype") != "/Widget":
                continue
            name, field_type, filled = _field_data(widget)
            if field_type != "/Sig" or filled:
                continue
            words = _tokens(name)
            prefixes = ((), ("signature",), ("signer",), ("assinatura",), ("assinante",), ("nome",))
            if identity and not any(words[:len(prefix)] == prefix and matches_name(words[len(prefix):], identity)
                                    for prefix in prefixes):
                continue
            # An invalid matching candidate makes the whole selection ambiguous.
            if int(page.get("/Rotate", 0)) % 360 != 0 or int(widget.get("/F", 0)) & (1 | 2 | 32):
                return None
            rect = widget.get("/Rect")
            if rect is None or len(rect) != 4:
                return None
            x1, y1, x2, y2 = (float(v) for v in rect)
            crop = tuple(float(v) for v in page.cropbox)
            media = tuple(float(v) for v in page.mediabox)
            if not all(math.isfinite(v) for v in (x1, y1, x2, y2, *crop, *media)):
                return None
            if x2 - x1 < 30 or y2 - y1 < 15:
                return None
            if not all(box[0] <= x1 < x2 <= box[2] and box[1] <= y1 < y2 <= box[3] for box in (crop, media)):
                return None
            candidates.append(SignaturePosition(page_number, (x1, y1, x2, y2), name))
    return candidates[0] if len(candidates) == 1 else None
