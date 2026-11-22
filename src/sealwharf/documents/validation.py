"""Bounded structural validation for PDF documents."""

from io import BytesIO

from pypdf import PdfReader

from .. import limits


class DocumentError(Exception):
    """The document cannot be processed safely."""


def validate_pdf(data: bytes) -> None:
    if not data.startswith(b"%PDF-") or len(data) > limits.current.max_file_bytes:
        raise DocumentError("The file is not a valid PDF or exceeds the size limit.")
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= limits.current.max_pages:
            raise DocumentError("The PDF is encrypted, empty, or has too many pages.")
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError("Could not read the PDF.") from exc
