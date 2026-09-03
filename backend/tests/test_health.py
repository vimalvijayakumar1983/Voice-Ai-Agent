"""Public API health and baseline hardening tests."""

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from httpx import AsyncClient

from app import main as main_module
from app.core import readiness as readiness_module
from app.core.readiness import EXPECTED_DATABASE_REVISIONS


class _ReadinessResult:
    def __init__(self, revisions: list[str]):
        self.revisions = revisions

    def scalars(self):
        return self

    def all(self) -> list[str]:
        return self.revisions


class _ReadinessConnection:
    def __init__(self, revisions: list[str] | None = None, error: Exception | None = None):
        self.revisions = revisions or []
        self.error = error
        self.statement = ""

    async def execute(self, statement):
        self.statement = str(statement)
        if self.error is not None:
            raise self.error
        return _ReadinessResult(self.revisions)


class _ReadinessConnectionContext:
    def __init__(self, connection: _ReadinessConnection):
        self.connection = connection

    async def __aenter__(self) -> _ReadinessConnection:
        return self.connection

    async def __aexit__(self, _exc_type, _exc, _traceback) -> None:
        return None


class _ReadinessEngine:
    def __init__(self, connection: _ReadinessConnection):
        self.connection = connection

    def connect(self) -> _ReadinessConnectionContext:
        return _ReadinessConnectionContext(self.connection)


@pytest.mark.asyncio
async def test_health_response_has_security_headers(client: AsyncClient, monkeypatch):
    readiness_probe = AsyncMock(side_effect=AssertionError("liveness checked a dependency"))
    monkeypatch.setattr(main_module, "database_schema_is_ready", readiness_probe)

    response = await client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "healthy"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
    readiness_probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_readiness_succeeds_without_exposing_dependency_details(
    client: AsyncClient,
    monkeypatch,
):
    readiness_probe = AsyncMock(return_value=True)
    monkeypatch.setattr(main_module, "database_schema_is_ready", readiness_probe)

    response = await client.get("/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready", "version": main_module.APP_VERSION}
    assert response.headers["cache-control"] == "no-store"
    readiness_probe.assert_awaited_once_with()


@pytest.mark.asyncio
async def test_readiness_fails_closed_without_exposing_dependency_details(
    client: AsyncClient,
    monkeypatch,
):
    readiness_probe = AsyncMock(return_value=False)
    monkeypatch.setattr(main_module, "database_schema_is_ready", readiness_probe)

    response = await client.get("/ready")

    assert response.status_code == 503
    assert response.json() == {"status": "not_ready"}
    assert "database" not in response.text.lower()
    assert "revision" not in response.text.lower()


def test_readiness_revision_matches_alembic_heads():
    backend_root = Path(__file__).resolve().parents[1]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "migrations"))
    script = ScriptDirectory.from_config(config)

    assert EXPECTED_DATABASE_REVISIONS == frozenset(script.get_heads())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revisions", "expected"),
    [
        (["20260903_019"], True),
        ([], False),
        (["20260827_007"], False),
        (["20260903_019", "unexpected_branch"], False),
    ],
)
async def test_database_readiness_requires_exact_migration_heads(
    monkeypatch,
    revisions: list[str],
    expected: bool,
):
    connection = _ReadinessConnection(revisions)
    monkeypatch.setattr(readiness_module, "engine", _ReadinessEngine(connection))

    assert await readiness_module.database_schema_is_ready() is expected
    assert "alembic_version" in connection.statement


@pytest.mark.asyncio
async def test_database_readiness_fails_closed_on_connection_error(monkeypatch):
    connection = _ReadinessConnection(error=RuntimeError("sensitive connection detail"))
    monkeypatch.setattr(readiness_module, "engine", _ReadinessEngine(connection))

    assert await readiness_module.database_schema_is_ready() is False
