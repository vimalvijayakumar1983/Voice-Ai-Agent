"""Dependency checks used by the public API readiness probe."""

import asyncio

import structlog
from sqlalchemy import text

from app.core.database import engine

logger = structlog.get_logger()

# Compatibility release for the additive 022/023/024 migration rollout. Deploy
# this small revision before applying those migrations so the previous app can
# remain healthy while Alembic advances, and retain its image as the safe
# application rollback target. The feature release closes this window again.
EXPECTED_DATABASE_REVISIONS = frozenset(
    {"20260904_021", "20260904_022", "20260904_023", "20260904_024"}
)
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

    return len(applied_revisions) == 1 and applied_revisions.issubset(EXPECTED_DATABASE_REVISIONS)
