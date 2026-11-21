"""Report on every signature embedded in a PDF.

The check is cryptographic: each signature must be intact, valid for its
certificate, and followed only by changes a later signature is allowed to
make. It says nothing about whether a certificate authority is trusted.
"""

from dataclasses import dataclass
from datetime import datetime
from io import BytesIO

from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko_certvalidator import ValidationContext

from ..messages import DEFAULT_LANGUAGE, text
from .signing import ACROBAT_POLICY
from .validation import validate_pdf

MAX_SIGNATURES = 50


@dataclass(frozen=True)
class SignatureReport:
    name: str | None  # None when the signer could not be read
    valid: bool
    signed_at: datetime | None = None
    timestamped: bool = False
    expired: bool = False


def verify_signatures(document: bytes) -> list[SignatureReport]:
    """Validate all signatures; raise DocumentError for an unreadable PDF."""
    validate_pdf(document)
    signatures = PdfFileReader(BytesIO(document), strict=not PdfFileReader(BytesIO(document), strict=False).xrefs.hybrid_xrefs_present).embedded_signatures
    if len(signatures) > MAX_SIGNATURES:
        raise ValueError("Too many signatures to verify.")
    reports = []
    for signature in signatures:
        try:
            certificate = signature.signer_cert
            name = certificate.subject.native.get("common_name")
            context = ValidationContext(trust_roots=[certificate], allow_fetching=False)
            status = validate_pdf_signature(signature, signer_validation_context=context, diff_policy=ACROBAT_POLICY)
            changed = status.modification_level is not None and status.modification_level.name == "OTHER"
            signed_at = status.signer_reported_dt
            moment = signed_at or datetime.now(certificate.not_valid_after.tzinfo)
            stamp = status.timestamp_validity
            reports.append(SignatureReport(
                name if isinstance(name, str) else None,
                bool(status.intact and status.valid and not changed), signed_at,
                stamp is not None and bool(stamp.intact and stamp.valid),
                not certificate.not_valid_before <= moment <= certificate.not_valid_after))
        except Exception:
            reports.append(SignatureReport(None, False))
    return reports


def describe(reports: list[SignatureReport], language: str = DEFAULT_LANGUAGE) -> str:
    """Summarize the reports as a short chat message."""
    if not reports:
        return text(language, "verify_none")
    lines = [text(language, "verify_found", count=len(reports))]
    for index, report in enumerate(reports, start=1):
        details = [text(language, "verify_valid" if report.valid else "verify_invalid")]
        if report.signed_at is not None:
            details.append(text(language, "verify_signed_at", moment=report.signed_at.strftime("%Y-%m-%d %H:%M %Z").strip()))
        if report.timestamped:
            details.append(text(language, "verify_timestamped"))
        if report.expired:
            details.append(text(language, "verify_expired"))
        lines.append(f"{index}. {report.name or text(language, 'verify_unknown')}: " + ", ".join(details))
    lines.append(text(language, "verify_all_valid" if all(report.valid for report in reports) else "verify_some_invalid"))
    lines.append(text(language, "verify_scope"))
    return "\n".join(lines)
