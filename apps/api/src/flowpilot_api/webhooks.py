"""HMAC-SHA256 webhook and Stripe billing signature verification with replay protection."""

from __future__ import annotations

import hashlib
import hmac
import time


def compute_webhook_signature(secret: str, timestamp: int, raw_body: bytes) -> str:
    """Compute hex HMAC-SHA256 over '<timestamp>.<raw_body>'."""
    signed_payload = f"{timestamp}.".encode() + raw_body
    return hmac.new(secret.encode("utf-8"), signed_payload, hashlib.sha256).hexdigest()


def verify_webhook_signature(
    *,
    secret: str,
    raw_body: bytes,
    signature_header: str,
    timestamp_header: str | None = None,
    tolerance_seconds: int = 300,
    now_ts: int | None = None,
) -> bool:
    """Verify FlowPilot webhook signature (`v1=<hex>` or `<hex>`) with replay window."""
    if not signature_header:
        return False

    # Support either "t=<ts>,v1=<sig>" (Stripe/standard format) or separate headers
    sig_hex = signature_header.strip()
    ts_val: int | None = None

    if "t=" in sig_hex and "v1=" in sig_hex:
        parts = dict(item.split("=", 1) for item in sig_hex.split(",") if "=" in item)
        try:
            ts_val = int(parts["t"].strip())
        except (KeyError, ValueError):
            return False
        sig_hex = parts.get("v1", "").strip()
    else:
        if sig_hex.startswith("v1="):
            sig_hex = sig_hex[3:]
        if timestamp_header is not None:
            try:
                ts_val = int(timestamp_header.strip())
            except ValueError:
                return False

    if ts_val is None:
        return False

    current = now_ts if now_ts is not None else int(time.time())
    if abs(current - ts_val) > tolerance_seconds:
        return False

    expected = compute_webhook_signature(secret, ts_val, raw_body)
    return hmac.compare_digest(expected, sig_hex)
