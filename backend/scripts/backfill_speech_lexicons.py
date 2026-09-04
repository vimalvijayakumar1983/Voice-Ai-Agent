"""Publish immutable speech lexicons for approved pre-migration knowledge bases."""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from uuid import UUID

from app.core.database import async_session_factory
from app.services.speech_lexicon import backfill_approved_speech_lexicons_batch


@dataclass(frozen=True, slots=True)
class BackfillRunSummary:
    published: int
    quarantined: int


async def _run(*, tenant_id: UUID | None, batch_size: int) -> BackfillRunSummary:
    published = 0
    quarantined = 0
    while True:
        async with async_session_factory() as db:
            result = await backfill_approved_speech_lexicons_batch(
                db,
                tenant_id=tenant_id,
                limit=batch_size,
            )
            await db.commit()
        published += result.published
        quarantined += result.failed
        if result.selected == 0:
            return BackfillRunSummary(
                published=published,
                quarantined=quarantined,
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant-id", type=UUID)
    parser.add_argument("--batch-size", type=int, default=100)
    args = parser.parse_args()
    if not 1 <= args.batch_size <= 5_000:
        parser.error("--batch-size must be between 1 and 5000")
    result = asyncio.run(_run(tenant_id=args.tenant_id, batch_size=args.batch_size))
    print(
        f"Published {result.published} speech lexicon artifact(s); "
        f"quarantined {result.quarantined} knowledge base(s)."
    )
    if result.quarantined:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
