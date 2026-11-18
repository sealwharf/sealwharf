"""Bounded structural validation for PDF documents."""

from io import BytesIO

from pypdf import PdfReader

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 200


class DocumentError(Exception):
    """The document cannot be processed safely."""


def validate_pdf(data: bytes) -> None:
    if not data.startswith(b"%PDF-") or len(data) > MAX_PDF_BYTES:
        raise DocumentError("The file is not a valid PDF or exceeds the size limit.")
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= MAX_PAGES:
            raise DocumentError("The PDF is encrypted, empty, or has too many pages.")
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError("Could not read the PDF.") from exc
