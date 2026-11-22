"""Gateway adapter for WAHA (https://waha.devlike.pro), a gateway you host yourself."""

import base64
import binascii
import re
from collections.abc import Iterator
from urllib.parse import urlsplit

import httpx

from ..documents.validation import DocumentError
from .gateway import HEADER, NUMBER, Gateway, GatewayError, Incoming

DIRECT = re.compile(r"([0-9]{5,20})@(c\.us|s\.whatsapp\.net|lid)")
FILE_PATH = re.compile(r"/api/files/[A-Za-z0-9._%@/-]{1,140}")
MEDIA_PREFIX = "waha."
NO_MEDIA = MEDIA_PREFIX + "none"  # a file the gateway did not fetch; downloading it fails
NOT_DOCUMENTS = ("audio/", "video/")


def _number(chat_id: object) -> str | None:
    """Return the phone number of an identifier such as `123@c.us`, or None for anything else."""
    match = DIRECT.fullmatch(chat_id) if isinstance(chat_id, str) else None
    return match[1] if match and match[2] != "lid" and NUMBER.fullmatch(match[1]) else None


def _media_id(url: object) -> str:
    """Keep only the path of a stored file, so it is always fetched from the configured gateway."""
    path = urlsplit(url).path if isinstance(url, str) else ""
    if not FILE_PATH.fullmatch(path) or ".." in path:
        return NO_MEDIA
    return MEDIA_PREFIX + base64.urlsafe_b64encode(path.encode()).decode().rstrip("=")


def _media_path(media_id: str) -> str:
    encoded = media_id.removeprefix(MEDIA_PREFIX)
    try:
        path = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode()
    except (binascii.Error, UnicodeDecodeError, ValueError):
        path = ""
    if not media_id.startswith(MEDIA_PREFIX) or not FILE_PATH.fullmatch(path) or ".." in path:
        raise DocumentError("Invalid media ID.")
    return path


class Waha(Gateway):
    def _headers(self, token: str) -> dict[str, str]:
        return {"X-Api-Key": token}

    def _phone_for_lid(self, lid: str) -> str | None:
        """Return the number behind a hidden identifier, or None when WhatsApp did not reveal it."""
        try:
            response = self.client.get(f"/api/{self.session}/lids/{lid}", timeout=10)
            response.raise_for_status()
            return _number(response.json().get("pn"))
        except (httpx.HTTPError, ValueError, AttributeError):
            return None

    def incoming(self, payload: object) -> list[Incoming]:
        if not isinstance(payload, dict) or payload.get("event") != "message" or payload.get("session") != self.session:
            return []
        message = payload.get("payload")
        if not isinstance(message, dict) or message.get("fromMe") is not False:
            return []
        sender = message.get("from")
        match = DIRECT.fullmatch(sender) if isinstance(sender, str) else None
        if match is None:
            return []  # a group, a channel or a status
        # WhatsApp may hide the sender's number. Without a number nobody is identified, so the message is dropped.
        address = self._phone_for_lid(match[1]) if match[2] == "lid" else _number(sender)
        if address is None:
            return []
        # The last part is WhatsApp's own message key; the parts before it repeat the sender.
        message_id = re.sub(r"[^A-Za-z0-9.-]", "-", str(message.get("id") or "").rsplit("_", 1)[-1])[:200]
        body = message.get("body") if isinstance(message.get("body"), str) else None
        if not message.get("hasMedia"):
            return [Incoming(message_id, address, text=body)] if body else []
        media = message.get("media")
        mime = media.get("mimetype") if isinstance(media, dict) else None
        picture = isinstance(mime, str) and mime.casefold().startswith("image/")
        if not isinstance(mime, str) or mime.casefold().startswith(NOT_DOCUMENTS) or (picture and not isinstance(media.get("url"), str)):
            return []
        filename = media.get("filename") if isinstance(media.get("filename"), str) else None
        return [Incoming(message_id, address, None, _media_id(media.get("url")), filename or None, mime, body or None)]

    def check(self) -> None:
        try:
            response = self.client.get(f"/api/sessions/{self.session}", timeout=15)
            if response.status_code != 404:
                response.raise_for_status()
            status = response.json().get("status") if response.status_code != 404 else None
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            raise ValueError(self.REFUSED) from exc
        if status != "WORKING":
            raise ValueError(self.NOT_LINKED)

    def _hook(self, url: str, secret: str) -> dict:
        return {"url": url + "/webhook", "events": ["message"], "customHeaders": [{"name": HEADER, "value": secret}],
                "retries": {"policy": "exponential", "delaySeconds": 2, "attempts": 8}}

    @staticmethod
    def _owned(hook: object) -> bool:
        headers = hook.get("customHeaders") if isinstance(hook, dict) else None
        return isinstance(headers, list) and any(
            isinstance(header, dict) and str(header.get("name", "")).casefold() == HEADER.casefold() for header in headers)

    def connect_webhook(self, url: str, secret: str, *, allow_incomplete_snapshot: bool = False) -> None:
        """Add this bot's webhook to the session, keeping its other settings and webhooks."""
        wanted = self._hook(url, secret)
        try:
            response = self.client.get(f"/api/sessions/{self.session}")
            if response.status_code == 404:
                self.client.post("/api/sessions", json={"name": self.session, "start": True,
                                                        "config": {"webhooks": [wanted]}}).raise_for_status()
            else:
                response.raise_for_status()
                config = response.json().get("config") or {}
                hooks = config.get("webhooks") or []
                if not isinstance(config, dict) or not isinstance(hooks, list):
                    raise GatewayError("Unexpected session settings format.")
                if [hook for hook in hooks if self._owned(hook)] != [wanted]:
                    # Changing the settings restarts the session, so it is done only when they differ.
                    others = [hook for hook in hooks if not self._owned(hook)]
                    self.client.put(f"/api/sessions/{self.session}",
                                    json={"config": {**config, "webhooks": [*others, wanted]}}).raise_for_status()
            response = self.client.get(f"/api/sessions/{self.session}")
            response.raise_for_status()
            observed = (response.json().get("config") or {}).get("webhooks") or []
        except (httpx.HTTPError, ValueError, AttributeError):
            raise GatewayError("The webhook update could not be verified.") from None
        if sum(1 for hook in observed if isinstance(hook, dict) and hook.get("url") == wanted["url"] and self._owned(hook)) != 1:
            raise GatewayError("The updated webhook could not be uniquely verified.")

    def _lookup(self, number: str) -> str | None:
        try:
            response = self.client.get("/api/contacts/check-exists", params={"phone": number, "session": self.session})
            response.raise_for_status()
            result = response.json()
            return _number(result.get("chatId")) if result.get("numberExists") is True else None
        except (httpx.HTTPError, ValueError, AttributeError):
            return None

    def _media_chunks(self, media_id: str) -> Iterator[bytes]:
        with self.client.stream("GET", _media_path(media_id)) as response:
            response.raise_for_status()
            yield from response.iter_bytes()

    def _send_text(self, address: str, text: str) -> None:
        self.client.post("/api/sendText", json={"session": self.session, "chatId": f"{address}@c.us", "text": text,
                                                "linkPreview": False}).raise_for_status()

    def _send_document(self, address: str, name: str, data: bytes) -> None:
        file = {"mimetype": "application/pdf", "filename": name, "data": base64.b64encode(data).decode()}
        self.client.post("/api/sendFile", json={"session": self.session, "chatId": f"{address}@c.us", "file": file},
                         timeout=120).raise_for_status()
