"""Immutable blue/green knowledge releases for caller-facing retrieval."""

from __future__ import annotations

import copy
import hashlib
import json
import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.agent import (
    AgentKnowledgeBinding,
    KnowledgeBase,
    KnowledgeServingRevision,
    KnowledgeServingRevisionSource,
    KnowledgeSpeechLexicon,
)
from app.models.call import Call
from app.services.knowledge_sources import has_searchable_content

SERVING_COMPILER_VERSION = "vav-knowledge-serving-1"
_SERVING_NAMESPACE = UUID("3384078e-4026-5a4b-8929-53d7e441f21e")
_ELIGIBLE_SOURCE_STATUSES = frozenset({"indexed", "local_only"})
_PARAGRAPH_SPLIT = re.compile(r"(?:\r?\n){2,}|(?<=[.!?।])\s+")
_MAX_CHUNK_CHARS = 1_200
_SERVING_METADATA_KEYS = frozenset(
    {"title", "page_title", "display_name", "url", "canonical_url", "language"}
)
KNOWLEDGE_ADMISSION_STATE = "admitted_before_dispatch"
INBOUND_KNOWLEDGE_ADMISSION_STATE = "admitted_before_media_stream"
_KNOWLEDGE_ADMISSION_STATES = frozenset(
    {KNOWLEDGE_ADMISSION_STATE, INBOUND_KNOWLEDGE_ADMISSION_STATE}
)


class KnowledgeServingError(ValueError):
    """Raised when a draft cannot be published as a safe serving revision."""


def knowledge_call_reservation_metadata(
    revision: KnowledgeServingRevision,
    revocation_generation: int,
) -> dict[str, str | int]:
    """Serialize the complete immutable identity every VAV media lane uses."""

    if (
        isinstance(revocation_generation, bool)
        or not isinstance(revocation_generation, int)
        or revocation_generation < 0
    ):
        raise KnowledgeServingError(
            "Knowledge serving revocation generation must be a non-negative integer"
        )
    manifest = revision.manifest if isinstance(revision.manifest, dict) else {}
    lexicon_manifest = manifest.get("speech_lexicon")
    if not isinstance(lexicon_manifest, dict):
        raise KnowledgeServingError("Knowledge serving speech lexicon manifest is missing")
    artifact_id = str(revision.speech_lexicon_artifact_id)
    content_sha256 = lexicon_manifest.get("content_sha256")
    if (
        lexicon_manifest.get("artifact_id") != artifact_id
        or content_sha256 != revision.entity_revision_sha256
        or not isinstance(content_sha256, str)
        or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None
    ):
        raise KnowledgeServingError("Knowledge serving speech lexicon manifest is invalid")
    return {
        "knowledge_serving_revision_id": str(revision.id),
        "knowledge_serving_knowledge_base_id": str(revision.knowledge_base_id),
        "knowledge_serving_content_sha256": revision.content_sha256,
        "knowledge_source_revision_sha256": revision.source_revision_sha256,
        "knowledge_serving_revocation_generation": revocation_generation,
        "speech_lexicon_artifact_id": artifact_id,
        "speech_lexicon_content_sha256": content_sha256,
    }


def parse_serving_revision_id(value: object) -> UUID | None:
    """Parse a persisted per-call pin without silently falling back to latest.

    Missing pins return ``None`` for backwards compatibility. A present but
    malformed pin is a hard error: serving the newest release in that case
    would mix knowledge revisions inside one call.
    """

    if value is None or value == "":
        return None
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise KnowledgeServingError("Knowledge serving revision pin must be a UUID")
    try:
        return UUID(value)
    except ValueError as exc:
        raise KnowledgeServingError("Knowledge serving revision pin is invalid") from exc


def serving_revision_id_from_call_metadata(value: object) -> UUID | None:
    """Return the immutable revision reserved in a call's runtime metadata."""

    if not isinstance(value, Mapping):
        return None
    runtime = value.get("runtime")
    if not isinstance(runtime, Mapping):
        return None
    return parse_serving_revision_id(runtime.get("knowledge_serving_revision_id"))


def serving_knowledge_base_id_from_call_metadata(value: object) -> UUID | None:
    """Return the knowledge-base identity bound when a call was reserved."""

    if not isinstance(value, Mapping):
        return None
    runtime = value.get("runtime")
    if not isinstance(runtime, Mapping):
        return None
    raw_id = runtime.get("knowledge_serving_knowledge_base_id")
    if raw_id is None or raw_id == "":
        return None
    if isinstance(raw_id, UUID):
        return raw_id
    if not isinstance(raw_id, str):
        raise KnowledgeServingError("Knowledge serving knowledge-base pin must be a UUID")
    try:
        return UUID(raw_id)
    except ValueError as exc:
        raise KnowledgeServingError("Knowledge serving knowledge-base pin is invalid") from exc


def serving_revocation_generation_from_call_metadata(value: object) -> int | None:
    """Return the explicit-revocation fence reserved for a call.

    Missing values identify reservations created during the rolling-upgrade
    window and retain the older, stricter live-pointer admission rule. A
    present malformed value is never silently replaced by the current value.
    """

    if not isinstance(value, Mapping):
        return None
    runtime = value.get("runtime")
    if not isinstance(runtime, Mapping):
        return None
    generation = runtime.get("knowledge_serving_revocation_generation")
    if generation is None:
        return None
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise KnowledgeServingError(
            "Knowledge serving revocation generation must be a non-negative integer"
        )
    return generation


def speech_lexicon_artifact_id_from_call_metadata(value: object) -> UUID | None:
    """Return the immutable speech-lexicon artifact reserved for a call."""

    if not isinstance(value, Mapping):
        return None
    runtime = value.get("runtime")
    if not isinstance(runtime, Mapping):
        return None
    raw_id = runtime.get("speech_lexicon_artifact_id")
    if raw_id is None or raw_id == "":
        return None
    if isinstance(raw_id, UUID):
        return raw_id
    if not isinstance(raw_id, str):
        raise KnowledgeServingError("Speech lexicon artifact pin must be a UUID")
    try:
        return UUID(raw_id)
    except ValueError as exc:
        raise KnowledgeServingError("Speech lexicon artifact pin is invalid") from exc


def speech_lexicon_content_sha256_from_call_metadata(value: object) -> str | None:
    """Return the immutable speech-lexicon content hash reserved for a call."""

    if not isinstance(value, Mapping):
        return None
    runtime = value.get("runtime")
    if not isinstance(runtime, Mapping):
        return None
    content_sha256 = runtime.get("speech_lexicon_content_sha256")
    if content_sha256 is None or content_sha256 == "":
        return None
    if not isinstance(content_sha256, str) or re.fullmatch(r"[0-9a-f]{64}", content_sha256) is None:
        raise KnowledgeServingError("Speech lexicon content hash is invalid")
    return content_sha256


def knowledge_admission_is_durable(value: object) -> bool:
    """Validate a server-authored knowledge admission marker.

    The marker predates speech-lexicon reservation fields and intentionally
    remains backward-compatible for in-flight LiveKit calls during rolling
    deployments. Native Twilio paths validate the newer lexicon identity
    separately before admitting or starting media.
    """

    if not isinstance(value, Mapping):
        return False
    runtime = value.get("runtime")
    if not isinstance(runtime, Mapping):
        return False
    state = runtime.get("knowledge_admission_state")
    admitted_at = runtime.get("knowledge_admitted_at")
    if state is None and admitted_at is None:
        return False
    if state not in _KNOWLEDGE_ADMISSION_STATES or not isinstance(admitted_at, str):
        raise KnowledgeServingError("Knowledge admission marker is invalid")
    try:
        parsed_at = datetime.fromisoformat(admitted_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise KnowledgeServingError("Knowledge admission timestamp is invalid") from exc
    if parsed_at.tzinfo is None:
        raise KnowledgeServingError("Knowledge admission timestamp must include a timezone")
    if (
        serving_revision_id_from_call_metadata(value) is None
        or serving_knowledge_base_id_from_call_metadata(value) is None
        or serving_revocation_generation_from_call_metadata(value) is None
    ):
        raise KnowledgeServingError("Knowledge admission identity is incomplete")
    return True


@dataclass(frozen=True)
class ServingSourceSnapshot:
    original_source_id: UUID
    source_type: str
    name: str
    location: str | None
    content: str
    structured_content: dict | None
    content_sha256: str
    compiled_at: datetime | None
    source_metadata: dict | None
    chunk_count: int


@dataclass(frozen=True)
class ServingRevisionBuild:
    revision_id: UUID
    source_revision_sha256: str
    chunk_revision_sha256: str
    fact_revision_sha256: str
    entity_revision_sha256: str
    content_sha256: str
    sources: tuple[ServingSourceSnapshot, ...]
    chunk_count: int
    fact_count: int
    entity_count: int
    manifest: dict[str, Any]


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: Any) -> str:
    payload = value if isinstance(value, str) else _canonical_json(value)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _isoformat(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _chunks(content: str) -> tuple[str, ...]:
    selected: list[str] = []
    for paragraph in _PARAGRAPH_SPLIT.split(content):
        normalized = " ".join(paragraph.split())
        while normalized:
            selected.append(normalized[:_MAX_CHUNK_CHARS])
            normalized = normalized[_MAX_CHUNK_CHARS:]
    return tuple(selected)


def _fact_count(value: object) -> int:
    """Count structured fact leaves for truthful release diagnostics."""

    if isinstance(value, dict):
        if {"subject", "predicate", "value"}.issubset(value):
            return 1
        return sum(_fact_count(item) for item in value.values())
    if isinstance(value, list):
        return sum(_fact_count(item) for item in value)
    return 0


def _safe_source_metadata(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    selected = {
        key: copy.deepcopy(item)
        for key, item in value.items()
        if key in _SERVING_METADATA_KEYS and isinstance(item, (str, int, float, bool))
    }
    return selected or None


def build_serving_revision(
    knowledge_base: KnowledgeBase,
    speech_lexicon: KnowledgeSpeechLexicon,
) -> ServingRevisionBuild:
    """Build a deterministic, self-contained release from the current draft."""

    if speech_lexicon.tenant_id != knowledge_base.tenant_id:
        raise KnowledgeServingError("Speech lexicon does not belong to this tenant")
    if speech_lexicon.knowledge_base_id != knowledge_base.id:
        raise KnowledgeServingError("Speech lexicon does not belong to this knowledge base")
    current_sources = tuple(knowledge_base.sources)
    for source in current_sources:
        if source.status not in _ELIGIBLE_SOURCE_STATUSES or not has_searchable_content(source):
            raise KnowledgeServingError(
                f"Source '{source.name}' must be searchable before publication"
            )
    # IDs alone are insufficient: a KB can retain an older valid artifact
    # while its draft sources have changed. Rebuild the deterministic manifest
    # and require an exact match before pairing speech hints with new content.
    from app.services.speech_lexicon import build_speech_lexicon

    expected_lexicon = build_speech_lexicon(
        knowledge_base,
        current_sources,
    )
    if (
        speech_lexicon.id != expected_lexicon.artifact_id
        or speech_lexicon.source_revision_sha256 != expected_lexicon.source_revision_sha256
        or tuple(speech_lexicon.source_revisions or ()) != expected_lexicon.source_revisions
        or speech_lexicon.content_sha256 != expected_lexicon.content_sha256
    ):
        raise KnowledgeServingError(
            "Speech lexicon does not match the current knowledge source revision"
        )

    snapshots: list[ServingSourceSnapshot] = []
    source_manifest: list[dict[str, Any]] = []
    chunk_manifest: list[dict[str, Any]] = []
    fact_manifest: list[dict[str, Any]] = []
    for source in sorted(knowledge_base.sources, key=lambda item: str(item.id)):
        if source.status not in _ELIGIBLE_SOURCE_STATUSES or not has_searchable_content(source):
            raise KnowledgeServingError(
                f"Source '{source.name}' must be searchable before publication"
            )
        content = str(source.content or "").strip()
        # Never trust a mutable/upstream checksum as the identity of bytes we
        # actually serve. Website rows intentionally use content_sha256 for
        # raw extraction identity, while this release hashes compiled content.
        content_sha256 = _sha256(content)
        chunks = _chunks(content)
        structured_content = (
            copy.deepcopy(source.structured_content)
            if isinstance(source.structured_content, dict)
            else None
        )
        source_metadata = _safe_source_metadata(source.source_metadata)
        snapshot = ServingSourceSnapshot(
            original_source_id=source.id,
            source_type=source.source_type,
            name=source.name,
            location=source.location,
            content=content,
            structured_content=structured_content,
            content_sha256=content_sha256,
            compiled_at=source.compiled_at,
            source_metadata=source_metadata,
            chunk_count=len(chunks),
        )
        snapshots.append(snapshot)
        source_manifest.append(
            {
                "source_id": str(source.id),
                "source_type": source.source_type,
                "name": source.name,
                "location": source.location,
                "content_sha256": content_sha256,
                "upstream_content_sha256": source.content_sha256,
                "compiled_at": _isoformat(source.compiled_at),
                "compiler_version": (structured_content or {}).get("compiler_version"),
            }
        )
        chunk_manifest.extend(
            {
                "source_id": str(source.id),
                "ordinal": ordinal,
                "sha256": _sha256(chunk),
            }
            for ordinal, chunk in enumerate(chunks)
        )
        fact_manifest.append(
            {
                "source_id": str(source.id),
                "structured_sha256": _sha256(structured_content or {}),
                "fact_count": _fact_count(structured_content),
            }
        )

    if not snapshots:
        raise KnowledgeServingError("At least one searchable source is required for publication")

    source_revision_sha256 = _sha256(source_manifest)
    # The serving and speech compilers intentionally hash different canonical
    # manifests. The explicit artifact check above proves they cover the same
    # locked source revision; retain both hashes for independent auditability.
    lexicon_source_revision = speech_lexicon.source_revision_sha256
    chunk_revision_sha256 = _sha256(chunk_manifest)
    fact_revision_sha256 = _sha256(fact_manifest)
    entity_revision_sha256 = speech_lexicon.content_sha256
    fact_count = sum(item["fact_count"] for item in fact_manifest)
    manifest = {
        "schema_version": 1,
        "source": {"revision_sha256": source_revision_sha256, "count": len(snapshots)},
        "chunks": {"revision_sha256": chunk_revision_sha256, "count": len(chunk_manifest)},
        "facts": {"revision_sha256": fact_revision_sha256, "count": fact_count},
        "entities": {
            "revision_sha256": entity_revision_sha256,
            "count": len(speech_lexicon.entries or []),
        },
        "speech_lexicon": {
            "artifact_id": str(speech_lexicon.id),
            "compiler_version": speech_lexicon.compiler_version,
            "source_revision_sha256": lexicon_source_revision,
            "content_sha256": speech_lexicon.content_sha256,
        },
        "sources": source_manifest,
    }
    content_sha256 = _sha256(
        {
            "compiler_version": SERVING_COMPILER_VERSION,
            "knowledge": {
                "name": knowledge_base.name,
                "description": knowledge_base.description,
                "content": knowledge_base.content,
                "scope_type": knowledge_base.scope_type,
                "scope_label": knowledge_base.scope_label,
                "languages": knowledge_base.languages or [],
                "tags": knowledge_base.tags or [],
                "provider": knowledge_base.provider,
                "provider_knowledge_base_id": knowledge_base.provider_knowledge_base_id,
            },
            "manifest": manifest,
        }
    )
    revision_id = uuid.uuid5(
        _SERVING_NAMESPACE,
        f"{knowledge_base.tenant_id}:{knowledge_base.id}:{content_sha256}",
    )
    return ServingRevisionBuild(
        revision_id=revision_id,
        source_revision_sha256=source_revision_sha256,
        chunk_revision_sha256=chunk_revision_sha256,
        fact_revision_sha256=fact_revision_sha256,
        entity_revision_sha256=entity_revision_sha256,
        content_sha256=content_sha256,
        sources=tuple(snapshots),
        chunk_count=len(chunk_manifest),
        fact_count=fact_count,
        entity_count=len(speech_lexicon.entries or []),
        manifest=manifest,
    )


def validate_serving_revision_integrity(
    revision: KnowledgeServingRevision,
    speech_lexicon: KnowledgeSpeechLexicon,
) -> None:
    """Fail closed if a retained release is not the immutable artifact published.

    Historical reactivation does not have the mutable draft used by
    ``build_serving_revision``. Recompute every hash that can be derived from
    the self-contained release rows and verify the remaining manifest links
    before an operator can move the live pointer back to it.
    """

    def invalid(message: str) -> None:
        raise KnowledgeServingError(
            f"Stored serving revision failed integrity validation: {message}"
        )

    if (
        speech_lexicon.tenant_id != revision.tenant_id
        or speech_lexicon.knowledge_base_id != revision.knowledge_base_id
        or speech_lexicon.id != revision.speech_lexicon_artifact_id
    ):
        invalid("speech lexicon ownership does not match")

    source_revisions = list(speech_lexicon.source_revisions or [])
    lexicon_revision_payload = {
        "knowledge_base_id": str(revision.knowledge_base_id),
        "name": str(revision.knowledge_name or ""),
        "scope_type": str(revision.scope_type or ""),
        "scope_label": str(revision.scope_label or ""),
        "languages": sorted(str(item) for item in (revision.languages or []) if item),
        "tags": sorted(str(item) for item in (revision.tags or []) if item),
        "sources": source_revisions,
    }
    if _sha256(lexicon_revision_payload) != speech_lexicon.source_revision_sha256:
        invalid("speech lexicon source hash does not match")
    expected_lexicon_id = uuid.uuid5(
        uuid.NAMESPACE_URL,
        ":".join(
            (
                "vav-speech-lexicon",
                str(revision.tenant_id),
                str(revision.knowledge_base_id),
                speech_lexicon.source_revision_sha256,
                speech_lexicon.compiler_version,
            )
        ),
    )
    if expected_lexicon_id != speech_lexicon.id:
        invalid("speech lexicon ID does not match its source revision")
    expected_lexicon_content_hash = _sha256(
        {
            "artifact_id": str(speech_lexicon.id),
            "compiler_version": speech_lexicon.compiler_version,
            "knowledge_base_id": str(revision.knowledge_base_id),
            "source_revision_sha256": speech_lexicon.source_revision_sha256,
            "source_revisions": source_revisions,
            "entries": list(speech_lexicon.entries or []),
            "coverage": dict(speech_lexicon.coverage or {}),
        }
    )
    if expected_lexicon_content_hash != speech_lexicon.content_sha256:
        invalid("speech lexicon content hash does not match")

    manifest = revision.manifest
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        invalid("manifest schema is unsupported")
    manifest_sources = manifest.get("sources")
    if not isinstance(manifest_sources, list) or not all(
        isinstance(item, dict) for item in manifest_sources
    ):
        invalid("source manifest is malformed")

    sources = sorted(revision.sources, key=lambda item: str(item.original_source_id))
    if not sources or revision.source_count != len(sources):
        invalid("source count does not match")
    source_manifest_by_id = {
        str(item.get("source_id")): item for item in manifest_sources if item.get("source_id")
    }
    if len(source_manifest_by_id) != len(manifest_sources) or set(source_manifest_by_id) != {
        str(source.original_source_id) for source in sources
    }:
        invalid("source manifest identity does not match snapshots")

    chunk_manifest: list[dict[str, Any]] = []
    fact_manifest: list[dict[str, Any]] = []
    fact_count = 0
    for source in sources:
        content = str(source.content or "")
        content_sha256 = _sha256(content)
        if content_sha256 != source.content_sha256:
            invalid(f"source {source.original_source_id} content hash does not match")
        source_manifest = source_manifest_by_id[str(source.original_source_id)]
        expected_source_fields = {
            "source_type": source.source_type,
            "name": source.name,
            "location": source.location,
            "content_sha256": content_sha256,
        }
        if any(source_manifest.get(key) != value for key, value in expected_source_fields.items()):
            invalid(f"source {source.original_source_id} manifest does not match")
        chunks = _chunks(content)
        if source.chunk_count != len(chunks):
            invalid(f"source {source.original_source_id} chunk count does not match")
        chunk_manifest.extend(
            {
                "source_id": str(source.original_source_id),
                "ordinal": ordinal,
                "sha256": _sha256(chunk),
            }
            for ordinal, chunk in enumerate(chunks)
        )
        structured_content = (
            source.structured_content if isinstance(source.structured_content, dict) else None
        )
        source_fact_count = _fact_count(structured_content)
        fact_count += source_fact_count
        fact_manifest.append(
            {
                "source_id": str(source.original_source_id),
                "structured_sha256": _sha256(structured_content or {}),
                "fact_count": source_fact_count,
            }
        )

    calculated = {
        "source": (_sha256(manifest_sources), len(sources)),
        "chunks": (_sha256(chunk_manifest), len(chunk_manifest)),
        "facts": (_sha256(fact_manifest), fact_count),
        "entities": (speech_lexicon.content_sha256, len(speech_lexicon.entries or [])),
    }
    stored = {
        "source": (revision.source_revision_sha256, revision.source_count),
        "chunks": (revision.chunk_revision_sha256, revision.chunk_count),
        "facts": (revision.fact_revision_sha256, revision.fact_count),
        "entities": (revision.entity_revision_sha256, revision.entity_count),
    }
    if calculated != stored:
        invalid("release component hashes or counts do not match")
    for section, (digest, count) in stored.items():
        value = manifest.get(section)
        if (
            not isinstance(value, dict)
            or value.get("revision_sha256") != digest
            or value.get("count") != count
        ):
            invalid(f"{section} manifest does not match")
    lexicon_manifest = manifest.get("speech_lexicon")
    if not isinstance(lexicon_manifest, dict) or lexicon_manifest != {
        "artifact_id": str(speech_lexicon.id),
        "compiler_version": speech_lexicon.compiler_version,
        "source_revision_sha256": speech_lexicon.source_revision_sha256,
        "content_sha256": speech_lexicon.content_sha256,
    }:
        invalid("speech lexicon manifest does not match")

    content_sha256 = _sha256(
        {
            "compiler_version": revision.compiler_version,
            "knowledge": {
                "name": revision.knowledge_name,
                "description": revision.knowledge_description,
                "content": revision.knowledge_content,
                "scope_type": revision.scope_type,
                "scope_label": revision.scope_label,
                "languages": revision.languages or [],
                "tags": revision.tags or [],
                "provider": revision.provider,
                "provider_knowledge_base_id": revision.provider_knowledge_base_id,
            },
            "manifest": manifest,
        }
    )
    if content_sha256 != revision.content_sha256:
        invalid("aggregate content hash does not match")
    expected_revision_id = uuid.uuid5(
        _SERVING_NAMESPACE,
        f"{revision.tenant_id}:{revision.knowledge_base_id}:{content_sha256}",
    )
    if expected_revision_id != revision.id:
        invalid("revision ID does not match its content")


async def publish_serving_revision(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    knowledge_base: KnowledgeBase,
    speech_lexicon: KnowledgeSpeechLexicon,
    published_by_user_id: UUID | None = None,
    allow_draft_for_approval: bool = False,
) -> KnowledgeServingRevision:
    """Append/reuse one release and atomically move the serving pointer."""

    if knowledge_base.tenant_id != tenant_id:
        raise KnowledgeServingError("Knowledge base does not belong to this tenant")
    locked_knowledge_base = await db.scalar(
        select(KnowledgeBase)
        .where(KnowledgeBase.id == knowledge_base.id, KnowledgeBase.tenant_id == tenant_id)
        .options(selectinload(KnowledgeBase.sources))
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if locked_knowledge_base is None:
        raise KnowledgeServingError("Knowledge base does not belong to this tenant")
    knowledge_base = locked_knowledge_base
    if knowledge_base.approval_status != "approved" and not allow_draft_for_approval:
        raise KnowledgeServingError("Approve the knowledge base before publishing a release")
    if knowledge_base.sync_status != "ready":
        raise KnowledgeServingError("Every source must be searchable before publishing a release")
    if (
        speech_lexicon.tenant_id != tenant_id
        or speech_lexicon.knowledge_base_id != knowledge_base.id
    ):
        raise KnowledgeServingError("Speech lexicon does not belong to this knowledge base")
    build = build_serving_revision(knowledge_base, speech_lexicon)
    existing = await db.scalar(
        select(KnowledgeServingRevision)
        .where(
            KnowledgeServingRevision.id == build.revision_id,
            KnowledgeServingRevision.tenant_id == tenant_id,
            KnowledgeServingRevision.knowledge_base_id == knowledge_base.id,
        )
        .options(selectinload(KnowledgeServingRevision.sources))
    )
    if existing is not None:
        if (
            existing.content_sha256 != build.content_sha256
            or existing.speech_lexicon_artifact_id != speech_lexicon.id
            or existing.source_revision_sha256 != build.source_revision_sha256
            or existing.chunk_revision_sha256 != build.chunk_revision_sha256
            or existing.fact_revision_sha256 != build.fact_revision_sha256
            or existing.entity_revision_sha256 != build.entity_revision_sha256
            or existing.source_count != len(build.sources)
            or existing.chunk_count != build.chunk_count
            or existing.fact_count != build.fact_count
            or existing.entity_count != build.entity_count
            or len(existing.sources) != len(build.sources)
            or {
                (str(source.original_source_id), source.content_sha256)
                for source in existing.sources
            }
            != {(str(source.original_source_id), source.content_sha256) for source in build.sources}
        ):
            raise KnowledgeServingError("Stored serving revision failed integrity validation")
        revision = existing
    else:
        now = datetime.now(UTC)
        revision = KnowledgeServingRevision(
            id=build.revision_id,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base.id,
            speech_lexicon_artifact_id=speech_lexicon.id,
            compiler_version=SERVING_COMPILER_VERSION,
            source_revision_sha256=build.source_revision_sha256,
            chunk_revision_sha256=build.chunk_revision_sha256,
            fact_revision_sha256=build.fact_revision_sha256,
            entity_revision_sha256=build.entity_revision_sha256,
            content_sha256=build.content_sha256,
            published_at=now,
            published_by_user_id=published_by_user_id,
            source_count=len(build.sources),
            chunk_count=build.chunk_count,
            fact_count=build.fact_count,
            entity_count=build.entity_count,
            knowledge_name=knowledge_base.name,
            knowledge_description=knowledge_base.description,
            knowledge_content=knowledge_base.content,
            scope_type=knowledge_base.scope_type,
            scope_label=knowledge_base.scope_label,
            languages=list(knowledge_base.languages or []),
            tags=list(knowledge_base.tags or []),
            provider=knowledge_base.provider,
            provider_knowledge_base_id=knowledge_base.provider_knowledge_base_id,
            manifest=build.manifest,
        )
        revision.sources = [
            KnowledgeServingRevisionSource(
                tenant_id=tenant_id,
                knowledge_base_id=knowledge_base.id,
                original_source_id=source.original_source_id,
                source_type=source.source_type,
                name=source.name,
                location=source.location,
                content=source.content,
                structured_content=source.structured_content,
                content_sha256=source.content_sha256,
                compiled_at=source.compiled_at,
                source_metadata=source.source_metadata,
                chunk_count=source.chunk_count,
            )
            for source in build.sources
        ]
        db.add(revision)
        await db.flush()
    knowledge_base.serving_revision_id = revision.id
    knowledge_base.speech_lexicon_artifact_id = speech_lexicon.id
    return revision


async def load_agent_serving_revision(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    serving_revision_id: UUID | None = None,
) -> KnowledgeServingRevision | None:
    """Resolve one immutable release for a tenant-owned agent binding.

    With no explicit ID this resolves the knowledge base's current serving
    pointer exactly once. An explicit ID may be a historical release, but it
    must belong to the same tenant and the knowledge base still bound to the
    agent. This is the per-call pinning contract: an approval during a call
    cannot move that call to a different corpus.
    """

    revision, _revocation_generation = await load_agent_serving_revision_identity(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        serving_revision_id=serving_revision_id,
        include_sources=True,
    )
    return revision


async def load_agent_serving_revision_identity(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    serving_revision_id: UUID | None = None,
    include_sources: bool = False,
) -> tuple[KnowledgeServingRevision | None, int | None]:
    """Resolve a revision and its revocation fence from one DB snapshot.

    Call admission and native session startup need only the immutable identity
    columns.  Keep source loading explicit so a large compiled website is not
    transferred on the telephony answer path. LiveKit/browser callers that
    inspect ``revision.sources`` must opt into eager loading.
    """

    query = (
        select(
            KnowledgeServingRevision,
            KnowledgeBase.serving_revocation_generation,
        )
        .join(KnowledgeBase, KnowledgeBase.id == KnowledgeServingRevision.knowledge_base_id)
        .join(
            AgentKnowledgeBinding,
            AgentKnowledgeBinding.knowledge_base_id == KnowledgeBase.id,
        )
        .where(
            AgentKnowledgeBinding.tenant_id == tenant_id,
            AgentKnowledgeBinding.agent_id == agent_id,
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.is_active.is_(True),
            KnowledgeServingRevision.tenant_id == tenant_id,
            KnowledgeServingRevision.knowledge_base_id == KnowledgeBase.id,
        )
    )
    if include_sources:
        query = query.options(selectinload(KnowledgeServingRevision.sources))
    if serving_revision_id is None:
        query = query.where(KnowledgeBase.serving_revision_id == KnowledgeServingRevision.id)
    else:
        query = query.where(KnowledgeServingRevision.id == serving_revision_id)
    row = (await db.execute(query)).one_or_none()
    if row is None:
        return None, None
    revision, revocation_generation = row
    return revision, revocation_generation


async def load_durably_admitted_serving_revision(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    knowledge_base_id: UUID,
    serving_revision_id: UUID,
    include_sources: bool = False,
) -> KnowledgeServingRevision | None:
    """Load an exact, already-admitted release without following a new binding.

    The caller must first validate the durable admission marker on its
    tenant/agent-bound Call row. This loader deliberately ignores later
    pointer, approval, and binding changes: those occurred after the paid call
    crossed its pre-dispatch admission boundary. Source bodies are omitted by
    default so identity validation remains safe on latency-sensitive paths.
    """

    query = select(KnowledgeServingRevision).where(
        KnowledgeServingRevision.id == serving_revision_id,
        KnowledgeServingRevision.tenant_id == tenant_id,
        KnowledgeServingRevision.knowledge_base_id == knowledge_base_id,
    )
    if include_sources:
        query = query.options(selectinload(KnowledgeServingRevision.sources))
    return await db.scalar(query)


async def validate_call_speech_lexicon_reservation(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    knowledge_base_id: UUID,
    revision: KnowledgeServingRevision,
    metadata: object,
) -> None:
    """Validate the exact speech artifact recorded at call admission.

    A revision ID alone proves which text corpus to serve, but it does not make
    the recognition context auditable.  Require the independently persisted
    lexicon ID and content hash to agree with the revision manifest and the
    retained artifact row before media is admitted or resumed.
    """

    artifact_id = speech_lexicon_artifact_id_from_call_metadata(metadata)
    content_sha256 = speech_lexicon_content_sha256_from_call_metadata(metadata)
    if artifact_id is None or content_sha256 is None:
        raise KnowledgeServingError("Knowledge speech lexicon reservation identity is incomplete")
    manifest = revision.manifest if isinstance(revision.manifest, dict) else {}
    lexicon_manifest = manifest.get("speech_lexicon")
    if not isinstance(lexicon_manifest, dict):
        raise KnowledgeServingError("Knowledge speech lexicon manifest is missing")
    if (
        revision.tenant_id != tenant_id
        or revision.knowledge_base_id != knowledge_base_id
        or artifact_id != revision.speech_lexicon_artifact_id
        or content_sha256 != revision.entity_revision_sha256
        or lexicon_manifest.get("artifact_id") != str(artifact_id)
        or lexicon_manifest.get("content_sha256") != content_sha256
    ):
        raise KnowledgeServingError(
            "Knowledge speech lexicon reservation failed integrity validation"
        )
    artifact_identity = (
        await db.execute(
            select(
                KnowledgeSpeechLexicon.id,
                KnowledgeSpeechLexicon.content_sha256,
            ).where(
                KnowledgeSpeechLexicon.id == artifact_id,
                KnowledgeSpeechLexicon.tenant_id == tenant_id,
                KnowledgeSpeechLexicon.knowledge_base_id == knowledge_base_id,
            )
        )
    ).one_or_none()
    if artifact_identity is None or artifact_identity.content_sha256 != content_sha256:
        raise KnowledgeServingError(
            "Knowledge speech lexicon artifact is unavailable or failed integrity validation"
        )


async def _admit_call_knowledge(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    call_id: UUID,
    direction: str,
    providers: tuple[str, ...],
    status: str,
    allow_legacy_livekit: bool,
    admission_state: str,
) -> Call:
    """Cross one provider-neutral immutable knowledge admission boundary.

    Lock order is Call -> KnowledgeBase -> binding. Ordinary publication may
    move the live pointer without invalidating the reserved immutable revision.
    Explicit revocation changes the generation and wins if it commits before
    this boundary.
    """

    if admission_state not in _KNOWLEDGE_ADMISSION_STATES:
        raise KnowledgeServingError("Knowledge admission state is unsupported")
    label = "Outbound" if direction == "outbound" else "Inbound"
    boundary = "dispatch" if direction == "outbound" else "admission"
    call = await db.scalar(
        select(Call)
        .where(
            Call.id == call_id,
            Call.tenant_id == tenant_id,
            Call.agent_id == agent_id,
            Call.direction == direction,
            Call.provider.in_(providers),
            Call.status == status,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if call is None:
        raise KnowledgeServingError(f"{label} knowledge reservation is unavailable")
    metadata = call.call_metadata if isinstance(call.call_metadata, dict) else {}
    revision_id = serving_revision_id_from_call_metadata(metadata)
    knowledge_base_id = serving_knowledge_base_id_from_call_metadata(metadata)
    revocation_generation = serving_revocation_generation_from_call_metadata(metadata)
    if revision_id is None:
        if allow_legacy_livekit and call.provider == "livekit_sip":
            # Bounded rolling-deploy compatibility for LiveKit reservations
            # created before migration 023. The LiveKit worker retains its
            # stricter connect-time admission for these already-persisted
            # calls. Native Twilio media calls never had such a second
            # admission boundary, so a newly admitted Twilio call must always
            # carry the complete immutable identity.
            return call
        raise KnowledgeServingError(f"{label} knowledge reservation identity is missing")
    if knowledge_base_id is None or revocation_generation is None:
        raise KnowledgeServingError(f"{label} knowledge reservation identity is incomplete")

    discovered_binding = await db.scalar(
        select(AgentKnowledgeBinding).where(
            AgentKnowledgeBinding.tenant_id == tenant_id,
            AgentKnowledgeBinding.agent_id == agent_id,
        )
    )
    if discovered_binding is None or discovered_binding.knowledge_base_id != knowledge_base_id:
        raise KnowledgeServingError(f"{label} knowledge binding changed before {boundary}")
    knowledge_revocation_generation = await db.scalar(
        select(KnowledgeBase.serving_revocation_generation)
        .where(
            KnowledgeBase.id == knowledge_base_id,
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.is_active.is_(True),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if knowledge_revocation_generation is None:
        raise KnowledgeServingError(f"{label} knowledge base was removed before {boundary}")
    binding = await db.scalar(
        select(AgentKnowledgeBinding)
        .where(
            AgentKnowledgeBinding.tenant_id == tenant_id,
            AgentKnowledgeBinding.agent_id == agent_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if binding is None or binding.knowledge_base_id != knowledge_base_id:
        raise KnowledgeServingError(f"{label} knowledge binding changed during admission")
    if knowledge_revocation_generation != revocation_generation:
        raise KnowledgeServingError(f"{label} knowledge was revoked before {boundary}")

    revision = await load_durably_admitted_serving_revision(
        db,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        serving_revision_id=revision_id,
        include_sources=False,
    )
    if revision is None:
        raise KnowledgeServingError(f"{label} knowledge revision is unavailable")
    await validate_call_speech_lexicon_reservation(
        db,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        revision=revision,
        metadata=metadata,
    )
    runtime = metadata.get("runtime")
    reserved_runtime = runtime if isinstance(runtime, dict) else {}
    if reserved_runtime.get("knowledge_serving_content_sha256") != revision.content_sha256 or (
        reserved_runtime.get("knowledge_source_revision_sha256") != revision.source_revision_sha256
    ):
        raise KnowledgeServingError(f"{label} knowledge reservation failed integrity validation")
    admitted_at = datetime.now(UTC).isoformat()
    call.call_metadata = {
        **metadata,
        "runtime": {
            **reserved_runtime,
            "knowledge_admission_state": admission_state,
            "knowledge_admitted_at": admitted_at,
        },
    }
    await db.flush()
    return call


async def pre_admit_outbound_knowledge_call(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    call_id: UUID,
) -> Call:
    """Atomically admit one release before a paid telephony side effect."""

    return await _admit_call_knowledge(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        call_id=call_id,
        direction="outbound",
        providers=("livekit_sip", "twilio"),
        status="dispatching",
        allow_legacy_livekit=True,
        admission_state=KNOWLEDGE_ADMISSION_STATE,
    )


async def admit_inbound_twilio_knowledge_call(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    call_id: UUID,
) -> Call:
    """Atomically pin knowledge before returning an inbound media stream.

    Unlike the bounded LiveKit rolling-upgrade path, native Twilio media
    sessions have no later worker admission hook. Every new inbound call must
    therefore carry a complete immutable knowledge identity and cross the
    revocation fence before VAV returns TwiML that can start caller audio.
    """

    return await _admit_call_knowledge(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        call_id=call_id,
        direction="inbound",
        providers=("twilio",),
        status="in_progress",
        allow_legacy_livekit=False,
        admission_state=INBOUND_KNOWLEDGE_ADMISSION_STATE,
    )


async def admit_inbound_livekit_knowledge_call(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    call_id: UUID,
) -> Call:
    """Atomically admit an answered LiveKit SIP call before agent startup.

    The LiveKit participant can already be carrier-billable when the worker is
    dispatched.  Its call row is therefore reserved first, then this boundary
    validates the immutable release and records the same durable inbound
    admission marker used by native media streams.
    """

    return await _admit_call_knowledge(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        call_id=call_id,
        direction="inbound",
        providers=("livekit_sip",),
        status="in_progress",
        allow_legacy_livekit=False,
        admission_state=INBOUND_KNOWLEDGE_ADMISSION_STATE,
    )


async def backfill_approved_serving_revisions(
    db: AsyncSession,
    *,
    tenant_id: UUID | None = None,
    limit: int = 500,
) -> int:
    """Publish serving releases for pre-migration approved knowledge bases."""

    result = await backfill_approved_serving_revisions_batch(
        db,
        tenant_id=tenant_id,
        limit=limit,
    )
    return result.published


@dataclass(frozen=True, slots=True)
class ServingRevisionBackfillBatch:
    selected: int
    published: int
    failed: int


async def backfill_approved_serving_revisions_batch(
    db: AsyncSession,
    *,
    tenant_id: UUID | None = None,
    limit: int = 500,
) -> ServingRevisionBackfillBatch:
    """Backfill one isolated batch and quarantine malformed legacy rows."""

    from app.services.speech_lexicon import SpeechLexiconError, publish_speech_lexicon

    query = (
        select(KnowledgeBase)
        .where(
            KnowledgeBase.is_active.is_(True),
            KnowledgeBase.approval_status == "approved",
            KnowledgeBase.sync_status == "ready",
            KnowledgeBase.serving_revision_id.is_(None),
        )
        .options(
            selectinload(KnowledgeBase.sources),
            selectinload(KnowledgeBase.speech_lexicon),
        )
        .order_by(KnowledgeBase.created_at, KnowledgeBase.id)
        .limit(max(1, min(limit, 5_000)))
        .with_for_update(skip_locked=True)
    )
    if tenant_id is not None:
        query = query.where(KnowledgeBase.tenant_id == tenant_id)
    knowledge_bases = (await db.scalars(query)).all()
    published = 0
    failed = 0
    for knowledge_base in knowledge_bases:
        # Always run the idempotent publisher. It recomputes the source
        # manifest and reuses the artifact only when it exactly matches the
        # locked draft, preventing a stale speech lexicon/new-content release.
        try:
            async with db.begin_nested():
                lexicon = await publish_speech_lexicon(
                    db,
                    tenant_id=knowledge_base.tenant_id,
                    knowledge_base=knowledge_base,
                )
                await publish_serving_revision(
                    db,
                    tenant_id=knowledge_base.tenant_id,
                    knowledge_base=knowledge_base,
                    speech_lexicon=lexicon,
                )
        except (SpeechLexiconError, KnowledgeServingError) as exc:
            failed += 1
            current = await db.scalar(
                select(KnowledgeBase)
                .where(KnowledgeBase.id == knowledge_base.id)
                .execution_options(populate_existing=True)
            )
            if current is not None:
                current.approval_status = "draft"
                current.published_at = None
                current.sync_status = "error"
                current.sync_error = (
                    "Immutable serving-revision backfill requires source repair: " + str(exc)[:500]
                )
        else:
            published += 1
    return ServingRevisionBackfillBatch(
        selected=len(knowledge_bases),
        published=published,
        failed=failed,
    )
