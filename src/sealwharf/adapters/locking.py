"""An operating-system lock for a single worker per inbox database (POSIX, as in the container)."""

from __future__ import annotations

from pathlib import Path
from typing import BinaryIO


class WorkerAlreadyRunning(RuntimeError):
    """Another process already owns this inbox's worker lock."""


def worker_lock_path(database: Path) -> Path:
    """Return a stable lock path adjacent to the resolved database path."""
    resolved = database.resolve()
    return resolved.with_name(resolved.name + ".worker.lock")


class WorkerLock:
    """Hold an advisory process lock until explicitly released or process exit.

    The lock file is intentionally retained after release: deleting it would
    allow another process to lock a different inode while this one is active.
    """

    def __init__(self, path: Path):
        self.path = path.resolve()
        self._stream: BinaryIO | None = None

    @classmethod
    def for_database(cls, database: Path) -> WorkerLock:
        return cls(worker_lock_path(database))

    @property
    def held(self) -> bool:
        return self._stream is not None

    def acquire(self) -> None:
        if self.held:
            raise RuntimeError("Worker lock is already held by this instance.")
        import fcntl  # POSIX only; imported here so the package still loads on other systems

        self.path.parent.mkdir(parents=True, exist_ok=True)
        stream = self.path.open("a+b")
        try:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WorkerAlreadyRunning("A worker is already running for this inbox.") from exc
        except BaseException:
            stream.close()
            raise
        self._stream = stream

    def release(self) -> None:
        stream = self._stream
        if stream is None:
            return
        self._stream = None
        import fcntl

        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
        finally:
            stream.close()

    def __enter__(self) -> WorkerLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.release()
