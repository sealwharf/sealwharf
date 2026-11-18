from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from asn1crypto import keys as asn1_keys
from asn1crypto import x509 as asn1_x509
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.timestamps import DummyTimeStamper
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko_certvalidator import ValidationContext
from pypdf import PdfWriter

from sealwharf.adapters.inbox import Inbox
from sealwharf.application.signers import UnlockedSigner, unlock_signers
from sealwharf.application.worker import process_one
from sealwharf.documents import signing
from sealwharf.documents.signing import CertificateRejected, SignaturePosition, SigningError, TrustSettings, sign_pdf
from sealwharf.signer_config import SignerConfig, load_config

PASSWORD = b"test-password"
POSITION = SignaturePosition(0, (20, 20, 200, 80))


def _name(common_name):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _authority(common_name="Test Root"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(_name(common_name)).issuer_name(_name(common_name))
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2)).not_valid_after(now + timedelta(days=30))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=False, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=True, crl_sign=True,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


def _leaf(authority, tmp_path, *, common_name="Jane Example", expires_in=timedelta(days=1), filename="leaf.p12"):
    authority_key, authority_cert = authority
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(_name(common_name)).issuer_name(authority_cert.subject)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=2)).not_valid_after(now + expires_in)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, content_commitment=True, key_encipherment=False,
                                         data_encipherment=False, key_agreement=False, key_cert_sign=False, crl_sign=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(authority_key.public_key()), critical=False)
            .sign(authority_key, hashes.SHA256()))
    path = tmp_path / filename
    path.write_bytes(pkcs12.serialize_key_and_certificates(b"leaf", key, cert, None, serialization.BestAvailableEncryption(PASSWORD)))
    return path, cert


def _revocation_list(authority, revoked=()):
    authority_key, authority_cert = authority
    now = datetime.now(UTC)
    builder = (x509.CertificateRevocationListBuilder().issuer_name(authority_cert.subject)
               .last_update(now - timedelta(hours=1)).next_update(now + timedelta(days=1))
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(authority_key.public_key()), critical=False)
               .add_extension(x509.CRLNumber(1), critical=False))
    for cert in revoked:
        builder = builder.add_revoked_certificate(x509.RevokedCertificateBuilder().serial_number(cert.serial_number)
                                                  .revocation_date(now - timedelta(hours=2)).build())
    return builder.sign(authority_key, hashes.SHA256()).public_bytes(serialization.Encoding.DER)


def _offline_context(monkeypatch, authority, revocation_list):
    root = asn1_x509.Certificate.load(authority[1].public_bytes(serialization.Encoding.DER))
    monkeypatch.setattr(signing, "_chain_context", lambda _: ValidationContext(
        trust_roots=[root], allow_fetching=False, crls=[revocation_list], revocation_mode="require"))


@pytest.fixture
def document():
    writer = PdfWriter()
    writer.add_blank_page(width=400, height=600)
    out = BytesIO()
    writer.write(out)
    return out.getvalue()


TRUSTED = TrustSettings(trust_roots=(Path("root.pem"),))


def test_trusted_unrevoked_chain_signs_and_embeds_validation_evidence(document, tmp_path, monkeypatch):
    authority = _authority()
    leaf, _ = _leaf(authority, tmp_path)
    _offline_context(monkeypatch, authority, _revocation_list(authority))
    signed = sign_pdf(document, leaf, PASSWORD, POSITION, trust=TRUSTED)
    reader = PdfFileReader(BytesIO(signed))
    assert len(reader.embedded_signatures) == 1
    assert "/DSS" in reader.root


def test_revoked_certificate_is_refused(document, tmp_path, monkeypatch):
    authority = _authority()
    leaf, cert = _leaf(authority, tmp_path)
    _offline_context(monkeypatch, authority, _revocation_list(authority, [cert]))
    with pytest.raises(CertificateRejected, match="revoked"):
        sign_pdf(document, leaf, PASSWORD, POSITION, trust=TRUSTED)


def test_untrusted_chain_or_missing_revocation_data_is_refused(document, tmp_path, monkeypatch):
    authority, stranger = _authority(), _authority("Other Root")
    leaf, _ = _leaf(authority, tmp_path)
    _offline_context(monkeypatch, stranger, _revocation_list(stranger))
    with pytest.raises(CertificateRejected):
        sign_pdf(document, leaf, PASSWORD, POSITION, trust=TRUSTED)
    root = asn1_x509.Certificate.load(authority[1].public_bytes(serialization.Encoding.DER))
    monkeypatch.setattr(signing, "_chain_context", lambda _: ValidationContext(
        trust_roots=[root], allow_fetching=False, crls=[_revocation_list(stranger)], revocation_mode="require"))
    with pytest.raises(CertificateRejected):
        sign_pdf(document, leaf, PASSWORD, POSITION, trust=TRUSTED)


def test_expired_certificate_is_refused_even_without_trust_settings(document, tmp_path):
    leaf, _ = _leaf(_authority(), tmp_path, expires_in=timedelta(days=-1))
    with pytest.raises(CertificateRejected):
        sign_pdf(document, leaf, PASSWORD, POSITION)
    config = SignerConfig("jane", "Jane Example", leaf, allowed_senders=frozenset({"5511999999999"}), password=PASSWORD.decode())
    with pytest.raises(ValueError, match="expired"):
        unlock_signers((config,))


def test_timestamp_is_embedded_and_required(document, tmp_path, monkeypatch):
    authority = _authority()
    leaf, _ = _leaf(authority, tmp_path)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(UTC)
    tsa = (x509.CertificateBuilder().subject_name(_name("Test TSA")).issuer_name(authority[1].subject)
           .public_key(key.public_key()).serial_number(x509.random_serial_number())
           .not_valid_before(now - timedelta(days=1)).not_valid_after(now + timedelta(days=1))
           .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.TIME_STAMPING]), critical=True)
           .sign(authority[0], hashes.SHA256()))
    stamper = DummyTimeStamper(
        asn1_x509.Certificate.load(tsa.public_bytes(serialization.Encoding.DER)),
        asn1_keys.PrivateKeyInfo.load(key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8,
                                                        serialization.NoEncryption())))
    requested = []
    monkeypatch.setattr(signing, "_timestamper", lambda url: requested.append(url) or stamper)
    signed = sign_pdf(document, leaf, PASSWORD, POSITION, trust=TrustSettings(timestamp_url="http://tsa.test/stamp"))
    assert requested == ["http://tsa.test/stamp"]
    signature = PdfFileReader(BytesIO(signed)).embedded_signatures[0]
    status = validate_pdf_signature(signature, signer_validation_context=ValidationContext(
        trust_roots=[signature.signer_cert], allow_fetching=False))
    assert status.timestamp_validity is not None and status.timestamp_validity.intact

    def unavailable(_):
        raise OSError("offline")
    monkeypatch.setattr(signing, "_timestamper", unavailable)
    with pytest.raises(SigningError):
        sign_pdf(document, leaf, PASSWORD, POSITION, trust=TrustSettings(timestamp_url="http://tsa.test/stamp"))


def test_worker_reports_rejected_certificate_without_sending_a_document(tmp_path, monkeypatch):
    sender = "5511999999999"
    config = SignerConfig("jane", "Jane Example", Path("cert"), allowed_senders=frozenset({sender}),
                          timestamp_url="http://tsa.test/stamp", trust_roots=(Path("root.pem"),))
    inbox = Inbox(tmp_path / "queue.db")
    inbox.add([("message", "media", "report.pdf", sender)])
    notices, received = [], []

    def refuse(*args):
        received.append(args[7])
        raise CertificateRejected("revoked")
    client = SimpleNamespace(download_document=lambda _: b"pdf", send_text=lambda *args: notices.append(args),
                             send_pdf=lambda *_: pytest.fail("A document must not be sent"))
    monkeypatch.setattr("sealwharf.application.worker.validate_pdf", lambda *_: None)
    monkeypatch.setattr("sealwharf.application.worker.find_signature_position",
                        lambda *_: SimpleNamespace(page=0, box=(1, 2, 30, 40), field_name="Jane Example"))
    monkeypatch.setattr("sealwharf.application.worker.sign_pdf", refuse)
    assert process_one(inbox, client, (UnlockedSigner(config, PASSWORD, b"fingerprint"),))
    assert received == [TrustSettings("http://tsa.test/stamp", (Path("root.pem"),))]
    assert len(notices) == 1 and "expired, revoked" in notices[0][1]
    with inbox.connect() as db:
        assert db.execute("SELECT state FROM jobs").fetchone()[0] == "failed"


def test_signer_configuration_validates_trust_settings(tmp_path):
    (tmp_path / "jane.p12").write_bytes(b"placeholder")
    (tmp_path / "root.pem").write_bytes(b"placeholder")
    path = tmp_path / "sealwharf.yaml"

    def write(extra):
        path.write_text('contacts:\n  jane:\n    number: "5511999999999"\n'
                        'certificates:\n  - id: jane\n    name: Jane Example\n    file: jane.p12\n'
                        '    password: \"x\"\n    owner: jane\n' + extra, encoding="utf-8")
    write('    timestamp_url: "https://tsa.example/stamp"\n    trust_roots: ["root.pem"]\n')
    loaded = load_config(path).signers[0]
    assert loaded.timestamp_url == "https://tsa.example/stamp"
    assert loaded.trust_roots == ((tmp_path / "root.pem").resolve(),)
    write("")
    assert load_config(path).signers[0].trust_roots == () and load_config(path).signers[0].timestamp_url is None
    for extra in ['    timestamp_url: "ftp://tsa.example"\n', '    timestamp_url: "https://user:secret@tsa.example/"\n',
                  '    trust_roots: ["missing.pem"]\n', '    trust_roots: "root.pem"\n', '    trust_roots: ["jane.p12"]\n']:
        write(extra)
        with pytest.raises(ValueError):
            load_config(path)
