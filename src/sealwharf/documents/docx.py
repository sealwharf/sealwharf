"""Conservative validation of Word (DOCX) and LibreOffice Writer (ODT) files, and local conversion to PDF."""

import re
import shutil
import subprocess
import tempfile
from io import BytesIO
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

from .. import limits
from .validation import validate_pdf

MAX_UNCOMPRESSED_BYTES = 100 * 1024 * 1024
MAX_MEMBERS = 5000
MAX_XML_BYTES = 20 * 1024 * 1024
MAIN_DOCUMENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"
RELATIONSHIPS_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/relationships"
CONTENT_TYPES_NAMESPACE = "http://schemas.openxmlformats.org/package/2006/content-types"
WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
DRAWING_NAMESPACE = "http://schemas.openxmlformats.org/drawingml/2006/main"
CONTENT_PART = re.compile(r"word/(?:document|footnotes|endnotes|header\d*|footer\d*)\.xml")
MAX_FONTS = 30
# Free fonts drawn to the same widths as the originals: replacing one keeps every line break.
_SANS = frozenset({"liberation sans", "arimo", "nimbus sans"})
_SERIF = frozenset({"liberation serif", "tinos", "nimbus roman"})
_MONO = frozenset({"liberation mono", "cousine", "nimbus mono ps"})
SAME_WIDTH = {
    "arial": _SANS, "helvetica": _SANS, "times new roman": _SERIF, "times": _SERIF,
    "courier new": _MONO, "courier": _MONO, "calibri": frozenset({"carlito"}), "cambria": frozenset({"caladea"}),
    "arial narrow": frozenset({"liberation sans narrow", "nimbus sans narrow"}),
    "palatino": frozenset({"p052"}), "palatino linotype": frozenset({"p052"}), "book antiqua": frozenset({"p052"}),
    "century schoolbook": frozenset({"c059"}), "bookman old style": frozenset({"urw bookman"}),
}
# LibreOffice draws these with its own symbol font.
SYMBOL_FONTS = frozenset({"symbol", "wingdings", "wingdings 2", "wingdings 3", "webdings"})
# A LibreOffice Writer text document names its type in a first, uncompressed "mimetype" entry.
ODT_MIMETYPE = b"application/vnd.oasis.opendocument.text"
ODF_XLINK = "http://www.w3.org/1999/xlink"
ODF_STYLE = "urn:oasis:names:tc:opendocument:xmlns:style:1.0"
ODF_SVG = "urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0"
XFORMS_NAMESPACE = "http://www.w3.org/2002/xforms"
# Documents of their own inside a package: LibreOffice merges them without the checks the outer file gets.
INNER_DOCUMENTS = (".docx", ".docm", ".dotx", ".dotm", ".doc", ".odt", ".ott", ".fodt", ".rtf", ".htm", ".html",
                   ".mht", ".mhtml", ".xhtml")
# Elements that run code, embed other programs or pull content from elsewhere when a document is opened.
ODT_UNSAFE = frozenset({"scripts", "script", "event-listener", "event-listeners", "dde-connection-decl",
                        "dde-connection-decls", "dde-connection", "section-source", "object", "object-ole", "applet",
                        "plugin", "floating-frame", "database-display", "database-next", "database-row-select",
                        "database-row-number", "database-name"})
UNSAFE_FIELD =re.compile(r"\b(?:DDE|DDEAUTO|LINK|HYPERLINK|INCLUDETEXT|INCLUDEPICTURE|DATABASE)\b", re.IGNORECASE)


class DocxError(Exception):
    """Document is unsafe, invalid, or could not be converted."""


def _parse_xml(data: bytes):
    if len(data) > MAX_XML_BYTES or b"<!doctype" in data.lower().replace(b"\x00", b""):
        raise DocxError("Unsupported XML content.")
    try:
        return ElementTree.fromstring(data)
    except ElementTree.ParseError as exc:
        raise DocxError("Invalid DOCX XML.") from exc


def is_opendocument(data: bytes) -> bool:
    """Whether the file is a LibreOffice Writer text document (.odt)."""
    try:
        with ZipFile(BytesIO(data)) as package:
            first = package.infolist()[0]
            return first.filename == "mimetype" and package.read(first)[:100].strip() == ODT_MIMETYPE
    except (BadZipFile, OSError, IndexError, RuntimeError, ValueError, KeyError):
        return False


def _check_entries(package: ZipFile, kind: str) -> set[str]:
    """Refuse oversized packages, odd paths, encrypted entries and embedded programs; return the entry names."""
    members = package.infolist()
    if len(members) > MAX_MEMBERS or sum(item.file_size for item in members) > MAX_UNCOMPRESSED_BYTES:
        raise DocxError(f"{kind} package exceeds resource limits.")
    names: set[str] = set()
    for item in members:
        name = item.filename
        lower = name.casefold()
        path = PurePosixPath(name)
        if (not name or "\\" in name or name.startswith("/") or ".." in path.parts
                or ":" in name or lower in names or item.flag_bits & 1):
            raise DocxError(f"Unsupported {kind} package entry.")
        names.add(lower)
        if ("vbaproject" in lower or "vbadat" in lower or "/activex/" in lower or "/embeddings/" in lower
                or lower.endswith(".exe") or lower.startswith(("basic/", "scripts/")) or lower.startswith("object ")
                or lower.endswith(INNER_DOCUMENTS)):
            raise DocxError("Embedded active content is not supported.")
    return names


def _inside(target: str, base: str, names: set[str]) -> bool:
    """Whether a reference stays inside the package: a "#anchor", or a part of the package that exists.

    Absolute paths, parent folders that leave the package and addresses with a scheme all point elsewhere.
    """
    if target.startswith("#"):
        return True
    if not target or target.startswith(("/", "\\")) or re.match(r"(?i)^[a-z][a-z0-9+.-]*:|^//", target):
        return False
    parts: list[str] = [part for part in base.split("/") if part]
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return False
            parts.pop()
        else:
            parts.append(part)
    joined = "/".join(parts).casefold()
    return joined in names or any(name.startswith(joined + "/") for name in names)


def validate_odt(data: bytes) -> None:
    """Reject LibreOffice documents with macros, embedded objects, scripts or links to outside content."""
    if len(data) > limits.current.max_file_bytes or not is_opendocument(data):
        raise DocxError("Invalid or oversized ODT file.")
    try:
        with ZipFile(BytesIO(data)) as package:
            names = _check_entries(package, "ODT")
            if "content.xml" not in names or "meta-inf/manifest.xml" not in names:
                raise DocxError("Missing ODT document parts.")
            for item in package.infolist():
                if not item.filename.casefold().endswith(".xml"):
                    continue
                root = _parse_xml(package.read(item))
                base = item.filename.rsplit("/", 1)[0] if "/" in item.filename else ""
                for node in root.iter():
                    namespace, _, local = node.tag.rpartition("}")
                    # LibreOffice always writes an empty list of scripts; only one with something in it is refused.
                    if local in ODT_UNSAFE and not (local in {"scripts", "event-listeners"} and len(node) == 0):
                        raise DocxError("Scripts, embedded objects and linked content are not supported.")
                    # A form model loads its data from an address of its own choosing.
                    if namespace == "{" + XFORMS_NAMESPACE:
                        raise DocxError("Form models are not supported.")
                    target = node.get(f"{{{ODF_XLINK}}}href")
                    if target is None:
                        continue
                    # A link in the text is only shown; anything else must be a part of this package.
                    hyperlink = local == "a" and re.match(r"(?i)^(https?|mailto):", target)
                    if not hyperlink and not _inside(target, base, names):
                        raise DocxError("Links to outside content are not supported.")
    except DocxError:
        raise
    except (BadZipFile, OSError, RuntimeError, ValueError, KeyError) as exc:
        raise DocxError("Invalid ODT package.") from exc


def validate_document(data: bytes) -> str:
    """Check a word-processor file and return its kind: "odt" for LibreOffice Writer, "docx" for Word."""
    if is_opendocument(data):
        validate_odt(data)
        return "odt"
    validate_docx(data)
    return "docx"


def validate_docx(data: bytes) -> None:
    """Reject oversized, macro-enabled, embedded, and externally linked packages.

    These checks are conservative, not an OS sandbox or a substitute for
    keeping the document converter updated.
    """
    if len(data) > limits.current.max_file_bytes or not data.startswith(b"PK\x03\x04"):
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
                    # A relationship of "word/_rels/document.xml.rels" is relative to "word/".
                    folder = item.filename.rsplit("_rels/", 1)[0].rstrip("/")
                    for rel in root.findall(f"{{{RELATIONSHIPS_NAMESPACE}}}Relationship"):
                        target = rel.get("Target", "")
                        if (rel.get("TargetMode", "").casefold() == "external"
                                or rel.get("Type", "").casefold().endswith("/afchunk")
                                or not _inside(target.lstrip("/"), "" if target.startswith("/") else folder, names)):
                            raise DocxError("External DOCX relationships are not supported.")
                elif lower.startswith("word/") and lower.endswith(".xml"):
                    root = _parse_xml(package.read(item))
                    field_parts = [node.text or "" for node in root.iter(f"{{{WORD_NAMESPACE}}}instrText")]
                    field_parts += [node.get(f"{{{WORD_NAMESPACE}}}instr", "")
                                    for node in root.iter(f"{{{WORD_NAMESPACE}}}fldSimple")]
                    field_text = " ".join(field_parts) + " " + "".join(field_parts)
                    if any(True for _ in root.iter(f"{{{WORD_NAMESPACE}}}altChunk")):
                        raise DocxError("Documents merged into a DOCX are not supported.")
                    if UNSAFE_FIELD.search(field_text):
                        raise DocxError("External document fields are not supported.")
    except DocxError:
        raise
    except (BadZipFile, OSError, RuntimeError, ValueError, KeyError) as exc:
        raise DocxError("Invalid DOCX package.") from exc


def _theme_fonts(package: ZipFile) -> dict[str, str]:
    name = next((item for item in sorted(package.namelist())
                 if item.casefold().startswith("word/theme/") and item.casefold().endswith(".xml")), None)
    if name is None:
        return {}
    root = _parse_xml(package.read(name))
    found = {}
    for kind in ("major", "minor"):
        latin = root.find(f".//{{{DRAWING_NAMESPACE}}}{kind}Font/{{{DRAWING_NAMESPACE}}}latin")
        if latin is not None and latin.get("typeface"):
            found[kind] = latin.get("typeface")
    return found


def _fonts_in(node, theme: dict[str, str]) -> list[str]:
    fonts = []
    for item in node.iter(f"{{{WORD_NAMESPACE}}}rFonts"):
        for attribute in ("ascii", "hAnsi"):
            themed = item.get(f"{{{WORD_NAMESPACE}}}{attribute}Theme", "")
            fonts.append(theme.get(themed[:5], "") if themed else item.get(f"{{{WORD_NAMESPACE}}}{attribute}", ""))
    return fonts


def _odt_fonts(data: bytes) -> list[str]:
    """Fonts a LibreOffice document declares for its text."""
    fonts = []
    with ZipFile(BytesIO(data)) as package:
        for name in ("styles.xml", "content.xml"):
            if name in package.namelist():
                root = _parse_xml(package.read(name))
                for face in root.iter(f"{{{ODF_STYLE}}}font-face"):
                    family = face.get(f"{{{ODF_SVG}}}font-family") or face.get(f"{{{ODF_STYLE}}}name") or ""
                    fonts.append(family.strip().strip("'\""))
    return fonts


def document_fonts(data: bytes) -> tuple[str, ...]:
    """Latin fonts a document asks for: for DOCX its defaults, the styles it uses and its own text."""
    if is_opendocument(data):
        return _unique(_odt_fonts(data))
    word = f"{{{WORD_NAMESPACE}}}"
    with ZipFile(BytesIO(data)) as package:
        names = {name.casefold(): name for name in package.namelist()}
        theme = _theme_fonts(package)
        direct: list[str] = []
        used: set[str] = set()
        for lower, name in names.items():
            if CONTENT_PART.fullmatch(lower):
                root = _parse_xml(package.read(name))
                direct += _fonts_in(root, theme)
                used.update(node.get(f"{word}val", "") for tag in ("pStyle", "rStyle", "tblStyle")
                            for node in root.iter(word + tag))
        fonts: list[str] = []
        if "word/styles.xml" in names:
            root = _parse_xml(package.read(names["word/styles.xml"]))
            for defaults in root.iter(f"{word}docDefaults"):
                fonts += _fonts_in(defaults, theme)
            styles = {style.get(f"{word}styleId", ""): style for style in root.iter(f"{word}style")}
            pending = [identifier for identifier, style in styles.items()
                       if identifier in used or style.get(f"{word}default") in ("1", "true")]
            seen: set[str] = set()
            while pending:
                identifier = pending.pop()
                if identifier in seen or identifier not in styles:
                    continue
                seen.add(identifier)
                fonts += _fonts_in(styles[identifier], theme)
                pending += [node.get(f"{word}val", "") for node in styles[identifier].iter(f"{word}basedOn")]
    return _unique(fonts + direct)


def _unique(fonts: list[str]) -> tuple[str, ...]:
    unique: dict[str, str] = {}
    for font in fonts:
        font = font.strip()
        if font and len(font) <= 64 and font.isprintable():
            unique.setdefault(font.casefold(), font)
    return tuple(unique.values())[:MAX_FONTS]


def substituted_fonts(data: bytes) -> tuple[tuple[str, str], ...]:
    """Fonts of a DOCX that are not installed, each with the font that takes its place.

    Replacements of the same width are left out, as they keep the layout. This
    is only advice for the requester, so any failure means no advice.
    """
    matcher = shutil.which("fc-match")
    if matcher is None:
        return ()
    replaced = []
    try:
        for font in document_fonts(data):
            key = font.casefold()
            if key in SYMBOL_FONTS:
                continue
            pattern = re.sub(r"([\\:,-])", r"\\\1", font)
            result = subprocess.run([matcher, "-f", "%{family}", "--", pattern], stdin=subprocess.DEVNULL,
                                    capture_output=True, text=True, timeout=10, check=False)
            families = [family.strip() for family in result.stdout.split(",") if family.strip()]
            if result.returncode != 0 or not families:
                continue
            installed = {family.casefold() for family in families}
            if key not in installed and not installed & SAME_WIDTH.get(key, frozenset()):
                replaced.append((font, families[0]))
    except (DocxError, BadZipFile, OSError, subprocess.SubprocessError, ValueError, KeyError):
        return ()
    return tuple(replaced)


def find_libreoffice() -> Path | None:
    """Find the LibreOffice executable installed in the image."""
    executable = shutil.which("soffice")
    return Path(executable) if executable else None


def convert_docx_to_pdf(data: bytes, executable: Path | None = None, timeout: float | None = None) -> bytes:
    """Convert a Word or LibreOffice Writer document to PDF without altering the original.

    Temporary data is removed on normal exit.
    """
    kind = validate_document(data)
    timeout = timeout or limits.current.conversion_seconds
    executable = executable or find_libreoffice()
    if executable is None or not executable.is_file():
        raise DocxError("LibreOffice is not installed or configured.")
    try:
        with tempfile.TemporaryDirectory(prefix="sealwharf-docx-") as temp:
            root = Path(temp)
            source = root / f"document.{kind}"
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
                pdf = stream.read(limits.current.max_file_bytes + 1)
            validate_pdf(pdf)
            return pdf
    except DocxError:
        raise
    except (OSError, subprocess.SubprocessError) as exc:
        raise DocxError("DOCX conversion could not be completed.") from exc
