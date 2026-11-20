"""WhatsApp gateway operations, in the API of Whapi, with one configured host and transfer limits."""

import re
import time
import unicodedata

import httpx

from ..documents.validation import MAX_PDF_BYTES, DocumentError, validate_pdf

WHAPI_URL = "https://gate.whapi.cloud"
MEDIA_ID = re.compile(r"^[A-Za-z0-9]+-[0-9a-fA-F-]+$")


def signed_filename(original: str | None, suffix: str = " - signed") -> str:
    """Preserve the document name while preventing unsafe multipart filenames."""
    if not original:
        return f"document{suffix}.pdf"
    name = original.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if not unicodedata.category(c).startswith("C"))
    name = re.sub(r"[<>:\"|?*]", "_", name).strip()
    if name.lower().endswith((".pdf", ".docx")):
        name = name.rsplit(".", 1)[0].rstrip(" .")
    return f"{name.strip(' .')[:200] or 'document'}{suffix}.pdf"


def check_channel(token: str, transport: httpx.BaseTransport | None = None, url: str = WHAPI_URL) -> None:
    """Raise ValueError unless the token is valid and its channel is connected to WhatsApp."""
    failure = "The gateway token was refused or the gateway could not be reached."
    try:
        with httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"},
                          timeout=15, follow_redirects=False, transport=transport) as client:
            response = client.get("/health", params={"wakeup": "false"})
            response.raise_for_status()
            health = response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ValueError(failure) from exc
    if not isinstance(health, dict) or not isinstance(health.get("status"), dict):
        raise ValueError(failure)
    if health["status"].get("text") != "AUTH":
        raise ValueError("The gateway is not connected to WhatsApp. Link the WhatsApp account in the gateway.")


class Whapi:
    def __init__(self, token: str, transport: httpx.BaseTransport | None = None, addresses=None, pace: float = 0.0,
                 url: str = WHAPI_URL):
        # `addresses` remembers how WhatsApp identifies each configured number (the inbox does this).
        # `pace` is the least time, in seconds, between two messages sent.
        self.addresses = addresses
        self.pace = pace
        self._last_sent = 0.0
        self.client = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"},
                                   timeout=httpx.Timeout(30, connect=10), follow_redirects=False, transport=transport)

    def resolve(self, number: str) -> str:
        """Return the identifier WhatsApp uses for a number, which can differ from how it is written.

        A number that already wrote to the bot is known. Otherwise WhatsApp is
        asked once and the answer is kept; if that fails, the number is used as written.
        """
        if self.addresses is None:
            return number
        known = self.addresses.known_address(number)
        if known is None:
            known = self._lookup(number) or number
            if known != number:
                self.addresses.remember_address(number, known)
        return known

    def _lookup(self, number: str) -> str | None:
        try:
            response = self.client.post("/contacts", json={"blocking": "wait", "contacts": [number], "force_check": True})
            response.raise_for_status()
            contact = response.json()["contacts"][0]
            identifier = str(contact["wa_id"]).removesuffix("@s.whatsapp.net")
            valid = contact["status"] == "valid"
        except (httpx.HTTPError, ValueError, AttributeError, LookupError, TypeError):
            return None
        return identifier if valid and re.fullmatch(r"[0-9]{9,15}", identifier) else None

    def download_document(self, media_id: str) -> bytes:
        if not MEDIA_ID.fullmatch(media_id):
            raise DocumentError("Invalid media ID.")
        result = bytearray()
        with self.client.stream("GET", f"/media/{media_id}", headers={"Accept": "*/*"}) as response:
            response.raise_for_status()
            for chunk in response.iter_bytes():
                result.extend(chunk)
                if len(result) > MAX_PDF_BYTES:
                    raise DocumentError("Document exceeds the size limit.")
        data = bytes(result)
        if not data.startswith((b"%PDF-", b"PK\x03\x04")):
            raise DocumentError("The document is not a PDF or DOCX.")
        return data

    def download_pdf(self, media_id: str) -> bytes:
        data = self.download_document(media_id)
        validate_pdf(data)
        return data

    def _wait_turn(self) -> None:
        remaining = self._last_sent + self.pace - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self._last_sent = time.monotonic()

    def send_pdf(self, sender: str, data: bytes, filename: str | None = None, *, signed: bool = True) -> None:
        """Send a PDF; an unsigned one, forwarded for approval, keeps its plain name."""
        validate_pdf(data)
        self._wait_turn()
        name = signed_filename(filename, " - signed" if signed else "")
        response = self.client.post("/messages/document", data={"to": f"{self.resolve(sender)}@s.whatsapp.net", "mime_type": "application/pdf", "filename": name},
                                    files={"media": (name, data, "application/pdf")})
        response.raise_for_status()

    def send_text(self, sender: str, text: str) -> None:
        self._wait_turn()
        response = self.client.post("/messages/text", json={"to": f"{self.resolve(sender)}@s.whatsapp.net", "body": text})
        response.raise_for_status()

    def close(self) -> None:
        self.client.close()
