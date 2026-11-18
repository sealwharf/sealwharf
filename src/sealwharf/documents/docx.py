"""Conservative DOCX validation and local LibreOffice conversion."""

import re
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from .validation import MAX_PDF_BYTES, validate_pdf

MAX_DOCX_BYTES = 20 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_MEMBERS = 5000
MAX_XML_BYTES = 20 * 1024 * 1024
MAIN_DOCUMENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
RELATIONSHIPS_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/content-types"
WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
UNSAFE_FIELD = re.compile(r"\b(?:DDE|DDEAUTO|LINK|HYPERLINK|INCLUDETEXT|INCLUDEPICTURE|DATABASE)\b", re.IGNORECASE)


class DocxError(Exception):
    """Document is unsafe, invalid, or could not be converted."""


def _parse_xml(data: bytes):
    if len(data) > MAX_XML_BYTES or b"<!doctype" in data.lower().replace(b"\x00", b""):
        raise DocxError("Unsupported XML content.")
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise DocxError("Invalid DOCX XML.") from exc


def validate_docx(data: bytes) -> None:
    """Reject oversized, macro-enabled, embedded, and externally linked packages.

    These checks are conservative, not an OS sandbox or a substitute for
    keeping the document converter updated.
    """
    if len(data) > MAX_DOCX_BYTES or not data.startswith(b"PK\x03\x04"):
        raise DocxError("Invalid or oversized DOCX file.")
    try:
        with ZipFile(BytesIO(data)) as package:
            members = package.infolist()
            if len(members) > MAX_MEMBERS or sum(item.file_size for item in members) > MAX_UNCOMPRESSED_BYTES:
                raise DocxError("DOCX package exceeds resource limits.")
            names: set[str] = set()
            for item in members:
                name = item.filename
                lower = name.casefold()
                path = PurePosixPath(name)
                if (not name or "\\" in name or name.startswith("/") or ".." in path.parts
                        or ":" in name or lower in names or item.flag_bits & 1):
                    raise DocxError("Unsupported DOCX package entry.")
                names.add(lower)
                if ("vbaproject" in lower or "vbadat" in lower or "/activex/" in lower
                        or "/embeddings/" in lower or lower.endswith(".exe")):
                    raise DocxError("Embedded active content is not supported.")
            if "[content_types].xml" not in names or "word/document.xml" not in names:
                raise DocxError("Missing DOCX document parts.")
            types_name = next(item.filename for item in members if item.filename.casefold() == "[content_types].xml")
            types = _parse_xml(package.read(types_name))
            main_types = [node.get("ContentType", "") for node in types.findall(f"{{{CONTENT_TYPES_NAMESPACE}}}Override")
                          if node.get("PartName", "").casefold() == "/word/document.xml"]
            if main_types != [MAIN_DOCUMENT_TYPE] or any("macroenabled" in str(node.attrib).casefold() for node in types):
                raise DocxError("The package is not a standard macro-free DOCX document.")
            for item in members:
                lower = item.filename.casefold()
                if lower.endswith(".rels"):
                    root = _parse_xml(package.read(item))
                    if any(rel.get("TargetMode", "").casefold() == "external"
                           or re.match(r"(?i)^[a-z][a-z0-9+.-]*:|^//", rel.get("Target", ""))
                           for rel in root.findall(f"{{{RELATIONSHIPS_NAMESPACE}}}Relationship")):
                        raise DocxError("External DOCX relationships are not supported.")
                elif lower.startswith("word/") and lower.endswith(".xml"):
                    root = _parse_xml(package.read(item))
                    field_parts = [node.text or "" for node in root.iter(f"{{{WORD_NAMESPACE}}}instrText")]
                    field_parts += [node.get(f"{{{WORD_NAMESPACE}}}instr", "")
                                    for node in root.iter(f"{{{WORD_NAMESPACE}}}fldSimple")]
                    field_text = " ".join(field_parts) + " " + "".join(field_parts)
                    if UNSAFE_FIELD.search(field_text):
                        raise DocxError("External document fields are not supported.")
    except DocxError:
        raise
    except (BadZipFile, OSError, RuntimeError, ValueError, KeyError) as exc:
        raise DocxError("Invalid DOCX package.") from exc


def find_libreoffice() -> Path | None:
    """Find the LibreOffice executable installed in the image."""
    executable = shutil.which("soffice")
    return Path(executable) if executable else None


def convert_docx_to_pdf(data: bytes, executable: Path | None = None, timeout: float = 60) -> bytes:
    """Convert without altering the original; temporary data is removed on normal exit."""
    validate_docx(data)
    executable = executable or find_libreoffice()
    if executable is None or not executable.is_file():
        raise DocxError("LibreOffice is not installed or configured.")
    try:
        with tempfile.TemporaryDirectory(prefix="sealwharf-docx-") as temp:
            root = Path(temp)
            source = root / "document.docx"
            source.write_bytes(data)
            profile = root / "profile"
            profile_user = profile / "user"
            profile_user.mkdir(parents=True)
            # A fresh profile has no trusted sources. Level 3 disables macros
            # except for trusted locations, which this temporary profile has none.
            (profile_user / "registrymodifications.xcu").write_text(
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<oor:items xmlns:oor="http://openoffice.org/2001/registry">'
                '<item oor:path="/org.openoffice.Office.Common/Security/Scripting">'
                '<prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop>'
                '</item></oor:items>', encoding="utf-8")
            command = [str(executable), f"-env:UserInstallation={profile.as_uri()}", "--headless", "--norestore",
                       "--convert-to", "pdf:writer_pdf_Export", "--outdir", str(root), str(source)]
            result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, timeout=timeout, check=False)
            if result.returncode != 0:
                raise DocxError("DOCX conversion failed.")
            output = root / "document.pdf"
            with output.open("rb") as stream:
                pdf = stream.read(MAX_PDF_BYTES + 1)
            validate_pdf(pdf)
            return pdf
    except DocxError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise DocxError("DOCX conversion could not be completed.") from exc
