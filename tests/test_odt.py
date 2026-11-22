import shutil
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZIP_STORED, ZipFile

import pytest
from pypdf import PdfReader

from sealwharf.documents.docx import DocxError, convert_docx_to_pdf, document_fonts, is_opendocument, validate_document

OFFICE = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" '
          'xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
          'xmlns:style="urn:oasis:names:tc:opendocument:xmlns:style:1.0" '
          'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" '
          'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" '
          'xmlns:script="urn:oasis:names:tc:opendocument:xmlns:script:1.0" '
          'xmlns:xlink="http://www.w3.org/1999/xlink"')
MANIFEST = ('<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
            '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
            '<manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>')


def content(body="<text:p>Signature: ____</text:p>", fonts='<style:font-face style:name="Liberation Serif" svg:font-family="\'Liberation Serif\'"/>'):
    return (f'<office:document-content {OFFICE}><office:font-face-decls>{fonts}</office:font-face-decls>'
            f'<office:body><office:text>{body}</office:text></office:body></office:document-content>')


def odt(body=None, extra=None, mimetype=b"application/vnd.oasis.opendocument.text"):
    """A LibreOffice Writer file: the uncompressed "mimetype" entry first, as the format requires."""
    out = BytesIO()
    with ZipFile(out, "w", ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", mimetype, compress_type=ZIP_STORED)
        archive.writestr("content.xml", content(body) if body is not None else content())
        archive.writestr("META-INF/manifest.xml", MANIFEST)
        for name, data in (extra or {}).items():
            archive.writestr(name, data)
    return out.getvalue()


def test_a_libreoffice_document_is_told_apart_and_accepted():
    assert is_opendocument(odt()) and validate_document(odt()) == "odt"
    assert not is_opendocument(odt(mimetype=b"application/vnd.oasis.opendocument.spreadsheet"))
    assert document_fonts(odt()) == ("Liberation Serif",)


@pytest.mark.parametrize("unsafe", [
    {"extra": {"Basic/Standard/Module1.xml": "<x/>"}},                                       # a macro library
    {"extra": {"Object 1/content.xml": "<x/>"}},                                             # an embedded object
    {"body": '<draw:frame><draw:image xlink:href="https://example.invalid/x.png"/></draw:frame>'},  # a picture from the web
    {"body": '<text:section><text:section-source xlink:href="../other.odt"/></text:section>'},     # a section from a file
    {"body": '<office:scripts><script:event-listener script:language="ooo:script"/></office:scripts>'},
])
def test_unsafe_libreoffice_documents_are_refused(unsafe):
    with pytest.raises(DocxError):
        validate_document(odt(**unsafe))


def test_a_picture_inside_the_document_is_fine():
    validate_document(odt(body='<draw:frame><draw:image xlink:href="Pictures/logo.png"/></draw:frame>',
                          extra={"Pictures/logo.png": b"png"}))


@pytest.mark.skipif(shutil.which("soffice") is None, reason="Needs LibreOffice, as installed in the container image")
def test_a_libreoffice_document_converts_to_pdf():
    pdf = convert_docx_to_pdf(odt(body="<text:p>Signature of Jane Example</text:p>"))
    assert "Jane Example" in PdfReader(BytesIO(pdf)).pages[0].extract_text()


def test_the_empty_list_of_scripts_libreoffice_always_writes_is_fine():
    validate_document(odt(body="<text:p>Signature:</text:p>", extra={"styles.xml": f"<office:document-styles {OFFICE}><office:scripts/></office:document-styles>"}))
