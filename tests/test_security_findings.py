"""Weaknesses found by the security review, each fixed and kept here as the behaviour the bot must have.

Each test went in with its fix, so the public repository never describes a weakness still open.
"""

import io
import zipfile

import pytest
from test_flow import OWNER_A, PREPARER, make_pdf, world  # noqa: F401  (world is a fixture)

from sealwharf.adapters.inbox import Inbox, Job, parse_command
from sealwharf.adapters.locking import worker_lock_path
from sealwharf.application.worker import process_one
from sealwharf.documents.docx import DocxError, validate_document

# Approval by the owner.

def test_an_answer_sent_before_its_question_was_asked_approves_nothing(world):  # noqa: F811
    """The owner answers one question twice in a row; the second answer must not approve a document seen only afterwards."""
    first = world.send(PREPARER, make_pdf("Alice Example"), filename="harmless.pdf")
    # A second request waits in line: the owner is asked about one document at a time.
    second = world.send(PREPARER, make_pdf("Alice Example", text="Something else entirely"), filename="other.pdf")
    assert [name for _, _, name in world.chat.forwarded] == ["harmless.pdf"]
    # Both answers leave the owner's phone before the bot reads either of them.
    world.inbox.add([Job("answer-1", "", None, OWNER_A, "text", parse_command("1")),
                     Job("answer-2", "", None, OWNER_A, "text", parse_command("ok"))])
    while process_one(world.inbox, world.chat, world.signers, runtime=world.runtime):
        pass
    assert world.state(first) == "sent"
    # The owner had not been shown other.pdf when the second answer was written.
    assert world.state(second) == "awaiting_confirmation"
    assert [name for _, _, name in world.chat.files] == ["harmless.pdf"]


# Files that reach LibreOffice.

OFFICE = ('xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0" xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0" '
          'xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0" xmlns:xlink="http://www.w3.org/1999/xlink" '
          'xmlns:svg="urn:oasis:names:tc:opendocument:xmlns:svg-compatible:1.0" xmlns:xforms="http://www.w3.org/2002/xforms"')
MANIFEST = ('<?xml version="1.0"?><manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0">'
            '<manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.text"/>'
            "</manifest:manifest>")
RELATIONSHIPS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
WORD = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
TYPES = ('<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
         '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
         '<Default Extension="xml" ContentType="application/xml"/>'
         '<Default Extension="docx" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document"/>'
         '<Override PartName="/word/document.xml" '
         'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')


def _odt(body: str) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        package.writestr(zipfile.ZipInfo("mimetype"), "application/vnd.oasis.opendocument.text")
        package.writestr("content.xml", f'<?xml version="1.0"?><office:document-content {OFFICE}><office:body><office:text>'
                                        f"<text:p>Text</text:p>{body}</office:text></office:body></office:document-content>")
        package.writestr("META-INF/manifest.xml", MANIFEST)
    return output.getvalue()


def _picture(target: str) -> str:
    return (f'<text:p><draw:frame svg:width="2cm" svg:height="2cm"><draw:image xlink:href="{target}" xlink:type="simple"/>'
            "</draw:frame></text:p>")


def _docx(document: str, relationships: str = "", parts: tuple = ()) -> bytes:
    package_relationships = "http://schemas.openxmlformats.org/package/2006/relationships"
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as package:
        package.writestr("[Content_Types].xml", TYPES)
        package.writestr("_rels/.rels", f'<?xml version="1.0"?><Relationships xmlns="{package_relationships}"><Relationship Id="rId1" '
                                        f'Type="{RELATIONSHIPS}/officeDocument" Target="word/document.xml"/></Relationships>')
        package.writestr("word/document.xml", f'<?xml version="1.0"?><w:document xmlns:w="{WORD}" xmlns:r="{RELATIONSHIPS}"><w:body>'
                                              f"<w:p><w:r><w:t>Text</w:t></w:r></w:p>{document}</w:body></w:document>")
        package.writestr("word/_rels/document.xml.rels", f'<?xml version="1.0"?><Relationships xmlns="{package_relationships}">'
                                                         f"{relationships}</Relationships>")
        for name, data in parts:
            package.writestr(name, data)
    return output.getvalue()


def _docx_with_an_unchecked_document_inside() -> bytes:
    # The inner document is never validated; LibreOffice merges it and follows its outside links.
    inner = _docx("", f'<Relationship Id="rId9" Type="{RELATIONSHIPS}/image" Target="http://198.51.100.7/x.png" TargetMode="External"/>')
    return _docx('<w:altChunk r:id="rId5"/>', f'<Relationship Id="rId5" Type="{RELATIONSHIPS}/aFChunk" Target="chunk.docx"/>',
                 (("word/chunk.docx", inner),))


# Each of these was converted by the LibreOffice of the image: it fetched the address, or put the local file in the PDF.
OUTSIDE_CONTENT = {
    "odt picture at an absolute path": lambda: _odt(_picture("/data/token-test-signed.pdf")),
    "odt picture above its own folder": lambda: _odt(_picture("x/../../secret.png")),
    "odt picture one folder up": lambda: _odt(_picture("./../secret.png")),
    "odt form model loaded from an address": lambda: _odt(
        '<office:forms><xforms:model id="M"><xforms:instance id="I" src="http://198.51.100.7/x.xml"/></xforms:model></office:forms>'),
    "docx with another document inside": _docx_with_an_unchecked_document_inside,
}


@pytest.mark.parametrize("case", list(OUTSIDE_CONTENT))
def test_a_document_that_pulls_content_from_outside_itself_is_refused(case):
    with pytest.raises(DocxError):
        validate_document(OUTSIDE_CONTENT[case]())


# Availability.

class _HeldLock:
    """Stands for the worker lock, which needs POSIX, so the test also runs on Windows."""
    held = True

    def __init__(self, database):
        self.path = worker_lock_path(database)


def test_a_document_that_keeps_stopping_the_worker_is_given_up(tmp_path):
    """A file that crashes or hangs the worker must not be handed back to it on every restart, for ever."""
    inbox = Inbox(tmp_path / "inbox.db")
    inbox.add([Job("m1", "media1", "poison.pdf", OWNER_A)])
    for _ in range(5):
        assert inbox.claim() is not None       # the worker takes the document and dies while reading it
        inbox.recover_interrupted(_HeldLock(inbox.path))  # the container restarts
    assert inbox.claim() is None


@pytest.mark.skipif(not hasattr(__import__("signal"), "setitimer"), reason="The reading time limit needs POSIX signals")
def test_a_document_that_is_slow_to_read_is_stopped_at_the_reading_limit(world, monkeypatch):  # noqa: F811
    import time

    from sealwharf import limits
    monkeypatch.setattr(limits, "current", limits.parse({"reading_seconds": 5}))
    monkeypatch.setattr("sealwharf.application.worker._referenced_signers", lambda *_: time.sleep(30))
    started = time.time()
    message = world.send(OWNER_A, make_pdf("Alice Example"))
    assert world.state(message) == "failed" and time.time() - started < 20
    assert "could not process" in world.chat.to(OWNER_A)[-1]
