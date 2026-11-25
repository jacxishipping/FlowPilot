"""Pure-Python RFC 6238 TOTP (2FA) secret generation, QR otpauth URI, and verification."""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time
from urllib.parse import quote


def generate_totp_secret(num_bytes: int = 20) -> str:
    """Generate a Base32-encoded 160-bit TOTP shared secret (RFC 4226/6238)."""
    return base64.b32encode(secrets.token_bytes(num_bytes)).decode("ascii").rstrip("=")


def _normalize_b32(secret: str) -> bytes:
    cleaned = secret.strip().replace(" ", "").upper()
    pad = (-len(cleaned)) % 8
    return base64.b32decode(cleaned + ("=" * pad))


def generate_totp_code(
    secret: str,
    *,
    for_time: int | None = None,
    interval: int = 30,
    digits: int = 6,
) -> str:
    """Compute a 6-digit RFC 6238 HMAC-SHA1 TOTP code for the given Unix timestamp."""
    key = _normalize_b32(secret)
    ts = int(time.time()) if for_time is None else int(for_time)
    counter = ts // interval
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(code_int % (10**digits)).zfill(digits)


def verify_totp_code(
    secret: str,
    code: str,
    *,
    for_time: int | None = None,
    interval: int = 30,
    window: int = 1,
) -> bool:
    """Constant-time verify a TOTP code within +/- `window` time steps."""
    candidate = code.strip()
    if not candidate.isdigit():
        return False
    ts = int(time.time()) if for_time is None else int(for_time)
    for delta in range(-window, window + 1):
        expected = generate_totp_code(secret, for_time=ts + delta * interval, interval=interval)
        if hmac.compare_digest(expected, candidate):
            return True
    return False


def build_otpauth_uri(*, secret: str, account_email: str, issuer: str = "FlowPilot") -> str:
    """Build standard otpauth://totp/... URI for Google Authenticator / 1Password / Authy."""
    label = quote(f"{issuer}:{account_email}")
    return f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&digits=6&period=30"
