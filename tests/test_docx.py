import shutil
import subprocess
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from zipfile import ZIP_DEFLATED, ZipFile

import pytest
from pypdf import PdfReader, PdfWriter

from sealwharf.documents.docx import DocxError, convert_docx_to_pdf, validate_docx

CONTENT_TYPES = b'''<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'''
DOC = b'''<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p/></w:body></w:document>'''


def docx(extra=None, document=DOC, types=CONTENT_TYPES):
    out = BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", types)
        archive.writestr("word/document.xml", document)
        for name, content in (extra or {}).items():
            archive.writestr(name, content)
    return out.getvalue()


def pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=400, height=600)
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


def test_rejects_unsafe_packages():
    validate_docx(docx())
    external = b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Target="https://example.invalid" TargetMode="External"/></Relationships>'
    dde = b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:fldSimple w:instr="DDEAUTO cmd"/></w:document>'
    for data in (b"not docx", docx({"word/vbaProject.bin": b"x"}),
                 docx({"word/embeddings/file.bin": b"x"}), docx({"word/_rels/document.xml.rels": external}),
                 docx(document=dde), docx({"../file": b"x"}),
                 docx(types=CONTENT_TYPES.replace(b"document.main+xml", b"document.macroEnabled.main+xml"))):
        with pytest.raises(DocxError):
            validate_docx(data)


def test_converts_with_isolated_profile_and_cleans_files(tmp_path, monkeypatch):
    executable = tmp_path / "soffice"
    executable.touch()
    directories = []
    def fake_run(command, **kwargs):
        root = Path(command[command.index("--outdir") + 1])
        directories.append(root)
        assert (root / "document.docx").read_bytes() == docx()
        assert (root / "profile" / "user" / "registrymodifications.xcu").exists()
        assert "--headless" in command
        assert kwargs["stdout"] == subprocess.DEVNULL
        (root / "document.pdf").write_bytes(pdf())
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr("sealwharf.documents.docx.subprocess.run", fake_run)
    assert convert_docx_to_pdf(docx(), executable) == pdf()
    assert not directories[0].exists()


def test_conversion_failures_are_reported(tmp_path, monkeypatch):
    executable = tmp_path / "soffice"
    executable.touch()
    monkeypatch.setattr("sealwharf.documents.docx.subprocess.run", lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("soffice", 1)))
    with pytest.raises(DocxError):
        convert_docx_to_pdf(docx(), executable, timeout=1)
    with pytest.raises(DocxError):
        convert_docx_to_pdf(docx(), tmp_path / "missing")


WORD_TYPES = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
              '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
              '<Default Extension="xml" ContentType="application/xml"/><Override PartName="/word/document.xml" '
              'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
WORD_RELS = ('<?xml version="1.0"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
             '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
             'Target="word/document.xml"/></Relationships>')


@pytest.mark.skipif(shutil.which("soffice") is None, reason="Needs LibreOffice, as installed in the container image")
def test_word_default_fonts_convert_to_fonts_of_the_same_width():
    """Aptos and Calibri must never fall back to a wide font, which breaks every line of the layout."""
    runs = "".join(f'<w:p><w:r><w:rPr><w:rFonts w:ascii="{font}" w:hAnsi="{font}"/></w:rPr><w:t>Sample in {font}</w:t></w:r></w:p>'
                   for font in ("Aptos", "Aptos Display", "Aptos Narrow", "Calibri", "Cambria", "Arial", "Times New Roman"))
    document = ('<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body>{runs}</w:body></w:document>")
    converted = convert_docx_to_pdf(docx({"_rels/.rels": WORD_RELS}, document.encode(), WORD_TYPES.encode()))
    used = {str(font.get_object()["/BaseFont"]).split("+")[-1]
            for page in PdfReader(BytesIO(converted)).pages for font in page["/Resources"]["/Font"].values()}
    assert not any("DejaVu" in name for name in used)
    assert any(name.startswith(("Carlito", "Aptos")) for name in used) and any(name.startswith("LiberationSans") for name in used)
