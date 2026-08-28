"""Distributed authentication rate-limit tests."""

from collections import defaultdict

import pytest
from fastapi import HTTPException
from redis.exceptions import RedisError
from starlette.requests import Request

from app.services import rate_limit


class FakeRedis:
    def __init__(self):
        self.counts: dict[str, int] = defaultdict(int)
        self.expiry_sets: dict[str, int] = defaultdict(int)
        self.ttls: dict[str, int] = {}

    async def eval(self, script: str, numkeys: int, key: str, ttl: int):
        assert script == rate_limit._FIXED_WINDOW_LUA
        assert numkeys == 1
        self.counts[key] += 1
        if self.counts[key] == 1:
            self.ttls[key] = ttl
            self.expiry_sets[key] += 1
        return [self.counts[key], self.ttls[key]]


class BrokenRedis:
    async def eval(self, *_args):
        raise RedisError("unavailable")


def _request(
    host: str = "10.0.0.8",
    *,
    headers: list[tuple[bytes, bytes]] | None = None,
) -> Request:
    return Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "https",
            "path": "/api/v1/auth/login",
            "raw_path": b"/api/v1/auth/login",
            "query_string": b"",
            "headers": headers or [],
            "client": (host, 43000),
            "server": ("voice.example.com", 443),
        }
    )


@pytest.mark.asyncio
async def test_production_limit_is_distributed_and_privacy_preserving(monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(rate_limit.settings, "app_env", "production")
    monkeypatch.setattr(rate_limit.settings, "secret_key", "s" * 32)
    monkeypatch.setattr(rate_limit.settings, "trust_railway_proxy_headers", True)
    monkeypatch.setattr(rate_limit, "_redis_client", fake_redis)
    monkeypatch.setattr(rate_limit.time, "time", lambda: 1_800_000_000)

    for _ in range(2):
        await rate_limit.enforce_rate_limit(
            _request(headers=[(b"x-real-ip", b"203.0.113.25")]),
            scope="login-account",
            subject="Person@Example.com",
            limit=2,
            window_seconds=300,
        )

    with pytest.raises(HTTPException) as exc_info:
        await rate_limit.enforce_rate_limit(
            _request(headers=[(b"x-real-ip", b"203.0.113.25")]),
            scope="login-account",
            subject="person@example.com",
            limit=2,
            window_seconds=300,
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers == {"Retry-After": "300"}
    stored_key = next(iter(fake_redis.counts))
    assert "person@example.com" not in stored_key
    assert "203.0.113.25" not in stored_key
    assert fake_redis.expiry_sets[stored_key] == 1
    assert fake_redis.ttls[stored_key] == 305


def test_railway_real_ip_is_opt_in_and_invalid_values_fail_closed(monkeypatch):
    request = _request(headers=[(b"x-real-ip", b"198.51.100.17")])

    monkeypatch.setattr(rate_limit.settings, "trust_railway_proxy_headers", False)
    assert rate_limit._client_identity(request) == "10.0.0.8"

    monkeypatch.setattr(rate_limit.settings, "trust_railway_proxy_headers", True)
    assert rate_limit._client_identity(request) == "198.51.100.17"
    invalid = _request(headers=[(b"x-real-ip", b"not-an-ip")])
    assert rate_limit._client_identity(invalid) == "10.0.0.8"


@pytest.mark.asyncio
async def test_account_limit_is_shared_across_client_addresses(monkeypatch):
    fake_redis = FakeRedis()
    monkeypatch.setattr(rate_limit.settings, "app_env", "production")
    monkeypatch.setattr(rate_limit.settings, "secret_key", "s" * 32)
    monkeypatch.setattr(rate_limit, "_redis_client", fake_redis)
    monkeypatch.setattr(rate_limit.time, "time", lambda: 1_800_000_000)

    await rate_limit.enforce_rate_limit(
        _request("10.0.0.1"),
        scope="login-account",
        subject="person@example.com",
        bind_to_client=False,
        limit=1,
        window_seconds=300,
    )
    with pytest.raises(HTTPException) as exc_info:
        await rate_limit.enforce_rate_limit(
            _request("10.0.0.2"),
            scope="login-account",
            subject="person@example.com",
            bind_to_client=False,
            limit=1,
            window_seconds=300,
        )
    assert exc_info.value.status_code == 429


@pytest.mark.asyncio
async def test_production_auth_rate_limit_fails_closed_when_redis_is_unavailable(monkeypatch):
    monkeypatch.setattr(rate_limit.settings, "app_env", "production")
    monkeypatch.setattr(rate_limit, "_redis_client", BrokenRedis())

    with pytest.raises(HTTPException) as exc_info:
        await rate_limit.enforce_rate_limit(
            _request(), scope="login-ip", limit=10, window_seconds=60
        )

    assert exc_info.value.status_code == 503
    assert exc_info.value.headers == {"Retry-After": "30"}


@pytest.mark.asyncio
async def test_development_rate_limit_does_not_require_redis(monkeypatch):
    monkeypatch.setattr(rate_limit.settings, "app_env", "development")
    monkeypatch.setattr(rate_limit, "_redis_client", BrokenRedis())

    await rate_limit.enforce_rate_limit(_request(), scope="login-ip", limit=1, window_seconds=60)
