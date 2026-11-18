"""Process queued chat events: sign, ask for confirmation, verify and report.

A certificate is used without asking only for its owner and for preparers
marked as auto-trusted. Any other request waits for the owner to reply with a
one-time code that is bound to the SHA-256 of the exact document.
"""

import hashlib
import os
import time
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from io import BytesIO
from pathlib import Path
from uuid import uuid4

from pypdf import PdfReader

from ..adapters.audit import AuditLog, operational_logger
from ..adapters.inbox import Inbox, Job, PendingSignature, SigningRequestRecord
from ..adapters.local_ai import suggest_local_ai_position
from ..authorization import format_number
from ..documents.contextual_position import suggest_contextual_position
from ..documents.docx import convert_docx_to_pdf
from ..documents.names import contains_name, matches_name
from ..documents.normalize import normalize_rotation
from ..documents.position import find_signature_position, has_signature_widgets
from ..documents.signing import (
    CertificateRejected,
    SignaturePosition,
    SigningRequest,
    TrustSettings,
    sign_pdf,
    sign_pdf_sequence,
)
from ..documents.validation import validate_pdf
from ..documents.verification import describe, verify_signatures
from ..documents.visual_position import suggest_visual_positions
from ..messages import DEFAULT_LANGUAGE, text
from .signers import UnlockedSigner, token_settings, words

VERIFY_WINDOW_SECONDS = 300


@dataclass(frozen=True)
class Runtime:
    """Where pending documents and the audit record live, the limits and each number's language."""
    data_dir: Path
    requests_per_hour: int = 20
    confirmation_minutes: int = 5
    languages: dict[str, str] = field(default_factory=dict)
    names: dict[str, str] = field(default_factory=dict)

    @property
    def audit(self) -> AuditLog:
        return AuditLog(self.data_dir / "audit.jsonl")

    def pending_file(self, request_id: str) -> Path:
        return self.data_dir / "pending" / f"{request_id}.pdf"

    def language(self, number: str) -> str:
        return self.languages.get(number, DEFAULT_LANGUAGE)

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


def _locate_signature(document: bytes, signer: UnlockedSigner, contextual_review, general_suggest):
    """Find the one place for this signer, trying each way the name is written."""
    found = {}
    for name in signer.config.names:
        position = _locate_named(document, name, signer.config.visual_review, contextual_review, general_suggest)
        if position is not None:
            found[(position.page, tuple(position.box))] = position
    return next(iter(found.values())) if len(found) == 1 else None


def _locate_named(document: bytes, name: str, visual_review: str, contextual_review, general_suggest):
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


def _overlapping(positions) -> bool:
    for index, first in enumerate(positions):
        for second in positions[:index]:
            if first.page == second.page and not (first.box[2] <= second.box[0] or second.box[2] <= first.box[0]
                                                  or first.box[3] <= second.box[1] or second.box[3] <= first.box[1]):
                return True
    return False


# Signing and recording.

def _request(signer: UnlockedSigner, position) -> SigningRequest:
    return SigningRequest(signer.config.certificate, signer.password,
                          SignaturePosition(position.page, tuple(position.box), getattr(position, "field_name", None)),
                          signer.config.logo, signer.config.logo_scale, signer.certificate_fingerprint,
                          TrustSettings(signer.config.timestamp_url, signer.config.trust_roots), token_settings(signer.config))


def _sign_one(document: bytes, request: SigningRequest) -> bytes:
    return sign_pdf(document, request.certificate, request.password, request.position,
                    request.logo, request.logo_scale, request.expected_certificate, request.trust, request.token)


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _audit(runtime: Runtime, signer: UnlockedSigner, *, request_id: str, requester: str, confirmed_by: str | None,
           before: bytes, after: bytes) -> None:
    if confirmed_by is not None:
        approval = "confirmed"
    elif signer.config.owner is None:
        approval = "trusted"
    else:
        approval = "owner" if requester == signer.config.owner else "autotrust"
    runtime.audit.record(event="signed", request_id=request_id, certificate_id=signer.config.id,
                         certificate_sha256=signer.certificate_fingerprint.hex(), requester=requester,
                         confirmed_by=confirmed_by, approval=approval,
                         document_sha256=_digest(before), signed_sha256=_digest(after))
    operational_logger().info("signed request=%s certificate=%s approval=%s", request_id, signer.config.id, approval)


# Requests that wait for an owner's approval.

def _names(signers: tuple[UnlockedSigner, ...], entries: list[PendingSignature], *statuses: str) -> str:
    by_id = {signer.config.id: signer.config.name for signer in signers}
    return ", ".join(by_id.get(entry.signer_id, entry.signer_id) for entry in entries if entry.status in statuses)


def _write_pending(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)


def _discard(inbox: Inbox, runtime: Runtime, request_id: str) -> None:
    inbox.delete_request(request_id)
    runtime.pending_file(request_id).unlink(missing_ok=True)


def _finish(inbox: Inbox, client, signers: tuple[UnlockedSigner, ...], runtime: Runtime, request_id: str) -> None:
    """Report progress; once nobody is left to confirm, deliver the result."""
    record = inbox.get_request(request_id)
    if record is None:
        return
    entries = inbox.request_signatures(request_id)
    signed = _names(signers, entries, "signed")
    waiting = _names(signers, entries, "pending")
    missing = _names(signers, entries, "expired", "rejected", "failed")
    requester = record.requester
    if waiting:
        runtime.say(client, requester, "progress", signed=signed or text(runtime.language(requester), "nobody"), waiting=waiting)
        return
    if signed:
        # A disruption during sending leaves an uncertain state; do not retry automatically.
        inbox.set_state(record.message_id, "sending")
        client.send_pdf(requester, runtime.pending_file(request_id).read_bytes(), record.filename)
        if missing:
            runtime.say(client, requester, "partial", signed=signed, missing=missing)
        inbox.set_state(record.message_id, "sent")
        if not missing:
            runtime.mention(client, requester, "signed_by", signed=signed)
    else:
        inbox.set_state(record.message_id, "review_sending")
        runtime.say(client, requester, "not_signed", missing=missing)
        inbox.set_state(record.message_id, "needs_review")
    _discard(inbox, runtime, request_id)


def expire_requests(inbox: Inbox, client, signers: tuple[UnlockedSigner, ...], runtime: Runtime, *, now: float | None = None) -> None:
    """Close confirmations that were not answered in time."""
    moment = time.time() if now is None else now
    for request_id in inbox.expired_offers(moment):
        record = inbox.get_request(request_id)
        _discard(inbox, runtime, request_id)
        if record is not None:
            inbox.set_state(record.message_id, "needs_review")
            runtime.mention(client, record.requester, "choice_expired")
    for entry in inbox.expired_signatures(moment):
        inbox.set_signature_status(entry.request_id, entry.signer_id, "expired")
        operational_logger().info("confirmation expired request=%s certificate=%s", entry.request_id, entry.signer_id)
        try:
            _finish(inbox, client, signers, runtime, entry.request_id)
        except Exception:
            record = inbox.get_request(entry.request_id)
            if record is not None:
                inbox.set_state(record.message_id, "uncertain")
                _discard(inbox, runtime, entry.request_id)


def _await_confirmations(inbox: Inbox, client, signers, runtime: Runtime, job: Job, document: bytes, planned) -> None:
    """Sign for trusted requesters now and ask the other owners to approve."""
    request_id = uuid4().hex[:12]
    current = document
    entries = []
    expires_at = time.time() + runtime.confirmation_minutes * 60
    for signer, position in planned:
        base = (request_id, signer.config.id, position.page, tuple(position.box), getattr(position, "field_name", None))
        if signer.config.needs_confirmation(job.sender):
            entries.append(PendingSignature(*base, "pending", signer.config.owner, expires_at))
        else:
            signed = _sign_one(current, _request(signer, position))
            _audit(runtime, signer, request_id=request_id, requester=job.sender, confirmed_by=None, before=current, after=signed)
            current = signed
            entries.append(PendingSignature(*base, "signed", signer.config.owner, None))
    _write_pending(runtime.pending_file(request_id), current)
    inbox.create_request(SigningRequestRecord(request_id, job.message_id, job.sender, job.filename, _digest(document), len(document)), entries)
    try:
        owners = list(dict.fromkeys(entry.owner for entry in entries if entry.status == "pending"))
        for owner in owners:
            # The owner reads who is asking, then sees exactly what will be signed.
            runtime.say(client, owner, "approval_request", requester=runtime.person(job.sender))
            client.send_pdf(owner, current, job.filename, signed=False)
        inbox.set_state(job.message_id, "awaiting_confirmation")
        if any(entry.status == "signed" for entry in entries):
            _finish(inbox, client, signers, runtime, request_id)
        else:
            runtime.say(client, job.sender, "owner_asked", owners=", ".join(runtime.called(owner) for owner in owners))
    except Exception:
        _discard(inbox, runtime, request_id)
        raise
    operational_logger().info("approval requested request=%s", request_id)


def _approve(inbox: Inbox, client, signers, runtime: Runtime, owner: str, request_id: str, answer: str) -> None:
    """Act on an owner's reply to an approval request: 1 signs, 0 refuses."""
    record = inbox.get_request(request_id)
    entries = [entry for entry in inbox.request_signatures(request_id) if entry.status == "pending" and entry.owner == owner]
    by_id = {signer.config.id: signer for signer in signers if signer.config.owner == owner}
    if record is None or not entries or any(entry.signer_id not in by_id for entry in entries):
        runtime.say(client, owner, "answer_none")
        return
    if answer == "0":
        for entry in entries:
            inbox.set_signature_status(request_id, entry.signer_id, "rejected")
        runtime.say(client, owner, "refused")
        _finish(inbox, client, signers, runtime, request_id)
        return
    if answer not in ("1", "all"):
        runtime.say(client, owner, "answer_invalid")
        return
    path = runtime.pending_file(request_id)
    try:
        current = path.read_bytes()
        # The approval covers exactly the document the owner was sent.
        if _digest(current[:record.original_size]) != record.original_sha256:
            raise ValueError("The pending document does not match the one that was sent for approval.")
        steps = []
        for entry in entries:
            signed = _sign_one(current, _request(by_id[entry.signer_id], SignaturePosition(entry.page, entry.box, entry.field_name)))
            steps.append((by_id[entry.signer_id], current, signed))
            current = signed
        _write_pending(path, current)
    except Exception as exc:
        for entry in entries:
            inbox.set_signature_status(request_id, entry.signer_id, "failed")
        runtime.say(client, owner, "approval_rejected" if isinstance(exc, CertificateRejected) else "approval_failed")
    else:
        for signer, before, after in steps:
            inbox.set_signature_status(request_id, signer.config.id, "signed")
            _audit(runtime, signer, request_id=request_id, requester=record.requester, confirmed_by=owner, before=before, after=after)
        runtime.say(client, owner, "approved")
    _finish(inbox, client, signers, runtime, request_id)


# Chat events.

def _handle_command(inbox: Inbox, client, signers: tuple[UnlockedSigner, ...], runtime: Runtime, job: Job) -> None:
    action, _, answer = (job.body or "").partition(" ")
    if action == "help":
        runtime.say(client, job.sender, "help")
    elif action == "ping":
        runtime.say(client, job.sender, "hello")
    elif action == "verify":
        inbox.set_mark(f"verify:{job.sender}")
        runtime.say(client, job.sender, "verify_prompt")
    elif action == "answer":
        question = inbox.open_question(job.sender, time.time())
        if question is None:
            runtime.say(client, job.sender, "answer_none")
        elif question[0] == "choice":
            _choose(inbox, client, signers, runtime, job.sender, question[1], answer)
        else:
            _approve(inbox, client, signers, runtime, job.sender, question[1], answer)
    inbox.set_state(job.message_id, "done")


def _handle_document(inbox: Inbox, client, signers, contextual_review, general_suggest, runtime: Runtime, job: Job) -> None:
    message_id, media_id, filename, sender = job[:4]
    authorized = tuple(signer for signer in signers if sender in signer.config.allowed_senders)
    if not authorized:
        inbox.set_state(message_id, "needs_review")
        return
    try:
        if inbox.recent_documents(sender, message_id) > runtime.requests_per_hour:
            inbox.set_state(message_id, "review_sending")
            runtime.say(client, sender, "rate_limited")
            inbox.set_state(message_id, "rate_limited")
            return
        runtime.mention(client, sender, "received")
        document = client.download_document(media_id)
        mark = inbox.get_mark(f"verify:{sender}")
        if job.body == "verify" or (mark is not None and time.time() - mark <= VERIFY_WINDOW_SECONDS):
            inbox.clear_mark(f"verify:{sender}")
            language = runtime.language(sender)
            report = (describe(verify_signatures(document), language) if document.startswith(b"%PDF-")
                      else text(language, "verify_only_pdf"))
            inbox.set_state(message_id, "review_sending")
            client.send_text(sender, report)
            inbox.set_state(message_id, "verified")
            return
        if document.startswith(b"PK\x03\x04"):
            document = convert_docx_to_pdf(document)
        validate_pdf(document)
        document = normalize_rotation(document)
        referenced = _referenced_signers(document, authorized)
        if _has_unresolved_name_variant(document, referenced, authorized):
            referenced = ()
        located = [(signer, position) for signer in referenced
                   if (position := _locate_signature(document, signer, contextual_review, general_suggest)) is not None]
        unplaced = [signer for signer in referenced if all(signer is not placed for placed, _ in located)]
        if not located:
            inbox.set_state(message_id, "review_sending")
            runtime.say(client, sender, "no_location", names=", ".join(s.config.name for s in (referenced or authorized)))
            inbox.set_state(message_id, "needs_review")
            return
        if _overlapping([position for _, position in located]):
            raise ValueError("Signature areas overlap.")
        if len(located) == 1 and not unplaced and not located[0][0].config.needs_confirmation(sender):
            _proceed(inbox, client, signers, runtime, job, document, located)
        else:
            _offer_choice(inbox, client, runtime, job, document, located, unplaced)
    except Exception as exc:
        _report_failure(inbox, client, runtime, message_id, sender, exc)


def _report_failure(inbox: Inbox, client, runtime: Runtime, message_id: str, sender: str, exc: Exception) -> None:
    # Never log API exceptions that may contain URLs or credentials.
    if inbox.get_state(message_id) in ("sending", "review_sending"):
        inbox.set_state(message_id, "uncertain")
        return
    inbox.set_state(message_id, "failure_sending")
    try:
        runtime.say(client, sender, "certificate_rejected" if isinstance(exc, CertificateRejected) else "processing_failed")
        inbox.set_state(message_id, "failed")
    except Exception:
        inbox.set_state(message_id, "uncertain")


def _proceed(inbox: Inbox, client, signers, runtime: Runtime, job: Job, document: bytes, planned) -> None:
    """Sign for the chosen signers, asking their owners first where that is required."""
    waiting = [signer.config.owner for signer, _ in planned if signer.config.needs_confirmation(job.sender)]
    busy = next((owner for owner in waiting if inbox.pending_for_owner(owner)), None)
    if busy is not None:
        # An owner answers with a plain 1 or 0, so only one question may be open for them at a time.
        inbox.set_state(job.message_id, "review_sending")
        runtime.say(client, job.sender, "owner_busy", owner=runtime.called(busy))
        inbox.set_state(job.message_id, "needs_review")
        return
    if waiting:
        _await_confirmations(inbox, client, signers, runtime, job, document, planned)
        return
    requests = tuple(_request(signer, position) for signer, position in planned)
    signed = _sign_one(document, requests[0]) if len(requests) == 1 else sign_pdf_sequence(document, requests)
    for signer, _ in planned:
        _audit(runtime, signer, request_id=uuid4().hex[:12], requester=job.sender, confirmed_by=None, before=document, after=signed)
    # A disruption during sending leaves an uncertain state; do not retry automatically.
    inbox.set_state(job.message_id, "sending")
    client.send_pdf(job.sender, signed, job.filename)
    inbox.set_state(job.message_id, "sent")
    runtime.mention(client, job.sender, "signed_by", signed=", ".join(signer.config.name for signer, _ in planned))


def _offer_choice(inbox: Inbox, client, runtime: Runtime, job: Job, document: bytes, located, unplaced) -> None:
    """Keep the document and ask the requester who should sign, or whether to ask an owner.

    Nothing reaches a certificate owner until the requester has answered.
    """
    for stale in {entry.request_id for entry in inbox.offers_for(job.sender)}:
        record = inbox.get_request(stale)
        _discard(inbox, runtime, stale)
        if record is not None:
            inbox.set_state(record.message_id, "needs_review")
    request_id = uuid4().hex[:12]
    expires_at = time.time() + runtime.confirmation_minutes * 60
    entries = [PendingSignature(request_id, signer.config.id, position.page, tuple(position.box), getattr(position, "field_name", None),
                                "offered", signer.config.owner, expires_at) for signer, position in located]
    _write_pending(runtime.pending_file(request_id), document)
    inbox.create_request(SigningRequestRecord(request_id, job.message_id, job.sender, job.filename, _digest(document), len(document)), entries)
    try:
        language = runtime.language(job.sender)
        if len(located) == 1 and not unplaced:
            question = text(language, "ask_owner", name=located[0][0].config.name, minutes=runtime.confirmation_minutes,
                            owner=runtime.called(located[0][0].config.owner))
        else:
            note = " (" + text(language, "needs_approval") + ")"
            options = "\n".join(f"{index} - {signer.config.name}{note if signer.config.needs_confirmation(job.sender) else ''}"
                                for index, (signer, _) in enumerate(located, start=1))
            question = text(language, "choose", minutes=runtime.confirmation_minutes, all=len(located) + 1, options=options)
        if unplaced:
            question += "\n" + text(language, "choose_unplaced", names=", ".join(signer.config.name for signer in unplaced))
        inbox.set_state(job.message_id, "review_sending")
        client.send_text(job.sender, question)
        inbox.set_state(job.message_id, "awaiting_choice")
    except Exception:
        _discard(inbox, runtime, request_id)
        raise


def _choose(inbox: Inbox, client, signers, runtime: Runtime, sender: str, request_id: str, answer: str) -> None:
    """Act on the requester's reply to "who should sign?" or "ask the owner?"."""
    record = inbox.get_request(request_id)
    entries = [entry for entry in inbox.request_signatures(request_id) if entry.status == "offered"]
    if record is None or not entries:
        runtime.say(client, sender, "answer_none")
        return
    if answer == "0":
        _discard(inbox, runtime, request_id)
        inbox.set_state(record.message_id, "needs_review")
        runtime.say(client, sender, "choice_cancelled")
        return
    number = len(entries) + 1 if answer == "all" else int(answer)
    if not 1 <= number <= len(entries) + 1:
        runtime.say(client, sender, "answer_invalid")
        return
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
        _report_failure(inbox, client, runtime, record.message_id, sender, exc)


def process_one(inbox: Inbox, client, signers: tuple[UnlockedSigner, ...], contextual_review=None, general_suggest=None,
                runtime: Runtime | None = None) -> bool:
    """Handle one queued event; return False when the queue is empty."""
    runtime = runtime or Runtime(inbox.path.parent)
    expire_requests(inbox, client, signers, runtime)
    job = inbox.claim()
    if job is None:
        return False
    if job.kind == "document":
        _handle_document(inbox, client, signers, contextual_review, general_suggest, runtime, job)
        return True
    try:
        if job.kind == "refused":
            runtime.say(client, job.sender, "refused_secret")
            inbox.set_state(job.message_id, "refused")
        else:
            _handle_command(inbox, client, signers, runtime, job)
    except Exception:
        inbox.set_state(job.message_id, "uncertain")
    return True
