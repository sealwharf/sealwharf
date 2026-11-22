"""Bounded structural validation for PDF documents."""

from io import BytesIO

from pypdf import PdfReader

from .. import limits


class DocumentError(Exception):
    """The document cannot be processed safely."""


# Actions a reader may run on its own. A signature lends the holder's name to the file, so such a file is not signed.
ACTIVE = {"/JavaScript", "/Launch", "/ImportData", "/Rendition", "/RichMediaExecute"}


def _actions(value, depth: int = 0):
    """Every action in an action entry: one action, a list of them, and the ones chained after each with /Next."""
    if depth > 20:
        return
    value = value.get_object() if hasattr(value, "get_object") else value
    if isinstance(value, list):
        for item in value:
            yield from _actions(item, depth + 1)
    elif hasattr(value, "get"):
        yield value
        if "/Next" in value:
            yield from _actions(value["/Next"], depth + 1)


def _active(holder) -> bool:
    holder = holder.get_object() if hasattr(holder, "get_object") else holder
    if not hasattr(holder, "get"):
        return False
    entries = [holder.get("/OpenAction"), holder.get("/A")]
    triggers = holder.get("/AA")
    triggers = triggers.get_object() if hasattr(triggers, "get_object") else triggers
    if hasattr(triggers, "values"):
        entries += list(triggers.values())
    return any(action.get("/S") in ACTIVE for entry in entries if entry is not None for action in _actions(entry))


def _runs_something(reader: PdfReader) -> bool:
    """Whether the PDF has scripts or programs that run when it is opened, its pages shown or its links and fields used."""
    root = reader.trailer["/Root"]
    names = root.get("/Names")
    names = names.get_object() if hasattr(names, "get_object") else names
    if hasattr(names, "get") and "/JavaScript" in names:
        return True
    if _active(root):
        return True
    for page in reader.pages:
        if _active(page):
            return True
        for annotation in page.get("/Annots") or []:
            if _active(annotation):
                return True
    fields = root.get("/AcroForm")
    fields = fields.get_object() if hasattr(fields, "get_object") else fields
    for field in (fields.get("/Fields") or []) if hasattr(fields, "get") else []:
        if _active(field):
            return True
    return False


def validate_pdf(data: bytes) -> None:
    if not data.startswith(b"%PDF-") or len(data) > limits.current.max_file_bytes:
        raise DocumentError("The file is not a valid PDF or exceeds the size limit.")
    try:
        reader = PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted or not 1 <= len(reader.pages) <= limits.current.max_pages:
            raise DocumentError("The PDF is encrypted, empty, or has too many pages.")
        if _runs_something(reader):
            raise DocumentError("The PDF runs a script or a program when it is opened or used.")
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError("Could not read the PDF.") from exc
