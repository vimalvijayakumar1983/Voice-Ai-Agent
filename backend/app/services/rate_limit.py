"""Redis-backed fixed-window limits for unauthenticated security boundaries."""

import hashlib
import hmac
import ipaddress
import time

from fastapi import HTTPException, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.core.config import settings

_redis_client: Redis | None = None

# INCR and first-write expiry must be one atomic operation. Refreshing expiry on
# every rejected attempt would let an attacker keep a known account locked out
# indefinitely with low-rate traffic.
_FIXED_WINDOW_LUA = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return {count, redis.call('TTL', KEYS[1])}
"""


def _client_identity(request: Request) -> str:
    peer = request.client.host if request.client else "unknown"
    if not settings.trust_railway_proxy_headers:
        return peer

    # Railway documents X-Real-IP as the remote client address injected by its
    # public edge. This opt-in is intentionally provider-specific; generic
    # X-Forwarded-For is never trusted here.
    railway_ip = request.headers.get("x-real-ip", "").strip()
    try:
        return ipaddress.ip_address(railway_ip).compressed
    except ValueError:
        return peer


def _rate_limit_key(scope: str, identity: str, window_seconds: int) -> str:
    bucket = int(time.time()) // window_seconds
    digest = hmac.new(
        settings.secret_key.encode("utf-8"),
        f"{scope}:{identity}:{bucket}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"voiceai:rate:{scope}:{bucket}:{digest}"


def _redis() -> Redis:
    global _redis_client
    if _redis_client is None:
        _redis_client = Redis.from_url(
            settings.redis_url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _redis_client


async def enforce_rate_limit(
    request: Request,
    *,
    scope: str,
    limit: int,
    window_seconds: int,
    subject: str | None = None,
    bind_to_client: bool = True,
) -> None:
    """Apply one production-only, privacy-preserving distributed limit."""
    if settings.app_env.strip().lower() != "production":
        return

    identity = _client_identity(request)
    if subject:
        normalized_subject = subject.strip().lower()
        identity = f"{identity}:{normalized_subject}" if bind_to_client else normalized_subject
    key = _rate_limit_key(scope, identity, window_seconds)
    try:
        count, _ttl = await _redis().eval(
            _FIXED_WINDOW_LUA,
            1,
            key,
            window_seconds + 5,
        )
    except RedisError as exc:
        # Authentication endpoints fail closed when their abuse-control state
        # is unavailable; normal authenticated API traffic remains unaffected.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Authentication is temporarily unavailable",
            headers={"Retry-After": "30"},
        ) from exc

    if int(count) > limit:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Too many authentication attempts",
            headers={"Retry-After": str(window_seconds)},
        )
