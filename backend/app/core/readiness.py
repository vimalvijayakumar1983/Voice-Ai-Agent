"""Dependency checks used by the public API readiness probe."""

import asyncio

import structlog
from sqlalchemy import text

from app.core.database import engine

logger = structlog.get_logger()

# Keep this in lockstep with the Alembic script head. A test compares the two so
# adding a migration without updating API readiness fails CI instead of making a
# newly deployed service advertise an older schema as ready.
EXPECTED_DATABASE_REVISIONS = frozenset({"20260831_016"})
READINESS_TIMEOUT_SECONDS = 3.0


async def database_schema_is_ready() -> bool:
    """Return whether PostgreSQL is reachable and stamped at the expected head.

    A single Alembic query proves connectivity and schema revision readiness.
    Any timeout, missing version table, connection error, or unexpected revision
    fails closed. Error details stay in structured server logs and are never
    returned by the public probe.
    """

    try:
        async with asyncio.timeout(READINESS_TIMEOUT_SECONDS):
            async with engine.connect() as connection:
                result = await connection.execute(text("SELECT version_num FROM alembic_version"))
                applied_revisions = frozenset(result.scalars().all())
    except Exception as exc:
        logger.warning(
            "database_readiness_check_failed",
            error_type=type(exc).__name__,
        )
        return False

    return applied_revisions == EXPECTED_DATABASE_REVISIONS
