"""Checking the configuration, health, backups and the webhook secret."""

import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from sealwharf.adapters.inbox import Inbox
from sealwharf.adapters.whapi import check_channel, read_messages
from sealwharf.app import create_app
from sealwharf.check import check
from sealwharf.healthcheck import healthy
from sealwharf.paths import webhook_secret
from sealwharf.session import backup_configuration

SECRET = "s" * 40


def write_certificate(path, common_name="John Doe", password=b"correct horse"):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(UTC)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=200)).sign(key, hashes.SHA256()))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(pkcs12.serialize_key_and_certificates(b"john", key, cert, None, serialization.BestAvailableEncryption(password)))


@pytest.fixture
def project(tmp_path, monkeypatch):
    """A config folder with one working certificate, and the environment pointing at it."""
    config = tmp_path / "config" / "sealwharf.yaml"
    write_certificate(config.parent / "certificates" / "john.p12")
    monkeypatch.setenv("SEALWHARF_CONFIG", str(config))
    monkeypatch.setenv("SEALWHARF_DATA", str(tmp_path / "data"))

    def write(*, password="correct horse", name="John Doe", head='public_url: https://bot.example.com\ngateway_token: "gateway-secret"\n'
                                                                  'tunnel_token: "tunnel-secret"\n'):
        config.write_text(head + 'contacts:\n  john:\n    name: John Doe\n    number: "5511900000001"\n'
                          f'certificates:\n  - id: john\n    name: {name}\n    file: certificates/john.p12\n'
                          f'    password: "{password}"\n    owner: john\n', encoding="utf-8")
    write()
    return write


def run_check(channel=lambda config: None, model=lambda: True):
    lines = []
    return check(channel=channel, model=model, output=lines.append), "\n".join(lines)


def test_check_confirms_a_working_configuration_without_printing_secrets(project):
    seen = []
    ready, report = run_check(lambda config: seen.append((config.gateway_type, config.gateway_token)))
    assert ready and seen == [("whapi", "gateway-secret")]
    assert "certificate john: opens and matches its name" in report and "WhatsApp gateway is connected" in report
    assert "AI model qwen3-vl:4b is ready" in report and report.endswith("Ready to start.")
    for secret in ("correct horse", "gateway-secret", "tunnel-secret"):
        assert secret not in report


def test_check_lists_every_problem_at_once(project):
    project(password="wrong", head="")

    def disconnected(token):
        raise AssertionError("No token, so the channel must not be contacted")
    ready, report = run_check(disconnected)
    assert not ready
    for expected in ("public_url is missing", "gateway_token is missing", "tunnel_token is missing", "Could not open certificate john"):
        assert expected in report
    assert report.endswith("4 problem(s) to fix in config/sealwharf.yaml.") and "wrong" not in report.replace("wrong PIN", "")


def test_check_reports_name_mismatch_channel_failure_and_unreadable_file(project):
    project(name="Jane Doe")

    def refused(config):
        raise ValueError("The gateway is not connected to WhatsApp. Link the WhatsApp account in the gateway.")
    ready, report = run_check(refused)
    assert not ready and "does not match certificate john" in report and "gateway is not connected" in report
    project(password='broken" quote')
    ready, report = run_check()
    assert not ready and report.startswith("PROBLEM  configuration:")


def test_check_requires_the_ai_model(project):
    ready, report = run_check(model=lambda: None)
    assert not ready and "the AI model service cannot be reached; start it with: docker compose up -d ollama" in report
    ready, report = run_check(model=lambda: False)
    assert not ready and "the AI model qwen3-vl:4b is not installed; download it with: docker compose up model" in report
    assert report.endswith("1 problem(s) to fix in config/sealwharf.yaml.")


def test_channel_check_accepts_only_a_connected_channel():
    def transport(body, status=200):
        return httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    check_channel("token", transport({"status": {"text": "AUTH"}}))
    with pytest.raises(ValueError, match="not connected"):
        check_channel("token", transport({"status": {"text": "QR"}}))
    for broken in (transport({}, 401), transport({"unexpected": True}), transport([])):
        with pytest.raises(ValueError, match="refused"):
            check_channel("token", broken)


def test_webhook_secret_is_created_once_in_the_data_folder(project, tmp_path):
    with pytest.raises(OSError):
        webhook_secret()
    first = webhook_secret(create=True)
    assert len(first) >= 32 and webhook_secret() == first == webhook_secret(create=True)
    assert (tmp_path / "data" / "webhook_secret").read_text(encoding="utf-8") == first


def test_readiness_follows_the_worker_heartbeat(tmp_path):
    database = tmp_path / "inbox.db"
    client = TestClient(create_app(read=read_messages, secret=SECRET, sender="5511900000001", db_path=database))
    assert client.get("/health").json() == {"status": "ok"}
    assert client.get("/ready").status_code == 503
    inbox = Inbox(database)
    inbox.set_mark("heartbeat")
    assert client.get("/ready").status_code == 200
    inbox.set_mark("heartbeat", time.time() - 600)
    assert client.get("/ready").status_code == 503

    class Reply:
        def __init__(self, status):
            self.status = status

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False
    assert healthy(opener=lambda url, timeout: Reply(200))
    assert not healthy(opener=lambda url, timeout: Reply(503))

    def refuse(url, timeout):
        raise OSError("down")
    assert not healthy(opener=refuse)


def test_configuration_backup_keeps_one_copy_per_version_and_no_secrets(project, tmp_path):
    source = tmp_path / "config" / "sealwharf.yaml"
    first = backup_configuration(source, tmp_path / "data")
    saved = first.read_text(encoding="utf-8")
    assert backup_configuration(source, tmp_path / "data") == first
    assert "id: john" in saved and "5511900000001" in saved and "owner: john" in saved and saved.count("REDACTED") == 3
    for secret in ("correct horse", "gateway-secret", "tunnel-secret"):
        assert secret not in saved
    # Changing only a secret is not a new version; changing a setting is.
    project(password="another")
    assert backup_configuration(source, tmp_path / "data") == first
    project(name="John M Doe")
    assert backup_configuration(source, tmp_path / "data") != first
    assert len(list((tmp_path / "data" / "backups").iterdir())) == 2
