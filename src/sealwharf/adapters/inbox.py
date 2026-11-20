"""Event validation and persistent queue of minimal metadata.

Only documents and a fixed set of short commands are stored. Free text is
never kept, so the database holds no conversation content.
"""

import json
import re
import sqlite3
import time
from pathlib import Path
from typing import NamedTuple

from ..authorization import canonical_number
from ..messages import normalized, phrase_kind
from .audit import DEFAULT_PERIOD, period_hours
from .gateway import Incoming
from .locking import WorkerLock, worker_lock_path

ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,200}$")
SECRET_FILE = re.compile(r"\.(p12|pfx|pem|key|jks|keystore)$", re.IGNORECASE)
ANSWERS = {"yes": "1", "no": "0", "all": "all"}
# A command, not a phrase of a language: it is written the same way by everyone, and answered in the reader's language.
STATUS_COMMANDS = frozenset({"bot status", "botstatus"})
# A document in one of these states waits for somebody's answer.
AWAITING = "('awaiting_choice', 'awaiting_confirmation')"
# A document in one of these states needs nothing more from the bot.
FINISHED = "('done', 'sent', 'verified', 'refused', 'needs_review', 'failed', 'rate_limited', 'duplicate', 'uncertain')"


def _question_kind(status: str) -> str:
    """An offered signature waits for the requester's choice; a pending one for its owner's approval."""
    return "choice" if status == "offered" else "approval"


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
    expires_at: float | None  # None while the question waits in line to be asked


class SigningRequestRecord(NamedTuple):
    request_id: str
    message_id: str
    requester: str
    filename: str | None
    original_sha256: str
    original_size: int
    question: str | None = None  # what the requester is asked, kept until it is their turn


def parse_command(text: object) -> str | None:
    """Reduce a chat message to the wake phrase, a command or an answer, or None so it is discarded.

    The number may also be used for ordinary conversations, which are never kept.
    """
    if not isinstance(text, str) or len(text) > 40:
        return None
    # The whole message must be exactly a wake phrase, in any supported language and letter case: the
    # number is also a personal one, so nothing that merely resembles the phrase may get a reply.
    if phrase_kind(text, ("wake",)) is not None:
        return "ping"
    if normalized(text) in STATUS_COMMANDS:
        return "status"
    log = re.fullmatch(r"bot ?log(?: (\S+))?", normalized(text))
    if log is not None:
        hours = period_hours(log[1] or DEFAULT_PERIOD)
        return f"log {hours}" if hours is not None else None
    words = re.sub(r"[^a-z0-9 ]", "", normalized(text)).split()
    if len(words) != 1:
        return None
    # One digit, or a plain word in any supported language, answers the question the bot last asked this number.
    if re.fullmatch(r"[0-9]", words[0]):
        return "answer " + words[0]
    kind = phrase_kind(words[0], tuple(ANSWERS))
    return "answer " + ANSWERS[kind] if kind is not None else None


def asks_verification(caption: object) -> bool:
    """Whether the caption of a document asks to check its signatures instead of signing it."""
    return phrase_kind(caption, ("verify",)) is not None


def accepted_events(messages: list[Incoming], senders: frozenset[str]) -> list[Job]:
    """Keep documents, the wake phrase and answers sent directly by configured numbers; drop everything else.

    The gateway adapter already reduced the webhook call to direct messages from other people.
    """
    result = []
    for message in messages:
        if (not isinstance(message, Incoming) or not isinstance(message.address, str)
                or not isinstance(message.message_id, str) or not ID_PATTERN.fullmatch(message.message_id)):
            continue
        sender = canonical_number(message.address)
        seen = message.address if message.address != sender else None
        if sender not in senders:
            continue
        if message.media_id is None:
            command = parse_command(message.text)
            if command is not None:
                result.append(Job(message.message_id, "", None, sender, "text", command, seen))
            continue
        if not isinstance(message.media_id, str) or not ID_PATTERN.fullmatch(message.media_id):
            continue
        filename = message.filename
        if not isinstance(filename, str) or not filename.strip() or len(filename) > 1024:
            filename = None
        mime = message.mime_type
        if (filename and SECRET_FILE.search(filename.strip())) or (isinstance(mime, str) and "pkcs12" in mime.casefold()):
            # Key material must never be fetched, stored or processed.
            result.append(Job(message.message_id, "", None, sender, "refused", None, seen))
            continue
        body = "verify" if asks_verification(message.caption) else None
        result.append(Job(message.message_id, message.media_id, filename, sender, "document", body, seen))
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
            db.execute("CREATE TABLE IF NOT EXISTS notes (id INTEGER PRIMARY KEY AUTOINCREMENT, recipient TEXT NOT NULL, "
                       "key TEXT NOT NULL, filename TEXT, data TEXT NOT NULL)")
            for table, column in (("jobs", "digest"), ("requests", "question")):
                if column not in {row[1] for row in db.execute(f"PRAGMA table_info({table})")}:
                    db.execute(f"ALTER TABLE {table} ADD COLUMN {column} TEXT")

    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        # What is erased is overwritten in the file, not merely marked as free.
        connection.execute("PRAGMA secure_delete = ON")
        return connection

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

    def documents_in_flight(self, sender: str, settle_seconds: float = 0) -> bool:
        """Whether this number still has documents to be read, or sent one a moment ago."""
        with self.connect() as db:
            return db.execute("SELECT 1 FROM jobs WHERE sender=? AND kind='document' AND (state IN ('pending', 'processing') "
                              "OR (? > 0 AND created_at > datetime('now', ?))) LIMIT 1",
                              (sender, settle_seconds, f"-{int(settle_seconds)} seconds")).fetchone() is not None

    def documents_waiting(self) -> int:
        """Count the documents that wait for somebody's answer."""
        with self.connect() as db:
            return db.execute(f"SELECT COUNT(*) FROM jobs WHERE state IN {AWAITING}").fetchone()[0]

    def mark_document(self, message_id: str, sender: str, digest: str) -> bool:
        """Record what a document is; return True when the same number already has that same file in hand.

        A file counts as in hand while it waits for an answer, so nobody is asked twice about it.
        """
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            repeated = db.execute(
                f"SELECT 1 FROM jobs WHERE sender=? AND digest=? AND message_id<>? AND state IN {AWAITING} LIMIT 1",
                (sender, digest, message_id)).fetchone() is not None
            db.execute("UPDATE jobs SET digest=? WHERE message_id=?", (None if repeated else digest, message_id))
        return repeated

    def forget_finished(self, keep_seconds: float = 3600) -> None:
        """Erase what is known about documents that need nothing more.

        The file name, the caption and the fingerprint go at once. The bare
        entry stays for an hour, only to count the hourly limit and to ignore
        a message that the gateway delivers twice.
        """
        with self.connect() as db:
            db.execute(f"UPDATE jobs SET media_id='', filename=NULL, body=NULL, digest=NULL WHERE state IN {FINISHED} "
                       "AND (media_id<>'' OR filename IS NOT NULL OR body IS NOT NULL OR digest IS NOT NULL)")
            db.execute(f"DELETE FROM jobs WHERE state IN {FINISHED} AND created_at < datetime('now', ?)",
                       (f"-{int(keep_seconds)} seconds",))

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
            db.execute("INSERT INTO requests (request_id, message_id, requester, filename, original_sha256, original_size, "
                       "question, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (*record, time.time()))
            db.executemany("INSERT INTO request_signers VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                           [(s.request_id, s.signer_id, s.page, *s.box, s.field_name, s.status, s.owner, s.expires_at)
                            for s in signatures])

    def get_request(self, request_id: str) -> SigningRequestRecord | None:
        with self.connect() as db:
            row = db.execute("SELECT request_id, message_id, requester, filename, original_sha256, original_size, question "
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

    def open_question(self, number: str, now: float) -> tuple[str, list[str]] | None:
        """Return the one question this number was asked and has not answered yet.

        The result is ("choice", requests) when the number is a requester asked
        who should sign, or ("approval", requests) when it is an owner asked to
        approve. One question may cover several documents, oldest first, and a
        short reply such as "1" always answers that one question. Questions
        still waiting in line have not been asked and are never returned.
        """
        with self.connect() as db:
            rows = db.execute(
                "SELECT DISTINCT s.status, r.request_id, r.created_at, r.rowid FROM requests r "
                "JOIN request_signers s ON s.request_id = r.request_id "
                "WHERE s.expires_at > ? AND ((s.status='offered' AND r.requester=?) OR (s.status='pending' AND s.owner=?)) "
                "ORDER BY r.created_at, r.rowid", (now, number, number)).fetchall()
        if not rows:
            return None
        status = rows[-1][0]
        return (_question_kind(status), [row[1] for row in rows if row[0] == status])

    def waiting_questions(self) -> list[tuple[str, str, str, str, str | None]]:
        """Questions not asked yet, oldest first: (number to ask, kind, request, requester, question text)."""
        with self.connect() as db:
            rows = db.execute(
                "SELECT DISTINCT CASE s.status WHEN 'offered' THEN r.requester ELSE s.owner END, s.status, r.request_id, "
                "r.requester, r.question, r.created_at, r.rowid FROM requests r "
                "JOIN request_signers s ON s.request_id = r.request_id "
                "WHERE s.expires_at IS NULL AND s.status IN ('offered', 'pending') ORDER BY r.created_at, r.rowid").fetchall()
        return [(row[0], _question_kind(row[1]), row[2], row[3], row[4]) for row in rows]

    def ask(self, request_id: str, kind: str, number: str, expires_at: float) -> None:
        """Start the clock of a question at the moment it is asked."""
        with self.connect() as db:
            if kind == "choice":
                db.execute("UPDATE request_signers SET expires_at=? WHERE request_id=? AND status='offered' AND expires_at IS NULL",
                           (expires_at, request_id))
            else:
                db.execute("UPDATE request_signers SET expires_at=? WHERE request_id=? AND status='pending' AND owner=? "
                           "AND expires_at IS NULL", (expires_at, request_id, number))

    def requests_waiting_for(self, owner: str) -> int:
        """How many documents wait for this owner's approval, asked or still in line."""
        return len({entry.request_id for entry in self.pending_for_owner(owner)})

    def expired_signatures(self, now: float) -> list[PendingSignature]:
        return self._signatures("status='pending' AND expires_at <= ?", (now,))

    def expired_offers(self, now: float) -> list[str]:
        return sorted({entry.request_id for entry in self._signatures("status='offered' AND expires_at <= ?", (now,))})

    def set_signature_status(self, request_id: str, signer_id: str, status: str) -> None:
        with self.connect() as db:
            db.execute("UPDATE request_signers SET status=? WHERE request_id=? AND signer_id=?", (status, request_id, signer_id))

    # Outcomes told to a person, gathered so that many documents make one message.

    def add_note(self, recipient: str, key: str, filename: str | None, values: dict) -> None:
        with self.connect() as db:
            db.execute("INSERT INTO notes (recipient, key, filename, data) VALUES (?, ?, ?, ?)",
                       (recipient, key, filename, json.dumps(values)))

    def note_recipients(self) -> list[str]:
        with self.connect() as db:
            return [row[0] for row in db.execute("SELECT recipient FROM notes GROUP BY recipient ORDER BY MIN(id)")]

    def take_notes(self, recipient: str) -> list[tuple[str, str | None, dict]]:
        """Remove and return a person's notes: each is told at most once, even if sending fails."""
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT key, filename, data FROM notes WHERE recipient=? ORDER BY id", (recipient,)).fetchall()
            db.execute("DELETE FROM notes WHERE recipient=?", (recipient,))
        return [(key, filename, json.loads(data)) for key, filename, data in rows]

    def delete_request(self, request_id: str) -> None:
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("DELETE FROM request_signers WHERE request_id=?", (request_id,))
            db.execute("DELETE FROM requests WHERE request_id=?", (request_id,))
