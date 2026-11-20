"""Record of every signature: who asked, who approved, which hashes.

The record identifies people only by the numbers and certificate aliases
already present in the configuration. It holds the name of each file that was
signed, so that people recognize their documents in their own log, and never
the content of a document. Entries are only ever added, and they are erased
when they grow older than the retention period.
"""

import json
import logging
import os
import re
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_PERIOD = "7d"


def period_hours(written: str) -> int | None:
    """Read how far back to look, as in `12h` or `7d`; None when it is not such a period."""
    match = re.fullmatch(r"([1-9]\d{0,3})([hd])", written.strip().casefold())
    return int(match[1]) * (24 if match[2] == "d" else 1) if match else None

ALLOWED = frozenset({"event", "request_id", "certificate_id", "certificate_sha256", "requester", "confirmed_by",
                     "approval", "document_sha256", "signed_sha256", "filename"})


class AuditLog:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)

    def record(self, **fields: str | None) -> None:
        if set(fields) - ALLOWED:
            raise ValueError("Unexpected audit field.")
        line = json.dumps({"time": datetime.now(UTC).isoformat(timespec="seconds"), **fields}, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(line + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def entries(self, since: datetime | None = None) -> list[dict]:
        """Return the entries, oldest first; with `since`, only those made from that moment on."""
        if not self.path.is_file():
            return []
        entries = [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]
        return entries if since is None else [entry for entry in entries if datetime.fromisoformat(entry["time"]) >= since]

    def prune(self, keep_days: int, *, now: datetime | None = None) -> int:
        """Erase the entries older than the retention period; return how many were erased."""
        everything = self.entries()
        kept = self.entries((now or datetime.now(UTC)) - timedelta(days=keep_days))
        if len(kept) < len(everything):
            temporary = self.path.with_suffix(".tmp")
            temporary.write_text("".join(json.dumps(entry, sort_keys=True) + "\n" for entry in kept), encoding="utf-8", newline="\n")
            os.replace(temporary, self.path)
        return len(everything) - len(kept)


def operational_logger() -> logging.Logger:
    """Log events to standard output for `docker logs`, without personal data.

    Callers pass only event names, internal request IDs and certificate
    aliases; never phone numbers, names, file names or document content.
    """
    logger = logging.getLogger("sealwharf")
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s", "%Y-%m-%dT%H:%M:%S%z"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False
    return logger
