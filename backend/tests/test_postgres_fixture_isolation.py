"""Exercise the real PostgreSQL cleanup contract; SQLite keeps its original lifecycle."""

import pytest
from sqlalchemy import func, select

from app.core.database import Base
from app.models.tenant import Tenant
from tests.conftest import TEST_DATABASE_URL, clear_postgres_data, test_session_factory

pytestmark = pytest.mark.skipif(TEST_DATABASE_URL.startswith("sqlite"), reason="PostgreSQL fixture")


async def test_committed_rows_are_cleared_and_schema_survives():
    async with test_session_factory() as session:
        session.add(Tenant(name="Fixture isolation", slug="fixture-isolation"))
        await session.commit()
    await clear_postgres_data()
    async with test_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Tenant)) == 0
        # Query every mapped table: cleanup must leave the full schema usable.
        for table in Base.metadata.sorted_tables:
            assert await session.scalar(select(func.count()).select_from(table)) == 0
        session.add(Tenant(name="Fixture isolation again", slug="fixture-isolation"))
        await session.commit()  # Same unique key can be inserted after cleanup.


async def test_cleanup_is_idempotent():
    await clear_postgres_data()
    await clear_postgres_data()
    async with test_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Tenant)) == 0
