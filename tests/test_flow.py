"""End-to-end battery with self-signed certificates and an in-memory chat client."""

import hashlib
import logging
import time
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from pyhanko.pdf_utils.reader import PdfFileReader
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from sealwharf.adapters.audit import AuditLog
from sealwharf.adapters.inbox import Inbox, Job, accepted_events, parse_command
from sealwharf.adapters.whapi import Whapi, read_messages, signed_filename
from sealwharf.application.history import log_report
from sealwharf.application.signers import status_report, unlock_signers, warn_expiring
from sealwharf.application.worker import Runtime, ask_questions, expire_requests, process_one, tell_outcomes
from sealwharf.authorization import canonical_number, format_number
from sealwharf.documents.names import only_in_running_text
from sealwharf.documents.signing import CertificateRejected, TokenUnavailable
from sealwharf.documents.verification import describe, verify_signatures
from sealwharf.messages import CATALOGS, wake_reply
from sealwharf.messages import text as message_text
from sealwharf.signer_config import SignerConfig, load_config

OWNER_A, OWNER_B, OWNER_C = "5511900000001", "5511900000002", "5511900000003"
PREPARER, TRUSTED, STRANGER = "5511900000010", "5511900000011", "5511900000099"
NAMES = {"alice": "Alice Example", "bruno": "Bruno Example", "carla": "Carla Example"}
PEOPLE = {OWNER_A: "Ana", OWNER_C: "Carla", PREPARER: "Paulo Preparer"}


def make_certificate(folder, alias, common_name, *, days=365, password="pw-"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=400))
            .not_valid_after(now + timedelta(days=days)).sign(key, hashes.SHA256()))
    path = folder / f"{alias}.p12"
    path.write_bytes(pkcs12.serialize_key_and_certificates(alias.encode(), key, cert, None,
                                                            serialization.BestAvailableEncryption((password + alias).encode())))
    return path


def make_pdf(*names, password=None, text=None):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    content = b""
    for index, name in enumerate(names):
        y = 650 - index * 150
        content += f"100 {y} m 250 {y} l S BT /F1 10 Tf 110 {y - 15} Td ({name}) Tj ET ".encode("ascii")
    if text:
        # A line of plain text near the top, with no signature line.
        content += f"BT /F1 10 Tf 40 760 Td ({text}) Tj ET ".encode("ascii")
    stream = DecodedStreamObject()
    stream.set_data(content)
    page[NameObject("/Contents")] = writer._add_object(stream)
    if password:
        writer.encrypt(password)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


# The reminder that follows every message that comes with a signed document.
REVIEW = {code: "\n" + message_text(code, "review_reminder") for code in ("en", "pt-br", "es")}


class Chat:
    COURTESY = ("Got it!", "Recebido!", "¡Recibido!", "All set!", "Tudo certo!", "¡Listo! Firmado")

    def __init__(self):
        self.documents, self.texts, self.files, self.downloads, self.notes, self.forwarded = {}, [], [], [], [], []
        self.order = []

    def download_document(self, media_id):
        self.downloads.append(media_id)
        return self.documents[media_id]

    def send_text(self, recipient, text):
        # Courtesy notes are kept apart so the other assertions read only the messages that matter.
        self.order.append((recipient, "text"))
        (self.notes if text.startswith(self.COURTESY) else self.texts).append((recipient, text))

    def send_pdf(self, recipient, data, filename=None, *, signed=True):
        # Signed results and documents forwarded to an owner for approval are kept apart.
        self.order.append((recipient, "pdf"))
        (self.files if signed else self.forwarded).append((recipient, data, filename))

    def to(self, recipient):
        return [text for number, text in self.texts if number == recipient]


@pytest.fixture
def world(tmp_path):
    """Three owners. Alice has a preparer who needs approval and a trusted one; her owner is trusted by Bruno."""
    configs = []
    owners = {"alice": OWNER_A, "bruno": OWNER_B, "carla": OWNER_C}
    for alias, name in NAMES.items():
        path = make_certificate(tmp_path, alias, name)
        allowed = {owners[alias], PREPARER} | ({TRUSTED} if alias == "alice" else set()) | ({OWNER_A} if alias != "alice" else set())
        autotrust = {TRUSTED} if alias == "alice" else ({OWNER_A} if alias == "bruno" else set())
        configs.append(SignerConfig(alias, name, path, allowed_senders=frozenset(allowed), owner=owners[alias],
                                    autotrust=frozenset(autotrust), password="pw-" + alias))
    signers = unlock_signers(tuple(configs))
    inbox = Inbox(tmp_path / "data" / "inbox.db")
    runtime = Runtime(tmp_path / "data", requests_per_hour=20, confirmation_minutes=5, names=dict(PEOPLE), settle_seconds=0,
                      timezone="UTC")
    chat = Chat()
    counter = iter(range(1, 1000))

    def send(sender, document=None, *, text=None, filename="report.pdf", caption=None, consent=True):
        number = next(counter)
        if text is not None:
            inbox.add([Job(f"m{number}", "", None, sender, "text", parse_command(text))])
        else:
            chat.documents[f"media{number}"] = document
            inbox.add([Job(f"m{number}", f"media{number}", filename, sender, "document", caption)])
        assert process_one(inbox, chat, signers, runtime=runtime)
        message = f"m{number}"
        # Most tests are about what happens after the requester agrees to involve the owner.
        if text is None and consent and state(message) == "awaiting_choice" and "To sign as" in chat.to(sender)[-1]:
            send(sender, text="1")
        return message

    def state(message_id):
        return inbox.get_state(message_id)

    class World:
        pass
    result = World()
    result.__dict__.update(send=send, state=state, chat=chat, inbox=inbox, runtime=runtime, signers=signers,
                           audit=AuditLog(tmp_path / "data" / "audit.jsonl"), tmp_path=tmp_path)
    return result


def signer_names(pdf):
    return [signature.signer_cert.subject.native["common_name"] for signature in PdfFileReader(BytesIO(pdf)).embedded_signatures]


def later(world, minutes):
    moment = time.time() + minutes * 60
    expire_requests(world.inbox, world.chat, world.signers, world.runtime, now=moment)
    ask_questions(world.inbox, world.chat, world.signers, world.runtime, now=moment)
    tell_outcomes(world.inbox, world.chat, world.runtime)


def burst(world, sender, documents, runtime=None):
    """Several documents that arrive together, before the bot reads any of them."""
    messages = []
    for filename, document in documents:
        message = f"b{len(world.chat.documents)}"
        world.chat.documents[message] = document
        world.inbox.add([Job(message, message, filename, sender)])
        messages.append(message)
    while process_one(world.inbox, world.chat, world.signers, runtime=runtime or world.runtime):
        pass
    return messages


# Who may use the bot, and with which certificate.

def test_unauthorized_number_is_ignored_at_the_webhook_and_by_the_worker(world):
    payload = {"event": {"type": "messages", "event": "post"},
               "messages": [{"id": "x1", "from_me": False, "type": "document", "chat_id": f"{STRANGER}@s.whatsapp.net",
                             "from": STRANGER, "document": {"id": "media-x", "file_name": "a.pdf"}}]}
    assert accepted_events(read_messages(payload), frozenset({OWNER_A})) == []
    message = world.send(STRANGER, make_pdf("Alice Example"))
    assert world.state(message) == "needs_review"
    assert not world.chat.texts and not world.chat.notes and not world.chat.files and not world.chat.downloads


def test_owner_signs_own_document_directly_and_it_is_audited(world):
    document = make_pdf("Alice Example")
    message = world.send(OWNER_A, document)
    assert world.state(message) == "sent" and world.chat.texts == []
    assert [text for _, text in world.chat.notes] == ["Got it! Working on it...", "All set! Signed by: Alice Example." + REVIEW["en"]]
    recipient, signed, _ = world.chat.files[0]
    assert recipient == OWNER_A and signer_names(signed) == ["Alice Example"]
    entry = world.audit.entries()[0]
    assert (entry["approval"], entry["requester"], entry["confirmed_by"], entry["certificate_id"]) == ("owner", OWNER_A, None, "alice")
    assert entry["document_sha256"] == hashlib.sha256(document).hexdigest()
    assert entry["signed_sha256"] == hashlib.sha256(signed).hexdigest()
    assert "Alice" not in str(entry) and entry["filename"] == "report.pdf"


def test_certificate_of_another_person_is_never_used(world):
    # Bruno's owner may use only Bruno's certificate; the document names Carla.
    message = world.send(OWNER_B, make_pdf("Carla Example"))
    assert world.state(message) == "needs_review" and not world.chat.files and world.audit.entries() == []
    assert "clear place to sign for: Bruno Example. Nothing was signed" in world.chat.to(OWNER_B)[0]


def test_trusted_preparer_signs_without_asking(world):
    message = world.send(TRUSTED, make_pdf("Alice Example"))
    assert world.state(message) == "sent" and signer_names(world.chat.files[0][1]) == ["Alice Example"]
    assert world.chat.to(OWNER_A) == [] and not world.chat.forwarded
    assert world.audit.entries()[0]["approval"] == "autotrust"


def test_a_name_under_two_lines_is_signed_only_where_the_requester_agrees(world):
    message = world.send(OWNER_A, make_pdf("Alice Example", "Alice Example"))
    assert world.state(message) == "awaiting_choice" and not world.chat.files and world.audit.entries() == []
    assert world.chat.to(OWNER_A)[-1].endswith("I could not find a clear place to sign for Alice Example. Shall I sign at the end "
                                               "of the last page? Reply within 5 minute(s):\n1 - Yes, sign at the end of the last page"
                                               "\n0 - Cancel")
    world.send(OWNER_A, text="0")
    assert world.state(message) == "needs_review" and not world.chat.files
    # Agreeing signs at the bottom of the last page, away from both lines.
    message = world.send(OWNER_A, make_pdf("Alice Example", "Alice Example", text="again"))
    world.send(OWNER_A, text="1")
    assert world.state(message) == "sent" and signer_names(world.chat.files[0][1]) == ["Alice Example"]


def test_nobody_is_offered_the_end_of_the_page_unless_the_document_writes_the_name(world):
    # Bruno's owner may use only Bruno's certificate, and the document never names Bruno.
    message = world.send(OWNER_B, make_pdf("Carla Example", "Carla Example"))
    assert world.state(message) == "needs_review" and "end of the last page" not in world.chat.to(OWNER_B)[-1]


def test_a_signature_that_cannot_be_made_is_reported_with_the_name_of_its_signer(world, monkeypatch):
    def refuse(error):
        return lambda *args, **kwargs: (_ for _ in ()).throw(error)
    # Signing with one's own certificate.
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf", refuse(CertificateRejected("The certificate is revoked.")))
    message = world.send(OWNER_A, make_pdf("Alice Example"))
    assert world.state(message) == "failed" and not world.chat.files and world.audit.entries() == []
    assert world.chat.to(OWNER_A)[-1] == ("Sorry, I could not sign this document: the certificate of Alice Example is expired, "
                                          "revoked or could not be validated. Nothing was signed.")
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf", refuse(TokenUnavailable("Could not reach the token.")))
    world.send(OWNER_A, make_pdf("Alice Example", "second"))
    assert world.chat.to(OWNER_A)[-1] == ("Sorry, I could not sign this document: the USB token of Alice Example could not be reached. "
                                          "Nothing was signed.")
    # After the owner approves: the requester learns whose signature failed, the owner that the certificate is theirs.
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf", refuse(CertificateRejected("The certificate is expired.")))
    message = world.send(PREPARER, make_pdf("Alice Example", "third"))
    world.send(OWNER_A, text="1")
    assert world.state(message) == "needs_review" and not world.chat.files and world.audit.entries() == []
    assert world.chat.to(OWNER_A)[-1] == "Sorry, your certificate is expired, revoked or could not be validated. Nothing was signed."
    assert world.chat.to(PREPARER)[-1] == ("Sorry, I could not sign this document: the certificate of Alice Example is expired, "
                                           "revoked or could not be validated. Nothing was signed.")
    assert not list((world.tmp_path / "data" / "pending").glob("*.pdf"))


def test_the_signer_that_failed_among_several_is_named_and_nothing_is_signed(world, monkeypatch):
    import sealwharf.documents.signing as signing
    real, calls = signing.sign_pdf, []

    def second_fails(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise signing.SigningError("Cryptographic signature verification failed.")
        return real(*args, **kwargs)
    monkeypatch.setattr(signing, "sign_pdf", second_fails)
    message = world.send(OWNER_A, make_pdf("Alice Example", "Bruno Example"))
    world.send(OWNER_A, text="3")
    assert world.state(message) == "failed" and not world.chat.files and world.audit.entries() == []
    assert world.chat.to(OWNER_A)[-1] == ("Sorry, I could not sign this document: the signature of Bruno Example could not be made. "
                                          "Nothing was signed.")
    portuguese = Runtime(world.runtime.data_dir, languages={OWNER_A: "pt-br"}, names=dict(PEOPLE), settle_seconds=0)
    calls.clear()
    world.inbox.add([Job("pt1", "pt1", "a.pdf", OWNER_A, "document", None), Job("pt2", "pt2", "b.pdf", OWNER_A, "document", None)])
    world.chat.documents.update(pt1=make_pdf("Bruno Example", "a"), pt2=make_pdf("Bruno Example", "b"))
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf", second_fails)
    while process_one(world.inbox, world.chat, world.signers, runtime=portuguese):
        pass
    assert world.chat.to(OWNER_A)[-1] == ("Aqui está o resultado de 2 documentos:\n\n✅ Assinados (1)\n• a.pdf\n\n"
                                          "❌ NÃO assinado, a assinatura de Bruno Example não pôde ser feita (1)\n• b.pdf\n"
                                          + REVIEW["pt-br"])


def test_a_person_the_document_only_talks_about_is_not_taken_as_a_signer(world):
    about = "I declare, for academic purposes, that Bruno Example works at this company since 2017"
    message = world.send(OWNER_A, make_pdf("Alice Example", text=about))
    assert world.state(message) == "sent" and signer_names(world.chat.files[0][1]) == ["Alice Example"]
    assert world.chat.to(OWNER_A) == [] and [entry["certificate_id"] for entry in world.audit.entries()] == ["alice"]


@pytest.mark.parametrize("written", ["Bruno Example", "I, Bruno Example, declare that everything written above is true",
                                     "Signature of the witness: Bruno Example"])
def test_a_signer_named_on_a_line_of_their_own_without_a_clear_place_is_still_asked_about(world, written):
    message = world.send(OWNER_A, make_pdf("Alice Example", text=written))
    assert world.state(message) == "awaiting_choice" and not world.chat.files and world.audit.entries() == []
    assert world.chat.to(OWNER_A)[-1].endswith("I could not find a clear place to sign for: Bruno Example.")


def test_a_name_is_running_text_only_when_every_occurrence_is_inside_a_sentence():
    name = [("bruno", "example")]
    assert only_in_running_text("We declare that Bruno Example works here since 2017.", name)
    assert only_in_running_text("It is hereby declared for all purposes that Bruno\nExample has worked here for years", name)
    assert only_in_running_text("The company states and declares that the employee Bruno Example", name)
    for label in ("Bruno Example", "Bruno Example\nDirector", "Signature: Bruno Example", "I, Bruno Example, declare that this is true",
                  "Prof. Dr. Bruno Example, head of the software engineering course",
                  "We declare that Bruno Example works here since 2017.\n____________\nBruno Example"):
        assert not only_in_running_text(label, name), label
    assert not only_in_running_text("We declare that nobody in particular works here.", name)


def test_password_protected_pdf_is_refused(world):
    message = world.send(OWNER_A, make_pdf("Alice Example", password="secret"))
    assert world.state(message) == "failed" and not world.chat.files
    assert "could not process this document" in world.chat.to(OWNER_A)[0]


def test_expired_certificate_and_wrong_password_stop_at_startup(tmp_path):
    expired = SignerConfig("old", "Old Example", make_certificate(tmp_path, "old", "Old Example", days=-1),
                           allowed_senders=frozenset({OWNER_A}), owner=OWNER_A, password="pw-old")
    with pytest.raises(ValueError, match="expired"):
        unlock_signers((expired,))
    path = make_certificate(tmp_path, "new", "New Example")
    wrong = SignerConfig("new", "New Example", path, allowed_senders=frozenset({OWNER_A}), owner=OWNER_A, password="wrong")
    with pytest.raises(ValueError, match="Could not open"):
        unlock_signers((wrong,))
    missing = SignerConfig("new", "New Example", path, allowed_senders=frozenset({OWNER_A}), owner=OWNER_A)
    with pytest.raises(ValueError, match="No password"):
        unlock_signers((missing,))
    assert "pw-old" not in repr(expired)


# Asking an owner to approve.

def test_requester_agrees_then_the_owner_approves_with_a_single_digit(world):
    document = make_pdf("Alice Example")
    message = world.send(PREPARER, document, filename="contract.pdf", consent=False)
    question = world.chat.to(PREPARER)[-1]
    # Nothing reaches the owner until the requester says so.
    assert world.state(message) == "awaiting_choice" and world.chat.to(OWNER_A) == [] and not world.chat.forwarded
    assert question.startswith("About contract.pdf:\nTo sign as Alice Example, I need the approval of Ana.")
    assert "1 - Send to Ana and ask for approval\n0 - Cancel" in question
    world.send(PREPARER, text="1")
    # The owner reads who is asking, with name and number, and then receives the unsigned document.
    request = world.chat.to(OWNER_A)[0]
    assert request == ("Hello! Paulo Preparer (+55 11 90000-0010) asks you to sign the following document with your digital "
                       "certificate. Reply 1 to approve. Reply 0, or simply ignore this message, to refuse.")
    assert world.chat.forwarded == [(OWNER_A, document, "contract.pdf")]
    assert [kind for number, kind in world.chat.order if number == OWNER_A] == ["text", "pdf"]
    assert "I sent the document to Ana for approval" in world.chat.to(PREPARER)[-1]
    assert world.state(message) == "awaiting_confirmation" and not world.chat.files and world.audit.entries() == []
    # Only the owner's own answer counts.
    world.send(PREPARER, text="1")
    quiet = len(world.chat.to(OWNER_B))
    world.send(OWNER_B, text="1")
    assert not world.chat.files and len(world.chat.to(OWNER_B)) == quiet
    world.send(OWNER_A, text="1")
    assert world.chat.to(OWNER_A)[-1] == "Approved! The document was signed."
    recipient, signed, filename = world.chat.files[0]
    assert (recipient, filename) == (PREPARER, "contract.pdf") and signer_names(signed) == ["Alice Example"]
    assert world.chat.notes[-1] == (PREPARER, "All set! Signed by: Alice Example." + REVIEW["en"]) and world.state(message) == "sent"
    entry = world.audit.entries()[0]
    assert (entry["approval"], entry["requester"], entry["confirmed_by"]) == ("confirmed", PREPARER, OWNER_A)
    assert not world.runtime.pending_file(entry["request_id"]).exists()
    # The approval was used; answering again finds nothing waiting.
    world.send(OWNER_A, text="1")
    assert len(world.chat.files) == 1 and world.chat.to(OWNER_A)[-1] == "Approved! The document was signed."


def test_requester_who_declines_never_involves_the_owner(world):
    message = world.send(PREPARER, make_pdf("Alice Example"), consent=False)
    world.send(PREPARER, text="0")
    assert world.state(message) == "needs_review" and world.chat.to(OWNER_A) == []
    assert not world.chat.forwarded and not world.chat.files and world.audit.entries() == []


@pytest.mark.parametrize("answer", ["0", "recusar", "Não"])
def test_owner_can_refuse_a_request(world, answer):
    message = world.send(PREPARER, make_pdf("Alice Example"))
    world.send(OWNER_A, text=answer)
    assert world.chat.to(OWNER_A)[-1] == "Refused. Nothing was signed."
    assert world.state(message) == "needs_review" and not world.chat.files and world.audit.entries() == []
    assert "was not signed" in world.chat.to(PREPARER)[-1]


def test_request_ignored_by_the_owner_expires_without_signing(world):
    message = world.send(PREPARER, make_pdf("Alice Example"))
    later(world, 4)
    assert world.state(message) == "awaiting_confirmation"
    later(world, 6)
    assert world.state(message) == "needs_review" and "was not signed" in world.chat.to(PREPARER)[-1]
    # The owner is told that the time has ended, and an answer sent after that is not taken.
    assert world.chat.to(OWNER_A)[-1] == ("The time to approve this request has ended, so nothing was signed with your certificate. "
                                          "An answer sent now no longer counts.")
    told = len(world.chat.to(OWNER_A))
    world.send(OWNER_A, text="1")
    assert not world.chat.files and world.audit.entries() == [] and len(world.chat.to(OWNER_A)) == told


def test_an_owner_who_lets_several_documents_expire_is_told_once(world):
    burst(world, PREPARER, [(f"{name}.pdf", make_pdf("Alice Example", name)) for name in ("one", "two")])
    world.send(PREPARER, text="1")
    asked = len(world.chat.to(OWNER_A))
    later(world, 6)
    assert world.chat.to(OWNER_A)[asked:] == [
        "Here is how 2 documents went:\n\n❌ NOT signed, the time to approve has ended (2)\n• one.pdf\n• two.pdf"]
    assert not list((world.tmp_path / "data" / "pending").glob("*.pdf"))


def test_approval_covers_only_the_document_that_was_sent(world):
    world.send(PREPARER, make_pdf("Alice Example"))
    entry = world.inbox.pending_for_owner(OWNER_A)[0]
    world.runtime.pending_file(entry.request_id).write_bytes(make_pdf("Alice Example", "Bruno Example"))
    world.send(OWNER_A, text="1")
    assert not world.chat.files and world.audit.entries() == []
    assert "Nothing was signed" in world.chat.to(OWNER_A)[-1]


def test_requests_for_a_busy_owner_wait_in_line_and_are_asked_one_at_a_time(world):
    # Carla is asked about the preparer's document; meanwhile Alice's owner asks for Carla's signature too.
    first = world.send(PREPARER, make_pdf("Carla Example"), filename="first.pdf")
    second = world.send(OWNER_A, make_pdf("Carla Example", "extra"), filename="second.pdf")
    # A plain 1 must never be able to approve the wrong document: Carla has seen only the first.
    assert [name for _, _, name in world.chat.forwarded] == ["first.pdf"] and len(world.chat.to(OWNER_C)) == 1
    assert world.state(second) == "awaiting_confirmation"
    assert world.chat.to(OWNER_A)[-1].startswith("Your document is in line for the approval of Carla, with 1 request(s) ahead")
    world.send(OWNER_C, text="1")
    assert world.state(first) == "sent" and world.chat.files[0][0] == PREPARER and world.chat.files[0][2] == "first.pdf"
    # Her answer released the next question, which waits for an answer of its own.
    assert [name for _, _, name in world.chat.forwarded] == ["first.pdf", "second.pdf"]
    assert world.state(second) == "awaiting_confirmation" and len(world.chat.files) == 1
    world.send(OWNER_C, text="0")
    assert world.state(second) == "needs_review" and len(world.chat.files) == 1
    assert "was not signed" in world.chat.to(OWNER_A)[-1]
    assert [entry["certificate_id"] for entry in world.audit.entries()] == ["carla"]


def test_time_to_answer_starts_only_when_the_question_is_asked(world):
    world.send(PREPARER, make_pdf("Carla Example"), filename="first.pdf")
    second = world.send(OWNER_A, make_pdf("Carla Example", "extra"), filename="second.pdf")
    # The first request expires unanswered; the second is asked then, with its full time to be answered.
    later(world, 6)
    assert [name for _, _, name in world.chat.forwarded] == ["first.pdf", "second.pdf"]
    assert world.state(second) == "awaiting_confirmation"
    entry = world.inbox.pending_for_owner(OWNER_C)[0]
    assert entry.expires_at > time.time() + 10 * 60


def test_the_line_for_an_owner_has_a_limit(world):
    short = Runtime(world.runtime.data_dir, names=dict(PEOPLE), queue_limit=1)
    burst(world, PREPARER, [("first.pdf", make_pdf("Carla Example"))], short)
    world.inbox.add([Job("yes", "", None, PREPARER, "text", "answer 1")])
    assert process_one(world.inbox, world.chat, world.signers, runtime=short)
    refused = burst(world, OWNER_A, [("second.pdf", make_pdf("Carla Example", "extra"))], short)
    world.inbox.add([Job("yes2", "", None, OWNER_A, "text", "answer 1")])
    assert process_one(world.inbox, world.chat, world.signers, runtime=short)
    assert world.state(refused[0]) == "needs_review" and len(world.chat.forwarded) == 1
    assert "already has many requests waiting for approval" in world.chat.to(OWNER_A)[-1]


def test_a_person_has_one_open_question_and_the_next_waits_its_turn(world):
    # Alice's owner is asked to approve one document and then sends another that needs her own choice.
    approval = world.send(PREPARER, make_pdf("Alice Example"))
    choice = world.send(OWNER_A, make_pdf("Alice Example", "Bruno Example"))
    assert world.state(choice) == "awaiting_choice" and not any("Who should sign?" in text for text in world.chat.to(OWNER_A))
    # Her reply answers the approval, the only question she was asked; the choice is asked right after.
    world.send(OWNER_A, text="1")
    assert world.state(approval) == "sent" and world.chat.files[0][0] == PREPARER
    assert "Who should sign?" in world.chat.to(OWNER_A)[-1] and world.state(choice) == "awaiting_choice"
    world.send(OWNER_A, text="2")
    assert world.state(choice) == "sent" and signer_names(world.chat.files[1][1]) == ["Bruno Example"]


def test_people_without_a_contact_name_are_shown_by_number(world):
    world.send(PREPARER, make_pdf("Bruno Example"), consent=False)
    assert "I need the approval of +55 11 90000-0002." in world.chat.to(PREPARER)[-1]
    assert format_number("555381234567") == "+55 53 8123-4567" and format_number("14155550123") == "+14155550123"


# Several signers on one document.

def test_requester_chooses_who_signs_when_several_signers_are_found(world):
    message = world.send(OWNER_A, make_pdf("Alice Example", "Bruno Example"))
    question = world.chat.to(OWNER_A)[-1]
    assert world.state(message) == "awaiting_choice" and not world.chat.files and world.audit.entries() == []
    assert "Who should sign?" in question and "1 - Alice Example\n2 - Bruno Example\n3 - All of them\n0 - Cancel" in question
    # An answer outside the list keeps the question open.
    world.send(OWNER_A, text="7")
    assert "not one of the options" in world.chat.to(OWNER_A)[-1] and not world.chat.files
    world.send(OWNER_A, text="2")
    assert world.state(message) == "sent" and signer_names(world.chat.files[0][1]) == ["Bruno Example"]
    assert world.chat.files[0][2] == "report.pdf" and world.chat.notes[-1][1] == "All set! Signed by: Bruno Example." + REVIEW["en"]
    assert [entry["certificate_id"] for entry in world.audit.entries()] == ["bruno"]
    quiet = len(world.chat.to(OWNER_A))
    world.send(OWNER_A, text="2")
    assert len(world.chat.to(OWNER_A)) == quiet and len(world.chat.files) == 1


def test_two_signatures_in_sequence_keep_the_first_valid(world):
    # Alice's owner is trusted by Bruno, so both sign as soon as "all" is chosen.
    message = world.send(OWNER_A, make_pdf("Alice Example", "Bruno Example"))
    world.send(OWNER_A, text="3")
    assert world.state(message) == "sent"
    signed = world.chat.files[0][1]
    assert sorted(signer_names(signed)) == ["Alice Example", "Bruno Example"]
    assert all(report.valid for report in verify_signatures(signed))
    assert [entry["approval"] for entry in world.audit.entries()] == ["owner", "autotrust"]


def test_three_signatures_report_who_signed_and_who_is_missing(world):
    document = make_pdf("Alice Example", "Bruno Example", "Carla Example")
    message = world.send(OWNER_A, document)
    assert "3 - Carla Example (needs approval)\n4 - All of them" in world.chat.to(OWNER_A)[-1]
    world.send(OWNER_A, text="todos")
    # Alice and Bruno sign immediately; Carla must approve.
    assert world.state(message) == "awaiting_confirmation" and not world.chat.files
    assert world.chat.to(OWNER_A)[-1] == ("Signed so far: Alice Example, Bruno Example. "
                                          "Still waiting for the approval of: Carla Example.")
    assert [recipient for recipient, _, _ in world.chat.forwarded] == [OWNER_C]
    assert signer_names(world.chat.forwarded[0][1]) == ["Alice Example", "Bruno Example"]
    world.send(OWNER_C, text="sim")
    recipient, signed, _ = world.chat.files[0]
    assert recipient == OWNER_A and signed.startswith(document)
    assert signer_names(signed) == ["Alice Example", "Bruno Example", "Carla Example"]
    reports = verify_signatures(signed)
    assert len(reports) == 3 and all(report.valid for report in reports)
    assert [entry["approval"] for entry in world.audit.entries()] == ["owner", "autotrust", "confirmed"]
    assert world.state(message) == "sent"


def test_partial_result_is_delivered_when_one_owner_does_not_approve(world):
    world.send(OWNER_A, make_pdf("Alice Example", "Carla Example"))
    world.send(OWNER_A, text="all")
    later(world, 6)
    assert signer_names(world.chat.files[0][1]) == ["Alice Example"]
    assert "NOT signed by: Carla Example" in world.chat.to(OWNER_A)[-1]


def test_a_document_that_names_none_of_the_signers_says_so_without_listing_the_certificates(world):
    message = world.send(OWNER_A, make_pdf("Somebody Else"))
    assert world.state(message) == "needs_review" and not world.chat.files
    told = world.chat.to(OWNER_A)[-1]
    assert told.startswith("I did not find in this document the full name of anyone whose certificate you may use")
    assert "Alice" not in told and "Bruno" not in told and "Carla" not in told


def test_a_signed_document_can_receive_another_signature_later(world):
    world.send(OWNER_A, make_pdf("Alice Example", "Carla Example"))
    world.send(OWNER_A, text="1")
    partially_signed = world.chat.files[0][1]
    assert signer_names(partially_signed) == ["Alice Example"]
    world.send(OWNER_C, partially_signed)
    final = world.chat.files[1][1]
    assert signer_names(final) == ["Alice Example", "Carla Example"]
    assert all(report.valid for report in verify_signatures(final))


def test_choice_can_be_cancelled_expires_and_only_the_requester_may_answer(world):
    first = world.send(OWNER_A, make_pdf("Alice Example", "Bruno Example"))
    world.send(OWNER_B, text="1")
    assert world.chat.to(OWNER_B) == [] and world.state(first) == "awaiting_choice"
    world.send(OWNER_A, text="cancelar")
    assert world.state(first) == "needs_review" and world.chat.to(OWNER_A)[-1] == "Cancelled. Nothing was signed."
    second = world.send(OWNER_A, make_pdf("Alice Example", "Bruno Example"))
    later(world, 6)
    assert world.state(second) == "needs_review"
    world.send(OWNER_A, text="3")
    assert not world.chat.files and world.audit.entries() == []
    assert not list((world.tmp_path / "data" / "pending").glob("*.pdf"))


def test_a_newer_document_waits_behind_an_unanswered_choice(world):
    first = world.send(OWNER_A, make_pdf("Alice Example", "Bruno Example"), filename="older.pdf")
    second = world.send(OWNER_A, make_pdf("Alice Example", "Carla Example"), filename="newer.pdf")
    # Nothing is dropped: the newer document keeps its place and its question is not asked yet.
    assert (world.state(first), world.state(second)) == ("awaiting_choice", "awaiting_choice")
    questions = [text for text in world.chat.to(OWNER_A) if "Who should sign?" in text]
    assert len(questions) == 1 and questions[0].startswith("About older.pdf:")
    world.send(OWNER_A, text="2")
    assert world.chat.files[0][2] == "older.pdf" and signer_names(world.chat.files[0][1]) == ["Bruno Example"]
    assert world.chat.to(OWNER_A)[-1].startswith("About newer.pdf:") and world.state(second) == "awaiting_choice"
    world.send(OWNER_A, text="1")
    assert world.chat.files[1][2] == "newer.pdf" and signer_names(world.chat.files[1][1]) == ["Alice Example"]


# Documents sent in bulk.

def test_documents_sent_together_by_their_owner_get_one_note_and_one_summary(world):
    messages = burst(world, OWNER_A, [("a.pdf", make_pdf("Alice Example")), ("b.pdf", make_pdf("Alice Example", "more")),
                                      ("c.pdf", make_pdf("Nobody Known"))])
    assert [world.state(message) for message in messages] == ["sent", "sent", "needs_review"]
    assert [name for _, _, name in world.chat.files] == ["a.pdf", "b.pdf"]
    assert [text for _, text in world.chat.notes] == ["Got it! Working on it..."]
    assert world.chat.to(OWNER_A) == ["Here is how 3 documents went:\n\n✅ Signed (2)\n• a.pdf\n• b.pdf\n\n"
                                      "❌ NOT signed, no signer's name found (1)\n• c.pdf\n"
                                      + REVIEW["en"].replace("document,", "documents,").replace("it on", "them on")]


def test_a_batch_summary_counts_and_groups_every_outcome_in_order(world):
    # Signed at once, sent to an owner, and with nobody named; files in the order people number them.
    documents = [(f"{index} doc.pdf", make_pdf("Alice Example", str(index))) for index in (10, 2, 1)]
    documents += [("3 nobody.pdf", make_pdf("Somebody Else"))]
    burst(world, OWNER_A, documents)
    summary = world.chat.to(OWNER_A)[0]
    assert summary == ("Here is how 4 documents went:\n\n✅ Signed (3)\n• 1 doc.pdf\n• 2 doc.pdf\n• 10 doc.pdf\n\n"
                       "❌ NOT signed, no signer's name found (1)\n• 3 nobody.pdf" + "\n\nPlease review the signed documents, including where each signature was placed, before sending them on.")
    # A preparer's batch: what waits for the owner is its own group, with the owner named.
    burst(world, PREPARER, [("b.pdf", make_pdf("Alice Example", "b")), ("a.pdf", make_pdf("Alice Example", "a")),
                            ("c.pdf", make_pdf("Somebody Else", "c"))])
    world.send(PREPARER, text="1")
    waiting = world.chat.to(PREPARER)[-1]
    assert waiting.startswith("Here is how 2 documents went:\n\n⏳ Sent to Ana for approval (2)\n• a.pdf\n• b.pdf")


def test_documents_sent_together_are_asked_and_approved_as_one(world):
    documents = [(f"{name}.pdf", make_pdf("Alice Example", name)) for name in ("a", "b", "c")]
    messages = burst(world, PREPARER, documents)
    # One question for the three documents, and nothing reaches the owner before the answer.
    questions = world.chat.to(PREPARER)
    assert len(questions) == 1 and questions[0].startswith("About 3 documents (a.pdf, b.pdf, c.pdf):\nTo sign as Alice Example")
    assert not world.chat.forwarded and world.chat.to(OWNER_A) == []
    world.send(PREPARER, text="1")
    # The owner is asked once, with every document to look at.
    assert world.chat.to(OWNER_A) == [
        "Hello! Paulo Preparer (+55 11 90000-0010) asks you to sign the following 3 documents with your digital certificate: "
        "a.pdf, b.pdf, c.pdf. Reply 1 to approve all of them. Reply 0, or simply ignore this message, to refuse all of them."]
    assert [(recipient, name) for recipient, _, name in world.chat.forwarded] == [(OWNER_A, "a.pdf"), (OWNER_A, "b.pdf"), (OWNER_A, "c.pdf")]
    assert world.chat.to(PREPARER)[-1] == "Here is how 3 documents went:\n\n⏳ Sent to Ana for approval (3)\n• a.pdf\n• b.pdf\n• c.pdf"
    assert not world.chat.files and world.audit.entries() == []
    world.send(OWNER_A, text="1")
    assert [world.state(message) for message in messages] == ["sent"] * 3
    assert [(recipient, name) for recipient, _, name in world.chat.files] == [(PREPARER, "a.pdf"), (PREPARER, "b.pdf"), (PREPARER, "c.pdf")]
    # Each approval still covers exactly one document, and each signature is recorded.
    entries = world.audit.entries()
    assert [entry["approval"] for entry in entries] == ["confirmed"] * 3 and len({entry["document_sha256"] for entry in entries}) == 3
    assert world.chat.to(OWNER_A)[-1] == "Here is how 3 documents went:\n\n✅ Approved and signed (3)\n• a.pdf\n• b.pdf\n• c.pdf"
    assert world.chat.to(PREPARER)[-1] == ("Here is how 3 documents went:\n\n✅ Signed (3)\n• a.pdf\n• b.pdf\n• c.pdf"
                                           "\n\nPlease review the signed documents, including where each signature was placed, before sending them on.")


def test_refusing_a_batch_refuses_every_document_in_it(world):
    messages = burst(world, PREPARER, [("a.pdf", make_pdf("Alice Example", "a")), ("b.pdf", make_pdf("Alice Example", "b"))])
    world.send(PREPARER, text="1")
    world.send(OWNER_A, text="0")
    assert [world.state(message) for message in messages] == ["needs_review"] * 2
    assert not world.chat.files and world.audit.entries() == []
    assert world.chat.to(OWNER_A)[-1] == "Here is how 2 documents went:\n\n❌ Refused (2)\n• a.pdf\n• b.pdf"
    assert "❌ NOT signed (not approved in time, refused or failed) (2)\n• a.pdf\n• b.pdf" in world.chat.to(PREPARER)[-1]
    assert not list((world.tmp_path / "data" / "pending").glob("*.pdf"))


def test_a_changed_document_in_a_batch_is_not_signed_while_the_others_are(world):
    burst(world, PREPARER, [("a.pdf", make_pdf("Alice Example", "a")), ("b.pdf", make_pdf("Alice Example", "b"))])
    world.send(PREPARER, text="1")
    tampered = world.inbox.pending_for_owner(OWNER_A)[0]
    world.runtime.pending_file(tampered.request_id).write_bytes(make_pdf("Alice Example", "Bruno Example"))
    world.send(OWNER_A, text="1")
    assert [name for _, _, name in world.chat.files] == ["b.pdf"] and len(world.audit.entries()) == 1
    assert "❌ NOT signed, signing failed (1)\n• a.pdf" in world.chat.to(OWNER_A)[-1]
    assert "✅ Approved and signed (1)\n• b.pdf" in world.chat.to(OWNER_A)[-1]


def test_documents_with_different_questions_are_asked_separately(world):
    burst(world, OWNER_A, [("two.pdf", make_pdf("Alice Example", "Bruno Example")), ("other.pdf", make_pdf("Alice Example", "Carla Example")),
                           ("same.pdf", make_pdf("Alice Example", "Bruno Example", "again"))])
    # The two documents with the same signers share a question; the other one waits for its own.
    questions = [text for text in world.chat.to(OWNER_A) if "Who should sign?" in text]
    assert len(questions) == 1 and questions[0].startswith("About 2 documents (two.pdf, same.pdf):")
    world.send(OWNER_A, text="2")
    assert [(name, signer_names(data)) for _, data, name in world.chat.files] == [("two.pdf", ["Bruno Example"]), ("same.pdf", ["Bruno Example"])]
    assert world.chat.to(OWNER_A)[-1].startswith("About other.pdf:")


def test_the_same_file_sent_twice_is_asked_about_once(world):
    document = make_pdf("Alice Example")
    messages = burst(world, PREPARER, [("a.pdf", document), ("a.pdf", document)])
    assert [world.state(message) for message in messages] == ["awaiting_choice", "duplicate"]
    texts = world.chat.to(PREPARER)
    assert any(text.startswith("About a.pdf:") for text in texts)
    assert any(text == "You had already sent this same file, so I am handling it only once." for text in texts)
    world.send(PREPARER, text="1")
    assert len(world.chat.forwarded) == 1
    # Once it is settled, the same file may be sent again.
    world.send(OWNER_A, text="0")
    again = world.send(PREPARER, document, filename="a.pdf", consent=False)
    assert world.state(again) == "awaiting_choice"


def test_a_question_waits_for_documents_that_are_still_arriving(world):
    patient = Runtime(world.runtime.data_dir, names=dict(PEOPLE), settle_seconds=30)
    messages = burst(world, PREPARER, [("a.pdf", make_pdf("Alice Example"))], patient)
    assert world.state(messages[0]) == "awaiting_choice" and world.chat.to(PREPARER) == []
    more = burst(world, PREPARER, [("b.pdf", make_pdf("Alice Example", "b"))], patient)
    assert world.state(more[0]) == "awaiting_choice" and world.chat.to(PREPARER) == []
    # Once the requester stops sending, one question covers what arrived.
    ask_questions(world.inbox, world.chat, world.signers, world.runtime)
    assert world.chat.to(PREPARER)[-1].startswith("About 2 documents (a.pdf, b.pdf):")


def test_a_question_that_cannot_be_delivered_closes_the_request(world):
    message = world.send(PREPARER, make_pdf("Alice Example"), consent=False)
    deliver = world.chat.send_pdf

    def unreachable(recipient, data, filename=None, *, signed=True):
        if not signed:
            raise TimeoutError()
        deliver(recipient, data, filename, signed=signed)
    world.chat.send_pdf = unreachable
    world.send(PREPARER, text="1")
    assert world.state(message) == "needs_review" and not world.chat.files and world.audit.entries() == []
    assert "was not signed" in world.chat.to(PREPARER)[-1]
    assert not list((world.tmp_path / "data" / "pending").glob("*.pdf"))


def test_a_contact_may_have_an_hourly_limit_of_its_own(world):
    bulk = Runtime(world.runtime.data_dir, requests_per_hour=1, limits={OWNER_A: 3})
    messages = burst(world, OWNER_A, [(f"{index}.pdf", make_pdf("Alice Example", str(index))) for index in range(4)], bulk)
    assert [world.state(message) for message in messages] == ["sent", "sent", "sent", "rate_limited"]
    assert bulk.limit(OWNER_B) == 1


def test_signer_without_a_clear_place_is_reported_and_never_offered(world):
    # Bruno's name appears twice, so his place is ambiguous; Alice can still be chosen.
    message = world.send(OWNER_A, make_pdf("Alice Example", "Bruno Example", "Bruno Example"))
    question = world.chat.to(OWNER_A)[-1]
    assert world.state(message) == "awaiting_choice"
    assert "1 - Alice Example\n2 - All of them" in question and "clear place to sign for: Bruno Example." in question
    world.send(OWNER_A, text="all")
    assert signer_names(world.chat.files[0][1]) == ["Alice Example"]


# Other commands.

def test_verify_command_checks_every_signature_without_signing(world):
    world.send(OWNER_A, make_pdf("Alice Example", "Bruno Example"))
    world.send(OWNER_A, text="3")
    signed = world.chat.files[0][1]
    message = world.send(PREPARER, signed, caption="verify")
    report = world.chat.to(PREPARER)[-1]
    assert world.state(message) == "verified" and len(world.chat.files) == 1
    assert "2 signature(s) found" in report and "All signatures are valid." in report
    assert "Alice Example: VALID" in report and "Bruno Example: VALID" in report
    # A caption works too, and tampering is detected.
    tampered = signed.replace(b"Alice Example) Tj", b"Alicx Example) Tj", 1)
    world.send(PREPARER, tampered, caption="verify")
    assert "NOT valid" in world.chat.to(PREPARER)[-1]
    world.send(PREPARER, make_pdf("Alice Example"), caption="verify")
    assert "no digital signatures" in world.chat.to(PREPARER)[-1]
    assert describe([]) == "This PDF has no digital signatures." and describe([], "pt-br") == "Este PDF não tem assinaturas digitais."


def test_wake_phrase_gets_one_of_the_replies_to_show_the_bot_is_alive(world):
    message = world.send(OWNER_A, text="Acorda Bot")
    assert len(world.chat.to(OWNER_A)) == 1 and world.chat.to(OWNER_A)[0] in CATALOGS["en"].replies["wake"] and world.state(message) == "done"
    portuguese = Runtime(world.runtime.data_dir, languages={OWNER_B: "pt-br"})
    world.inbox.add([Job("wake", "", None, OWNER_B, "text", parse_command("acordabot"))])
    assert process_one(world.inbox, world.chat, world.signers, runtime=portuguese)
    assert world.chat.to(OWNER_B)[0] in CATALOGS["pt-br"].replies["wake"]
    # One of the replies is picked at random.
    assert len({wake_reply("pt-br") for _ in range(200)}) > 5
    assert not world.chat.files and not world.chat.downloads and world.audit.entries() == []


def test_bot_status_lists_the_certificates_with_their_expiry_dates(world):
    message = world.send(PREPARER, text="Bot Status")
    report = world.chat.to(PREPARER)[0].splitlines()
    expiry = world.signers[0].expires_at.date().isoformat()
    assert world.state(message) == "done" and len(world.chat.to(PREPARER)) == 1
    assert report[0] == "Up and running. Certificates installed: 3."
    assert report[1] == f"1. Alice Example (file): valid until {expiry}, 364 day(s) left"
    assert all(name in line for name, line in zip(NAMES.values(), report[1:4], strict=True))
    assert report[4] == "Documents waiting for an answer: 0." and len(report) == 5
    # A document that waits for an answer is counted, and nothing about the status is recorded.
    world.send(PREPARER, make_pdf("Alice Example"))
    now = datetime.now(UTC)
    soon, late = now + timedelta(days=340), now + timedelta(days=400)
    assert status_report(world.inbox, world.signers, "en", now=now).endswith("Documents waiting for an answer: 1.")
    assert f"1. Alice Example (file): expires on {expiry}, only 24 day(s) left. Time to renew it!" in status_report(
        world.inbox, world.signers, "en", now=soon)
    assert f"1. Alice Example (file): EXPIRED on {expiry}" in status_report(world.inbox, world.signers, "en", now=late)
    portuguese = status_report(world.inbox, world.signers, "pt-br", now=now).splitlines()
    assert portuguese[0] == "Tudo funcionando. Certificados instalados: 3."
    assert portuguese[1] == f"1. Alice Example (arquivo): válido até {expiry}, faltam 364 dia(s)"
    assert status_report(world.inbox, world.signers, "es", now=now).splitlines()[-1] == "Documentos esperando respuesta: 1."
    token = (replace(world.signers[0], config=replace(world.signers[0].config, token_module=Path("module.so"), token_label="t")),)
    assert "1. Alice Example (USB token): valid until" in status_report(world.inbox, token, "en", now=now)
    assert world.audit.entries() == []


def test_bot_log_shows_each_person_only_the_signatures_they_took_part_in(world):
    world.send(PREPARER, make_pdf("Alice Example", "one"), filename="contract.pdf")
    world.send(OWNER_A, text="1")
    world.send(TRUSTED, make_pdf("Alice Example", "two"), filename="a letter\nwith a very long name " + "x" * 60 + ".docx")
    world.send(OWNER_B, make_pdf("Bruno Example"))

    def log(number, command="bot log"):
        world.send(number, text=command)
        return world.chat.to(number)[-1].splitlines()
    # The owner sees what was done with the certificate, whoever asked.
    owner = log(OWNER_A)
    assert owner[0] == "Signatures in the last 7 day(s): 2. Times in UTC." and len(owner) == 3
    assert owner[1].endswith(" · contract.pdf · Alice Example · asked by Paulo Preparer · approved by Ana")
    long_name = "a letter with a very long name " + "x" * 26 + "..."
    assert owner[2].endswith(f" · {long_name} · Alice Example · asked by {format_number(TRUSTED)} · trusted contact")
    # Whoever asked sees their own requests; nobody sees the signatures of other people.
    assert log(PREPARER, "Bot Log 12h")[0] == "Signatures in the last 12 hour(s): 1. Times in UTC."
    assert log(OWNER_B, "botlog 30d")[1].endswith(f" · report.pdf · Bruno Example · asked by {format_number(OWNER_B)} · own certificate")
    assert log(OWNER_C) == ["No signatures of yours in the last 7 day(s)."]
    assert all("Bruno Example" not in line for line in owner) and len(world.audit.entries()) == 3
    # An entry made before file names were recorded shows the start of the fingerprint of the signed file.
    world.audit.record(event="signed", certificate_id="carla", requester=OWNER_C, approval="owner", signed_sha256="abcdef0123456789")
    assert log(OWNER_C)[1].endswith(f" · abcdef01 · Carla Example · asked by {world.runtime.called(OWNER_C)} · own certificate")
    # The time filter, and the language of whoever asks.
    later = datetime.now(UTC) + timedelta(days=8)
    assert log_report(world.audit, world.signers, OWNER_A, "pt-br", world.runtime.called, 168, now=later) == (
        "Nenhuma assinatura sua no período de 7 dia(s).")
    assert log_report(world.audit, world.signers, OWNER_A, "es", world.runtime.called, 24 * 30, zone=UTC, now=later).startswith(
        "Firmas en el período de 30 día(s): 2. Horas en UTC.")
    # Times are written in the configured time zone.
    recorded = datetime.fromisoformat(world.audit.entries()[0]["time"])
    tokyo = log_report(world.audit, world.signers, OWNER_A, "pt-br", world.runtime.called, 168,
                        zone=Runtime(world.runtime.data_dir, timezone="Asia/Tokyo").zone).splitlines()
    assert tokyo[0] == "Assinaturas no período de 7 dia(s): 2. Horários em UTC+09:00."
    assert tokyo[1].startswith((recorded + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M") + " · contract.pdf · Alice Example")
    assert Runtime(world.runtime.data_dir).zone is None
    # A period longer than the record is kept is shortened to it.
    short = Runtime(world.runtime.data_dir, names=dict(PEOPLE), log_days=2, timezone="UTC")
    world.inbox.add([Job("log-long", "", None, OWNER_A, "text", parse_command("bot log 400d"))])
    assert process_one(world.inbox, world.chat, world.signers, runtime=short)
    assert world.chat.to(OWNER_A)[-1].startswith("Signatures in the last 2 day(s): 2.")


def test_bot_help_lists_the_commands_in_the_language_of_whoever_asks(world):
    message = world.send(OWNER_A, text="Bot Help")
    assert world.state(message) == "done" and len(world.chat.to(OWNER_A)) == 1
    assert world.chat.to(OWNER_A)[0].startswith("This is what you can send me:\n- A PDF, Word or LibreOffice document")
    portuguese = Runtime(world.runtime.data_dir, languages={OWNER_B: "pt-br"})
    world.inbox.add([Job("help-pt", "", None, OWNER_B, "text", parse_command("bothelp"))])
    assert process_one(world.inbox, world.chat, world.signers, runtime=portuguese)
    assert world.chat.to(OWNER_B)[0].startswith("Isto é o que você pode me enviar:") and "- acorda bot:" in world.chat.to(OWNER_B)[0]


def test_the_log_command_takes_a_period_and_nothing_else():
    assert parse_command("bot log") == "log 168" and parse_command("BOT LOG 3d") == "log 72" and parse_command("botlog 12h") == "log 12"
    for near in ("bot logs", "log", "bot log 7", "bot log 0d", "bot log week", "bot log 7d please", "bot log 99999d", "show bot log"):
        assert parse_command(near) is None, near


def test_the_record_of_signatures_is_erased_after_the_retention_period(world):
    world.send(OWNER_A, make_pdf("Alice Example"))
    assert len(world.audit.entries()) == 1 and world.audit.prune(90) == 0
    assert world.audit.prune(1, now=datetime.now(UTC) + timedelta(days=2)) == 1
    assert world.audit.entries() == [] and world.audit.path.read_text(encoding="utf-8") == ""


def test_ordinary_conversation_is_never_answered(world):
    """The number is also a personal one: only documents, the ping and awaited answers get a reply."""
    for words in ("1", "0", "sim", "Não", "todos", "ok"):
        message = world.send(OWNER_A, text=words)
        assert world.state(message) == "done"
    assert world.chat.to(OWNER_A) == [] and not world.chat.files and world.audit.entries() == []
    chat = f"{OWNER_A}@s.whatsapp.net"
    payload = {"event": {"type": "messages", "event": "post"},
               "messages": [{"id": f"t{index}", "from_me": False, "type": "text", "chat_id": chat, "text": {"body": body}}
                            for index, body in enumerate(["help", "ajuda", "verificar", "ping", "see you at 5", "BOTM4", "acordsa bot", "acorda o bot",
                                                       "acorda bot!", "bom dia, acorda bot", "status", "bot status please", "ACORDA BOT"])]}
    assert [job.body for job in accepted_events(read_messages(payload), frozenset({OWNER_A}))] == ["ping"]
    payload["messages"] = [{"id": "d1", "from_me": False, "type": "document", "chat_id": chat,
                            "document": {"id": "media-d", "file_name": "a.pdf", "caption": " Verificar "}}]
    assert accepted_events(read_messages(payload), frozenset({OWNER_A}))[0].body == "verify"


def test_a_photo_and_a_picture_file_are_read_as_documents(world):
    chat = f"{OWNER_A}@s.whatsapp.net"
    payload = {"event": {"type": "messages", "event": "post"},
               "messages": [{"id": "p1", "from_me": False, "type": "image", "chat_id": chat,
                             "image": {"id": "media-p", "mime_type": "image/jpeg"}},
                            {"id": "p2", "from_me": False, "type": "document", "chat_id": chat,
                             "document": {"id": "media-f", "file_name": "scan.jpeg", "mime_type": "image/jpeg"}}]}
    photo, picture_file = accepted_events(read_messages(payload), frozenset({OWNER_A}))
    assert (photo.kind, photo.filename, photo.body) == ("document", None, "photo")
    assert (picture_file.filename, picture_file.body) == ("scan.jpeg", None)


def test_a_photo_the_bot_cannot_sign_gets_no_answer_while_a_picture_file_does(world):
    from PIL import Image
    buffer = BytesIO()
    Image.new("RGB", (1200, 1600), "white").save(buffer, "JPEG")
    message = world.send(OWNER_A, buffer.getvalue(), filename=None, caption="photo")
    # It may be an ordinary photo: no "Got it", and no answer when nothing can be signed.
    assert world.state(message) == "needs_review" and world.chat.to(OWNER_A) == [] and not world.chat.notes
    message = world.send(OWNER_A, buffer.getvalue(), filename="scan.jpeg")
    assert world.state(message) == "needs_review" and len(world.chat.to(OWNER_A)) == 1


def test_key_material_sent_in_chat_is_refused_without_download(world):
    payload = {"event": {"type": "messages", "event": "post"},
               "messages": [{"id": "k1", "from_me": False, "type": "document", "chat_id": f"{OWNER_A}@s.whatsapp.net",
                             "document": {"id": "media-k", "file_name": "alice.P12"}},
                            {"id": "k2", "from_me": False, "type": "text", "chat_id": f"{OWNER_A}@s.whatsapp.net",
                             "text": {"body": "my password is hunter2"}}]}
    events = accepted_events(read_messages(payload), frozenset({OWNER_A}))
    assert events == [Job("k1", "", None, OWNER_A, "refused", None)]
    world.inbox.add(events)
    assert process_one(world.inbox, world.chat, world.signers, runtime=world.runtime)
    assert world.state("k1") == "refused" and not world.chat.downloads
    assert "never send certificates" in world.chat.to(OWNER_A)[0]


def test_only_known_commands_are_kept_from_chat_text():
    assert parse_command("ACORDA BOT") == "ping" and parse_command(" acorda bot ") == "ping" and parse_command("AcordaBot") == "ping"
    # The number is also a personal one, so anything short of the exact phrase is ordinary conversation.
    for near in ["botm4", "acordsa bot", "acorda  bot", "acorda bot!", "acorda, bot", "acorda o bot", "acorda", "bot", "acorda bot 1"]:
        assert parse_command(near) is None
    assert parse_command("2") == "answer 2" and parse_command("Todos") == "answer all"
    assert parse_command("Sim") == "answer 1" and parse_command("aprovar") == "answer 1"
    assert parse_command("cancelar") == "answer 0" and parse_command("recusar") == "answer 0" and parse_command("Não") == "answer 0"
    for text in ["verificar", "ajuda", "help", "ping", "hello", "my cpf is 123.456.789-00", "47", "47 please", "1 2", "1234567", None, "x" * 50]:
        assert parse_command(text) is None


def test_requests_are_limited_per_number(world):
    limited = Runtime(world.runtime.data_dir, requests_per_hour=2, confirmation_minutes=5)
    for index in range(3):
        world.chat.documents[f"r{index}"] = make_pdf("Alice Example")
        world.inbox.add([Job(f"limit{index}", f"r{index}", "report.pdf", OWNER_A)])
    for _ in range(3):
        assert process_one(world.inbox, world.chat, world.signers, runtime=limited)
    assert len(world.chat.files) == 2 and world.state("limit2") == "rate_limited"
    assert "❌ NOT signed, over the hourly limit (1)\n• report.pdf" in world.chat.to(OWNER_A)[-1]


def test_owner_is_warned_once_a_day_thirty_days_before_expiry(tmp_path):
    soon = SignerConfig("soon", "Soon Example", make_certificate(tmp_path, "soon", "Soon Example", days=10),
                        allowed_senders=frozenset({OWNER_A}), owner=OWNER_A, password="pw-soon")
    late = SignerConfig("later", "Later Example", make_certificate(tmp_path, "later", "Later Example", days=90),
                        allowed_senders=frozenset({OWNER_B}), owner=OWNER_B, password="pw-later")
    signers = unlock_signers((soon, late))
    inbox, chat = Inbox(tmp_path / "inbox.db"), Chat()
    warn_expiring(inbox, chat, signers)
    warn_expiring(inbox, chat, signers)
    assert len(chat.texts) == 1 and chat.texts[0][0] == OWNER_A and '"soon" expires on' in chat.texts[0][1]
    warn_expiring(inbox, chat, signers, now=datetime.now(UTC) + timedelta(days=1))
    assert len(chat.texts) == 2


def test_operational_log_has_no_numbers_names_or_file_names(world, caplog):
    logger = logging.getLogger("sealwharf")
    logger.propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="sealwharf"):
            world.send(PREPARER, make_pdf("Alice Example"), filename="salary-of-alice.pdf")
            world.send(OWNER_A, text="1")
    finally:
        logger.propagate = False
    assert "signed request=" in caplog.text
    for private in (PREPARER, OWNER_A, "Alice", "Ana", "Paulo", "salary"):
        assert private not in caplog.text


# Languages and numbers.

def test_each_person_is_answered_in_their_own_language(world):
    portuguese = Runtime(world.runtime.data_dir, languages={PREPARER: "pt-br", OWNER_A: "pt-br"}, names=dict(PEOPLE))
    world.chat.documents["lang"] = make_pdf("Alice Example")
    world.inbox.add([Job("lang1", "lang", "relatorio.pdf", PREPARER)])
    assert process_one(world.inbox, world.chat, world.signers, runtime=portuguese)
    assert world.chat.notes[0] == (PREPARER, "Recebido! Estou processando...")
    assert world.chat.to(PREPARER)[0].startswith("Sobre relatorio.pdf:\nPara assinar como Alice Example, preciso da aprovação de Ana.")
    world.inbox.add([Job("lang2", "", None, PREPARER, "text", "answer 1")])
    assert process_one(world.inbox, world.chat, world.signers, runtime=portuguese)
    assert world.chat.to(OWNER_A)[0] == ("Olá! Recebemos um pedido de Paulo Preparer (+55 11 90000-0010) para assinar o documento "
                                         "a seguir com o seu certificado digital. Digite 1 para aprovar e 0 ou ignore para reprovar.")
    assert world.chat.to(PREPARER)[1].startswith("Pronto! Enviei o documento para Ana aprovar.")
    # A number with no language set reads English.
    world.inbox.add([Job("lang3", "", None, OWNER_C, "text", "answer 1"), Job("lang4", "", None, OWNER_A, "text", "answer 1")])
    assert process_one(world.inbox, world.chat, world.signers, runtime=portuguese)
    assert process_one(world.inbox, world.chat, world.signers, runtime=portuguese)
    assert world.chat.to(OWNER_C) == []
    assert world.chat.to(OWNER_A)[-1] == "Aprovado! O documento foi assinado."
    assert world.chat.notes[-1] == (PREPARER, "Tudo certo! Assinado por: Alice Example." + REVIEW["pt-br"])


def test_a_spanish_speaker_is_asked_and_answered_in_spanish(world):
    spanish = Runtime(world.runtime.data_dir, languages={PREPARER: "es", OWNER_A: "es"}, names=dict(PEOPLE))

    def step(message, body=None, document=None):
        if document is not None:
            world.chat.documents[message] = document
        world.inbox.add([Job(message, message if document else "", "informe.pdf" if document else None,
                             OWNER_A if message.startswith("owner") else PREPARER, "document" if document else "text", body)])
        assert process_one(world.inbox, world.chat, world.signers, runtime=spanish)
    step("es1", parse_command("Despierta Bot"))
    assert world.chat.to(PREPARER)[0] in CATALOGS["es"].replies["wake"]
    step("es2", document=make_pdf("Alice Example"))
    assert world.chat.notes[0] == (PREPARER, "¡Recibido! Lo estoy procesando...")
    assert world.chat.to(PREPARER)[1].startswith("Sobre informe.pdf:\nPara firmar como Alice Example, necesito la aprobación de Ana.")
    step("es3", parse_command("Sí"))
    assert world.chat.to(OWNER_A)[0].startswith("¡Hola! Paulo Preparer (+55 11 90000-0010) te pide firmar el siguiente documento")
    assert world.chat.to(PREPARER)[2].startswith("¡Listo! Envié el documento a Ana para su aprobación.")
    step("owner1", parse_command("aprobar"))
    assert world.chat.to(OWNER_A)[-1] == "¡Aprobado! El documento fue firmado."
    assert world.chat.notes[-1] == (PREPARER, "¡Listo! Firmado por: Alice Example." + REVIEW["es"])
    assert signer_names(world.chat.files[0][1]) == ["Alice Example"]


def test_brazilian_numbers_match_with_or_without_the_ninth_digit(world):
    assert canonical_number("555381234567") == "5553981234567" == canonical_number("5553981234567")
    # Landlines, other countries and complete numbers are left alone.
    for number in ("555332123456", "5511900000001", "14155550123", "351912345678"):
        assert canonical_number(number) == number
    written, whatsapp = "5553981234567", "555381234567"
    payload = {"event": {"type": "messages", "event": "post"},
               "messages": [{"id": "n1", "from_me": False, "type": "text", "chat_id": f"{whatsapp}@s.whatsapp.net",
                             "from": whatsapp, "text": {"body": "Acorda Bot"}}]}
    events = accepted_events(read_messages(payload), frozenset({written}))
    assert events == [Job("n1", "", None, written, "text", "ping", whatsapp)]
    world.inbox.add(events)
    # The job carries the configured number; replies go to the identifier WhatsApp used.
    assert world.inbox.claim() == Job("n1", "", None, written, "text", "ping")
    assert world.inbox.known_address(written) == whatsapp and world.inbox.known_address(OWNER_A) is None
    sent = []

    def handler(request):
        sent.append((request.url.path, request.content.decode().replace(" ", "")))
        return httpx.Response(200, json={})
    Whapi("token", httpx.MockTransport(handler), addresses=world.inbox).send_text(written, "Hello")
    assert sent == [("/messages/text", sent[0][1])] and f'"{whatsapp}@s.whatsapp.net"' in sent[0][1]


def test_identifier_of_a_number_that_never_wrote_is_looked_up_once(world):
    written, whatsapp = "5553987654321", "555387654321"
    calls = []
    answers = {"lookup": {"contacts": [{"input": written, "status": "valid", "wa_id": f"{whatsapp}@s.whatsapp.net"}]}}

    def handler(request):
        calls.append((request.url.path, request.content.decode().replace(" ", "")))
        if request.url.path == "/contacts":
            return httpx.Response(200, json=answers["lookup"])
        return httpx.Response(200, json={})
    client = Whapi("token", httpx.MockTransport(handler), addresses=world.inbox)
    client.send_text(written, "first")
    client.send_text(written, "second")
    # One lookup, then both messages go to the identifier WhatsApp uses.
    assert [path for path, _ in calls] == ["/contacts", "/messages/text", "/messages/text"]
    assert all(f'"{whatsapp}@s.whatsapp.net"' in body for _, body in calls[1:])
    assert world.inbox.known_address(written) == whatsapp
    # A number WhatsApp does not know, or a failed lookup, falls back to the number as written.
    for lookup in ({"contacts": [{"input": "x", "status": "invalid"}]}, {"unexpected": True}):
        answers["lookup"] = lookup
        calls.clear()
        client.send_text("5511900000042", "hello")
        assert '"5511900000042@s.whatsapp.net"' in calls[-1][1] and world.inbox.known_address("5511900000042") is None


def test_unsigned_documents_are_forwarded_under_their_own_name():
    assert signed_filename("contract.pdf") == "contract - signed.pdf" and signed_filename("contract.pdf", "") == "contract.pdf"
    assert signed_filename("report.docx", "") == "report.pdf" and signed_filename(None, "") == "document.pdf"


# The configuration file.

CONTACTS = ('contacts:\n  ana:\n    name: Ana Owner\n    number: "5511900000001"\n    lang: en\n'
            '  paulo:\n    name: Paulo Preparer\n    number: "5511900000010"\n'
            '  tina:\n    number: "5511900000011"\n    lang: pt-br\n')
CERTIFICATE = ('certificates:\n  - id: john\n    name: John Doe\n    file: certificates/john.p12\n'
               '    password: "0123"\n    owner: ana\n')


@pytest.fixture
def config_file(tmp_path):
    (tmp_path / "certificates").mkdir()
    (tmp_path / "certificates" / "john.p12").write_bytes(b"placeholder")
    (tmp_path / "certificates" / "jane.p12").write_bytes(b"placeholder")
    return tmp_path / "sealwharf.yaml"


def test_configuration_defines_contacts_roles_tokens_and_languages(config_file):
    config_file.write_text('public_url: https://bot.example.com\ngateway_token: "gateway-secret"\n'
                           + CONTACTS + CERTIFICATE + '    preparers: [paulo]\n    trusted: [tina]\n'
                           'limits:\n  requests_per_hour: 7\n  confirmation_minutes: 3\n', encoding="utf-8")
    config = load_config(config_file)
    signer = config.signers[0]
    assert (config.requests_per_hour, config.confirmation_minutes) == (7, 3)
    assert (config.public_url, config.gateway_token) == ("https://bot.example.com", "gateway-secret")
    assert config.senders == {"5511900000001", "5511900000010", "5511900000011"} and signer.owner == "5511900000001"
    assert config.names == {"5511900000001": "Ana Owner", "5511900000010": "Paulo Preparer"}
    # A contact without its own lang uses the language of the file, which is English unless set.
    assert config.languages == {"5511900000001": "en", "5511900000010": "en", "5511900000011": "pt-br"}
    assert not signer.needs_confirmation("5511900000001") and not signer.needs_confirmation("5511900000011")
    assert signer.needs_confirmation("5511900000010") and signer.password == "0123"
    # Secrets never show up when the configuration is printed.
    assert "secret" not in repr(config) and "0123" not in repr(config)
    # The gateway is Whapi.Cloud unless another one is named; the earlier name of its token still works.
    assert (config.gateway_type, config.gateway_url, config.gateway_session) == ("whapi", None, "default")
    config_file.write_text('gateway_url: http://host.docker.internal:8787\nwhapi_token: "old-name"\n' + CONTACTS + CERTIFICATE,
                           encoding="utf-8")
    config = load_config(config_file)
    assert (config.gateway_url, config.gateway_token) == ("http://host.docker.internal:8787", "old-name")
    config_file.write_text("gateway_type: waha\ngateway_url: http://host.docker.internal:3000\ngateway_session: bot\n"
                           + CONTACTS + CERTIFICATE, encoding="utf-8")
    config = load_config(config_file)
    assert (config.gateway_type, config.gateway_url, config.gateway_session) == ("waha", "http://host.docker.internal:3000", "bot")
    config_file.write_text("language: pt-br\n" + CONTACTS + CERTIFICATE, encoding="utf-8")
    assert load_config(config_file).languages == {"5511900000001": "en", "5511900000010": "pt-br", "5511900000011": "pt-br"}


def test_one_contact_can_be_used_by_several_certificates(config_file):
    second = ('  - id: jane\n    name: Jane Doe\n    file: certificates/jane.p12\n    password: "x"\n'
              '    owner: paulo\n    trusted: [ana]\n')
    config_file.write_text(CONTACTS + CERTIFICATE + "    preparers: [paulo]\n" + second, encoding="utf-8")
    john, jane = load_config(config_file).signers
    assert john.owner == "5511900000001" and jane.autotrust == {"5511900000001"}
    assert jane.owner == "5511900000010" and john.needs_confirmation("5511900000010") and not jane.needs_confirmation("5511900000001")


@pytest.mark.parametrize("broken", [
    CERTIFICATE,                                                                    # no contacts at all
    CONTACTS + CERTIFICATE.replace("owner: ana", "owner: nobody"),                  # unknown contact
    CONTACTS + CERTIFICATE + "    preparers: [ana]\n",                              # the owner listed twice
    CONTACTS + CERTIFICATE.replace("    owner: ana\n", "    preparers: [paulo]\n"),  # nobody to approve
    CONTACTS + CERTIFICATE.replace("    owner: ana\n", ""),                         # nobody at all
    CONTACTS + CERTIFICATE + '    owner_number: "5511900000001"\n',                 # unknown setting
    CONTACTS.replace('"5511900000010"', '"5511900000001"') + CERTIFICATE,           # two contacts, one number
    CONTACTS.replace('"5511900000010"', "5511900000010") + CERTIFICATE,             # number without quotes
    CONTACTS.replace("lang: pt-br", "lang: fr") + CERTIFICATE,                      # unsupported language
    "language: fr\n" + CONTACTS + CERTIFICATE,
    CONTACTS + CERTIFICATE + "limits:\n  confirmation_minutes: 0\n",
    "gateway_token: has space\n" + CONTACTS + CERTIFICATE,
    "whapi_token: has space\n" + CONTACTS + CERTIFICATE,
    'gateway_token: "one"\nwhapi_token: "two"\n' + CONTACTS + CERTIFICATE,         # the same setting under both names
    "gateway_url: http://bridge.example/path\n" + CONTACTS + CERTIFICATE,
    "gateway_type: telegram\n" + CONTACTS + CERTIFICATE,                           # no adapter with this name
    "gateway_type: waha\n" + CONTACTS + CERTIFICATE,                               # a hosted gateway needs its address
    "gateway_session: has space\n" + CONTACTS + CERTIFICATE,
    "gateway_url: http://user:secret@bridge.example\n" + CONTACTS + CERTIFICATE,
    'tunnel_token: "moved"\n' + CONTACTS + CERTIFICATE,                           # the tunnel token has a file of its own
    "public_url: http://bot.example.com\n" + CONTACTS + CERTIFICATE,
])
def test_configuration_mistakes_are_refused(config_file, broken):
    config_file.write_text(broken, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(config_file)


def test_password_must_be_text_in_quotes(config_file):
    # A bare number would silently lose its leading zero, and a missing password cannot open anything.
    for broken in [CERTIFICATE.replace('"0123"', "0123"), CERTIFICATE.replace('    password: "0123"\n', "")]:
        config_file.write_text(CONTACTS + broken, encoding="utf-8")
        with pytest.raises(ValueError, match="in quotes"):
            load_config(config_file)


def test_a_certificate_without_owner_accepts_only_trusted_contacts(config_file):
    config_file.write_text(CONTACTS + CERTIFICATE.replace("    owner: ana\n", "    trusted: [paulo, tina]\n"), encoding="utf-8")
    signer = load_config(config_file).signers[0]
    assert signer.owner is None and not signer.needs_confirmation("5511900000010")


def test_a_single_name_may_be_written_without_a_list(config_file):
    extra = chr(10).join(['    preparers: paulo', '    trusted: tina', '    aliases: Johnny Doe', ''])
    config_file.write_text(CONTACTS + CERTIFICATE + extra, encoding="utf-8")
    signer = load_config(config_file).signers[0]
    assert signer.needs_confirmation("5511900000010") and not signer.needs_confirmation("5511900000011")
    assert signer.names == ("John Doe", "Johnny Doe")


def test_a_contact_limit_is_read_from_the_configuration(tmp_path):
    from sealwharf.signer_config import load_config
    (tmp_path / "certificate.p12").write_bytes(b"synthetic")
    config = tmp_path / "sealwharf.yaml"
    body = ('contacts:\n  jane:\n    number: "5511999999999"\n    limit: {limit}\n  john:\n    number: "5511988888888"\n'
            'certificates:\n  - id: one\n    name: Jane Example\n    file: certificate.p12\n    password: "synthetic"\n    owner: jane\n')
    config.write_text(body.format(limit=300), encoding="utf-8")
    assert load_config(config).request_limits == {"5511999999999": 300}
    for wrong in ("0", "many", "true", "9000"):
        config.write_text(body.format(limit=wrong), encoding="utf-8")
        with pytest.raises(ValueError, match="limit of contact jane"):
            load_config(config)
