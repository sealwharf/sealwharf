"""Nothing about a finished document is kept, and one command erases the rest."""
import sqlite3

from sealwharf.adapters.inbox import Inbox, Job
from sealwharf.application.worker import Runtime, forget_finished
from sealwharf.purge import erase_stored_data


def rows(inbox):
    with inbox.connect() as db:
        return db.execute("SELECT message_id, media_id, filename, body, digest, state FROM jobs ORDER BY rowid").fetchall()


def test_a_finished_document_leaves_no_name_and_no_copy(tmp_path):
    inbox, runtime = Inbox(tmp_path / "inbox.db"), Runtime(tmp_path)
    inbox.add([Job("done", "media-1", "contract.pdf", "5511900000001", "document", "verify"),
               Job("waiting", "media-2", "letter.pdf", "5511900000001"), Job("old", "media-3", "old.pdf", "5511900000001")])
    inbox.mark_document("done", "5511900000001", "a" * 64)
    inbox.set_state("done", "sent")
    inbox.set_state("waiting", "awaiting_confirmation")
    inbox.set_state("old", "failed")
    with inbox.connect() as db:
        db.execute("UPDATE jobs SET created_at=datetime('now', '-2 hours') WHERE message_id='old'")
    orphan, kept = runtime.pending_file("orphan"), runtime.pending_file("kept")
    orphan.parent.mkdir(parents=True)
    orphan.write_bytes(b"left behind")
    kept.write_bytes(b"still awaited")
    with inbox.connect() as db:
        db.execute("INSERT INTO requests VALUES ('kept', 'waiting', '5511900000001', 'letter.pdf', 'hash', 1, 0, NULL)")
    forget_finished(inbox, runtime)
    # The finished entry is bare, the old one is gone, and the one that waits for an answer is untouched.
    assert rows(inbox) == [("done", "", None, None, None, "sent"),
                           ("waiting", "media-2", "letter.pdf", None, None, "awaiting_confirmation")]
    assert not orphan.exists() and kept.read_bytes() == b"still awaited"
    # The bare entry still counts for the hourly limit.
    assert inbox.recent_documents("5511900000001", "waiting") == 2
    assert b"contract.pdf" not in (tmp_path / "inbox.db").read_bytes()


def test_purge_erases_everything_but_the_token_driver_and_guards(tmp_path):
    for name in ("inbox.db", "audit.jsonl", "webhook_secret", "stray.pdf", "token-pin-rejected-one", "inbox.db.worker.lock"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    for folder in ("pending", "backups", "pkcs11"):
        (tmp_path / folder).mkdir()
        (tmp_path / folder / "file").write_text("x", encoding="utf-8")
    assert erase_stored_data(tmp_path) == 6
    assert sorted(path.name for path in tmp_path.iterdir()) == ["inbox.db.worker.lock", "pkcs11", "token-pin-rejected-one"]
    assert (tmp_path / "pkcs11" / "file").exists()
    assert erase_stored_data(tmp_path) == 0 and erase_stored_data(tmp_path / "missing") == 0
    # The bot starts again from an empty queue.
    with sqlite3.connect(Inbox(tmp_path / "inbox.db").path) as db:
        assert db.execute("SELECT COUNT(*) FROM jobs").fetchone()[0] == 0
