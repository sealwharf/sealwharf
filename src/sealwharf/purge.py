"""Erase everything the bot has stored, for when nothing may be left behind.

The bot must be stopped first: the command takes the worker lock, so it never
erases the state of a bot that is running.
"""

import shutil
import sys
from pathlib import Path

from .adapters.locking import WorkerAlreadyRunning, WorkerLock
from .paths import data_dir

# What stays: the token driver, which is software, and the guards that forbid another login on a
# token that refused its PIN. Erasing a guard would allow the attempt that locks the device.
KEPT = ("pkcs11", "token-pin-rejected-*", "*.worker.lock")


def erase_stored_data(data: Path) -> int:
    """Remove the queue, the documents awaiting approval, the signature record and the configuration copies.

    Return how many files and folders were removed.
    """
    removed = 0
    for entry in sorted(data.iterdir()) if data.is_dir() else ():
        if any(entry.match(pattern) for pattern in KEPT):
            continue
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
        removed += 1
    return removed


def main() -> None:
    data = data_dir()
    lock = WorkerLock.for_database(data / "inbox.db")
    try:
        lock.acquire()
    except WorkerAlreadyRunning:
        sys.exit("The bot is running. Stop it first with: docker compose stop sealwharf")
    try:
        removed = erase_stored_data(data)
    finally:
        lock.release()
    print(f"Erased {removed} stored item(s): the queue, documents awaiting approval, the signature record "
          "and the configuration copies. The token driver and the token guards were kept.")


if __name__ == "__main__":
    main()
