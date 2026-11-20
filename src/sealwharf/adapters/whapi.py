"""Gateway adapter for Whapi.Cloud and services that speak the same API."""

import re
from collections.abc import Iterator

import httpx

from ..documents.validation import DocumentError
from .gateway import NUMBER, Gateway, Incoming, signed_filename  # noqa: F401  (signed_filename is imported from here)
from .whapi_webhook import configure_verified

WHAPI_URL = "https://gate.whapi.cloud"
MEDIA_ID = re.compile(r"^[A-Za-z0-9]+-[0-9a-fA-F-]+$")
SUFFIX = "@s.whatsapp.net"


def read_messages(payload: object) -> list[Incoming]:
    """Read a Whapi webhook call: direct text and document messages sent by other people."""
    if not isinstance(payload, dict):
        return []
    event = payload.get("event")
    if not isinstance(event, dict) or event.get("type") != "messages" or event.get("event") != "post":
        return []
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return []
    result = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        chat_id, message_id = message.get("chat_id"), message.get("id")
        if not isinstance(chat_id, str) or not chat_id.endswith(SUFFIX) or not isinstance(message_id, str):
            continue
        address = chat_id.removesuffix(SUFFIX)
        if message.get("from_me") is not False or message.get("from") not in (None, address):
            continue
        if message.get("type") == "text":
            text = message.get("text")
            body = text.get("body") if isinstance(text, dict) else None
            if isinstance(body, str):
                result.append(Incoming(message_id, address, text=body))
            continue
        document = message.get("document")
        if message.get("type") != "document" or not isinstance(document, dict) or not isinstance(document.get("id"), str):
            continue

        def text_of(*keys: str, document=document) -> str | None:
            return next((document[key] for key in keys if isinstance(document.get(key), str) and document[key]), None)

        result.append(Incoming(message_id, address, None, document["id"], text_of("file_name", "filename"),
                               text_of("mime_type"), text_of("caption")))
    return result


class Whapi(Gateway):
    DEFAULT_URL = WHAPI_URL

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    def incoming(self, payload: object) -> list[Incoming]:
        return read_messages(payload)

    def check(self) -> None:
        try:
            response = self.client.get("/health", params={"wakeup": "false"}, timeout=15)
            response.raise_for_status()
            health = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ValueError(self.REFUSED) from exc
        if not isinstance(health, dict) or not isinstance(health.get("status"), dict):
            raise ValueError(self.REFUSED)
        if health["status"].get("text") != "AUTH":
            raise ValueError(self.NOT_LINKED)

    def connect_webhook(self, url: str, secret: str, *, allow_incomplete_snapshot: bool = False) -> None:
        configure_verified(self.client, url, secret, allow_incomplete_snapshot=allow_incomplete_snapshot)

    def _lookup(self, number: str) -> str | None:
        try:
            response = self.client.post("/contacts", json={"blocking": "wait", "contacts": [number], "force_check": True})
            response.raise_for_status()
            contact = response.json()["contacts"][0]
            identifier = str(contact["wa_id"]).removesuffix(SUFFIX)
            valid = contact["status"] == "valid"
        except (httpx.HTTPError, ValueError, AttributeError, LookupError, TypeError):
            return None
        return identifier if valid and NUMBER.fullmatch(identifier) else None

    def _media_chunks(self, media_id: str) -> Iterator[bytes]:
        if not MEDIA_ID.fullmatch(media_id):
            raise DocumentError("Invalid media ID.")
        with self.client.stream("GET", f"/media/{media_id}", headers={"Accept": "*/*"}) as response:
            response.raise_for_status()
            yield from response.iter_bytes()

    def _send_text(self, address: str, text: str) -> None:
        self.client.post("/messages/text", json={"to": address + SUFFIX, "body": text}).raise_for_status()

    def _send_document(self, address: str, name: str, data: bytes) -> None:
        self.client.post("/messages/document", data={"to": address + SUFFIX, "mime_type": "application/pdf", "filename": name},
                         files={"media": (name, data, "application/pdf")}).raise_for_status()


def check_channel(token: str, transport: httpx.BaseTransport | None = None, url: str = WHAPI_URL) -> None:
    """Raise ValueError unless the token is valid and its channel is connected to WhatsApp."""
    gateway = Whapi(token, transport, url=url)
    try:
        gateway.check()
    finally:
        gateway.close()
