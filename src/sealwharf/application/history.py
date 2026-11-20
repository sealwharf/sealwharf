"""Answer `bot log`: what a person may read from the record of signatures.

Nobody reads the whole record through the chat. A person sees an entry only
when they asked for that signature, approved it, or own the certificate that
made it.
"""

from datetime import UTC, datetime, timedelta

from ..adapters.audit import AuditLog
from ..messages import text
from .signers import UnlockedSigner

MAX_LINES = 30


def visible_entries(entries: list[dict], signers: tuple[UnlockedSigner, ...], number: str) -> list[dict]:
    """Keep the entries this number took part in, or that used a certificate it owns."""
    owned = {signer.config.id for signer in signers if signer.config.owner == number}
    return [entry for entry in entries
            if number in (entry.get("requester"), entry.get("confirmed_by")) or entry.get("certificate_id") in owned]


def log_report(audit: AuditLog, signers: tuple[UnlockedSigner, ...], number: str, language: str, called, hours: int,
               *, now: datetime | None = None) -> str:
    """Write the signatures of the last hours that this number may see, oldest first.

    `called` turns a number into the way that person is referred to in a message.
    """
    moment = now or datetime.now(UTC)
    entries = visible_entries(audit.entries(moment - timedelta(hours=hours)), signers, number)
    period = text(language, "log_hours", count=hours) if hours % 24 else text(language, "log_days", count=hours // 24)
    if not entries:
        return text(language, "log_none", period=period)
    names = {signer.config.id: signer.config.name for signer in signers}
    lines = [text(language, "log_header", period=period, count=len(entries))]
    for entry in entries[-MAX_LINES:]:
        approval = entry.get("approval")
        how = (text(language, "log_confirmed", owner=called(entry["confirmed_by"])) if approval == "confirmed"
               else text(language, {"owner": "log_own", "autotrust": "log_autotrust"}.get(approval, "log_trusted")))
        lines.append(text(language, "log_line", when=datetime.fromisoformat(entry["time"]).astimezone(UTC).strftime("%Y-%m-%d %H:%M"),
                          name=names.get(entry.get("certificate_id"), entry.get("certificate_id")),
                          requester=called(entry["requester"]), approval=how, fingerprint=(entry.get("signed_sha256") or "")[:8]))
    if len(entries) > MAX_LINES:
        lines.append(text(language, "log_more", shown=MAX_LINES))
    return "\n".join(lines)
