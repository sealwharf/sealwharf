"""Event validation and persistent queue of minimal metadata.

Only documents and a fixed set of short commands are stored. Free text is
never kept, so the database holds no conversation content.
"""

import re
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple

from ..authorization import canonical_number
from .locking import WorkerLock, worker_lock_path

ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
SECRET_FILE = re.compile(r"\.(p12|pfx|pem|key|jks|keystore)$", re.IGNORECASE)
VERIFY_WORDS = frozenset({"verify", "verificar", "check"})
YES_WORDS = frozenset({"yes", "sim", "approve", "aprovar", "aprovo", "ok"})
NO_WORDS = frozenset({"no", "nao", "reject", "recusar", "reprovar", "cancel", "cancelar"})
ALL_WORDS = frozenset({"all", "todos", "todas", "tudo"})
PING_WORDS = frozenset({"botm4"})


class Job(NamedTuple):
    message_id: str
    media_id: str
    filename: str | None
    sender: str
    kind: str = "document"  # document, text or refused
    body: str | None = None
    # The identifier WhatsApp used for the sender, when it differs from the canonical number.
    address: str | None = None


class PendingSignature(NamedTuple):
    request_id: str
    signer_id: str
    page: int
    box: tuple[float, float, float, float]
    field_name: str | None
    status: str  # offered (waiting for the requester's choice), pending (waiting for the owner), signed, expired, rejected or failed
    owner: str | None
    expires_at: float | None


class SigningRequestRecord(NamedTuple):
    request_id: str
    message_id: str
    requester: str
    filename: str | None
    original_sha256: str
    original_size: int


def parse_command(text: object) -> str | None:
    """Reduce a chat message to the ping or an answer, or None so it is discarded.

    The number may also be used for ordinary conversations, which are never kept.
    """
    if not isinstance(text, str) or len(text) > 40:
        return None
    words = re.sub(r"[^a-z0-9 ]", "", text.strip().casefold().replace("ã", "a")).split()
    if len(words) == 1 and (re.fullmatch(r"[0-9]", words[0]) or words[0] in YES_WORDS | NO_WORDS | ALL_WORDS):
        # One digit, or a plain word, answers the question the bot last asked this number.
        word = words[0]
        return "answer " + ("1" if word in YES_WORDS else "0" if word in NO_WORDS else "all" if word in ALL_WORDS else word)
    if len(words) == 1 and words[0] in PING_WORDS:
        return "ping"
    return None


def asks_verification(caption: object) -> bool:
    """Whether the caption of a document asks to check its signatures instead of signing it."""
    return isinstance(caption, str) and caption.strip().casefold() in VERIFY_WORDS


def accepted_events(payload: object, senders: frozenset[str]) -> list[Job]:
    """Keep documents, the ping and answers sent directly by configured numbers; drop everything else."""
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
        chat_id = message.get("chat_id")
        if not isinstance(chat_id, str) or not chat_id.endswith("@s.whatsapp.net"):
            continue
        address = chat_id.removesuffix("@s.whatsapp.net")
        sender = canonical_number(address)
        seen = address if address != sender else None
        message_id = message.get("id")
        if (sender not in senders or message.get("from_me") is not False or message.get("from") not in (None, address)
                or not isinstance(message_id, str) or not ID_PATTERN.fullmatch(message_id)):
            continue
        if message.get("type") == "text":
            text = message.get("text")
            command = parse_command(text.get("body") if isinstance(text, dict) else None)
            if command is not None:
                result.append(Job(message_id, "", None, sender, "text", command, seen))
            continue
        document = message.get("document")
        if message.get("type") != "document" or not isinstance(document, dict):
            continue
        media_id = document.get("id")
        if not isinstance(media_id, str) or not ID_PATTERN.fullmatch(media_id):
            continue
        filename = document.get("file_name") or document.get("filename")
        if not isinstance(filename, str) or not filename.strip() or len(filename) > 1024:
            filename = None
        mime = document.get("mime_type")
        if (filename and SECRET_FILE.search(filename.strip())) or (isinstance(mime, str) and "pkcs12" in mime.casefold()):
            # Key material must never be fetched, stored or processed.
            result.append(Job(message_id, "", None, sender, "refused", None, seen))
            continue
        body = "verify" if asks_verification(document.get("caption")) else None
        result.append(Job(message_id, media_id, filename, sender, "document", body, seen))
    return result


class Inbox:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("CREATE TABLE IF NOT EXISTS jobs (message_id TEXT PRIMARY KEY, media_id TEXT NOT NULL, "
                       "filename TEXT, sender TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'document', body TEXT, "
                       "state TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)")
            db.execute("CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, message_id TEXT NOT NULL, "
                       "requester TEXT NOT NULL, filename TEXT, original_sha256 TEXT NOT NULL, original_size INTEGER NOT NULL, "
                       "created_at REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS request_signers (request_id TEXT NOT NULL, signer_id TEXT NOT NULL, "
                       "page INTEGER NOT NULL, x1 REAL NOT NULL, y1 REAL NOT NULL, x2 REAL NOT NULL, y2 REAL NOT NULL, "
                       "field_name TEXT, status TEXT NOT NULL, owner TEXT, expires_at REAL, "
                       "PRIMARY KEY (request_id, signer_id))")
            db.execute("CREATE TABLE IF NOT EXISTS marks (name TEXT PRIMARY KEY, value REAL NOT NULL)")
            db.execute("CREATE TABLE IF NOT EXISTS addresses (number TEXT PRIMARY KEY, address TEXT NOT NULL)")

    def connect(self):
        return sqlite3.connect(self.path, timeout=10)

    def add(self, jobs: list) -> None:
        rows = [Job(*job) for job in jobs]
        with self.connect() as db:
            db.executemany("INSERT OR IGNORE INTO jobs (message_id, media_id, filename, sender, kind, body) VALUES (?, ?, ?, ?, ?, ?)",
                           [tuple(job[:6]) for job in rows])
            db.executemany("INSERT OR REPLACE INTO addresses (number, address) VALUES (?, ?)",
                           [(job.sender, job.address) for job in rows if job.address])

    def known_address(self, number: str) -> str | None:
        """Return how WhatsApp identifies this number, if that was ever seen or looked up."""
        with self.connect() as db:
            row = db.execute("SELECT address FROM addresses WHERE number=?", (number,)).fetchone()
        return row[0] if row else None

    def remember_address(self, number: str, address: str) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO addresses (number, address) VALUES (?, ?)", (number, address))

    def claim(self) -> Job | None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT message_id, media_id, filename, sender, kind, body FROM jobs WHERE state='pending' ORDER BY created_at, rowid LIMIT 1").fetchone()
            if row:
                db.execute("UPDATE jobs SET state='processing' WHERE message_id=?", (row[0],))
            return Job(*row) if row else None

    def get_state(self, message_id: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT state FROM jobs WHERE message_id=?", (message_id,)).fetchone()
        return row[0] if row else None

    def set_state(self, message_id: str, state: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE jobs SET state=? WHERE message_id=?", (state, message_id))

    def recent_documents(self, sender: str, message_id: str, *, hours: float = 1.0) -> int:
        """Count the documents a number sent recently, up to and including this one."""
        with self.connect() as db:
            return db.execute("SELECT COUNT(*) FROM jobs WHERE sender=? AND kind='document' AND created_at >= datetime('now', ?) "
                              "AND rowid <= (SELECT rowid FROM jobs WHERE message_id=?)",
                              (sender, f"-{int(hours * 3600)} seconds", message_id)).fetchone()[0]

    def recover_interrupted(self, lock: WorkerLock) -> tuple[int, int]:
        """Recover jobs only while holding the exclusive lock for this inbox.

        Processing has no external send side effects and can be retried. An
        interrupted send may already have been delivered and must never be
        retried automatically. Return counts for requeued and uncertain jobs.
        """
        if not lock.held or lock.path != worker_lock_path(self.path):
            raise RuntimeError("The exclusive worker lock for this inbox is required.")
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            requeued = db.execute("UPDATE jobs SET state='pending' WHERE state='processing'").rowcount
            uncertain = db.execute("UPDATE jobs SET state='uncertain' WHERE state IN ('sending', 'review_sending', 'failure_sending')").rowcount
        return requeued, uncertain

    # Marks: heartbeat, one-shot flags and daily notices.

    def set_mark(self, name: str, value: float | None = None) -> None:
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO marks (name, value) VALUES (?, ?)", (name, time.time() if value is None else value))

    def get_mark(self, name: str) -> float | None:
        with self.connect() as db:
            row = db.execute("SELECT value FROM marks WHERE name=?", (name,)).fetchone()
        return row[0] if row else None

    def clear_mark(self, name: str) -> None:
        with self.connect() as db:
            db.execute("DELETE FROM marks WHERE name=?", (name,))

    # Signing requests that wait for an owner's confirmation.

    def create_request(self, record: SigningRequestRecord, signatures: list[PendingSignature]) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("INSERT INTO requests VALUES (?, ?, ?, ?, ?, ?, ?)", (*record, time.time()))
            db.executemany("INSERT INTO request_signers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           [(s.request_id, s.signer_id, s.page, *s.box, s.field_name, s.status, s.owner, s.expires_at)
                            for s in signatures])

    def get_request(self, request_id: str) -> SigningRequestRecord | None:
        with self.connect() as db:
            row = db.execute("SELECT request_id, message_id, requester, filename, original_sha256, original_size "
                             "FROM requests WHERE request_id=?", (request_id,)).fetchone()
        return SigningRequestRecord(*row) if row else None

    def _signatures(self, where: str, values: tuple) -> list[PendingSignature]:
        with self.connect() as db:
            rows = db.execute("SELECT request_id, signer_id, page, x1, y1, x2, y2, field_name, status, owner, expires_at "
                              f"FROM request_signers WHERE {where} ORDER BY rowid", values).fetchall()
        return [PendingSignature(r[0], r[1], r[2], tuple(r[3:7]), *r[7:]) for r in rows]

    def request_signatures(self, request_id: str) -> list[PendingSignature]:
        return self._signatures("request_id=?", (request_id,))

    def pending_for_owner(self, owner: str) -> list[PendingSignature]:
        return self._signatures("owner=? AND status='pending'", (owner,))

    def open_question(self, number: str, now: float) -> tuple[str, str] | None:
        """Return the newest question still waiting for this number's answer.

        The result is ("choice", request) when the number is a requester asked
        who should sign, or ("approval", request) when it is an owner asked to
        approve. A short reply such as "1" always answers that one question.
        """
        with self.connect() as db:
            row = db.execute(
                "SELECT s.status, r.request_id FROM requests r JOIN request_signers s ON s.request_id = r.request_id "
                "WHERE s.expires_at > ? AND ((s.status='offered' AND r.requester=?) OR (s.status='pending' AND s.owner=?)) "
                "ORDER BY r.created_at DESC LIMIT 1", (now, number, number)).fetchone()
        return ("choice" if row[0] == "offered" else "approval", row[1]) if row else None

    def expired_signatures(self, now: float) -> list[PendingSignature]:
        return self._signatures("status='pending' AND expires_at <= ?", (now,))

    def offers_for(self, requester: str) -> list[PendingSignature]:
        """The signers offered to a requester who has not chosen yet, newest document first."""
        return self._signatures("status='offered' AND request_id IN (SELECT request_id FROM requests WHERE requester=? "
                                "ORDER BY created_at DESC LIMIT 1)", (requester,))

    def expired_offers(self, now: float) -> list[str]:
        return sorted({entry.request_id for entry in self._signatures("status='offered' AND expires_at <= ?", (now,))})

    def set_signature_status(self, request_id: str, signer_id: str, status: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE request_signers SET status=? WHERE request_id=? AND signer_id=?", (status, request_id, signer_id))

    def delete_request(self, request_id: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM request_signers WHERE request_id=?", (request_id,))
            db.execute("DELETE FROM requests WHERE request_id=?", (request_id,))
