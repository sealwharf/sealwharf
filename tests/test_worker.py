from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from sealwharf.adapters.inbox import Inbox
from sealwharf.application.worker import UnlockedSigner, process_one
from sealwharf.signer_config import SignerConfig


def signer(allowed=("5511999999999",), *, visual="deterministic", logo=None, scale=1.0):
    config = SignerConfig("primary", "Jane Example", Path("cert"), visual, logo, scale, frozenset(allowed))
    return (UnlockedSigner(config, b"pass", b"synthetic-fingerprint"),)


def state(inbox):
    with inbox.connect() as db:
        return db.execute("SELECT state FROM jobs").fetchone()[0]


def test_review_and_no_send(tmp_path, monkeypatch):
    inbox = Inbox(tmp_path / "queue.db")
    inbox.add([("message", "media", "report.pdf", "5511999999999")])
    notifications = []
    client = SimpleNamespace(download_document=lambda _: b"pdf", send_pdf=lambda *_: (_ for _ in ()).throw(AssertionError()), send_text=lambda *args: notifications.append(args))
    monkeypatch.setattr("sealwharf.application.worker.validate_pdf", lambda *_: None)
    monkeypatch.setattr("sealwharf.application.worker.has_signature_widgets", lambda *_: True)
    monkeypatch.setattr("sealwharf.application.worker.find_signature_position", lambda *_: None)
    assert process_one(inbox, client, signer())
    assert state(inbox) == "needs_review"
    assert len(notifications) == 1


def test_uncertain_send_is_not_retried(tmp_path, monkeypatch):
    inbox = Inbox(tmp_path / "queue.db")
    inbox.add([("message", "media", "report.pdf", "5511999999999")])
    def fail(*_):
        raise TimeoutError()
    client = SimpleNamespace(download_document=lambda _: b"pdf", send_pdf=fail)
    monkeypatch.setattr("sealwharf.application.worker.validate_pdf", lambda *_: None)
    monkeypatch.setattr("sealwharf.application.worker.find_signature_position", lambda *_: SimpleNamespace(page=0, box=(1, 2, 3, 4), field_name="Nome"))
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf", lambda *_: b"signed")
    assert process_one(inbox, client, signer())
    assert state(inbox) == "uncertain"
    assert not process_one(inbox, client, signer())


def test_docx_is_converted_and_sent(tmp_path, monkeypatch):
    inbox = Inbox(tmp_path / "queue.db")
    inbox.add([("message", "media", "Report.docx", "5511999999999")])
    sent = []
    client = SimpleNamespace(download_document=lambda _: b"PK\x03\x04docx", send_pdf=lambda _, data, name: sent.append((data, name)))
    monkeypatch.setattr("sealwharf.application.worker.convert_docx_to_pdf", lambda _: b"pdf")
    monkeypatch.setattr("sealwharf.application.worker.validate_pdf", lambda *_: None)
    monkeypatch.setattr("sealwharf.application.worker.find_signature_position", lambda *_: SimpleNamespace(page=0, box=(1, 2, 3, 4), field_name="Signature"))
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf", lambda *_: b"signed")
    assert process_one(inbox, client, signer())
    assert sent == [(b"signed", "Report.docx")]
    assert state(inbox) == "sent"


def test_worker_passes_signer_logo_to_signing(tmp_path, monkeypatch):
    inbox = Inbox(tmp_path / "queue.db")
    inbox.add([("message", "media", "report.pdf", "5511999999999")])
    client = SimpleNamespace(download_document=lambda _: b"pdf", send_pdf=lambda *_: None)
    monkeypatch.setattr("sealwharf.application.worker.validate_pdf", lambda *_: None)
    monkeypatch.setattr("sealwharf.application.worker.find_signature_position", lambda *_: SimpleNamespace(page=0, box=(1, 2, 3, 4)))
    logos = []
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf", lambda *args: logos.append(args[4:6]) or b"signed")
    logo = tmp_path / "company.png"
    assert process_one(inbox, client, signer(logo=logo, scale=0.5))
    assert logos == [(logo, 0.5)]


def test_worker_routes_to_original_sender_and_rechecks_authorization(tmp_path, monkeypatch):
    first, second = "5511999999999", "5511888888888"
    inbox = Inbox(tmp_path / "queue.db")
    inbox.add([("one", "media", "one.pdf", first), ("two", "media", "two.pdf", second),
               ("three", "media", "three.pdf", second)])
    sent = []
    client = SimpleNamespace(download_document=lambda _: b"pdf", send_pdf=lambda recipient, *_: sent.append(recipient))
    monkeypatch.setattr("sealwharf.application.worker.validate_pdf", lambda *_: None)
    monkeypatch.setattr("sealwharf.application.worker.find_signature_position", lambda *_: SimpleNamespace(page=0, box=(1, 2, 3, 4)))
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf", lambda *_: b"signed")
    assert process_one(inbox, client, signer((first, second)))
    assert process_one(inbox, client, signer((first, second)))
    assert process_one(inbox, client, signer((first,)))
    assert sent == [first, second]
    with inbox.connect() as db:
        assert db.execute("SELECT state FROM jobs WHERE message_id='three'").fetchone()[0] == "needs_review"


def test_visual_candidate_is_used_only_without_signature_widgets(tmp_path, monkeypatch):
    inbox = Inbox(tmp_path / "queue.db")
    inbox.add([("message", "media", "report.pdf", "5511999999999")])
    sent = []
    client = SimpleNamespace(download_document=lambda _: b"pdf", send_pdf=lambda _, data, name: sent.append((data, name)))
    monkeypatch.setattr("sealwharf.application.worker.validate_pdf", lambda *_: None)
    monkeypatch.setattr("sealwharf.application.worker.find_signature_position", lambda *_: None)
    monkeypatch.setattr("sealwharf.application.worker.has_signature_widgets", lambda *_: False)
    monkeypatch.setattr("sealwharf.application.worker.suggest_visual_positions", lambda *_, **__: [SimpleNamespace(page=0, box=(1, 2, 3, 4))])
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf", lambda *_: b"signed")
    assert process_one(inbox, client, signer())
    assert sent == [(b"signed", "report.pdf")]


def test_ai_review_failure_does_not_sign(tmp_path, monkeypatch):
    inbox = Inbox(tmp_path / "queue.db")
    inbox.add([("message", "media", "report.pdf", "5511999999999")])
    notifications = []
    client = SimpleNamespace(download_document=lambda _: b"pdf", send_pdf=lambda *_: (_ for _ in ()).throw(AssertionError()), send_text=lambda *args: notifications.append(args))
    monkeypatch.setattr("sealwharf.application.worker.validate_pdf", lambda *_: None)
    monkeypatch.setattr("sealwharf.application.worker.find_signature_position", lambda *_: None)
    monkeypatch.setattr("sealwharf.application.worker.has_signature_widgets", lambda *_: False)
    monkeypatch.setattr("sealwharf.application.worker.suggest_local_ai_position", lambda *_: None)
    assert process_one(inbox, client, signer(visual="ollama"))
    assert state(inbox) == "needs_review"
    assert len(notifications) == 1


def test_certificate_selection_respects_each_allowlist_and_ambiguity(tmp_path, monkeypatch):
    first, second = "5511999999999", "5511888888888"
    jane = UnlockedSigner(SignerConfig("jane", "Jane Example", Path("jane.p12"), allowed_senders=frozenset({first})), b"jane-secret", b"jane-fingerprint")
    john = UnlockedSigner(SignerConfig("john", "John Example", Path("john.p12"), allowed_senders=frozenset({first, second})), b"john-secret", b"john-fingerprint")
    def document(text):
        writer = PdfWriter()
        page = writer.add_blank_page(width=400, height=600)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"), NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
        stream = DecodedStreamObject()
        stream.set_data(b"BT /F1 10 Tf 20 300 Td (" + text.encode("ascii") + b") Tj ET")
        page[NameObject("/Contents")] = writer._add_object(stream)
        result = BytesIO()
        writer.write(result)
        return result.getvalue()
    docs = {"john": document("John Example"), "jane": document("Jane Example"), "both": document("Jane Example and John Example")}
    sent, notices, signed_with = [], [], []
    client = SimpleNamespace(download_document=lambda media: docs[media], send_pdf=lambda recipient, *_: sent.append(recipient),
                             send_text=lambda recipient, _: notices.append(recipient))
    def locate(stream, name):
        text = PdfReader(stream).pages[0].extract_text()
        box = (1, 2, 30, 40) if name == "Jane Example" else (50, 2, 80, 40)
        return SimpleNamespace(page=0, box=box) if name in text else None
    monkeypatch.setattr("sealwharf.application.worker.find_signature_position", locate)
    monkeypatch.setattr("sealwharf.application.worker.has_signature_widgets", lambda _: False)
    monkeypatch.setattr("sealwharf.application.worker.suggest_visual_positions", lambda *_, **__: [])
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf", lambda document, cert, password, *args: signed_with.append((cert, password)) or b"signed")
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf_sequence",
                        lambda document, requests: signed_with.extend((r.certificate, r.password) for r in requests) or b"signed")
    inbox = Inbox(tmp_path / "queue.db")
    inbox.add([("a", "john", "one.pdf", first), ("b", "jane", "two.pdf", second), ("c", "both", "three.pdf", first)])
    for _ in range(3):
        assert process_one(inbox, client, (jane, john))
    # One signer signs at once; with two, the requester is asked and then chooses both.
    assert signed_with == [(Path("john.p12"), b"john-secret")] and sent == [first]
    assert notices == [second, first]
    inbox.add([("choice", "", None, first, "text", "answer all")])
    assert process_one(inbox, client, (jane, john))
    assert signed_with == [(Path("john.p12"), b"john-secret"), (Path("jane.p12"), b"jane-secret"),
                           (Path("john.p12"), b"john-secret")]
    assert sent == [first, first]
    del notices[1]
    jane_long = UnlockedSigner(SignerConfig("jane", "Jane Middle Example", Path("jane.p12"),
                               allowed_senders=frozenset({first})), b"jane-secret", b"jane-fingerprint")
    inbox.add([("d", "both", "four.pdf", first)])
    assert process_one(inbox, client, (jane_long, john))
    assert sent == [first, first]
    assert notices == [second, first]


def test_multiple_signers_are_not_partially_signed_when_a_position_is_missing(tmp_path, monkeypatch):
    sender = "5511999999999"
    jane = UnlockedSigner(SignerConfig("jane", "Jane Example", Path("jane.p12"), allowed_senders=frozenset({sender})), b"secret", b"fingerprint")
    john = UnlockedSigner(SignerConfig("john", "John Example", Path("john.p12"), allowed_senders=frozenset({sender})), b"secret", b"fingerprint")
    inbox = Inbox(tmp_path / "queue.db")
    inbox.add([("message", "media", "report.pdf", sender)])
    sent, notices = [], []
    client = SimpleNamespace(download_document=lambda _: b"pdf", send_pdf=lambda *args: sent.append(args),
                             send_text=lambda *args: notices.append(args))
    monkeypatch.setattr("sealwharf.application.worker.validate_pdf", lambda *_: None)
    monkeypatch.setattr("sealwharf.application.worker._referenced_signers", lambda *_: (jane, john))
    monkeypatch.setattr("sealwharf.application.worker._locate_signature",
                        lambda doc, signer, *_: SimpleNamespace(page=0, box=(1, 2, 30, 40)) if signer == jane else None)
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf_sequence", lambda *_: (_ for _ in ()).throw(AssertionError()))
    assert process_one(inbox, client, (jane, john))
    # Jane can still be chosen; John, who has no clear place, is named and never signed for.
    assert not sent and len(notices) == 1 and state(inbox) == "awaiting_choice"
    assert "1 - Jane Example" in notices[0][1] and "clear place to sign for: John Example" in notices[0][1]


def _text_pdf(text):
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=600)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 10 Tf 20 300 Td (" + text.encode("ascii") + b") Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


def test_initialed_names_identify_signers_only_when_unambiguous():
    from sealwharf.application.worker import _referenced_signers
    sender = frozenset({"5511999999999"})
    maria = UnlockedSigner(SignerConfig("maria", "Jane Maria Example", Path("a.p12"), allowed_senders=sender), b"x", b"a")
    marta = UnlockedSigner(SignerConfig("marta", "Jane Marta Example", Path("b.p12"), allowed_senders=sender), b"x", b"b")
    john = UnlockedSigner(SignerConfig("john", "John Example", Path("c.p12"), allowed_senders=sender), b"x", b"c")
    assert _referenced_signers(_text_pdf("Jane M. Example and John Example"), (maria, john)) == (maria, john)
    assert _referenced_signers(_text_pdf("Jane M. Example and John Example"), (maria, marta, john)) == ()
    assert _referenced_signers(_text_pdf("Jane Maria Example"), (maria, marta, john)) == (maria,)
