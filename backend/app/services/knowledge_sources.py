"""Shared source identity and consolidation rules for VAV knowledge.

Knowledge ingestion and retrieval are local to VAV.  No external knowledge
provider is consulted; every agent runtime reads the same approved records.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import KnowledgeBase, KnowledgeCrawlPage, KnowledgeSource

VAV_NATIVE_KNOWLEDGE_PROVIDERS = frozenset({"sarvam", "elevenlabs", "inworld"})
WEBSITE_SOURCE_TYPES = frozenset({"url", "website", "sitemap"})
KNOWLEDGE_PROVIDER = "vav"
_TRACKING_QUERY_KEYS = frozenset({"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid"})


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


def mark_native_bindings_live(knowledge_base: KnowledgeBase) -> None:
    """Record that bound VAV-native agents read the current knowledge directly.

    VAV runtimes retrieve approved knowledge on every caller turn, so a source
    change is live as soon as it is indexed locally.  A legacy binding to an
    agent on another voice provider is explicitly not live: that agent never
    reads VAV's local index, so it is moved to ``pending`` rather than left
    reporting a historical success.
    """
    now = datetime.now(UTC)
    for binding in knowledge_base.agent_bindings:
        agent = binding.agent
        provider = str(getattr(agent, "voice_provider", "") or "").strip()
        if provider not in VAV_NATIVE_KNOWLEDGE_PROVIDERS:
            binding.sync_status = "pending"
            binding.last_synced_at = None
            continue
        binding.provider = provider
        binding.sync_status = "synced"
        binding.last_synced_at = now


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


def _source_quality(source: KnowledgeSource) -> tuple[int, int, float, int]:
    content = str(source.content or "").strip()
    return (
        int(bool(content)),
        int(source.status == "indexed"),
        _timestamp(source.last_synced_at),
        len(content),
    )


async def consolidate_duplicate_url_sources(
    db: AsyncSession,
    knowledge_base: KnowledgeBase,
    *,
    preferred_source: KnowledgeSource | None = None,
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
        strongest_content = survivor

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
