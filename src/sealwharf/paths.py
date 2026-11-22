"""Locations inside the container, overridable for development and tests."""

import os
import secrets
from pathlib import Path


def config_path() -> Path:
    return Path(os.environ.get("SEALWHARF_CONFIG", "/config/sealwharf.yaml"))


def data_dir() -> Path:
    return Path(os.environ.get("SEALWHARF_DATA", "/data"))


def webhook_secret(*, create: bool = False) -> str:
    """Return the secret that proves a webhook call comes from our own channel settings.

    Nobody has to choose it: the bot creates it once in the data folder.
    """
    path = data_dir() / "webhook_secret"
    if create and not path.is_file():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        path.chmod(0o600)
    return path.read_text(encoding="utf-8").strip()
