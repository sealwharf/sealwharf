"""Append-only record of every signature: who asked, who approved, which hashes.

The record identifies people only by the numbers and certificate aliases
already present in the configuration. Document names, signer names and
document content are never written.
"""

import json
import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

ALLOWED = frozenset({"event", "request_id", "certificate_id", "certificate_sha256", "requester", "confirmed_by",
                     "approval", "document_sha256", "signed_sha256"})


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

    def entries(self) -> list[dict]:
        if not self.path.is_file():
            return []
        return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines() if line.strip()]


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
