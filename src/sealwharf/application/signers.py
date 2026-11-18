"""Open the configured certificates once and watch their expiry dates."""

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ..adapters.audit import operational_logger
from ..adapters.inbox import Inbox
from ..documents.names import holds_name, name_words
from ..documents.signing import (
    CertificateRejected,
    TokenPinRejected,
    TokenSettings,
    check_validity_period,
    read_signing_certificate,
)
from ..messages import DEFAULT_LANGUAGE, text
from ..signer_config import SignerConfig

EXPIRY_WARNING_DAYS = 30


@dataclass(frozen=True)
class UnlockedSigner:
    config: SignerConfig
    password: bytes = field(repr=False)
    certificate_fingerprint: bytes
    expires_at: datetime | None = None


words = name_words


def token_settings(signer: SignerConfig) -> TokenSettings | None:
    return TokenSettings(signer.token_module, signer.token_label) if signer.token_module is not None else None


def pin_guard(data_dir: Path, identifier: str) -> Path:
    """The file whose presence forbids another login attempt on a token."""
    return data_dir / f"token-pin-rejected-{identifier}"


def _is_subsequence(part: tuple[str, ...], whole: tuple[str, ...]) -> bool:
    remaining = iter(whole)
    return all(word in remaining for word in part)


def unlock_signers(signers: tuple[SignerConfig, ...], *, data_dir: Path | None = None) -> tuple[UnlockedSigner, ...]:
    """Open each certificate with its password and verify it matches the configuration.

    A token that refuses its PIN is never tried again until the guard file is
    removed, because a few wrong attempts lock the device for good.
    """
    unlocked = []
    fingerprints = set()
    for signer in signers:
        if signer.password is None:
            raise ValueError(f"No password is configured for certificate {signer.id}.")
        password = signer.password.encode()
        guard = pin_guard(data_dir, signer.id) if data_dir is not None and signer.token_module is not None else None
        if guard is not None and guard.exists():
            raise ValueError(f"The token of {signer.id} refused its PIN earlier and was not tried again. "
                             "Correct the PIN in the configuration, then run the token-retry command.")
        try:
            certificate = read_signing_certificate(signer.certificate, password, token_settings(signer), holder=signer.name)
        except TokenPinRejected:
            if guard is not None:
                guard.parent.mkdir(parents=True, exist_ok=True)
                guard.touch()
            raise ValueError(f"The token of {signer.id} refused its PIN. It will not be tried again until you run "
                             "the token-retry command.") from None
        except Exception:
            raise ValueError(f"Could not open certificate {signer.id}. Check its password"
                             + (" and that the token is connected." if signer.token_module else ".")) from None
        common_name = certificate.subject.native.get("common_name")
        if not isinstance(common_name, str):
            raise ValueError(f"Certificate {signer.id} has no supported common name.")
        subject = words(common_name)
        if not holds_name(subject, words(signer.name)):
            raise ValueError(f"The configured name does not match certificate {signer.id}.")
        if not all(_is_subsequence(words(alias), subject) for alias in signer.aliases):
            raise ValueError(f"Every alias of {signer.id} must use only names from its certificate, in the same order.")
        try:
            check_validity_period(certificate)
        except CertificateRejected as exc:
            raise ValueError(f"Certificate {signer.id} is expired or not yet valid.") from exc
        fingerprint = hashlib.sha256(certificate.dump()).digest()
        if fingerprint in fingerprints:
            raise ValueError("The same certificate cannot be configured more than once.")
        fingerprints.add(fingerprint)
        unlocked.append(UnlockedSigner(signer, password, fingerprint, certificate.not_valid_after))
    return tuple(unlocked)


def warn_expiring(inbox: Inbox, client, signers: tuple[UnlockedSigner, ...], languages: dict[str, str] | None = None,
                  *, now: datetime | None = None) -> None:
    """Tell each owner, once a day, that a certificate expires within thirty days."""
    moment = now or datetime.now(UTC)
    for signer in signers:
        owner = signer.config.owner
        if signer.expires_at is None or owner is None:
            continue
        days = (signer.expires_at - moment).days
        mark = f"expiry:{signer.config.id}:{moment.date().isoformat()}"
        if days > EXPIRY_WARNING_DAYS or inbox.get_mark(mark) is not None:
            continue
        try:
            client.send_text(owner, text((languages or {}).get(owner, DEFAULT_LANGUAGE), "expiry_warning",
                                         certificate=signer.config.id, date=signer.expires_at.date().isoformat(), days=max(days, 0)))
            inbox.set_mark(mark)
        except Exception:
            operational_logger().info("expiry warning failed certificate=%s", signer.config.id)
