from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from sealwharf.adapters.inbox import Inbox
from sealwharf.app import create_app

SENDER = "5511999999999"
SECRET = "s" * 32


def event():
    return {"event": {"type": "messages", "event": "post"}, "messages": [{
        "id": "message-1", "from_me": False, "type": "document", "from": SENDER,
        "chat_id": f"{SENDER}@s.whatsapp.net", "document": {"id": "media-1", "file_name": "Report.docx"}}]}


def test_auth_dedup_and_rejections(tmp_path):
    path = tmp_path / "inbox.db"
    client = TestClient(create_app(secret=SECRET, sender=SENDER, db_path=path))
    assert client.post("/webhook", json=event()).status_code == 401
    headers = {"x-sealwharf-secret": SECRET}
    assert client.post("/webhook", json=event(), headers=headers).status_code == 200
    assert client.post("/webhook", json=event(), headers=headers).status_code == 200
    for field, value in (("from_me", True), ("from", "5511888888888"), ("chat_id", "123@g.us")):
        payload = event()
        payload["messages"][0][field] = value
        payload["messages"][0]["id"] = field
        assert client.post("/webhook", json=payload, headers=headers).status_code == 200
    with Inbox(path).connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
        assert db.execute("SELECT filename FROM jobs").fetchone()[0] == "Report.docx"


def test_concurrent_insertion(tmp_path):
    inbox = Inbox(tmp_path / "inbox.db")
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: inbox.add([("same", "media", "report.pdf", SENDER)]), range(30)))
    with inbox.connect() as db:
        assert db.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1


def test_limit(tmp_path):
    client = TestClient(create_app(secret=SECRET, sender=SENDER, db_path=tmp_path / "db"))
    assert client.post("/webhook", content=b"x" * (256 * 1024 + 1), headers={"x-sealwharf-secret": SECRET}).status_code == 413


def test_alternate_filename_and_missing_filename():
    from sealwharf.adapters.inbox import Job, accepted_events
    payload = event()
    payload["messages"][0]["document"] = {"id": "media-1", "filename": "contract.pdf"}
    assert accepted_events(payload, frozenset({SENDER})) == [Job("message-1", "media-1", "contract.pdf", SENDER)]
    payload["messages"][0]["document"] = {"id": "media-1"}
    assert accepted_events(payload, frozenset({SENDER})) == [Job("message-1", "media-1", None, SENDER)]


def test_multiple_authorized_senders_and_mismatched_identifiers(tmp_path):
    other = "5511888888888"
    client = TestClient(create_app(secret=SECRET, sender=frozenset({SENDER, other}), db_path=tmp_path / "inbox.db"))
    headers = {"x-sealwharf-secret": SECRET}
    first = event()
    second = event()
    second["messages"][0].update(id="message-2", **{"from": other, "chat_id": f"{other}@s.whatsapp.net"})
    bad = event()
    bad["messages"][0].update(id="bad", **{"from": SENDER, "chat_id": f"{other}@s.whatsapp.net"})
    unauthorized = event()
    unauthorized["messages"][0].update(id="unknown", **{"from": "5511777777777", "chat_id": "5511777777777@s.whatsapp.net"})
    for payload in (first, second, bad, unauthorized):
        assert client.post("/webhook", json=payload, headers=headers).status_code == 200
    with Inbox(tmp_path / "inbox.db").connect() as db:
        assert db.execute("SELECT message_id, sender FROM jobs ORDER BY message_id").fetchall() == [("message-1", SENDER), ("message-2", other)]


def test_sender_numbers_are_strict():
    import pytest

    from sealwharf.authorization import parse_senders
    assert parse_senders(SENDER) == frozenset({SENDER})
    assert parse_senders(f"{SENDER}, 5511888888888") == frozenset({SENDER, "5511888888888"})
    for value in ("", "*", f"{SENDER},", "+5511999999999", f"{SENDER},123@g.us"):
        with pytest.raises(ValueError):
            parse_senders(value)


def test_webhook_accepts_only_configured_numbers(tmp_path, monkeypatch):
    from sealwharf.app import from_environment
    (tmp_path / "certificate.p12").write_bytes(b"synthetic")
    config = tmp_path / "sealwharf.yaml"
    config.write_text('''contacts:
  jane:
    number: "5511999999999"
certificates:
  - id: one
    name: Jane Example
    file: certificate.p12
    password: "synthetic"
    owner: jane
''', encoding="utf-8")
    (tmp_path / "webhook_secret").write_text(SECRET + "\n", encoding="utf-8")
    monkeypatch.setenv("SEALWHARF_CONFIG", str(config))
    monkeypatch.setenv("SEALWHARF_DATA", str(tmp_path))
    client = TestClient(from_environment())
    headers = {"x-sealwharf-secret": SECRET}
    assert client.post("/webhook", json=event(), headers=headers).status_code == 200
    unauthorized = event()
    unauthorized["messages"][0].update(id="other", **{"from": "5511888888888", "chat_id": "5511888888888@s.whatsapp.net"})
    assert client.post("/webhook", json=unauthorized, headers=headers).status_code == 200
    with Inbox(tmp_path / "inbox.db").connect() as db:
        assert db.execute("SELECT message_id FROM jobs").fetchall() == [("message-1",)]
