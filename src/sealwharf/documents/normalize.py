"""Rewrite rotated pages as equivalent unrotated pages before locating a signature.

Rewriting would invalidate existing signatures and misplace annotations, so a
document with form fields, or with annotations on a rotated page, is returned
unchanged and remains unsupported.
"""

from io import BytesIO

from pypdf import PdfReader, PdfWriter


def normalize_rotation(document: bytes) -> bytes:
    """Return a visually identical PDF whose pages carry no rotation entry."""
    try:
        reader = PdfReader(BytesIO(document), strict=True)
        if reader.is_encrypted:
            return document
        rotated = [page for page in reader.pages if int(page.rotation or 0) % 360]
        if not rotated:
            return document
        if "/AcroForm" in reader.trailer["/Root"] or any(page.get("/Annots") for page in rotated):
            return document
        if any(int(page.rotation) % 90 or float(page.get("/UserUnit", 1)) != 1 for page in rotated):
            return document
        writer = PdfWriter(clone_from=reader)
        for page in writer.pages:
            if int(page.rotation or 0) % 360:
                page.transfer_rotation_to_content()
        output = BytesIO()
        writer.write(output)
        result = output.getvalue()
        check = PdfReader(BytesIO(result), strict=True)
        if len(check.pages) != len(reader.pages) or any(int(page.rotation or 0) % 360 for page in check.pages):
            return document
        return result
    except Exception:
        # An unreadable document is rejected by the later, stricter steps.
        return document
