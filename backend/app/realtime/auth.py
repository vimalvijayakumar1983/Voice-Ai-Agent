"""Short-lived HMAC capability tokens for provider media WebSockets."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from uuid import UUID

from app.core.config import settings

TOKEN_TTL_SECONDS = 5 * 60


def _key() -> bytes:
    material = settings.integration_encryption_key.strip() or settings.secret_key.strip()
    return hashlib.sha256(b"vav/realtime-media-token/v1\x00" + material.encode()).digest()


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def create_media_token(call_id: UUID, *, now: int | None = None) -> str:
    issued_at = int(time.time() if now is None else now)
    payload = json.dumps(
        {"call_id": str(call_id), "exp": issued_at + TOKEN_TTL_SECONDS},
        separators=(",", ":"),
        sort_keys=True,
    ).encode()
    encoded = _encode(payload)
    signature = _encode(hmac.new(_key(), encoded.encode(), hashlib.sha256).digest())
    return f"{encoded}.{signature}"


def verify_media_token(token: str, call_id: UUID, *, now: int | None = None) -> bool:
    try:
        encoded, supplied_signature = token.split(".", 1)
        expected = _encode(hmac.new(_key(), encoded.encode(), hashlib.sha256).digest())
        if not hmac.compare_digest(supplied_signature, expected):
            return False
        payload = json.loads(_decode(encoded))
        observed_at = int(time.time() if now is None else now)
        return payload.get("call_id") == str(call_id) and observed_at <= int(payload.get("exp", 0))
    except (ValueError, TypeError, json.JSONDecodeError):
        return False
