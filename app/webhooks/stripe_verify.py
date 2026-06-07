"""Self-contained Stripe webhook signature verification.

Stripe signs webhooks with a scheme close to (but not identical to) svix. We
implement it inline rather than pulling the full `stripe` SDK just for signature
checking, keeping the dependency surface small and the check unit-testable.

Header: ``Stripe-Signature: t=<ts>,v1=<sig>[,v1=<sig2>...]``
signed_payload = f"{t}.{raw_body}"
expected       = HEX( HMAC_SHA256(whsec, signed_payload) )   # note: HEX, not base64
A message may carry several v1 signatures (key rotation) — ANY match passes.
We also enforce a timestamp tolerance to blunt replay.

SECURITY: constant-time compare; never log the secret or signatures.
"""

from __future__ import annotations

import hashlib
import hmac
import time

_TOLERANCE_SECONDS = 5 * 60  # Stripe's recommended default


class StripeVerificationError(Exception):
    """Raised when a Stripe-signed payload fails verification."""


def _parse_header(header: str) -> tuple[str | None, list[str]]:
    """Pull the timestamp (t=) and all v1= signatures from the header."""
    ts: str | None = None
    sigs: list[str] = []
    for part in header.split(","):
        key, _, value = part.strip().partition("=")
        if key == "t":
            ts = value
        elif key == "v1":
            sigs.append(value)
    return ts, sigs


def verify(
    *,
    secret: str,
    raw_body: bytes,
    signature_header: str | None,
    now: float | None = None,
) -> None:
    """Verify a Stripe webhook signature. None on success, raises otherwise.

    `now` is injectable so tests pin the clock (no real-time flakiness).
    """
    if not signature_header:
        raise StripeVerificationError("missing Stripe-Signature header")
    ts, sigs = _parse_header(signature_header)
    if not ts or not sigs:
        raise StripeVerificationError("malformed Stripe-Signature header")

    try:
        ts_int = int(ts)
    except ValueError as exc:
        raise StripeVerificationError("invalid timestamp") from exc
    current = int(now if now is not None else time.time())
    if abs(current - ts_int) > _TOLERANCE_SECONDS:
        raise StripeVerificationError("timestamp outside tolerance")

    signed_payload = f"{ts}.".encode() + raw_body
    expected = hmac.new(secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    for sig in sigs:
        if hmac.compare_digest(sig, expected):
            return
    raise StripeVerificationError("no matching signature")
