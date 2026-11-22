"""Process queued chat events: sign, ask for confirmation, verify and report.

A certificate is used without asking only for its owner and for preparers
marked as auto-trusted. Any other request waits for the owner's approval,
which is bound to the SHA-256 of the exact document.

A person has one open question at a time and answers it with a plain digit.
Further questions wait in line, and questions that are alike are asked
together, so one answer may cover several documents. What happened to each
document is gathered into one message per person.
"""

import hashlib
import os
import re
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from pypdf import PdfReader

from ..adapters.audit import AuditLog, operational_logger
from ..adapters.inbox import Inbox, Job, PendingSignature, SigningRequestRecord
from ..adapters.local_ai import suggest_local_ai_position
from ..authorization import format_number
from ..documents.candidates import free_places_at_end, place_by_name, signature_places
from ..documents.contextual_position import suggest_contextual_position
from ..documents.docx import convert_docx_to_pdf, substituted_fonts
from ..documents.names import contains_name, matches_name, only_in_running_text
from ..documents.normalize import normalize_rotation
from ..documents.position import find_signature_position, has_signature_widgets
from ..documents.signing import (
    BreaksSignature,
    CertificateRejected,
    SignaturePosition,
    SigningError,
    SigningRequest,
    TokenPinRejected,
    TokenUnavailable,
    TrustSettings,
    overlapping,
    sign_pdf,
    sign_pdf_sequence,
)
from ..documents.validation import validate_pdf
from ..documents.verification import describe, verify_signatures
from ..documents.visual_position import suggest_visual_positions
from ..messages import DEFAULT_LANGUAGE, text, wake_reply
from .history import log_report
from .signers import UnlockedSigner, status_report, token_settings, words

SETTLE_SECONDS = 5


@dataclass(frozen=True)
class Runtime:
    """Where pending documents and the audit record live, the limits and each number's language."""
    data_dir: Path
    requests_per_hour: int = 20
    confirmation_minutes: int = 5
    languages: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)
    limits: dict[str, int] = field(default_factory=dict)
    # A question waits this long after the requester's last document, so documents sent together share it.
    settle_seconds: float = field(default_factory=lambda: SETTLE_SECONDS)
    # How many documents may wait for one owner's approval.
    queue_limit: int = 20
    # How long the record of signatures is kept.
    log_days: int = 90
    # The time zone of the times people read; None for the time zone of the server.
    timezone: str | None = None

    @property
    def audit(self) -> AuditLog:
        return AuditLog(self.data_dir / "audit.jsonl")

    def pending_file(self, request_id: str) -> Path:
        return self.data_dir / "pending" / f"{request_id}.pdf"

    @property
    def zone(self) -> ZoneInfo | None:
        return ZoneInfo(self.timezone) if self.timezone else None

    def language(self, number: str) -> str:
        return self.languages.get(number, DEFAULT_LANGUAGE)

    def limit(self, number: str) -> int:
        """How many documents this number may send per hour."""
        return self.limits.get(number, self.requests_per_hour)

    def called(self, number: str) -> str:
        """How a person is referred to in a message: by contact name, or by number when there is none."""
        return self.names.get(number, format_number(number))

    def person(self, number: str) -> str:
        """A person introduced to someone else: the name together with the number."""
        return f"{self.names[number]} ({format_number(number)})" if number in self.names else format_number(number)

    def say(self, client, number: str, key: str, **values: object) -> None:
        client.send_text(number, text(self.language(number), key, **values))

    def mention(self, client, number: str, key: str, **values: object) -> None:
        """Send a courtesy message; failing to deliver it never changes the outcome."""
        try:
            self.say(client, number, key, **values)
        except Exception:
            operational_logger().info("courtesy message not delivered key=%s", key)


# Telling people what happened.

# Outcomes that come with a signed document.
DELIVERED = frozenset({"signed_by", "partial", "partial_problem"})
# Outcomes of a document that is still on its way, and remarks that add to another outcome of the same document.
WAITING = frozenset({"owner_asked", "owner_queued", "progress"})
REMARKS = frozenset({"fonts_replaced"})
# How each kind of outcome opens its group in a summary, in the order the groups are shown.
MARKS = (("✅", DELIVERED | {"approved"}), ("⏳", WAITING), ("⚠️", REMARKS | {"duplicate"}))


def _mark(key: str) -> tuple[int, str]:
    """Where a group goes in the summary and its sign: done first, then waiting, remarks, and what was not signed."""
    return next(((index, sign) for index, (sign, keys) in enumerate(MARKS) if key in keys), (len(MARKS), "❌"))


def _natural(name: str) -> list:
    """Order file names as people do, so that "2 report" comes before "10 report"."""
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", name)]


def _note(inbox: Inbox, recipient: str, key: str, filename: str | None = None, **values: object) -> None:
    """Keep what happened to a document, to be told together with the person's other documents."""
    inbox.add_note(recipient, key, filename, values)


def _told(language: str, key: str, values: dict) -> str:
    """Write one outcome; a signature that could not be made is described in the reader's language."""
    if "problem" in values:
        values = {name: value for name, value in values.items() if name != "name"} | {
            "problem": text(language, "problem_" + values["problem"], name=values["name"])}
    return text(language, key, **values)


def tell_outcomes(inbox: Inbox, client, runtime: Runtime) -> None:
    """Tell each person what happened, once all the documents they sent have been read.

    One document gets its own message; several are gathered into one summary.
    """
    for recipient in inbox.note_recipients():
        if inbox.documents_in_flight(recipient):
            continue
        notes = inbox.take_notes(recipient)
        language = runtime.language(recipient)
        documents = [note for note in notes if note[0] not in REMARKS]
        if len(documents) <= 1:
            # One document: its outcome, and any remark about it, each in a sentence of its own.
            message = "\n".join(_told(language, key, values) for key, _, values in documents + [n for n in notes if n not in documents])
        else:
            groups: dict[tuple[int, str, str], list[str]] = {}
            for key, filename, values in notes:
                order, sign = _mark(key)
                groups.setdefault((order, sign, _told(language, "sum_" + key, values)), []).append(_file(runtime, recipient, filename))
            blocks = [text(language, "summary", count=len(documents))]
            for (_, sign, label), files in sorted(groups.items(), key=lambda item: item[0][0]):
                blocks.append(f"{sign} {label} ({len(files)})\n" + "\n".join(f"• {name}" for name in sorted(files, key=_natural)))
            message = "\n\n".join(blocks)
        # Whoever receives a signed document is asked to look at it, the place of each signature included.
        delivered = sum(1 for key, _, _ in notes if key in DELIVERED)
        if delivered:
            message += ("\n\n" if len(documents) > 1 else "\n") + text(language, "review_reminder_many" if delivered > 1 else "review_reminder")
        try:
            client.send_text(recipient, message)
        except Exception:
            operational_logger().info("outcome not delivered documents=%d", len(notes))


# Finding who signs, and where.

def _sources(document: bytes) -> list[tuple[str, ...]]:
    reader = PdfReader(BytesIO(document), strict=True)
    sources = [words(page.extract_text() or "") for page in reader.pages]
    sources.extend(words(name) for name in (reader.get_fields() or {}))
    return sources


def _referenced_signers(document: bytes, signers: tuple[UnlockedSigner, ...]) -> tuple[UnlockedSigner, ...]:
    """Identify authorized signers whose names appear in the document.

    Middle names may be abbreviated to initials. If one written name could
    belong to more than one authorized signer, nobody is identified.
    """
    if len(signers) <= 1:
        return signers
    sources = _sources(document)
    names = [[words(name) for name in signer.config.names] for signer in signers]
    for source in sources:
        for index in range(len(source)):
            if sum(any(matches_name(source[index:index + len(name)], name) for name in variants) for variants in names) > 1:
                return ()
    return tuple(signer for signer, variants in zip(signers, names, strict=True)
                 if any(contains_name(source, name) for source in sources for name in variants))


def _has_unresolved_name_variant(document: bytes, referenced: tuple[UnlockedSigner, ...],
                                 authorized: tuple[UnlockedSigner, ...]) -> bool:
    """Refuse a partial result if another authorized name might be abbreviated."""
    remaining = [signer for signer in authorized if signer not in referenced]
    if not remaining:
        return False
    sources = _sources(document)
    for signer in remaining:
        name = words(signer.config.name)
        if len(name) < 2:
            return True
        for source in sources:
            for index, first in enumerate(source):
                if first != name[0]:
                    continue
                suffix = "".join(name[1:])
                for count in range(1, min(len(name), 4)):
                    part = "".join(source[index + 1:index + 1 + count])
                    if len(part) >= 4 and (suffix.endswith(part) or
                            (len(part) >= 6 and SequenceMatcher(None, suffix, part).ratio() >= 0.9)):
                        return True
    return False


def _only_mentioned(document: bytes, signer: UnlockedSigner) -> bool:
    """Whether the document merely talks about this person: the name is never a field nor on a line of its own."""
    reader = PdfReader(BytesIO(document), strict=True)
    identities = [words(name) for name in signer.config.names]
    if any(contains_name(words(field), identity) for field in (reader.get_fields() or {}) for identity in identities):
        return False
    return only_in_running_text("\n".join(page.extract_text() or "" for page in reader.pages), identities)


def _locate_signature(document: bytes, signer: UnlockedSigner, contextual_review, general_suggest, choose_place=None):
    """Find the one place for this signer, trying each way the name is written."""
    found = {}
    for name in signer.config.names:
        position = _locate_named(document, name, signer.config.visual_review, contextual_review, general_suggest, choose_place)
        if position is not None:
            found[(position.page, tuple(position.box))] = position
    return next(iter(found.values())) if len(found) == 1 else None


def _named(document: bytes, name: str) -> bool:
    return any(contains_name(source, words(name)) for source in _sources(document))


def _locate_named(document: bytes, name: str, visual_review: str, contextual_review, general_suggest, choose_place=None):
    position = find_signature_position(BytesIO(document), name)
    if position is not None or has_signature_widgets(document):
        return position
    if visual_review == "ollama":
        candidate = suggest_local_ai_position(document, name)
        candidates = [candidate] if candidate is not None else []
    elif visual_review == "deterministic":
        candidates = suggest_visual_positions(document, signer_name=name)
    else:
        raise ValueError("Unsupported visual review mode")
    if len(candidates) == 1:
        return SignaturePosition(candidates[0].page, candidates[0].box)
    # Reading the document: the empty place whose own block names the person, or the one the local model picks.
    try:
        places = signature_places(document) if _named(document, name) else []
    except Exception:
        # A page this reading does not handle is left to the other ways below.
        places = []
    if places:
        by_name = place_by_name(places, name)
        if by_name is not None:
            return SignaturePosition(by_name.page, by_name.box)
        if choose_place is not None:
            verdict, place = choose_place(document, name)
            if verdict == "chosen":
                return SignaturePosition(place.page, place.box)
            if verdict == "none":
                return None
    contextual = None
    if contextual_review is not None:
        contextual = suggest_contextual_position(document, name)
        if contextual is not None and contextual_review(document, name, contextual) == contextual:
            return SignaturePosition(contextual.page, contextual.box)
    if contextual is None and general_suggest is not None:
        candidate = general_suggest(document, name)
        if candidate is not None:
            return SignaturePosition(candidate.page, candidate.box)
    return None


# Signing and recording.

def _place(position) -> SignaturePosition:
    """The place of a signature, whichever finder suggested it."""
    return SignaturePosition(position.page, tuple(position.box), getattr(position, "field_name", None))


def _request(signer: UnlockedSigner, position) -> SigningRequest:
    return SigningRequest(signer.config.certificate, signer.password, _place(position),
                          signer.config.logo, signer.config.logo_scale, signer.certificate_fingerprint,
                          TrustSettings(signer.config.timestamp_url, signer.config.trust_roots), token_settings(signer.config))


def _sign_one(document: bytes, request: SigningRequest) -> bytes:
    return sign_pdf(document, request.certificate, request.password, request.position,
                    request.logo, request.logo_scale, request.expected_certificate, request.trust, request.token)


class SignerFailed(Exception):
    """One person's signature could not be made, so whoever asked can be told whose it was and why."""

    def __init__(self, signer: UnlockedSigner, cause: Exception):
        super().__init__("A signature could not be made.")
        self.signer = signer
        self.problem = ("certificate" if isinstance(cause, CertificateRejected)
                        else "certified" if isinstance(cause, BreaksSignature)
                        else "token" if isinstance(cause, TokenUnavailable | TokenPinRejected) else "signature")

    @property
    def values(self) -> dict[str, str]:
        return {"problem": self.problem, "name": self.signer.config.name}


def _sign_as(document: bytes, signer: UnlockedSigner, position) -> bytes:
    try:
        return _sign_one(document, _request(signer, position))
    except Exception as exc:
        raise SignerFailed(signer, exc) from exc


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _audit(runtime: Runtime, signer: UnlockedSigner, *, request_id: str, requester: str, confirmed_by: str | None,
           before: bytes, after: bytes, filename: str | None) -> None:
    if confirmed_by is not None:
        approval = "confirmed"
    elif signer.config.owner is None:
        approval = "trusted"
    else:
        approval = "owner" if requester == signer.config.owner else "autotrust"
    runtime.audit.record(event="signed", request_id=request_id, certificate_id=signer.config.id,
                         certificate_sha256=signer.certificate_fingerprint.hex(), requester=requester,
                         confirmed_by=confirmed_by, approval=approval,
                         document_sha256=_digest(before), signed_sha256=_digest(after),
                         # The name as it was sent, on one line, so people recognize the document in their log.
                         filename=" ".join((filename or "").split())[:200] or None)
    operational_logger().info("signed request=%s certificate=%s approval=%s", request_id, signer.config.id, approval)


# Requests that wait for an owner's approval.

def _names(signers: tuple[UnlockedSigner, ...], entries: list[PendingSignature], *statuses: str) -> str:
    by_id = {signer.config.id: signer.config.name for signer in signers}
    return ", ".join(by_id.get(entry.signer_id, entry.signer_id) for entry in entries if entry.status in statuses)


def _new_request() -> str:
    return uuid4().hex[:12]


def _entry(request_id: str, signer: UnlockedSigner, position, status: str) -> PendingSignature:
    """One signature of a request, kept until it is made; its question has not been asked yet."""
    place = _place(position)
    return PendingSignature(request_id, signer.config.id, place.page, place.box, place.field_name, status, signer.config.owner, None)


def _deliver(inbox: Inbox, client, message_id: str, recipient: str, signed: bytes, filename: str | None) -> None:
    # A disruption during sending leaves an uncertain state; do not retry automatically.
    inbox.set_state(message_id, "sending")
    client.send_pdf(recipient, signed, filename)
    inbox.set_state(message_id, "sent")


def _write_pending(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _discard(inbox: Inbox, runtime: Runtime, request_id: str) -> None:
    inbox.delete_request(request_id)
    runtime.pending_file(request_id).unlink(missing_ok=True)


def _finish(inbox: Inbox, client, signers: tuple[UnlockedSigner, ...], runtime: Runtime, request_id: str,
            failure: SignerFailed | None = None) -> None:
    """Report progress; once nobody is left to confirm, deliver the result.

    A signature that has just failed is reported with the name of its signer and the reason.
    """
    record = inbox.get_request(request_id)
    if record is None:
        return
    entries = inbox.request_signatures(request_id)
    signed = _names(signers, entries, "signed")
    waiting = _names(signers, entries, "pending")
    missing = _names(signers, entries, "expired", "rejected", "failed")
    requester = record.requester
    if waiting:
        if signed:
            _note(inbox, requester, "progress", record.filename, signed=signed, waiting=waiting)
        return
    if signed:
        _deliver(inbox, client, record.message_id, requester, runtime.pending_file(request_id).read_bytes(), record.filename)
        if failure is not None:
            _note(inbox, requester, "partial_problem", record.filename, signed=signed, **failure.values)
        elif missing:
            _note(inbox, requester, "partial", record.filename, signed=signed, missing=missing)
        else:
            _note(inbox, requester, "signed_by", record.filename, signed=signed)
    else:
        inbox.set_state(record.message_id, "needs_review")
        if failure is not None:
            _note(inbox, requester, "signer_problem", record.filename, **failure.values)
        else:
            _note(inbox, requester, "not_signed", record.filename, missing=missing)
    _discard(inbox, runtime, request_id)


def forget_finished(inbox: Inbox, runtime: Runtime) -> None:
    """Keep nothing about a document once it is done: its entry in the queue and any copy left on disk."""
    inbox.forget_finished()
    folder = runtime.pending_file("x").parent
    for path in folder.iterdir() if folder.is_dir() else ():
        if inbox.get_request(path.stem) is None:
            path.unlink(missing_ok=True)


def expire_requests(inbox: Inbox, client, signers: tuple[UnlockedSigner, ...], runtime: Runtime, *, now: float | None = None) -> None:
    """Close confirmations that were not answered in time, telling whoever was asked that the time has ended."""
    moment = time.time() if now is None else now
    told: set[tuple[str, str | None]] = set()
    for request_id in inbox.expired_offers(moment):
        record = inbox.get_request(request_id)
        _discard(inbox, runtime, request_id)
        if record is not None:
            inbox.set_state(record.message_id, "needs_review")
            _note(inbox, record.requester, "choice_expired", record.filename)
    for entry in inbox.expired_signatures(moment):
        inbox.set_signature_status(entry.request_id, entry.signer_id, "expired")
        record = inbox.get_request(entry.request_id)
        # The owner hears it too, once for each document: a late answer must not seem to be still awaited.
        if record is not None and (entry.request_id, entry.owner) not in told:
            told.add((entry.request_id, entry.owner))
            _note(inbox, entry.owner, "approval_expired", record.filename)
        operational_logger().info("confirmation expired request=%s certificate=%s", entry.request_id, entry.signer_id)
        try:
            _finish(inbox, client, signers, runtime, entry.request_id)
        except Exception:
            record = inbox.get_request(entry.request_id)
            if record is not None:
                inbox.set_state(record.message_id, "uncertain")
                _discard(inbox, runtime, entry.request_id)


def _await_confirmations(inbox: Inbox, client, signers, runtime: Runtime, job: Job, document: bytes, planned) -> None:
    """Sign for trusted requesters now and put the other signatures in line for their owners' approval."""
    request_id = _new_request()
    current = document
    entries = []
    for signer, position in planned:
        if signer.config.needs_confirmation(job.sender):
            entries.append(_entry(request_id, signer, position, "pending"))
        else:
            signed = _sign_as(current, signer, position)
            _audit(runtime, signer, request_id=request_id, requester=job.sender, confirmed_by=None, before=current, after=signed,
                   filename=job.filename)
            current = signed
            entries.append(_entry(request_id, signer, position, "signed"))
    owners = list(dict.fromkeys(entry.owner for entry in entries if entry.status == "pending"))
    # Requests of other people that each owner has to answer before this one.
    ahead = sum(1 for owner in owners for waiting in {entry.request_id for entry in inbox.pending_for_owner(owner)}
                if (other := inbox.get_request(waiting)) is not None and other.requester != job.sender)
    _write_pending(runtime.pending_file(request_id), current)
    inbox.create_request(SigningRequestRecord(request_id, job.message_id, job.sender, job.filename, _digest(document), len(document)), entries)
    inbox.set_state(job.message_id, "awaiting_confirmation")
    called = ", ".join(runtime.called(owner) for owner in owners)
    if any(entry.status == "signed" for entry in entries):
        _finish(inbox, client, signers, runtime, request_id)
    elif ahead:
        _note(inbox, job.sender, "owner_queued", job.filename, owners=called, ahead=ahead)
    else:
        _note(inbox, job.sender, "owner_asked", job.filename, owners=called)
    operational_logger().info("approval requested request=%s", request_id)


def ask_questions(inbox: Inbox, client, signers: tuple[UnlockedSigner, ...], runtime: Runtime, *, now: float | None = None) -> None:
    """Ask each person the next question in their line, never more than one at a time.

    Questions that are alike are asked together: the same choice for several
    documents, or the approval of several documents sent by the same person.
    """
    moment = time.time() if now is None else now
    waiting = inbox.waiting_questions()
    seen: set[str] = set()
    for number, kind, _, requester, question in waiting:
        if number in seen:
            continue
        seen.add(number)
        # More documents may still join this question while the requester is sending.
        if inbox.open_question(number, moment) is not None or inbox.documents_in_flight(requester, runtime.settle_seconds):
            continue
        requests = [item[2] for item in waiting if item[:2] == (number, kind) and item[3] == requester
                    and (kind == "approval" or item[4] == question)]
        expires_at = moment + runtime.confirmation_minutes * 60
        try:
            if kind == "choice":
                _ask_choice(inbox, client, runtime, number, requests, question or "")
            else:
                _ask_approval(inbox, client, runtime, number, requests, requester)
            for request_id in requests:
                inbox.ask(request_id, kind, number, expires_at)
        except Exception:
            operational_logger().info("question not delivered kind=%s documents=%d", kind, len(requests))
            _abandon(inbox, client, signers, runtime, number, kind, requests)


def _file(runtime: Runtime, number: str, filename: str | None) -> str:
    """How a document is called in a message to this person."""
    name = (filename or "").strip() or text(runtime.language(number), "a_document")
    return name if len(name) <= 60 else name[:57] + "..."


def _files(inbox: Inbox, runtime: Runtime, number: str, requests: list[str]) -> str:
    records = [inbox.get_request(request_id) for request_id in requests]
    return ", ".join(_file(runtime, number, record.filename) for record in records if record is not None)


def _ask_choice(inbox: Inbox, client, runtime: Runtime, requester: str, requests: list[str], question: str) -> None:
    language = runtime.language(requester)
    files = _files(inbox, runtime, requester, requests)
    header = (text(language, "about", file=files) if len(requests) == 1
              else text(language, "about_many", count=len(requests), files=files))
    client.send_text(requester, header + "\n" + question)


def _ask_approval(inbox: Inbox, client, runtime: Runtime, owner: str, requests: list[str], requester: str) -> None:
    # The owner reads who is asking, then sees exactly what will be signed.
    if len(requests) == 1:
        runtime.say(client, owner, "approval_request", requester=runtime.person(requester))
    else:
        runtime.say(client, owner, "approval_request_many", requester=runtime.person(requester), count=len(requests),
                    files=_files(inbox, runtime, owner, requests))
    for request_id in requests:
        record = inbox.get_request(request_id)
        client.send_pdf(owner, runtime.pending_file(request_id).read_bytes(), record.filename if record else None, signed=False)


def _abandon(inbox: Inbox, client, signers, runtime: Runtime, number: str, kind: str, requests: list[str]) -> None:
    """Close questions that could not be asked, telling the requester that nothing more will be signed."""
    for request_id in requests:
        record = inbox.get_request(request_id)
        if record is None:
            continue
        try:
            if kind == "choice":
                _discard(inbox, runtime, request_id)
                inbox.set_state(record.message_id, "failed")
                _note(inbox, record.requester, "processing_failed", record.filename)
            else:
                for entry in inbox.request_signatures(request_id):
                    if entry.status == "pending" and entry.owner == number:
                        inbox.set_signature_status(request_id, entry.signer_id, "failed")
                _finish(inbox, client, signers, runtime, request_id)
        except Exception:
            inbox.set_state(record.message_id, "uncertain")
            _discard(inbox, runtime, request_id)


def _approve(inbox: Inbox, client, signers, runtime: Runtime, owner: str, requests: list[str], answer: str) -> None:
    """Act on an owner's reply to an approval request: 1 signs every document that was asked, 0 refuses them all."""
    if answer not in ("0", "1", "all"):
        runtime.say(client, owner, "answer_invalid")
        return
    for request_id in requests:
        _approve_one(inbox, client, signers, runtime, owner, request_id, answer)


def _approve_one(inbox: Inbox, client, signers, runtime: Runtime, owner: str, request_id: str, answer: str) -> None:
    record = inbox.get_request(request_id)
    entries = [entry for entry in inbox.request_signatures(request_id) if entry.status == "pending" and entry.owner == owner]
    by_id = {signer.config.id: signer for signer in signers if signer.config.owner == owner}
    if record is None or not entries or any(entry.signer_id not in by_id for entry in entries):
        return
    if answer == "0":
        for entry in entries:
            inbox.set_signature_status(request_id, entry.signer_id, "rejected")
        _note(inbox, owner, "refused", record.filename)
        _finish(inbox, client, signers, runtime, request_id)
        return
    path = runtime.pending_file(request_id)
    failure = None
    try:
        current = path.read_bytes()
        # The approval covers exactly the document the owner was sent.
        if _digest(current[:record.original_size]) != record.original_sha256:
            raise ValueError("The pending document does not match the one that was sent for approval.")
        steps = []
        for entry in entries:
            signed = _sign_as(current, by_id[entry.signer_id], SignaturePosition(entry.page, entry.box, entry.field_name))
            steps.append((by_id[entry.signer_id], current, signed))
            current = signed
        _write_pending(path, current)
    except Exception as exc:
        for entry in entries:
            inbox.set_signature_status(request_id, entry.signer_id, "failed")
        failure = exc if isinstance(exc, SignerFailed) else None
        _note(inbox, owner, "approval_rejected" if failure and failure.problem == "certificate" else "approval_failed", record.filename)
    else:
        for signer, before, after in steps:
            inbox.set_signature_status(request_id, signer.config.id, "signed")
            _audit(runtime, signer, request_id=request_id, requester=record.requester, confirmed_by=owner, before=before, after=after,
                   filename=record.filename)
        _note(inbox, owner, "approved", record.filename)
    _finish(inbox, client, signers, runtime, request_id, failure)


# Chat events.

def _handle_command(inbox: Inbox, client, signers: tuple[UnlockedSigner, ...], runtime: Runtime, job: Job) -> None:
    action, _, answer = (job.body or "").partition(" ")
    if action == "ping":
        client.send_text(job.sender, wake_reply(runtime.language(job.sender)))
    elif action == "status":
        client.send_text(job.sender, status_report(inbox, signers, runtime.language(job.sender)))
    elif action == "help":
        runtime.say(client, job.sender, "help")
    elif action == "log":
        client.send_text(job.sender, log_report(runtime.audit, signers, job.sender, runtime.language(job.sender), runtime.called,
                                                min(int(answer), runtime.log_days * 24), zone=runtime.zone))
    elif action == "answer":
        question = inbox.open_question(job.sender, time.time())
        # With no open question, a stray "1" or "yes" belongs to an ordinary conversation: stay silent.
        if question is None:
            pass
        elif question[0] == "choice":
            _choose(inbox, client, signers, runtime, job.sender, question[1], answer)
        else:
            _approve(inbox, client, signers, runtime, job.sender, question[1], answer)
    inbox.set_state(job.message_id, "done")


def _handle_document(inbox: Inbox, client, signers, contextual_review, general_suggest, runtime: Runtime, job: Job,
                     choose_place=None) -> None:
    message_id, media_id, filename, sender = job[:4]
    authorized = tuple(signer for signer in signers if sender in signer.config.allowed_senders)
    if not authorized:
        inbox.set_state(message_id, "needs_review")
        return
    try:
        if inbox.recent_documents(sender, message_id) > runtime.limit(sender):
            inbox.set_state(message_id, "rate_limited")
            _note(inbox, sender, "rate_limited", filename)
            return
        # Documents sent together are acknowledged once.
        if inbox.recent_documents(sender, message_id, hours=1 / 60) == 1:
            runtime.mention(client, sender, "received")
        document = client.download_document(media_id)
        if job.body == "verify":
            language = runtime.language(sender)
            report = (describe(verify_signatures(document), language) if document.startswith(b"%PDF-")
                      else text(language, "verify_only_pdf"))
            inbox.set_state(message_id, "review_sending")
            client.send_text(sender, report)
            inbox.set_state(message_id, "verified")
            return
        if inbox.mark_document(message_id, sender, _digest(document)):
            inbox.set_state(message_id, "duplicate")
            _note(inbox, sender, "duplicate", filename)
            return
        # A Word or LibreOffice Writer document becomes a PDF first.
        if document.startswith(b"PK\x03\x04"):
            replaced = substituted_fonts(document)
            document = convert_docx_to_pdf(document)
            if replaced:
                _note(inbox, sender, "fonts_replaced", filename, fonts=", ".join(f"{font} → {other}" for font, other in replaced))
        validate_pdf(document)
        document = normalize_rotation(document)
        referenced = _referenced_signers(document, authorized)
        if _has_unresolved_name_variant(document, referenced, authorized):
            referenced = ()
        located = [(signer, position) for signer in referenced
                   if (position := _locate_signature(document, signer, contextual_review, general_suggest, choose_place)) is not None]
        # Two people given the same place: neither is placed, so neither is signed there.
        clashing = {signer.config.id for signer, position in located
                    for other, place in located if other is not signer and overlapping([position, place])}
        located = [(signer, position) for signer, position in located if signer.config.id not in clashing]
        if not located:
            # Somebody named in the document, but with no clear place: offer to sign at the end of the last page.
            at_end = _at_end(document, referenced)
            if at_end:
                _offer_choice(inbox, client, runtime, job, document, at_end, [], at_end=True)
                return
            inbox.set_state(message_id, "needs_review")
            # Nobody named is told apart from somebody named without a place, so no certificate seems to be meant.
            if referenced:
                _note(inbox, sender, "no_location", filename, names=", ".join(signer.config.name for signer in referenced))
            else:
                _note(inbox, sender, "no_signer", filename)
            return
        # Somebody named only inside a sentence, with no place to sign, is the subject of the document, not a signer.
        unplaced = [signer for signer in referenced
                    if all(signer is not placed for placed, _ in located) and not _only_mentioned(document, signer)]
        if len(located) == 1 and not unplaced and not located[0][0].config.needs_confirmation(sender):
            _proceed(inbox, client, signers, runtime, job, document, located)
        else:
            _offer_choice(inbox, client, runtime, job, document, located, unplaced)
    except Exception as exc:
        _report_failure(inbox, message_id, sender, filename, exc)


def _at_end(document: bytes, referenced) -> list:
    """Places at the end of the last page for the signers whose names the document really writes, or none.

    The identity rule stands: a signer is offered only when the full name, or a registered alias,
    is written in the document, and not only inside a sentence about that person.
    """
    try:
        named = [signer for signer in referenced
                 if any(_named(document, name) for name in signer.config.names) and not _only_mentioned(document, signer)]
        spare = free_places_at_end(document, len(named)) if named else []
    except Exception:
        return []
    if not named or len(spare) != len(named):
        return []
    return [(signer, SignaturePosition(page, box, None)) for signer, (page, box) in zip(named, spare, strict=True)]


def _report_failure(inbox: Inbox, message_id: str, sender: str, filename: str | None, exc: Exception) -> None:
    # Never log API exceptions that may contain URLs or credentials.
    if inbox.get_state(message_id) in ("sending", "review_sending"):
        inbox.set_state(message_id, "uncertain")
        return
    inbox.set_state(message_id, "failed")
    if isinstance(exc, SignerFailed):
        _note(inbox, sender, "signer_problem", filename, **exc.values)
    else:
        _note(inbox, sender, "processing_failed", filename)


def _proceed(inbox: Inbox, client, signers, runtime: Runtime, job: Job, document: bytes, planned) -> None:
    """Sign for the chosen signers, asking their owners first where that is required."""
    waiting = [signer.config.owner for signer, _ in planned if signer.config.needs_confirmation(job.sender)]
    full = next((owner for owner in waiting if inbox.requests_waiting_for(owner) >= runtime.queue_limit), None)
    if full is not None:
        inbox.set_state(job.message_id, "needs_review")
        _note(inbox, job.sender, "queue_full", job.filename, owner=runtime.called(full))
        return
    if waiting:
        _await_confirmations(inbox, client, signers, runtime, job, document, planned)
        return
    if len(planned) == 1:
        signed = _sign_as(document, *planned[0])
    else:
        try:
            signed = sign_pdf_sequence(document, tuple(_request(signer, position) for signer, position in planned))
        except SigningError as exc:
            if exc.request_index is None:
                raise
            raise SignerFailed(planned[exc.request_index][0], exc) from exc
    for signer, _ in planned:
        _audit(runtime, signer, request_id=_new_request(), requester=job.sender, confirmed_by=None, before=document, after=signed,
               filename=job.filename)
    _deliver(inbox, client, job.message_id, job.sender, signed, job.filename)
    _note(inbox, job.sender, "signed_by", job.filename, signed=", ".join(signer.config.name for signer, _ in planned))


def _offer_choice(inbox: Inbox, client, runtime: Runtime, job: Job, document: bytes, located, unplaced, *,
                  at_end: bool = False) -> None:
    """Keep the document and put in line the question of who should sign, or whether to ask an owner.

    Nothing reaches a certificate owner until the requester has answered. The
    question is asked when it is its turn, together with the ones that are alike.
    """
    request_id = _new_request()
    entries = [_entry(request_id, signer, position, "offered") for signer, position in located]
    language = runtime.language(job.sender)
    if at_end:
        names = ", ".join(signer.config.name for signer, _ in located)
        question = text(language, "at_end", names=names, minutes=runtime.confirmation_minutes)
        if len(located) > 1:
            options = "\n".join(f"{index} - {signer.config.name}" for index, (signer, _) in enumerate(located, start=1))
            question = text(language, "at_end_many", names=names) + "\n" + text(
                language, "choose", minutes=runtime.confirmation_minutes, all=len(located) + 1, options=options)
    elif len(located) == 1 and not unplaced:
        question = text(language, "ask_owner", name=located[0][0].config.name, minutes=runtime.confirmation_minutes,
                        owner=runtime.called(located[0][0].config.owner))
    else:
        note = " (" + text(language, "needs_approval") + ")"
        options = "\n".join(f"{index} - {signer.config.name}{note if signer.config.needs_confirmation(job.sender) else ''}"
                            for index, (signer, _) in enumerate(located, start=1))
        question = text(language, "choose", minutes=runtime.confirmation_minutes, all=len(located) + 1, options=options)
    if unplaced:
        question += "\n" + text(language, "choose_unplaced", names=", ".join(signer.config.name for signer in unplaced))
    _write_pending(runtime.pending_file(request_id), document)
    inbox.create_request(SigningRequestRecord(request_id, job.message_id, job.sender, job.filename, _digest(document), len(document),
                                              question), entries)
    inbox.set_state(job.message_id, "awaiting_choice")


def _choose(inbox: Inbox, client, signers, runtime: Runtime, sender: str, requests: list[str], answer: str) -> None:
    """Act on the requester's reply to "who should sign?" or "ask the owner?", for every document that was asked."""
    options = len([entry for entry in inbox.request_signatures(requests[0]) if entry.status == "offered"])
    if answer != "0" and not (answer == "all" or 1 <= int(answer) <= options + 1):
        runtime.say(client, sender, "answer_invalid")
        return
    for request_id in requests:
        _choose_one(inbox, client, signers, runtime, sender, request_id, answer)


def _choose_one(inbox: Inbox, client, signers, runtime: Runtime, sender: str, request_id: str, answer: str) -> None:
    record = inbox.get_request(request_id)
    entries = [entry for entry in inbox.request_signatures(request_id) if entry.status == "offered"]
    if record is None or not entries:
        return
    if answer == "0":
        _discard(inbox, runtime, request_id)
        inbox.set_state(record.message_id, "needs_review")
        _note(inbox, sender, "choice_cancelled", record.filename)
        return
    number = len(entries) + 1 if answer == "all" else int(answer)
    chosen = entries if number > len(entries) else [entries[number - 1]]
    job = Job(record.message_id, "", record.filename, sender)
    try:
        document = runtime.pending_file(request_id).read_bytes()
        if _digest(document) != record.original_sha256:
            raise ValueError("The stored document does not match the one that was offered.")
        by_id = {signer.config.id: signer for signer in signers}
        planned = [(by_id[entry.signer_id], SignaturePosition(entry.page, entry.box, entry.field_name)) for entry in chosen]
        _discard(inbox, runtime, request_id)
        inbox.set_state(record.message_id, "processing")
        _proceed(inbox, client, signers, runtime, job, document, planned)
    except Exception as exc:
        _discard(inbox, runtime, request_id)
        _report_failure(inbox, record.message_id, sender, record.filename, exc)


def process_one(inbox: Inbox, client, signers: tuple[UnlockedSigner, ...], contextual_review=None, general_suggest=None,
                runtime: Runtime | None = None, choose_place=None) -> bool:
    """Handle one queued event; return False when the queue is empty."""
    runtime = runtime or Runtime(inbox.path.parent)
    expire_requests(inbox, client, signers, runtime)
    job = inbox.claim()
    if job is not None:
        _handle(inbox, client, signers, contextual_review, general_suggest, runtime, job, choose_place)
    # Outcomes first, so the last thing a person reads is the question that waits for an answer.
    tell_outcomes(inbox, client, runtime)
    ask_questions(inbox, client, signers, runtime)
    tell_outcomes(inbox, client, runtime)
    forget_finished(inbox, runtime)
    return job is not None


def _handle(inbox: Inbox, client, signers, contextual_review, general_suggest, runtime: Runtime, job: Job,
            choose_place=None) -> None:
    if job.kind == "document":
        _handle_document(inbox, client, signers, contextual_review, general_suggest, runtime, job, choose_place)
        return
    try:
        if job.kind == "refused":
            runtime.say(client, job.sender, "refused_secret")
            inbox.set_state(job.message_id, "refused")
        else:
            _handle_command(inbox, client, signers, runtime, job)
    except Exception:
        inbox.set_state(job.message_id, "uncertain")
