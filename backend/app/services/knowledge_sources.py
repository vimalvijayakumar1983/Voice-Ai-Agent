"""Shared source identity and consolidation rules for VAV knowledge."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import KnowledgeBase, KnowledgeCrawlPage, KnowledgeSource

if TYPE_CHECKING:
    from app.providers.smallest import SmallestAIClient

VAV_NATIVE_KNOWLEDGE_PROVIDERS = frozenset({"sarvam", "elevenlabs", "inworld"})
WEBSITE_SOURCE_TYPES = frozenset({"url", "website", "sitemap"})
_TRACKING_QUERY_KEYS = frozenset({"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid"})
REMOTE_CREATION_UNKNOWN_PREFIX = "provider_knowledge_base_creation_outcome_unknown:"


def has_searchable_content(source: KnowledgeSource) -> bool:
    """Return whether VAV can actually place this source in an agent prompt."""
    return bool(str(getattr(source, "content", None) or "").strip())


def invalidate_knowledge_approval(knowledge_base: KnowledgeBase) -> bool:
    """Stage source edits without withdrawing the last approved release.

    ``approval_status`` describes the mutable draft.  The immutable serving
    pointer and its lexicon remain pinned until approval atomically publishes a
    replacement (or an administrator explicitly revokes publication).
    """
    was_approved = knowledge_base.approval_status == "approved"
    if was_approved:
        knowledge_base.approval_status = "draft"
        # The mutable draft is no longer published. The active release keeps
        # its own immutable published_at timestamp on serving_revision.
        knowledge_base.published_at = None
    return was_approved


def remote_creation_outcome_unknown(knowledge_base: KnowledgeBase) -> bool:
    return str(knowledge_base.sync_error or "").startswith(REMOTE_CREATION_UNKNOWN_PREFIX)


def mark_remote_creation_outcome_unknown(knowledge_base: KnowledgeBase) -> None:
    knowledge_base.sync_status = "error"
    knowledge_base.sync_error = (
        f"{REMOTE_CREATION_UNKNOWN_PREFIX} Smallest.ai may have created the knowledge base, "
        "but VAV did not receive its ID. Automatic creation is paused to prevent duplicates; "
        "reconcile the provider workspace before retrying."
    )


def canonical_source_url(value: str | None) -> str | None:
    """Build a stable URL identity without deciding whether the page is crawl-worthy."""
    raw = str(value or "").strip()
    if not raw:
        return None
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    scheme = parsed.scheme.lower()
    host = parsed.hostname.rstrip(".").encode("idna").decode("ascii").lower()
    default_port = 80 if scheme == "http" else 443
    netloc = host if port in {None, default_port} else f"{host}:{port}"
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.casefold().startswith("utm_") and key.casefold() not in _TRACKING_QUERY_KEYS
    ]
    return urlunsplit((scheme, netloc, path, urlencode(sorted(query)), ""))


def _timestamp(value: datetime | None) -> float:
    return value.timestamp() if value is not None else 0.0


def _source_quality(source: KnowledgeSource) -> tuple[int, int, float, int, int]:
    content = str(source.content or "").strip()
    return (
        int(bool(content)),
        int(source.status == "indexed"),
        _timestamp(source.last_synced_at),
        len(content),
        int(bool(source.provider_item_id)),
    )


def _provider_item_id(item: dict | None) -> str | None:
    if not isinstance(item, dict):
        return None
    item_id = item.get("_id") or item.get("id")
    return str(item_id) if item_id else None


def _provider_file_name(item: dict) -> str:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return str(item.get("fileName") or metadata.get("fileName") or "")


def _provider_item_urls(item: dict) -> list[object]:
    metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
    return [
        value
        for value in (
            item.get("url"),
            item.get("hostUrl"),
            item.get("location"),
            item.get("sourceUrl"),
            item.get("sourceURL"),
            item.get("source_url"),
            metadata.get("url"),
            metadata.get("sourceUrl"),
            metadata.get("sourceURL"),
            metadata.get("source_url"),
        )
        if value
    ]


def _expanded_scraped_items(scraped: list[dict]) -> list[tuple[str, dict]]:
    expanded: list[tuple[str, dict]] = []
    for batch in scraped:
        batch_id = _provider_item_id(batch)
        if not batch_id:
            continue
        expanded.append((batch_id, batch))
        nested = batch.get("scrapedUrls")
        if not isinstance(nested, list):
            continue
        for value in nested:
            child = dict(value) if isinstance(value, dict) else {"url": value}
            expanded.append((batch_id, child))
    return expanded


def _provider_source_target(
    source: KnowledgeSource,
    *,
    scraped: list[dict],
    items: list[dict],
) -> tuple[str, str] | None:
    """Resolve the provider collection that currently owns one local source."""
    scraped_by_id = {
        item_id: batch for batch in scraped if (item_id := _provider_item_id(batch)) is not None
    }
    item_ids = {item_id for item in items if (item_id := _provider_item_id(item)) is not None}
    if source.provider_item_id in scraped_by_id:
        return "scraped", source.provider_item_id
    if source.provider_item_id in item_ids:
        return "item", source.provider_item_id

    source_key = canonical_source_url(source.location)
    if source_key:
        for batch_id, item in _expanded_scraped_items(scraped):
            if any(
                canonical_source_url(str(value)) == source_key
                for value in _provider_item_urls(item)
            ):
                return "scraped", batch_id
        for item in items:
            if any(
                canonical_source_url(str(value)) == source_key
                for value in _provider_item_urls(item)
            ):
                item_id = _provider_item_id(item)
                if item_id:
                    return "item", item_id

    if source.source_type == "file":
        for item in items:
            if _provider_file_name(item) == source.name:
                item_id = _provider_item_id(item)
                if item_id:
                    return "item", item_id
    return None


def _scraped_batch_url_keys(batch_id: str, scraped: list[dict]) -> set[str]:
    return {
        canonical
        for candidate_batch_id, item in _expanded_scraped_items(scraped)
        if candidate_batch_id == batch_id
        for value in _provider_item_urls(item)
        if (canonical := canonical_source_url(str(value))) is not None
    }


async def _retire_smallest_duplicate(
    provider: SmallestAIClient,
    knowledge_base: KnowledgeBase,
    *,
    scraped: list[dict],
    items: list[dict],
    duplicate: KnowledgeSource,
    survivor: KnowledgeSource,
    retired_targets: set[tuple[str, str]],
) -> bool:
    """Delete a distinct stale artifact only when no other page depends on it."""
    remote_id = knowledge_base.provider_knowledge_base_id
    if not remote_id:
        return True
    duplicate_target = _provider_source_target(duplicate, scraped=scraped, items=items)
    survivor_target = _provider_source_target(survivor, scraped=scraped, items=items)
    if duplicate_target is None or duplicate_target == survivor_target:
        return True
    if duplicate_target in retired_targets:
        return True

    # Never remove an artifact that a different canonical local source still uses.
    duplicate_key = canonical_source_url(duplicate.location)
    for other in knowledge_base.sources:
        if other is duplicate or other is survivor:
            continue
        if canonical_source_url(other.location) == duplicate_key:
            continue
        if _provider_source_target(other, scraped=scraped, items=items) == duplicate_target:
            return False

    collection, provider_item_id = duplicate_target
    if collection == "scraped":
        # A scrape record can contain a whole site. Delete it only when every URL
        # has a searchable, independently indexed replacement artifact.
        batch_keys = _scraped_batch_url_keys(provider_item_id, scraped)
        if not batch_keys:
            return False
        for batch_key in batch_keys:
            replacement_found = False
            for candidate in knowledge_base.sources:
                if candidate is duplicate:
                    continue
                if canonical_source_url(candidate.location) != batch_key:
                    continue
                candidate_target = _provider_source_target(
                    candidate,
                    scraped=scraped,
                    items=items,
                )
                if (
                    has_searchable_content(candidate)
                    and candidate.status == "indexed"
                    and candidate_target is not None
                    and candidate_target != duplicate_target
                ):
                    replacement_found = True
                    break
            if not replacement_found:
                return False
        await provider.delete_scraped_knowledge_url(
            knowledge_base_id=remote_id,
            scraped_url_id=provider_item_id,
        )
        scraped[:] = [item for item in scraped if _provider_item_id(item) != provider_item_id]
    else:
        await provider.delete_knowledge_item(
            knowledge_base_id=remote_id,
            item_id=provider_item_id,
        )
        items[:] = [item for item in items if _provider_item_id(item) != provider_item_id]
    retired_targets.add(duplicate_target)
    return True


async def consolidate_smallest_url_duplicates(
    db: AsyncSession,
    knowledge_base: KnowledgeBase,
    provider: SmallestAIClient,
    *,
    scraped: list[dict] | None = None,
    items: list[dict] | None = None,
    preferred_source: KnowledgeSource | None = None,
) -> int:
    """Consolidate local duplicates only after retiring distinct remote artifacts."""
    if not knowledge_base.provider_knowledge_base_id:
        return await consolidate_duplicate_url_sources(
            db,
            knowledge_base,
            preferred_source=preferred_source,
        )
    remote_id = knowledge_base.provider_knowledge_base_id
    scraped_inventory = (
        scraped if scraped is not None else await provider.list_scraped_knowledge_urls(remote_id)
    )
    item_inventory = items if items is not None else await provider.list_knowledge_items(remote_id)
    retired_targets: set[tuple[str, str]] = set()

    async def retire(duplicate: KnowledgeSource, survivor: KnowledgeSource) -> bool:
        return await _retire_smallest_duplicate(
            provider,
            knowledge_base,
            scraped=scraped_inventory,
            items=item_inventory,
            duplicate=duplicate,
            survivor=survivor,
            retired_targets=retired_targets,
        )

    return await consolidate_duplicate_url_sources(
        db,
        knowledge_base,
        preferred_source=preferred_source,
        retire_provider_duplicate=retire,
    )


async def consolidate_duplicate_url_sources(
    db: AsyncSession,
    knowledge_base: KnowledgeBase,
    *,
    preferred_source: KnowledgeSource | None = None,
    retire_provider_duplicate: Callable[[KnowledgeSource, KnowledgeSource], Awaitable[bool]]
    | None = None,
) -> int:
    """Merge canonical URL duplicates while retaining the strongest usable row.

    Crawl ledgers are repointed before duplicate rows are removed, so historical
    progress remains traceable and the only searchable copy is never discarded.
    """
    groups: dict[str, list[KnowledgeSource]] = {}
    for source in list(knowledge_base.sources):
        if source.source_type not in WEBSITE_SOURCE_TYPES:
            continue
        canonical = canonical_source_url(source.location)
        if canonical:
            groups.setdefault(canonical, []).append(source)

    removed = 0
    for canonical, candidates in groups.items():
        if len(candidates) < 2:
            candidates[0].location = canonical
            continue

        survivor = (
            preferred_source
            if preferred_source in candidates and has_searchable_content(preferred_source)
            else max(candidates, key=_source_quality)
        )
        strongest_content = (
            preferred_source
            if preferred_source in candidates and has_searchable_content(preferred_source)
            else max(candidates, key=_source_quality)
        )
        provider_source = (
            preferred_source
            if preferred_source in candidates and preferred_source.provider_item_id
            else max(
                candidates,
                key=lambda item: (
                    int(bool(item.provider_item_id)),
                    int(item.status == "indexed"),
                    _timestamp(item.last_synced_at),
                ),
            )
        )

        if has_searchable_content(strongest_content):
            survivor.content = strongest_content.content
            if strongest_content.mime_type:
                survivor.mime_type = strongest_content.mime_type
            if strongest_content.size_bytes is not None:
                survivor.size_bytes = strongest_content.size_bytes
            if strongest_content.source_metadata:
                survivor.source_metadata = {
                    **(survivor.source_metadata or {}),
                    **strongest_content.source_metadata,
                }
        if provider_source.provider_item_id:
            survivor.provider_item_id = provider_source.provider_item_id
        survivor.location = canonical
        survivor.last_synced_at = max(
            (item.last_synced_at for item in candidates if item.last_synced_at),
            default=survivor.last_synced_at,
        )

        if has_searchable_content(survivor):
            if any(item.status == "indexed" for item in candidates):
                survivor.status = "indexed"
            survivor.error_message = None
        elif any(item.status in {"pending", "processing"} for item in candidates):
            survivor.status = "processing"
            survivor.error_message = None
        else:
            survivor.status = "failed"
            survivor.error_message = next(
                (item.error_message for item in candidates if item.error_message),
                "VAV found no searchable content for this website page.",
            )

        for duplicate in candidates:
            if duplicate is survivor:
                continue
            if retire_provider_duplicate is not None:
                if not await retire_provider_duplicate(duplicate, survivor):
                    duplicate.status = "failed"
                    duplicate.error_message = (
                        "VAV kept this duplicate visible because its shared provider artifact "
                        "could not be retired without risking other indexed pages."
                    )
                    continue
            elif knowledge_base.provider_knowledge_base_id:
                # Never hide a local duplicate while a distinct provider artifact
                # might still be searchable by a Smallest.ai agent.
                duplicate.status = "failed"
                duplicate.error_message = (
                    "VAV must reconcile the provider artifact before consolidating this duplicate."
                )
                continue
            await db.execute(
                update(KnowledgeCrawlPage)
                .where(KnowledgeCrawlPage.knowledge_source_id == duplicate.id)
                .values(knowledge_source_id=survivor.id)
            )
            if duplicate in knowledge_base.sources:
                knowledge_base.sources.remove(duplicate)
            await db.delete(duplicate)
            removed += 1

    return removed
