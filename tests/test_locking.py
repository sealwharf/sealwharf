import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from sealwharf.adapters.inbox import Inbox
from sealwharf.adapters.locking import WorkerAlreadyRunning, WorkerLock

pytest.importorskip("fcntl", reason="The worker lock is POSIX only; run these tests in the container")


def _child_attempt(database: Path) -> str:
    code = """
import sys
from pathlib import Path
from sealwharf.adapters.locking import WorkerAlreadyRunning, WorkerLock
try:
    with WorkerLock.for_database(Path(sys.argv[1])):
        print('acquired')
except WorkerAlreadyRunning:
    print('blocked')
"""
    env = os.environ.copy()
    src = str(Path(__file__).resolve().parents[1] / "src")
    env["PYTHONPATH"] = src + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run([sys.executable, "-c", code, str(database)], env=env,
                            capture_output=True, text=True, timeout=15, check=True)
    return result.stdout.strip()


def test_only_one_process_can_hold_worker_lock(tmp_path):
    database = tmp_path / "inbox.db"
    lock = WorkerLock.for_database(database)
    with lock:
        assert lock.held
        assert _child_attempt(database) == "blocked"
        with pytest.raises(WorkerAlreadyRunning):
            WorkerLock.for_database(database).acquire()
    assert not lock.held
    assert _child_attempt(database) == "acquired"


def test_worker_crash_releases_os_lock(tmp_path):
    database = tmp_path / "inbox.db"
    ready = tmp_path / "ready"
    code = """
import sys, time
from pathlib import Path
from sealwharf.adapters.locking import WorkerLock
with WorkerLock.for_database(Path(sys.argv[1])):
    Path(sys.argv[2]).write_text('ready')
    time.sleep(30)
"""
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    process = subprocess.Popen([sys.executable, "-c", code, str(database), str(ready)],
                               env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            if ready.exists() or process.poll() is not None:
                break
            time.sleep(0.1)
        assert ready.exists(), "Child worker did not acquire its lock."
        with pytest.raises(WorkerAlreadyRunning):
            WorkerLock.for_database(database).acquire()
    finally:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=10)
    with WorkerLock.for_database(database):
        pass


def test_recovery_requires_matching_held_lock_and_is_idempotent(tmp_path):
    database = tmp_path / "inbox.db"
    inbox = Inbox(database)
    states = ["pending", "processing", "sending", "review_sending", "failure_sending", "sent", "needs_review", "failed", "uncertain"]
    for index, state in enumerate(states):
        inbox.add([(f"message-{index}", f"media-{index}", None, "5511999999999")])
        inbox.set_state(f"message-{index}", state)

    lock = WorkerLock.for_database(database)
    with pytest.raises(RuntimeError):
        inbox.recover_interrupted(lock)
    with WorkerLock.for_database(tmp_path / "other.db") as wrong_lock:
        with pytest.raises(RuntimeError):
            inbox.recover_interrupted(wrong_lock)
    with lock:
        assert inbox.recover_interrupted(lock) == (1, 3)
        assert inbox.recover_interrupted(lock) == (0, 0)

    with inbox.connect() as db:
        actual = [row[0] for row in db.execute("SELECT state FROM jobs ORDER BY rowid")]
    assert actual == ["pending", "pending", "uncertain", "uncertain", "uncertain", "sent", "needs_review", "failed", "uncertain"]
