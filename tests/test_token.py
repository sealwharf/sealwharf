"""Token signing with an emulated PKCS#11 device, aliases and owner-less certificates."""

import hashlib
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import PurePosixPath

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from pkcs11 import Attribute, ObjectClass
from pkcs11.exceptions import PinIncorrect
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign import pkcs11 as hanko_pkcs11
from pyhanko.sign.signers import SimpleSigner
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from sealwharf.adapters.inbox import Inbox
from sealwharf.application.signers import pin_guard, unlock_signers
from sealwharf.application.worker import process_one
from sealwharf.documents.signing import SignaturePosition, SigningError, TokenSettings, sign_pdf
from sealwharf.session import allow_token_retry
from sealwharf.signer_config import SignerConfig, load_config

PIN = "123456"
KEY_ID = b"\x01\x02"
SENDER = "5511900000001"
HOLDER = "John Michael Doe"


def certificate_for(common_name):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=1)).sign(key, hashes.SHA256()))
    return key, cert


class FakeSession:
    def __init__(self, certificates):
        self.certificates, self.closed = certificates, False

    def get_objects(self, attributes):
        assert attributes == {Attribute.CLASS: ObjectClass.CERTIFICATE}
        return [{Attribute.ID: identifier, Attribute.VALUE: value} for identifier, value in self.certificates]

    def close(self):
        self.closed = True


@pytest.fixture
def token(tmp_path, monkeypatch):
    """Emulate a token: the key stays behind the session; an authority certificate sits beside the holder's."""
    key, cert = certificate_for(HOLDER + ":00000000000")
    der = cert.public_bytes(serialization.Encoding.DER)
    authority = certificate_for("Example Authority")[1].public_bytes(serialization.Encoding.DER)
    hidden = tmp_path / "hidden.p12"
    hidden.write_bytes(pkcs12.serialize_key_and_certificates(b"john", key, cert, None, serialization.BestAvailableEncryption(PIN.encode())))
    state = {"logins": [], "sessions": [], "certificates": [(b"\x09", authority), (KEY_ID, der)]}

    def open_session(lib_location, slot_no=None, token_criteria=None, user_pin=None):
        # A label selects the token by itself; otherwise the occupied slot must have been chosen.
        assert (slot_no is None) == (token_criteria is not None) and slot_no in (None, 2)
        state["logins"].append((lib_location, token_criteria.label if token_criteria else None, user_pin))
        if user_pin != PIN:
            raise PinIncorrect()
        state["sessions"].append(FakeSession(state["certificates"]))
        return state["sessions"][-1]

    def signer(session, signing_cert, key_id):
        assert key_id == KEY_ID and signing_cert.dump() == der
        return SimpleSigner.load_pkcs12(str(hidden), passphrase=PIN.encode())
    def occupied_slot(module):
        state["slot_lookups"] = state.get("slot_lookups", 0) + 1
        return 2
    monkeypatch.setattr("sealwharf.documents.signing._single_token_slot", occupied_slot)
    monkeypatch.setattr(hanko_pkcs11, "open_pkcs11_session", open_session)
    monkeypatch.setattr(hanko_pkcs11, "PKCS11Signer", signer)
    config = SignerConfig("john", HOLDER, None, allowed_senders=frozenset({SENDER}), password=PIN,
                          aliases=("Michael Doe",), token_module=PurePosixPath("/usr/lib/vendor-pkcs11.so"), token_label="John token")
    return config, state, der


def page_with(*names):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b" ".join(f"100 {650 - i * 150} m 250 {650 - i * 150} l S BT /F1 10 Tf 110 {635 - i * 150} Td ({name}) Tj ET".encode()
                              for i, name in enumerate(names)))
    page[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


def test_token_certificate_is_found_by_holder_name_and_the_session_is_closed(token):
    config, state, der = token
    unlocked = unlock_signers((config,))[0]
    assert unlocked.certificate_fingerprint == hashlib.sha256(der).digest()
    signed = sign_pdf(page_with(HOLDER), None, PIN.encode(), SignaturePosition(0, (20, 20, 200, 80)),
                      expected_certificate=unlocked.certificate_fingerprint, token=TokenSettings(config.token_module, config.token_label))
    signatures = PdfFileReader(BytesIO(signed)).embedded_signatures
    assert len(signatures) == 1 and signatures[0].signer_cert.dump() == der
    assert state["logins"] == [("/usr/lib/vendor-pkcs11.so", "John token", PIN)] * 2
    assert all(session.closed for session in state["sessions"])


def test_token_without_exactly_one_matching_certificate_is_refused(token):
    config, state, der = token
    state["certificates"].append((b"\x03", certificate_for(HOLDER)[1].public_bytes(serialization.Encoding.DER)))
    with pytest.raises(ValueError, match="Could not open"):
        unlock_signers((config,))
    with pytest.raises(SigningError):
        sign_pdf(page_with(HOLDER), None, PIN.encode(), SignaturePosition(0, (20, 20, 200, 80)),
                 expected_certificate=b"\x00" * 32, token=TokenSettings(config.token_module))
    assert state["sessions"][-1].closed and state["slot_lookups"] == 1


def test_refused_pin_is_tried_once_and_never_again_until_released(token, tmp_path, monkeypatch):
    config, state, _ = token
    data = tmp_path / "data"
    with pytest.raises(ValueError, match="refused its PIN"):
        unlock_signers((replace(config, password="000000"),), data_dir=data)
    assert len(state["logins"]) == 1 and pin_guard(data, "john").exists()
    # Restarts, even with the right PIN, do not touch the device again.
    for _ in range(3):
        with pytest.raises(ValueError, match="was not tried again"):
            unlock_signers((config,), data_dir=data)
    assert len(state["logins"]) == 1
    monkeypatch.setenv("SEALWHARF_DATA", str(data))
    allow_token_retry()
    assert unlock_signers((config,), data_dir=data)[0].config == config and len(state["logins"]) == 2


def test_worker_signs_with_the_token_where_an_alias_is_written(token, tmp_path):
    config, state, der = token
    signers = unlock_signers((config,))
    inbox = Inbox(tmp_path / "queue.db")
    sent, texts = [], []

    class Chat:
        def download_document(self, media):
            return documents[media]

        def send_pdf(self, recipient, data, filename=None):
            sent.append(data)

        def send_text(self, recipient, text):
            texts.append(text)
    documents = {"alias": page_with("Michael Doe"), "both": page_with(HOLDER, "Michael Doe"), "other": page_with("Michael Smith")}
    inbox.add([("m1", "alias", "a.pdf", SENDER), ("m2", "both", "b.pdf", SENDER), ("m3", "other", "c.pdf", SENDER)])
    for _ in range(3):
        assert process_one(inbox, Chat(), signers)
    # One clear place signs; another person's name does not; the name written twice is asked about.
    assert len(sent) == 1 and len(texts) == 2
    assert "✅ Signed (1)\n• a.pdf" in texts[0] and "no clear place to sign (1)\n• c.pdf" in texts[0]
    assert texts[1].startswith("About b.pdf:") and "Shall I sign at the end of the last page?" in texts[1]
    assert PdfFileReader(BytesIO(sent[0])).embedded_signatures[0].signer_cert.dump() == der


def test_aliases_must_come_from_the_certificate_name(token):
    config, _, _ = token
    invented = SignerConfig("john", HOLDER, None, allowed_senders=frozenset({SENDER}), password=PIN,
                            aliases=("John Smith",), token_module=config.token_module)
    with pytest.raises(ValueError, match="alias"):
        unlock_signers((invented,))


def test_configuration_accepts_tokens_aliases_and_certificates_without_owner(tmp_path):
    path = tmp_path / "sealwharf.yaml"

    def write(body):
        path.write_text('contacts:\n  john:\n    number: "5511900000001"\n'
                        'certificates:\n  - id: john\n    name: John Michael Doe\n    password: "123456"\n' + body, encoding="utf-8")
    write('    aliases: ["Michael Doe"]\n    token:\n      module: /usr/lib/vendor-pkcs11.so\n      label: John token\n'
          '    trusted: [john]\n')
    signer = load_config(path).signers[0]
    assert (signer.certificate, signer.token_module, signer.token_label) == (None, PurePosixPath("/usr/lib/vendor-pkcs11.so"), "John token")
    assert signer.names == ("John Michael Doe", "Michael Doe") and signer.owner is None
    assert not signer.needs_confirmation("5511900000001")
    for body in ['    token:\n      module: relative.so\n    owner: john\n',
                 '    token:\n      module: /lib/a.so\n    file: a.p12\n    owner: john\n',
                 '    token:\n      module: /lib/a.so\n',
                 '    token:\n      module: /lib/a.so\n    preparers: [john]\n',
                 '    token:\n      module: /lib/a.so\n    owner: john\n    aliases: ["Doe"]\n']:
        write(body)
        with pytest.raises(ValueError):
            load_config(path)


def test_driver_package_is_unpacked_into_the_data_folder(tmp_path, monkeypatch):
    from zipfile import ZipFile

    from sealwharf.token_tools import install_driver, library_name
    assert library_name("libaetpkss.so.3.9.33.1") == "libaetpkss.so.3" and library_name("libexample.so") == "libexample.so"
    assert library_name("libexample.so.4") == "libexample.so.4" and library_name("readme.txt") is None
    monkeypatch.setenv("SEALWHARF_CONFIG", str(tmp_path / "config" / "sealwharf.yaml"))
    monkeypatch.setenv("SEALWHARF_DATA", str(tmp_path / "data"))
    lines = []
    assert install_driver(output=lines.append) == [] and "No driver package found" in lines[0]
    packages = tmp_path / "config" / "pkcs11"
    packages.mkdir(parents=True)
    with ZipFile(packages / "vendor.zip", "w") as archive:
        archive.writestr("Vendor Driver ub2204 x86_64.deb", b"package")
        archive.writestr("readme.txt", b"text")
    unpacked = []

    def unpack(package, target):
        unpacked.append(package.name)
        (target / "usr" / "lib").mkdir(parents=True)
        (target / "usr" / "lib" / "libvendorpk.so.3.9.1").write_bytes(b"library")
        (target / "usr" / "lib" / "libvendorhelper.so.4.2.9.1").write_bytes(b"helper")
        (target / "usr" / "share").mkdir()
        (target / "usr" / "share" / "notes.txt").write_bytes(b"notes")
    installed = install_driver(output=lines.append, unpack=unpack)
    assert unpacked == ["Vendor Driver ub2204 x86_64.deb"] and installed == ["libvendorhelper.so.4", "libvendorpk.so.3"]
    assert sorted(path.name for path in (tmp_path / "data" / "pkcs11").iterdir()) == installed
    assert "token-check" in lines[-1]


def test_token_check_names_the_library_and_the_holder_without_the_pin(tmp_path, monkeypatch):
    from sealwharf.token_tools import OPENSC, check_token
    monkeypatch.setenv("SEALWHARF_DATA", str(tmp_path))
    (tmp_path / "pkcs11").mkdir()
    for name in ("libvendorhelper.so.4", "libvendorpk.so.3"):
        (tmp_path / "pkcs11" / name).write_bytes(b"library")
    vendor = str(tmp_path / "pkcs11" / "libvendorpk.so.3")
    answers = {OPENSC: {"state": "not_recognized"}, vendor: {"state": "read", "holders": [{"name": "John Michael Doe", "days": 157}]}}
    lines = []
    assert check_token(output=lines.append, probe=lambda module: answers.get(module, {"state": "unusable"})) == vendor
    report = "\n".join(lines)
    assert f"module: {vendor}" in report and "Certificate holder: John Michael Doe (valid for 157 more day(s))" in report
    # A token nobody can read asks for the vendor driver; no token at all asks to connect it.
    lines.clear()
    assert check_token(output=lines.append, probe=lambda module: {"state": "not_recognized"}) is None and "token-driver" in lines[0]
    lines.clear()
    assert check_token(output=lines.append, probe=lambda module: {"state": "no_token"}) is None and "No token was found" in lines[0]
