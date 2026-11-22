"""What the bot needs from a WhatsApp gateway, whichever service provides it.

A gateway is the service that carries the messages: Whapi.Cloud, WAHA and so
on. Each one has its own API, so each gets an adapter: a subclass of `Gateway`
that reports incoming messages as `Incoming` and implements the few operations
marked below. Nothing outside the adapters knows which gateway is in use.

Deciding who may use the bot is not an adapter's job. An adapter only reports
which number WhatsApp says a message came from; when the gateway cannot tell,
the adapter must drop the message instead of guessing.
"""

import re
import time
import unicodedata
from collections.abc import Iterator
from typing import NamedTuple

import httpx

from ..documents.validation import MAX_PDF_BYTES, DocumentError, validate_pdf

HEADER = "X-Sealwharf-Secret"  # carries the webhook secret on every call from the gateway
NUMBER = re.compile(r"[0-9]{9,15}")


class GatewayError(Exception):
    """The gateway could not be prepared to deliver messages to this bot."""


class Incoming(NamedTuple):
    """One direct message from another person. A document has a `media_id`; a text does not."""
    message_id: str
    address: str  # the digits WhatsApp uses for the sender, which may differ from the written number
    text: str | None = None
    media_id: str | None = None
    filename: str | None = None
    mime_type: str | None = None
    caption: str | None = None


def signed_filename(original: str | None, suffix: str = " - signed") -> str:
    """Preserve the document name while preventing unsafe multipart filenames."""
    if not original:
        return f"document{suffix}.pdf"
    name = original.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if not unicodedata.category(c).startswith("C"))
    name = re.sub(r"[<>:\"|?*]", "_", name).strip()
    if name.lower().endswith((".pdf", ".docx", ".odt")):
        name = name.rsplit(".", 1)[0].rstrip(" .")
    return f"{name.strip(' .')[:200] or 'document'}{suffix}.pdf"


class Gateway:
    """Shared behaviour of every adapter: pacing, size limits and how numbers are addressed."""

    DEFAULT_URL: str | None = None  # None when the gateway has no public address of its own
    REFUSED = "The gateway token was refused or the gateway could not be reached."
    NOT_LINKED = "The gateway is not connected to WhatsApp. Link the WhatsApp account in the gateway."

    def __init__(self, token: str, transport: httpx.BaseTransport | None = None, addresses=None, pace: float = 0.0,
                 url: str | None = None, session: str = "default"):
        # `addresses` remembers how WhatsApp identifies each configured number (the inbox does this).
        # `pace` is the least time, in seconds, between two messages sent.
        if (url or self.DEFAULT_URL) is None:
            raise ValueError("gateway_url is missing: this gateway needs its address.")
        self.addresses = addresses
        self.pace = pace
        self.session = session
        self._last_sent = 0.0
        self.client = httpx.Client(base_url=url or self.DEFAULT_URL, headers=self._headers(token),
                                   timeout=httpx.Timeout(30, connect=10), follow_redirects=False, transport=transport)

    # What an adapter implements.

    def _headers(self, token: str) -> dict[str, str]:
        raise NotImplementedError

    def incoming(self, payload: object) -> list[Incoming]:
        """Read one webhook call. Return only direct messages from others whose number is known."""
        raise NotImplementedError

    def check(self) -> None:
        """Raise ValueError unless the token is accepted and the gateway is linked to WhatsApp."""
        raise NotImplementedError

    def connect_webhook(self, url: str, secret: str, *, allow_incomplete_snapshot: bool = False) -> None:
        """Make the gateway call `url`/webhook with `HEADER: secret`; raise GatewayError when unsure."""
        raise NotImplementedError

    def _lookup(self, number: str) -> str | None:
        """Ask WhatsApp how it identifies a number; None when that cannot be learned."""
        raise NotImplementedError

    def _media_chunks(self, media_id: str) -> Iterator[bytes]:
        raise NotImplementedError

    def _send_text(self, address: str, text: str) -> None:
        raise NotImplementedError

    def _send_document(self, address: str, name: str, data: bytes) -> None:
        raise NotImplementedError

    # What the rest of the bot uses.

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

    def download_document(self, media_id: str) -> bytes:
        result = bytearray()
        for chunk in self._media_chunks(media_id):
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
        self._send_document(self.resolve(sender), signed_filename(filename, " - signed" if signed else ""), data)

    def send_text(self, sender: str, text: str) -> None:
        self._wait_turn()
        self._send_text(self.resolve(sender), text)

    def close(self) -> None:
        self.client.close()
