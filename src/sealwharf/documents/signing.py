"""Sign PDFs locally with a PKCS#12 certificate or a PKCS#11 token.

Coordinates are PDF points with the origin at the bottom left of an unrotated
page. The caller must select and confirm the position before signing.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path, PurePosixPath
from uuid import uuid4

from pyhanko.keys import load_certs_from_pemder
from pyhanko.pdf_utils.incremental_writer import IncrementalPdfFileWriter
from pyhanko.pdf_utils.reader import PdfFileReader
from pyhanko.sign import fields, signers, timestamps
from pyhanko.sign.validation import validate_pdf_signature
from pyhanko_certvalidator import CertificateValidator, ValidationContext
from pyhanko_certvalidator.errors import RevokedError
from pypdf import PdfReader

from .appearance import signature_style
from .names import holds_name, name_words
from .position import _field_data


class SigningError(Exception):
    """Signing or verification error without sensitive data."""
    # In a sequence of signatures, which of the requests failed, when that is known.
    request_index: int | None = None


class CertificateRejected(SigningError):
    """The signing certificate is expired, revoked or not trusted."""


@dataclass(frozen=True)
class TrustSettings:
    """Optional trust services for a signer.

    A timestamp URL adds a signed timestamp from that authority. Trust roots
    require the certificate chain and its revocation status to validate against
    those roots, and embed the evidence in the PDF. Both need network access.
    """
    timestamp_url: str | None = None
    trust_roots: tuple[Path, ...] = ()


@dataclass(frozen=True)
class TokenSettings:
    """A PKCS#11 token or smart card that holds the private key.

    The module is the PKCS#11 library of the device vendor, as a path inside
    the container. The label selects one device when several are connected.
    """
    module: PurePosixPath
    label: str | None = None


class TokenPinRejected(SigningError):
    """The token refused the PIN. Trying again could lock the device."""


class TokenUnavailable(SigningError):
    """The token is not connected or could not be reached; no login was attempted."""


def _single_token_slot(module: str) -> int:
    """Return the index of the only slot that holds a token, without logging in."""
    import pkcs11
    occupied = [index for index, slot in enumerate(pkcs11.lib(module).get_slots())
                if slot.flags & pkcs11.SlotFlag.TOKEN_PRESENT]
    if len(occupied) != 1:
        raise SigningError("Expected exactly one connected token.")
    return occupied[0]


def _token_signer(session, hanko_pkcs11, holder: str | None, fingerprint: bytes | None):
    """Pick the one certificate on the token that belongs to the configured holder."""
    from asn1crypto import x509
    from pkcs11 import Attribute, ObjectClass
    matches = []
    for entry in session.get_objects({Attribute.CLASS: ObjectClass.CERTIFICATE}):
        certificate = x509.Certificate.load(bytes(entry[Attribute.VALUE]))
        if fingerprint is not None:
            chosen = hmac.compare_digest(hashlib.sha256(certificate.dump()).digest(), fingerprint)
        else:
            common_name = certificate.subject.native.get("common_name")
            chosen = isinstance(common_name, str) and holds_name(name_words(common_name), name_words(holder or ""))
        if chosen:
            matches.append((certificate, bytes(entry[Attribute.ID])))
    if len(matches) != 1:
        raise SigningError("The token does not hold exactly one certificate for the configured name.")
    certificate, key_id = matches[0]
    return hanko_pkcs11.PKCS11Signer(session, signing_cert=certificate, key_id=key_id)


@contextmanager
def open_signer(certificate: Path | None, password: bytes, token: TokenSettings | None = None, *,
                holder: str | None = None, fingerprint: bytes | None = None) -> Iterator[signers.Signer]:
    """Open the signing key from a PKCS#12 file or, with its PIN, from a token.

    A token login is attempted exactly once: wrong PINs lock the device, so a
    refusal is reported as TokenPinRejected and must never be retried blindly.
    """
    if token is None:
        signer = signers.SimpleSigner.load_pkcs12(str(certificate), passphrase=password)
        if signer is None:
            raise SigningError("Could not open certificate.")
        yield signer
        return
    try:
        from pkcs11 import exceptions as pkcs11_errors
        from pyhanko.sign import pkcs11 as hanko_pkcs11
    except ImportError as exc:
        raise SigningError("PKCS#11 support is not installed.") from exc
    criteria = hanko_pkcs11.TokenCriteria(label=token.label) if token.label else None
    try:
        # Vendor libraries expose several slots, so the occupied one is chosen before any login.
        slot = None if criteria else _single_token_slot(str(token.module))
    except Exception as exc:
        raise TokenUnavailable("Could not find exactly one connected token. Connect it, or set its label.") from exc
    try:
        session = hanko_pkcs11.open_pkcs11_session(str(token.module), slot_no=slot, token_criteria=criteria,
                                                   user_pin=password.decode())
    except (pkcs11_errors.PinIncorrect, pkcs11_errors.PinInvalid, pkcs11_errors.PinLenRange,
            pkcs11_errors.PinExpired, pkcs11_errors.PinLocked) as exc:
        raise TokenPinRejected("The token refused the PIN.") from exc
    except Exception as exc:
        raise TokenUnavailable("Could not reach the token. Check that it is connected.") from exc
    try:
        yield _token_signer(session, hanko_pkcs11, holder, fingerprint)
    finally:
        session.close()


def read_signing_certificate(certificate: Path | None, password: bytes, token: TokenSettings | None = None, *,
                             holder: str | None = None):
    """Prove the key opens with this password or PIN and return its certificate."""
    with open_signer(certificate, password, token, holder=holder) as signer:
        return signer.signing_cert


def _timestamper(url: str) -> timestamps.TimeStamper:
    return timestamps.HTTPTimeStamper(url, timeout=20)


def _chain_context(roots: tuple[Path, ...]) -> ValidationContext:
    anchors = list(load_certs_from_pemder([str(root) for root in roots]))
    if not anchors:
        raise SigningError("No trust roots could be loaded.")
    return ValidationContext(trust_roots=anchors, allow_fetching=True, revocation_mode="require")


def check_validity_period(certificate, *, now: datetime | None = None) -> None:
    """Refuse a certificate outside its validity period."""
    moment = now or datetime.now(UTC)
    if not certificate.not_valid_before <= moment <= certificate.not_valid_after:
        raise CertificateRejected("The certificate is expired or not yet valid.")


def _validate_chain(signer, context: ValidationContext) -> None:
    try:
        intermediates = list(signer.cert_registry) if signer.cert_registry is not None else []
        asyncio.run(CertificateValidator(signer.signing_cert, intermediates, context).async_validate_usage(set()))
    except RevokedError as exc:
        raise CertificateRejected("The certificate is revoked.") from exc
    except Exception as exc:
        raise CertificateRejected("The certificate chain or revocation status could not be validated.") from exc


@dataclass(frozen=True)
class SignaturePosition:
    page: int  # zero-based index
    box: tuple[float, float, float, float]  # left, bottom, right, top
    field_name: str | None = None


@dataclass(frozen=True)
class SigningRequest:
    certificate: Path | None
    password: bytes = field(repr=False)
    position: SignaturePosition
    logo: Path | None = None
    logo_scale: float = 1.0
    expected_certificate: bytes | None = None
    trust: TrustSettings = TrustSettings()
    token: TokenSettings | None = None


def sign_pdf_sequence(document: bytes, requests: tuple[SigningRequest, ...]) -> bytes:
    """Apply all requested signatures in memory and verify them in the final PDF."""
    if not requests:
        raise SigningError("At least one signature is required.")
    for index, request in enumerate(requests):
        first = request.position
        for other in requests[:index]:
            second = other.position
            if first.page == second.page and not (
                first.box[2] <= second.box[0] or second.box[2] <= first.box[0]
                or first.box[3] <= second.box[1] or second.box[3] <= first.box[1]
            ):
                raise SigningError("Signature areas overlap.")
    try:
        known = {signature.field_name for signature in PdfFileReader(BytesIO(document)).embedded_signatures}
        added = set()
        result = document
        for index, request in enumerate(requests):
            try:
                result = sign_pdf(result, request.certificate, request.password, request.position,
                                  request.logo, request.logo_scale, request.expected_certificate, request.trust, request.token)
            except SigningError as exc:
                # The caller tells people whose signature could not be made.
                exc.request_index = index
                raise
            signatures = PdfFileReader(BytesIO(result)).embedded_signatures
            names = {signature.field_name for signature in signatures}
            new = names - known
            if len(new) != 1 or not known.issubset(names):
                raise SigningError("Could not identify the new signature.")
            added.update(new)
            known = names
        final = PdfFileReader(BytesIO(result)).embedded_signatures
        selected = [signature for signature in final if signature.field_name in added]
        if len(selected) != len(requests):
            raise SigningError("One or more signatures are missing.")
        for signature in selected:
            context = ValidationContext(trust_roots=[signature.signer_cert], allow_fetching=False)
            status = validate_pdf_signature(signature, signer_validation_context=context)
            if not status.intact or not status.valid:
                raise SigningError("Cryptographic signature verification failed.")
        return result
    except SigningError:
        raise
    except Exception as exc:
        raise SigningError("Could not sign or verify the PDF.") from exc


def sign_pdf(document: bytes, certificate: Path | None, password: bytes, position: SignaturePosition,
             logo: Path | None = None, logo_scale: float = 1.0,
             expected_certificate: bytes | None = None, trust: TrustSettings = TrustSettings(),
             token: TokenSettings | None = None) -> bytes:
    """Sign a copy in memory and verify integrity and the cryptographic signature.

    The caller supplies the password, which is never stored here. An expired
    certificate is always refused. Chain trust, revocation and a timestamp are
    checked only when requested through the trust settings. Input files are
    never overwritten.
    """
    try:
        reader = PdfReader(BytesIO(document), strict=True)
        if reader.is_encrypted or not 0 <= position.page < len(reader.pages):
            raise SigningError("Encrypted PDF or invalid page.")
        page = reader.pages[position.page]
        if int(page.rotation or 0) % 360:
            raise SigningError("Rotated pages are not supported yet.")
        left, bottom, right, top = position.box
        if not all(
            float(bounds.left) <= left < right <= float(bounds.right)
            and float(bounds.bottom) <= bottom < top <= float(bounds.top)
            for bounds in (page.cropbox, page.mediabox)
        ):
            raise SigningError("Signature area is outside the page.")

        with open_signer(certificate, password, token, fingerprint=expected_certificate if token else None) as signer:
            if expected_certificate is not None and not hmac.compare_digest(
                hashlib.sha256(signer.signing_cert.dump()).digest(), expected_certificate
            ):
                raise SigningError("Certificate changed after startup.")
            check_validity_period(signer.signing_cert)
            chain_context = _chain_context(trust.trust_roots) if trust.trust_roots else None
            if chain_context is not None:
                _validate_chain(signer, chain_context)
            timestamper = _timestamper(trust.timestamp_url) if trust.timestamp_url else None
            field_name = position.field_name or f"Sealwharf_{uuid4().hex}"
            if position.field_name:
                matches = []
                for page_number, candidate_page in enumerate(reader.pages):
                    for reference in candidate_page.get("/Annots", []):
                        widget = reference.get_object()
                        name, field_type, filled = _field_data(widget)
                        if name == field_name and field_type == "/Sig" and not filled:
                            matches.append((page_number, tuple(float(value) for value in widget.get("/Rect", []))))
                if matches != [(position.page, tuple(position.box))]:
                    raise SigningError("Signature field is missing, ambiguous or incompatible.")
            writer = IncrementalPdfFileWriter(BytesIO(document))
            if chain_context is not None:
                metadata = signers.PdfSignatureMetadata(field_name=field_name, subfilter=fields.SigSeedSubFilter.PADES,
                                                        embed_validation_info=True, validation_context=chain_context)
            else:
                metadata = signers.PdfSignatureMetadata(field_name=field_name)
            output = BytesIO()
            signers.PdfSigner(
                metadata,
                signer=signer,
                timestamper=timestamper,
                stamp_style=signature_style(logo, logo_scale),
                new_field_spec=None if position.field_name else fields.SigFieldSpec(
                    sig_field_name=field_name, on_page=position.page, box=position.box
                ),
            ).sign_pdf(writer, output=output, existing_fields_only=bool(position.field_name))
            result = output.getvalue()
            signed_reader = PdfFileReader(BytesIO(result))
            signature = next(sig for sig in signed_reader.embedded_signatures if sig.field_name == field_name)
            context = ValidationContext(trust_roots=[signer.signing_cert], allow_fetching=False)
            status = validate_pdf_signature(signature, signer_validation_context=context)
            if not status.intact or not status.valid:
                raise SigningError("Cryptographic signature verification failed.")
            if timestamper is not None:
                stamp = status.timestamp_validity
                if stamp is None or not stamp.intact or not stamp.valid:
                    raise SigningError("The signature timestamp is missing or invalid.")
            return result
    except SigningError:
        raise
    except Exception as exc:
        raise SigningError("Could not sign or verify the PDF.") from exc
