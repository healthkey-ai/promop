"""Data-exchange integrity, non-repudiation, and version negotiation helpers.

Implements the content-integrity primitives required by PHR-S FM S.3.6#10 /
PH.2.3#09 (message integrity & non-repudiation) and the bounded multi-version
interchange declaration for TI.5.2#01 (issue #306).

Design goals:
- **Opt-in on import.** A request WITHOUT an integrity header keeps its current
  behavior. Only when a client asserts a digest do we verify it and reject a
  mismatch — so existing no-header clients never break.
- **Emit on export.** Every exported FHIR bundle carries a SHA-256 digest (and an
  optional HMAC signature) so recipients can verify content integrity without a
  round trip.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import re

from django.conf import settings

# Header a client MAY send to assert the SHA-256 of the request body / bundle
# bytes. Value is lowercase hex (64 chars).
CONTENT_DIGEST_HEADER = 'X-Content-SHA256'
# RFC 3230 style header: "Digest: sha-256=<base64>". Also honored on import.
RFC3230_DIGEST_HEADER = 'Digest'
# Response headers emitted on export.
EXPORT_DIGEST_HEADER = 'X-Content-SHA256'
EXPORT_SIGNATURE_HEADER = 'X-Content-Signature'

# FHIR interchange version this system speaks. Declaration/negotiation only — we
# do NOT transform to/from other versions (TI.5.2#01 is bounded to declaration).
SUPPORTED_FHIR_VERSION = '4.0.1'
# Tokens accepted as "R4" when a client negotiates a version.
_SUPPORTED_VERSION_TOKENS = {'4.0', '4.0.0', '4.0.1', 'r4'}

_META_HTTP_DIGEST = f"HTTP_{CONTENT_DIGEST_HEADER.upper().replace('-', '_')}"
_META_HTTP_RFC3230 = f"HTTP_{RFC3230_DIGEST_HEADER.upper().replace('-', '_')}"


def sha256_hex(data: bytes) -> str:
    """Return the lowercase hex SHA-256 digest of ``data``."""
    return hashlib.sha256(data).hexdigest()


def _export_key() -> bytes:
    return (getattr(settings, 'EXPORT_SIGNING_KEY', '') or settings.SECRET_KEY).encode()


def export_signature(data: bytes) -> str:
    """Return an HMAC-SHA256 signature (hex) over ``data`` for non-repudiation."""
    return hmac.new(_export_key(), data, hashlib.sha256).hexdigest()


def signature_valid(data: bytes, signature: str) -> bool:
    """Constant-time check of an HMAC signature produced by :func:`export_signature`."""
    if not signature:
        return False
    return hmac.compare_digest(signature, export_signature(data))


def _asserted_digest(request) -> str | None:
    """Extract the client-asserted SHA-256 (lowercase hex) from request headers.

    Supports both ``X-Content-SHA256: <hex>`` and RFC 3230
    ``Digest: sha-256=<base64>``. Returns None when no integrity header is
    present (opt-in: no header → no verification).
    """
    raw = request.META.get(_META_HTTP_DIGEST)
    if raw:
        return raw.strip().lower()

    rfc = request.META.get(_META_HTTP_RFC3230)
    if rfc:
        # Digest may carry multiple algorithms, comma-separated.
        for part in rfc.split(','):
            m = re.match(r'\s*sha-256\s*=\s*(.+)\s*$', part, re.IGNORECASE)
            if m:
                token = m.group(1).strip()
                # Accept either base64 (RFC 3230) or bare hex.
                if re.fullmatch(r'[0-9a-fA-F]{64}', token):
                    return token.lower()
                try:
                    return binascii.hexlify(base64.b64decode(token, validate=True)).decode()
                except (binascii.Error, ValueError):
                    return token.lower()
    return None


def verify_content_digest(request, raw_bytes: bytes) -> str | None:
    """Verify an optional client content digest against ``raw_bytes``.

    Returns:
        - ``None`` when no digest header is present (opt-in — current behavior), or
          when the asserted digest matches.
        - An error message string when a digest is asserted but does NOT match.
    """
    asserted = _asserted_digest(request)
    if asserted is None:
        return None
    actual = sha256_hex(raw_bytes)
    if not hmac.compare_digest(asserted, actual):
        return (
            'Content integrity check failed: asserted SHA-256 digest does not '
            'match the received payload.'
        )
    return None


def negotiated_fhir_version(request) -> str | None:
    """Return a client-requested FHIR version token, if any.

    Honors a ``fhirVersion`` query parameter and a ``fhirVersion`` parameter on
    the ``Accept`` header (e.g. ``application/fhir+json; fhirVersion=3.0``).
    Returns None when the client does not request a specific version.
    """
    requested = request.GET.get('fhirVersion') if hasattr(request, 'GET') else None
    if not requested:
        accept = request.META.get('HTTP_ACCEPT', '') or ''
        m = re.search(r'fhirVersion\s*=\s*([0-9A-Za-z.]+)', accept)
        if m:
            requested = m.group(1)
    return requested.strip() if requested else None


def is_supported_fhir_version(version: str) -> bool:
    return (version or '').strip().lower() in _SUPPORTED_VERSION_TOKENS


def unsupported_version_message(requested: str) -> str:
    return (
        f"Unsupported FHIR interchange version '{requested}'. This system supports "
        f"FHIR R4 ({SUPPORTED_FHIR_VERSION}) only."
    )


def check_fhir_version(request) -> str | None:
    """Return an error message if the client requests an unsupported FHIR version.

    Bounded multi-version interchange (TI.5.2#01): we declare and negotiate the
    supported version rather than transforming between versions. A request for
    anything other than R4 is rejected cleanly by the caller (HTTP 406).
    """
    requested = negotiated_fhir_version(request)
    if requested and not is_supported_fhir_version(requested):
        return unsupported_version_message(requested)
    return None
