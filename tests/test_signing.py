import hashlib
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from PIL import Image
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.fields import SigFieldSpec, append_signature_field
from pyhanko.sign.signers import SimpleSigner
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko_certvalidator import ValidationContext
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from sealwharf.adapters.inbox import Inbox
from sealwharf.application.signers import UnlockedSigner, unlock_signers
from sealwharf.application.worker import process_one
from sealwharf.documents.signing import BreaksSignature, SignaturePosition, SigningError, SigningRequest, sign_pdf, sign_pdf_sequence
from sealwharf.signer_config import SignerConfig


def unlocked(config, password):
    cert = SimpleSigner.load_pkcs12(str(config.certificate), passphrase=password).signing_cert
    return UnlockedSigner(config, password, hashlib.sha256(cert.dump()).digest())


@pytest.fixture
def certificate(tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Jos\u00e9 da Silva")])
    now = datetime.now(UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    path = tmp_path / "test.p12"
    path.write_bytes(pkcs12.serialize_key_and_certificates(
        b"test", key, cert, None, serialization.BestAvailableEncryption(b"test-password")
    ))
    return path


@pytest.fixture
def document():
    writer = PdfWriter()
    writer.add_blank_page(width=400, height=600)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


def test_signs_and_validates_cryptographically(document, certificate):
    signed = sign_pdf(document, certificate, b"test-password", SignaturePosition(0, (20, 20, 200, 80)))
    assert signed.startswith(document)
    reader = PdfFileReader(BytesIO(signed))
    assert len(reader.embedded_signatures) == 1
    signature = reader.embedded_signatures[0]
    context = ValidationContext(trust_roots=[signature.signer_cert], allow_fetching=False)
    status = validate_pdf_signature(signature, signer_validation_context=context)
    assert status.intact and status.valid


def test_signs_twice_and_validates_every_signature_in_final_document(document, certificate, tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Other Example")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
    other = tmp_path / "other.p12"
    other.write_bytes(pkcs12.serialize_key_and_certificates(
        b"other", key, cert, None, serialization.BestAvailableEncryption(b"test-password")))
    requests = (SigningRequest(certificate, b"test-password", SignaturePosition(0, (20, 20, 200, 80))),
                SigningRequest(other, b"test-password", SignaturePosition(0, (20, 100, 200, 160))))
    signed = sign_pdf_sequence(document, requests)
    signatures = PdfFileReader(BytesIO(signed)).embedded_signatures
    assert len(signatures) == 2
    assert signatures[0].signer_cert != signatures[1].signer_cert
    for signature in signatures:
        context = ValidationContext(trust_roots=[signature.signer_cert], allow_fetching=False)
        status = validate_pdf_signature(signature, signer_validation_context=context)
        assert status.intact and status.valid

    # Exercise the worker with real certificates and PDF fields, while the
    # WhatsApp client is replaced by an in-memory transport.
    pdf = IncrementalPdfFileWriter(BytesIO(document))
    append_signature_field(pdf, SigFieldSpec(sig_field_name="José da Silva", on_page=0, box=(20, 20, 200, 80)))
    append_signature_field(pdf, SigFieldSpec(sig_field_name="Other Example", on_page=0, box=(20, 100, 200, 160)))
    source = BytesIO()
    pdf.write(source)
    sender = "5511999999999"
    first_config = SignerConfig("first", "José da Silva", certificate, allowed_senders=frozenset({sender}))
    other_config = SignerConfig("other", "Other Example", other, allowed_senders=frozenset({sender}))
    signers = (unlocked(first_config, b"test-password"), unlocked(other_config, b"test-password"))
    inbox = Inbox(tmp_path / "multi-signature-queue.db")
    inbox.add([("message", "media", "form.pdf", sender)])
    sent, questions = [], []
    client = SimpleNamespace(download_document=lambda _: source.getvalue(),
                             send_pdf=lambda *args: sent.append(args),
                             send_text=lambda *args: questions.append(args))
    assert process_one(inbox, client, signers)
    assert not sent and len(questions) == 1 and "3 - All of them" in questions[0][1]
    inbox.add([("choice", "", None, sender, "text", "answer all")])
    assert process_one(inbox, client, signers)
    assert len(sent) == 1 and sent[0][0] == sender and len(questions) == 2 and "Signed by" in questions[1][1]
    signatures = PdfFileReader(BytesIO(sent[0][1])).embedded_signatures
    assert len(signatures) == 2
    for signature in signatures:
        status = validate_pdf_signature(signature, signer_validation_context=ValidationContext(
            trust_roots=[signature.signer_cert], allow_fetching=False))
        assert status.intact and status.valid


def test_multiple_signatures_reject_overlapping_areas(document, certificate):
    first = SigningRequest(certificate, b"test-password", SignaturePosition(0, (20, 20, 200, 80)))
    second = SigningRequest(certificate, b"test-password", SignaturePosition(0, (100, 30, 250, 90)))
    with pytest.raises(SigningError, match="overlap"):
        sign_pdf_sequence(document, (first, second))


def test_multiple_signatures_do_not_return_a_partially_signed_pdf(document, certificate):
    first = SigningRequest(certificate, b"test-password", SignaturePosition(0, (20, 20, 200, 80)))
    second = SigningRequest(certificate, b"incorrect", SignaturePosition(0, (20, 100, 200, 160)))
    with pytest.raises(SigningError):
        sign_pdf_sequence(document, (first, second))
    assert "test-password" not in repr(first)


def test_signature_appearance_contains_name_and_signing_details(document, certificate):
    signed = sign_pdf(document, certificate, b"test-password", SignaturePosition(0, (20, 20, 200, 80)))
    widget = PdfReader(BytesIO(signed)).pages[0]["/Annots"][0].get_object()
    appearance = widget["/AP"]["/N"].get_object().get_data()
    assert b"/NameFont" in appearance
    assert b"/DetailsFont" in appearance
    assert b"Digitally signed" in appearance
    assert b"Date" in appearance
    assert b"/BackgroundGS" in appearance
    assert "/XObject" in widget["/AP"]["/N"].get_object()["/Resources"]


def test_signs_with_custom_logo(document, certificate, tmp_path):
    logo = tmp_path / "logo.png"
    Image.new("RGBA", (12, 8), "red").save(logo)
    signed = sign_pdf(document, certificate, b"test-password", SignaturePosition(0, (20, 20, 200, 80)), logo)
    signature = PdfFileReader(BytesIO(signed)).embedded_signatures[0]
    status = validate_pdf_signature(signature, signer_validation_context=ValidationContext(trust_roots=[signature.signer_cert], allow_fetching=False))
    assert status.intact and status.valid
    appearance = PdfReader(BytesIO(signed)).pages[0]["/Annots"][0].get_object()["/AP"]["/N"].get_object()
    images = appearance["/Resources"]["/XObject"]
    assert any(image.get_object().get("/Width") == 12 for image in images.values())


def test_logo_scale_is_embedded_without_affecting_signature(document, certificate):
    signed = sign_pdf(document, certificate, b"test-password", SignaturePosition(0, (20, 20, 200, 80)), logo_scale=0.5)
    signature = PdfFileReader(BytesIO(signed)).embedded_signatures[0]
    status = validate_pdf_signature(signature, signer_validation_context=ValidationContext(trust_roots=[signature.signer_cert], allow_fetching=False))
    assert status.intact and status.valid
    appearance = PdfReader(BytesIO(signed)).pages[0]["/Annots"][0].get_object()["/AP"]["/N"].get_object().get_data()
    assert b"0.5 0 0 0.5" in appearance


@pytest.mark.parametrize("position", [
    SignaturePosition(1, (20, 20, 200, 80)),
    SignaturePosition(0, (-1, 20, 200, 80)),
    SignaturePosition(0, (20, 20, 200, 601)),
    SignaturePosition(0, (20, 20, 20, 80)),
])
def test_rejects_invalid_position(document, certificate, position):
    with pytest.raises(SigningError):
        sign_pdf(document, certificate, b"test-password", position)


def test_rejects_wrong_password(document, certificate):
    with pytest.raises(SigningError):
        sign_pdf(document, certificate, b"wrong-password", SignaturePosition(0, (20, 20, 200, 80)))


def test_rejects_certificate_replaced_after_unlock(document, certificate):
    with pytest.raises(SigningError, match="changed after startup"):
        sign_pdf(document, certificate, b"test-password", SignaturePosition(0, (20, 20, 200, 80)),
                 expected_certificate=b"\0" * 32)


def test_rejects_invalid_document(certificate):
    with pytest.raises(SigningError):
        sign_pdf(b"not a pdf", certificate, b"test-password", SignaturePosition(0, (20, 20, 200, 80)))


def test_never_invalidates_a_certification_that_allows_no_new_signature_fields(document, certificate):
    from pyhanko.sign import fields, signers
    certifier = SimpleSigner.load_pkcs12(str(certificate), passphrase=b"test-password")
    certified = BytesIO()
    signers.PdfSigner(signers.PdfSignatureMetadata(field_name="Certification", certify=True,
                                                   docmdp_permissions=fields.MDPPerm.FILL_FORMS),
                      signer=certifier, new_field_spec=SigFieldSpec("Certification", on_page=0, box=(220, 20, 380, 80))
                      ).sign_pdf(IncrementalPdfFileWriter(BytesIO(document)), output=certified)
    with pytest.raises(BreaksSignature):
        sign_pdf(certified.getvalue(), certificate, b"test-password", SignaturePosition(0, (20, 20, 200, 80)))
    # An ordinary signature already in the document stays valid, so another one is added.
    approved = sign_pdf(document, certificate, b"test-password", SignaturePosition(0, (220, 20, 380, 80)))
    assert len(PdfFileReader(BytesIO(sign_pdf(approved, certificate, b"test-password",
                                                SignaturePosition(0, (20, 20, 200, 80))))).embedded_signatures) == 2


def test_signs_existing_field_without_creating_another(document, certificate):
    writer = IncrementalPdfFileWriter(BytesIO(document))
    append_signature_field(writer, SigFieldSpec("Maria Silva", on_page=0, box=(20, 20, 200, 80)))
    output = BytesIO()
    writer.write(output)
    source = output.getvalue()
    signed = sign_pdf(source, certificate, b"test-password", SignaturePosition(0, (20, 20, 200, 80), "Maria Silva"))
    assert [sig.field_name for sig in PdfFileReader(BytesIO(signed)).embedded_signatures] == ["Maria Silva"]
    with pytest.raises(SigningError):
        sign_pdf(source, certificate, b"test-password", SignaturePosition(0, (20, 20, 201, 80), "Maria Silva"))


def test_unlock_rejects_certificate_with_wrong_identity(certificate):
    allowed = frozenset({"5511999999999"})
    correct = SignerConfig("one", "Jose da Silva", certificate, allowed_senders=allowed, password="test-password")
    wrong = SignerConfig("two", "Jane Example", certificate, allowed_senders=allowed, password="test-password")
    assert unlock_signers((correct,))[0].config == correct
    with pytest.raises(ValueError, match="does not match"):
        unlock_signers((wrong,))
    duplicate = SignerConfig("duplicate", "Jose da Silva", certificate, allowed_senders=allowed, password="test-password")
    with pytest.raises(ValueError, match="same certificate"):
        unlock_signers((correct, duplicate))


def test_overlapping_permissions_select_correct_certificate(document, certificate, tmp_path):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "John Example")])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1)).add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(key, hashes.SHA256()))
    john_path = tmp_path / "john.p12"
    john_path.write_bytes(pkcs12.serialize_key_and_certificates(b"john", key, cert, None, serialization.BestAvailableEncryption(b"john-password")))
    writer = IncrementalPdfFileWriter(BytesIO(document))
    append_signature_field(writer, SigFieldSpec("John Example", on_page=0, box=(20, 20, 200, 80)))
    source = BytesIO()
    writer.write(source)
    sender = "5511999999999"
    signers = (unlocked(SignerConfig("jose", "Jose da Silva", certificate, allowed_senders=frozenset({sender})), b"test-password"),
               unlocked(SignerConfig("john", "John Example", john_path, allowed_senders=frozenset({sender})), b"john-password"))
    class Client:
        sent = None
        def download_document(self, _): return source.getvalue()
        def send_pdf(self, _, data, filename): self.sent = data
        def send_text(self, *_): raise AssertionError("Unexpected failure notice")
    client = Client()
    inbox = Inbox(tmp_path / "queue.db")
    inbox.add([("message", "media", "report.pdf", sender)])
    assert process_one(inbox, client, signers)
    signature = PdfFileReader(BytesIO(client.sent)).embedded_signatures[0]
    assert signature.signer_cert.subject.native["common_name"] == "John Example"
    status = validate_pdf_signature(signature, signer_validation_context=ValidationContext(trust_roots=[signature.signer_cert], allow_fetching=False))
    assert status.intact and status.valid


def test_worker_signs_visual_line_with_real_crypto(certificate, tmp_path):
    writer = PdfWriter()
    page = writer.add_blank_page(width=400, height=600)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"100 200 m 250 200 l S BT /F1 10 Tf 140 185 Td (Jane Example) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = BytesIO()
    writer.write(buffer)
    class Client:
        sent = None
        notices = []
        def download_document(self, _):
            return buffer.getvalue()
        def send_pdf(self, _, document, filename):
            self.sent = document
        def send_text(self, _, text):
            self.notices.append(text)
    client = Client()
    inbox = Inbox(tmp_path / "queue.db")
    inbox.add([("message", "media", "report.pdf", "5511999999999")])
    assert process_one(inbox, client, (unlocked(SignerConfig("test", "Jane Example", certificate, allowed_senders=frozenset({"5511999999999"})), b"test-password"),))
    assert client.sent is not None
    signature = PdfFileReader(BytesIO(client.sent)).embedded_signatures[0]
    status = validate_pdf_signature(signature, signer_validation_context=ValidationContext(trust_roots=[signature.signer_cert], allow_fetching=False))
    assert status.intact and status.valid
    other = Client()
    inbox.add([("message-2", "media-2", "report.pdf", "5511999999999")])
    assert process_one(inbox, other, (unlocked(SignerConfig("test", "Another Person", certificate, allowed_senders=frozenset({"5511999999999"})), b"test-password"),))
    assert other.sent is None
    assert "clear place to sign" in other.notices[-1]
