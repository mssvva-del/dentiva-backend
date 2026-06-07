"""Self-contained svix / Standard Webhooks signature verification.

Clerk signs webhooks with the svix scheme (https://www.standardwebhooks.com).
We implement the verification inline instead of adding the `svix` package so the
dependency surface stays minimal and the check is fully testable offline.

The scheme (must match svix byte-for-byte or real webhooks get rejected):

  signing secret : "whsec_<base64>"  → base64-decode the part after the prefix
  signed content : f"{svix-id}.{svix-timestamp}.{raw_body}"
  signature      : base64( HMAC_SHA256(secret_bytes, signed_content) )
  header         : svix-signature = space-separated "v1,<sig>" tokens
                   (a message may carry several; ANY match passes — secret rotation)

We also enforce a timestamp tolerance to blunt replay attacks.

SECURITY: compare in constant time (hmac.compare_digest). Never log the secret
or the raw signatures.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

# Reject messages whose timestamp is more than this far from now (seconds).
# svix's own default is 5 minutes; we match it.
_TOLERANCE_SECONDS = 5 * 60


class SvixVerificationError(Exception):
    """Raised when an svix-signed payload fails verification."""


def _decode_secret(secret: str) -> bytes:
    """Turn a 'whsec_...' signing secret into the raw HMAC key bytes."""
    if secret.startswith("whsec_"):
        secret = secret[len("whsec_") :]
    # svix secrets are standard base64 (with padding).
    return base64.b64decode(secret)


def _check_timestamp(svix_timestamp: str, *, now: float | None = None) -> None:
    try:
        ts = int(svix_timestamp)
    except (TypeError, ValueError) as exc:
        raise SvixVerificationError("invalid svix-timestamp") from exc
    current = int(now if now is not None else time.time())
    if ts < current - _TOLERANCE_SECONDS:
        raise SvixVerificationError("message timestamp too old")
    if ts > current + _TOLERANCE_SECONDS:
        raise SvixVerificationError("message timestamp too new")


def verify(
    *,
    secret: str,
    raw_body: bytes,
    svix_id: str | None,
    svix_timestamp: str | None,
    svix_signature: str | None,
    now: float | None = None,
) -> None:
    """Verify a svix-signed webhook. Returns None on success, raises otherwise.

    `now` is injectable so tests can pin the clock (no Date.now flakiness).
    """
    if not (svix_id and svix_timestamp and svix_signature):
        raise SvixVerificationError("missing svix headers")

    _check_timestamp(svix_timestamp, now=now)

    secret_bytes = _decode_secret(secret)
    signed_content = b".".join(
        [svix_id.encode(), svix_timestamp.encode(), raw_body]
    )
    expected = base64.b64encode(
        hmac.new(secret_bytes, signed_content, hashlib.sha256).digest()
    ).decode()

    # The header is a space-separated list of "v<version>,<signature>" tokens.
    # We only support v1 (svix's current version). Constant-time compare each.
    for token in svix_signature.split(" "):
        _, _, sig = token.partition(",")
        if sig and hmac.compare_digest(sig, expected):
            return  # match found
    raise SvixVerificationError("no matching signature")
