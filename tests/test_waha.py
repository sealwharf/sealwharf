import base64
import json
from io import BytesIO

import httpx
import pytest
from pypdf import PdfWriter

from sealwharf.adapters.gateway import HEADER, GatewayError, Incoming
from sealwharf.adapters.gateways import GATEWAYS, open_gateway
from sealwharf.adapters.inbox import Job, accepted_events
from sealwharf.adapters.waha import Waha
from sealwharf.documents.validation import DocumentError
from sealwharf.signer_config import GATEWAY_TYPES, AppConfig

NUMBER = "5511900000001"
URL = "http://waha.example:3000"
PDF = b"%PDF-1.7 test"


@pytest.fixture
def pdf_bytes() -> bytes:
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def gateway(handler, **options) -> Waha:
    return Waha("key", httpx.MockTransport(handler), url=URL, **options)


def nothing(request):
    raise AssertionError(f"unexpected call to {request.url.path}")


def event(**changes) -> dict:
    message = {"id": f"false_{NUMBER}@c.us_3EB0ABCDEF", "from": f"{NUMBER}@c.us", "fromMe": False, "body": "1", "hasMedia": False}
    return {"event": "message", "session": "default", "payload": {**message, **changes}}


def document(**changes) -> dict:
    media = {"url": "http://localhost:3000/api/files/default/3EB0ABCDEF.pdf", "mimetype": "application/pdf", "filename": "contract.pdf"}
    return event(**{"body": "", "hasMedia": True, "media": media, **changes})


def test_every_configurable_gateway_has_an_adapter():
    assert set(GATEWAY_TYPES) == set(GATEWAYS)
    config = AppConfig((), gateway_token="key", gateway_type="waha", gateway_url=URL, gateway_session="bot")
    opened = open_gateway(config)
    assert isinstance(opened, Waha) and opened.session == "bot" and opened.client.headers["x-api-key"] == "key"
    with pytest.raises(ValueError):
        Waha("key")  # a gateway you host has no default address


def test_text_and_document_are_read_in_the_neutral_form():
    waha = gateway(nothing)
    assert waha.incoming(event()) == [Incoming("3EB0ABCDEF", NUMBER, text="1")]
    [message] = waha.incoming(document(body="verify"))
    assert (message.address, message.filename, message.mime_type, message.caption) == (NUMBER, "contract.pdf", "application/pdf", "verify")
    assert accepted_events([message], frozenset({NUMBER})) == [Job("3EB0ABCDEF", message.media_id, "contract.pdf", NUMBER, "document", "verify")]


@pytest.mark.parametrize("payload", [
    event(fromMe=True),
    event(fromMe=None),
    event(**{"from": "120363000000000000@g.us"}),
    event(**{"from": "status@broadcast"}),
    event(**{"from": "120363000000000000@newsletter"}),
    event(body=""),
    document(media={"url": None, "mimetype": "image/jpeg"}),
    document(media=None),
    {**event(), "event": "message.ack"},
    {**event(), "session": "another"},
    "not an object",
])
def test_everything_else_is_dropped(payload):
    assert gateway(nothing).incoming(payload) == []


def test_hidden_sender_is_identified_only_when_whatsapp_reveals_the_number():
    known = {"99887766554433": "551199990001@c.us"}

    def handler(request):
        assert request.headers["x-api-key"] == "key"
        return httpx.Response(200, json={"lid": "x", "pn": known.get(request.url.path.rsplit("/", 1)[-1])})
    waha = gateway(handler)
    hidden = event(**{"from": "99887766554433@lid", "id": "false_99887766554433@lid_3EB0AA"})
    [message] = waha.incoming(hidden)
    # The number comes back as WhatsApp writes it; the inbox compares it in the written, thirteen-digit form.
    assert message == Incoming("3EB0AA", "551199990001", text="1")
    assert accepted_events([message], frozenset({"5511999990001"}))[0].sender == "5511999990001"
    # An identifier WhatsApp does not explain, or a gateway that fails, identifies nobody: nothing may be signed.
    assert waha.incoming(event(**{"from": "11111111111111@lid"})) == []
    assert gateway(lambda _: httpx.Response(500)).incoming(hidden) == []
    assert gateway(lambda _: httpx.Response(200, json={"pn": "120363000000000000@g.us"})).incoming(hidden) == []


def test_key_material_is_refused_even_when_the_gateway_did_not_fetch_it():
    [message] = gateway(nothing).incoming(document(media={"url": None, "mimetype": "application/x-pkcs12", "filename": "john.p12"}))
    assert accepted_events([message], frozenset({NUMBER})) == [Job("3EB0ABCDEF", "", None, NUMBER, "refused")]


def test_document_is_downloaded_only_from_the_gateway_files():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, content=PDF)
    waha = gateway(handler)
    [message] = waha.incoming(document())
    assert waha.download_document(message.media_id) == PDF
    assert str(requests[0].url) == URL + "/api/files/default/3EB0ABCDEF.pdf"
    # A file address that points elsewhere is never followed.
    for url in ("http://elsewhere.example/api/sessions", "http://waha.example:3000/api/files/../sessions", None):
        [other] = waha.incoming(document(media={"url": url, "mimetype": "application/pdf"}))
        with pytest.raises(DocumentError):
            waha.download_document(other.media_id)
    forged = "waha." + base64.urlsafe_b64encode(b"/api/sessions").decode().rstrip("=")
    for media_id in ("media-1", forged, "waha.***"):
        with pytest.raises(DocumentError):
            waha.download_document(media_id)
    assert len(requests) == 1


def test_text_and_signed_pdf_are_sent(pdf_bytes):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(201, json={})
    waha = gateway(handler, session="bot")
    waha.send_text(NUMBER, "Signed.")
    waha.send_pdf(NUMBER, pdf_bytes, "Report 004.docx")
    text, file = (json.loads(request.content) for request in requests)
    assert requests[0].url.path == "/api/sendText" and requests[1].url.path == "/api/sendFile"
    assert (text["session"], text["chatId"], text["text"]) == ("bot", f"{NUMBER}@c.us", "Signed.")
    assert file["chatId"] == f"{NUMBER}@c.us" and file["file"]["filename"] == "Report 004 - signed.pdf"
    assert base64.b64decode(file["file"]["data"]) == pdf_bytes


def test_check_tells_a_linked_gateway_from_the_rest():
    gateway(lambda _: httpx.Response(200, json={"status": "WORKING"})).check()
    for response in (httpx.Response(200, json={"status": "SCAN_QR_CODE"}), httpx.Response(404, json={})):
        with pytest.raises(ValueError, match="not connected"):
            gateway(lambda _, response=response: response).check()
    with pytest.raises(ValueError, match="refused"):
        gateway(lambda _: httpx.Response(401)).check()


class Session:
    """A gateway session that remembers its settings."""

    def __init__(self, config=None, *, exists=True):
        self.config, self.exists, self.calls = config or {}, exists, []

    def __call__(self, request):
        self.calls.append(request.method)
        if request.method == "POST":
            body = json.loads(request.content)
            self.config, self.exists = body["config"], body["name"] == "default" and body["start"] is True
        elif request.method == "PUT":
            self.config = json.loads(request.content)["config"]
        elif not self.exists:
            return httpx.Response(404, json={})
        return httpx.Response(200, json={"name": "default", "status": "WORKING", "config": self.config})


def test_webhook_is_added_without_touching_other_settings():
    other = {"url": "https://other.example/hook", "events": ["message"]}
    stale = {"url": "https://old.example/webhook", "events": ["message"], "customHeaders": [{"name": HEADER, "value": "old"}]}
    session = Session({"webhooks": [other, stale], "ignore": {"groups": True}})
    waha = gateway(session)
    waha.connect_webhook("https://bot.example.com", "s" * 43)
    hooks = session.config["webhooks"]
    assert session.config["ignore"] == {"groups": True} and hooks[0] == other and len(hooks) == 2
    assert hooks[1]["url"] == "https://bot.example.com/webhook" and hooks[1]["events"] == ["message"]
    assert hooks[1]["customHeaders"] == [{"name": HEADER, "value": "s" * 43}]
    # Asking again changes nothing, because changing the settings restarts the session.
    session.calls.clear()
    waha.connect_webhook("https://bot.example.com", "s" * 43)
    assert "PUT" not in session.calls


def test_webhook_creates_a_missing_session_and_reports_an_unverified_update():
    session = Session(exists=False)
    gateway(session).connect_webhook("https://bot.example.com", "s" * 43)
    assert session.exists and session.config["webhooks"][0]["url"] == "https://bot.example.com/webhook"
    with pytest.raises(GatewayError):
        gateway(lambda request: httpx.Response(200, json={"config": {}})).connect_webhook("https://bot.example.com", "s" * 43)
    with pytest.raises(GatewayError):
        gateway(lambda request: httpx.Response(500)).connect_webhook("https://bot.example.com", "s" * 43)
