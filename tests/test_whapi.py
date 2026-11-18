from io import BytesIO

import httpx
import pytest
from pypdf import PdfWriter

from sealwharf.adapters.whapi import DocumentError, Whapi, signed_filename


def pdf():
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def test_download_fixed_host_and_send():
    requests = []
    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=pdf() if request.method == "GET" else b"{}")
    client = Whapi("token", httpx.MockTransport(handler))
    data = client.download_pdf("pdf-abc123")
    client.send_pdf("5511999999999", data, "Report 004.docx")
    client.send_text("5511999999999", "Review needed")
    assert [str(r.url) for r in requests] == ["https://gate.whapi.cloud/media/pdf-abc123", "https://gate.whapi.cloud/messages/document", "https://gate.whapi.cloud/messages/text"]
    assert b"Report 004 - signed.pdf" in requests[1].content


@pytest.mark.parametrize(("original", "expected"), [
    ("Checklist.pdf", "Checklist - signed.pdf"),
    ("Report.DOCX", "Report - signed.pdf"),
    ("Relat\u00f3rio final.docx", "Relat\u00f3rio final - signed.pdf"),
    ("../folder\\contract.pdf", "contract - signed.pdf"),
    ("bad\r\nname.pdf", "badname - signed.pdf"),
    (".pdf", "document - signed.pdf"),
    (None, "document - signed.pdf"),
])
def test_signed_filename(original, expected):
    assert signed_filename(original) == expected


def test_rejects_arbitrary_urls_and_bad_pdf():
    client = Whapi("token", httpx.MockTransport(lambda _: httpx.Response(200, content=b"not pdf")))
    with pytest.raises(DocumentError):
        client.download_pdf("https://example.com/foo")
    with pytest.raises(DocumentError):
        client.download_pdf("pdf-abc123")
