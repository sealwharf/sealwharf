"""Synthetic form sections must not be confused with other signers."""

import json
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import httpx
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from sealwharf.adapters.inbox import Inbox
from sealwharf.adapters.local_ai import review_contextual_candidate, suggest_general_position
from sealwharf.application.worker import UnlockedSigner, process_one
from sealwharf.documents.contextual_position import suggest_contextual_position
from sealwharf.signer_config import SignerConfig


def form(*, signature=b"Signature:", name=b"Name: Jane Example", extra=b"", rotation=0, borders=None):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(
        b"q 1 1 1 rg 0 0 600 800 re f Q "
        + (borders or b"50 400 m 50 700 l S 550 400 m 550 700 l S ")
        + b"50 400 m 550 400 l S 50 460 m 550 460 l S 50 700 m 550 700 l S "
        b"BT /F1 12 Tf 65 650 Td (" + name + b") Tj ET "
        b"BT /F1 12 Tf 65 440 Td (" + signature + b") Tj ET " + extra
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    page[NameObject("/Rotate")] = NumberObject(rotation)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def test_finds_empty_signature_row_in_named_section():
    candidate = suggest_contextual_position(form(), "Jane Example")
    assert candidate is not None
    assert candidate.page == 0
    assert candidate.reason == "contextual_signature_row"
    assert candidate.context_box is not None
    assert 400 < candidate.box[1] < 440 < candidate.box[3] < 460
    assert 65 < candidate.box[0] < candidate.box[2] <= 546


def test_side_borders_drawn_one_row_at_a_time_still_bound_the_section():
    pieces = b"".join(b"%d %d m %d %d l S " % (x, low, x, high) for x in (50, 550) for low, high in ((400, 430), (430, 460), (460, 700)))
    assert suggest_contextual_position(form(borders=pieces), "Jane Example") == suggest_contextual_position(form(), "Jane Example")
    # Pieces with a gap between them are two tables, not one.
    apart = b"".join(b"%d %d m %d %d l S " % (x, low, x, high) for x in (50, 550) for low, high in ((400, 450), (470, 700)))
    assert suggest_contextual_position(form(borders=apart), "Jane Example") is None


def test_general_model_suggestion_requires_geometry_and_visual_review():
    calls = []
    def handler(request):
        payload = json.loads(request.content)
        calls.append(payload)
        if len(calls) % 2:
            result = {"found": True, "visible_name": "Jane Example", "box": [250, 437.5, 500, 487.5]}
        else:
            result = {"signature_area": True, "visible_name": "Jane Example"}
        return httpx.Response(200, json={"done": True, "message": {"content": json.dumps(result)}})
    candidate = suggest_general_position(form(), "Jane Example", transport=httpx.MockTransport(handler))
    assert candidate is not None and candidate.reason == "ai_proposal"
    assert candidate.box == (150, 410, 300, 450)
    assert len(calls) == 2


def test_general_model_cannot_override_identity_or_occupied_area():
    for name, box in (("John Example", [250, 437.5, 500, 487.5]),
                      ("Jane Example", [80, 437.5, 300, 487.5]),
                      ("Jane Example", [-1, 0, 100, 100])):
        result = {"found": True, "visible_name": name, "box": box}
        transport = httpx.MockTransport(lambda _, value=result: httpx.Response(
            200, json={"done": True, "message": {"content": json.dumps(value)}}))
        assert suggest_general_position(form(), "Jane Example", transport=transport) is None


def test_does_not_assign_other_roles_or_wrong_names():
    assert suggest_contextual_position(form(signature=b"Signature of entity representative:"), "Jane Example") is None
    assert suggest_contextual_position(form(), "John Example") is None
    assert suggest_contextual_position(form(name=b"Name: Jane Example Junior"), "Jane Example") is None
    assert suggest_contextual_position(form(rotation=90), "Jane Example") is None


def test_a_label_naming_a_role_belongs_to_the_person_written_with_that_role():
    role = {"name": b"Entity representative: Jane Example", "signature": b"Signature of the entity representative:"}
    candidate = suggest_contextual_position(form(**role), "Jane Example")
    assert candidate is not None and 400 < candidate.box[1] < 440 < candidate.box[3] < 460
    # Another role, another name with that role, the role written apart from the name, or two people with it: not signed.
    assert suggest_contextual_position(form(name=b"Witness: Jane Example", signature=role["signature"]), "Jane Example") is None
    assert suggest_contextual_position(form(**role), "John Example") is None
    assert suggest_contextual_position(form(name=b"Entity representative:", signature=role["signature"],
                                            extra=b"BT /F1 12 Tf 65 620 Td (Jane Example) Tj ET "), "Jane Example") is None
    assert suggest_contextual_position(form(**role, extra=b"BT /F1 12 Tf 65 620 Td (Entity representative: Jane Example) Tj ET "),
                                       "Jane Example") is None


def test_rejects_multiple_names_or_signatures_and_occupied_space():
    for extra in (b"BT /F1 12 Tf 65 620 Td (Name: John Example) Tj ET ",
                  b"BT /F1 12 Tf 65 420 Td (Signature:) Tj ET ",
                  b"BT /F1 12 Tf 100 415 Td (Existing content) Tj ET "):
        assert suggest_contextual_position(form(extra=extra), "Jane Example") is None


def test_local_model_reviews_only_prevalidated_section():
    document = form()
    candidate = suggest_contextual_position(document, "Jane Example")
    calls = []
    def handler(request):
        calls.append(request)
        payload = json.loads(request.content)
        assert "form section" in payload["messages"][0]["content"]
        assert payload["messages"][0]["images"]
        return httpx.Response(200, json={"done": True, "message": {"content": json.dumps({"signature_area": True, "visible_name": "Jane Example"})}})
    transport = httpx.MockTransport(handler)
    assert review_contextual_candidate(document, "Jane Example", candidate, transport=transport) == candidate
    assert len(calls) == 1
    assert review_contextual_candidate(document, "John Example", candidate, transport=transport) is None
    assert review_contextual_candidate(document, "Jane Example", replace(candidate, box=(1, 1, 50, 50)), transport=transport) is None
    assert len(calls) == 1


def test_local_model_uncertainty_cannot_authorize_signature():
    document = form()
    candidate = suggest_contextual_position(document, "Jane Example")
    for verdict in ({"signature_area": False, "visible_name": "Jane Example"},
                    {"signature_area": True, "visible_name": "John Example"}):
        transport = httpx.MockTransport(lambda _, value=verdict: httpx.Response(200, json={"done": True, "message": {"content": json.dumps(value)}}))
        assert review_contextual_candidate(document, "Jane Example", candidate, transport=transport) is None


def test_worker_signs_a_form_section_that_names_the_signer_without_the_model(tmp_path, monkeypatch):
    sender = "5511999999999"
    signer = UnlockedSigner(SignerConfig("jane", "Jane Example", Path("certificate.p12"),
                                         allowed_senders=frozenset({sender})), b"password", b"fingerprint")
    sent, notices, reviewed = [], [], []
    client = SimpleNamespace(download_document=lambda _: form(), send_pdf=lambda *args: sent.append(args),
                             send_text=lambda *args: notices.append(args))
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf", lambda *args: b"signed")
    inbox = Inbox(tmp_path / "queue.db")
    inbox.add([("one", "media", "report.pdf", sender)])
    def review(document, name, candidate):
        reviewed.append((name, candidate.reason))
        return candidate
    # The empty place after "Signature:", in the section whose "Name:" is the signer, needs no model to be found.
    assert process_one(inbox, client, (signer,), review)
    assert len(sent) == 1 and reviewed == []


def test_worker_does_not_sign_rejected_contextual_candidate(tmp_path, monkeypatch):
    sender = "5511999999999"
    signer = UnlockedSigner(SignerConfig("jane", "Jane Example", Path("certificate.p12"),
                                         allowed_senders=frozenset({sender})), b"password", b"fingerprint")
    sent, notices = [], []
    client = SimpleNamespace(download_document=lambda _: form(), send_pdf=lambda *args: sent.append(args),
                             send_text=lambda *args: notices.append(args))
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf", lambda *args: (_ for _ in ()).throw(AssertionError()))
    inbox = Inbox(tmp_path / "queue.db")
    inbox.add([("one", "media", "report.pdf", sender)])
    def unexpected_fallback(*_):
        raise AssertionError("A rejected candidate must not be overridden")
    assert process_one(inbox, client, (signer,), lambda *_: None, unexpected_fallback)
    assert not sent and len(notices) == 1


def test_worker_uses_model_proposal_when_no_contextual_candidate(tmp_path, monkeypatch):
    sender = "5511999999999"
    signer = UnlockedSigner(SignerConfig("jane", "Jane Example", Path("certificate.p12"),
                                         allowed_senders=frozenset({sender})), b"password", b"fingerprint")
    sent = []
    client = SimpleNamespace(download_document=lambda _: form(), send_pdf=lambda *args: sent.append(args),
                             send_text=lambda *args: None)
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf", lambda *args: b"signed")
    monkeypatch.setattr("sealwharf.application.worker.suggest_contextual_position", lambda *_: None)
    candidate = suggest_contextual_position(form(), "Jane Example")
    inbox = Inbox(tmp_path / "queue.db")
    inbox.add([("one", "media", "report.pdf", sender)])
    assert process_one(inbox, client, (signer,), lambda *_: None, lambda *_: candidate)
    assert len(sent) == 1
