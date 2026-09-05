"""Versioned, provider-neutral speech lexicons for approved knowledge.

The artifact is compiled once when a knowledge revision is approved.  Live
calls load the immutable publication pointer instead of scanning arbitrary
source prose, and provider-specific budgets select whole, ranked terms without
silently cutting a person or company name.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from difflib import SequenceMatcher
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.agent import (
    AgentKnowledgeBinding,
    KnowledgeBase,
    KnowledgeServingRevision,
    KnowledgeSource,
    KnowledgeSpeechLexicon,
)

SPEECH_LEXICON_COMPILER_VERSION = "vav-speech-lexicon-1"
DEFAULT_PROVIDER_TERM_LIMIT = 80
DEFAULT_PROVIDER_CHAR_LIMIT = 1_500
_ELIGIBLE_SOURCE_STATUSES = frozenset({"indexed", "local_only"})
_ENTITY_TYPES = frozenset(
    {"organization", "person", "branch", "location", "service", "product", "other"}
)
_TIER_ONE_TYPES = frozenset({"organization", "person", "branch", "location"})
_ENTITY_PRIORITY = {
    "organization": 1_000,
    "person": 950,
    "branch": 920,
    "location": 900,
    "service": 720,
    "product": 680,
    "other": 400,
}
_TIER_WEIGHT = {1: 100, 2: 30, 3: 10}
_TERM_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_FILE_EXTENSION = re.compile(r"\.(?:docx?|html?|pdf|pptx?|txt|xlsx?)$", re.IGNORECASE)
_URL_OR_PATH = re.compile(r"(?:^[a-z][a-z0-9+.-]*://|[/\\])", re.IGNORECASE)
_TITLE_SEPARATOR = re.compile(r"\s+(?:[-|:–—])\s+")
_PERSON_WITH_TITLE = re.compile(
    r"\b(?:Dr|Doctor|Prof|Professor|Mr|Mrs|Ms)\.?\s+"
    r"((?:[A-Z][\w'’-]{2,})(?:\s+[A-Z][\w'’-]{2,}){1,3})\b",
    re.UNICODE,
)
_ORGANIZATION_NAME = re.compile(
    r"\b((?:[A-Z][\w&'’-]{1,}\s+){1,6}"
    r"(?:Centre|Center|Clinic|Company|Corporation|Factory|Group|Hospital|LLC|Ltd|Trading))\b",
    re.UNICODE,
)
_BRANCH_NAME = re.compile(
    r"\b((?:[A-Z][\w&'’-]{1,}\s+){1,5}(?:Branch|Office|Showroom))\b",
    re.UNICODE,
)
_CAPITALIZED_SEQUENCE = re.compile(
    r"\b(?:[A-Z][\w&'’-]{2,})(?:\s+(?:(?:and|of|the)\s+)?[A-Z][\w&'’-]{2,}){1,3}\b",
    re.UNICODE,
)
_MAX_FALLBACK_ENTITIES_PER_SOURCE = 64
_NOISE_TOKENS = frozenset(
    {
        "base",
        "content",
        "copy",
        "crawl",
        "document",
        "download",
        "extracted",
        "file",
        "final",
        "home",
        "html",
        "indexed",
        "knowledge",
        "local",
        "page",
        "pdf",
        "searchable",
        "source",
        "text",
        "upload",
        "url",
        "vav",
        "voice",
        "web",
        "website",
    }
)
_GENERIC_TITLE_TOKENS = _NOISE_TOKENS | frozenset(
    {"about", "contact", "directory", "faq", "overview", "privacy", "services", "terms"}
)
_LATIN_LANGUAGES = frozenset(
    {"de", "en", "es", "fi", "fr", "id", "it", "ms", "nl", "pl", "pt", "ro", "sv", "tr", "vi"}
)
LANGUAGE_SCRIPT_ALLOWLIST: dict[str, frozenset[str]] = {
    **{language: frozenset({"LATIN"}) for language in _LATIN_LANGUAGES},
    "ar": frozenset({"ARABIC", "LATIN"}),
    "bn": frozenset({"BENGALI", "LATIN"}),
    "el": frozenset({"GREEK", "LATIN"}),
    "gu": frozenset({"GUJARATI", "LATIN"}),
    "hi": frozenset({"DEVANAGARI", "LATIN"}),
    "ja": frozenset({"CJK", "HIRAGANA", "KATAKANA", "LATIN"}),
    "kn": frozenset({"KANNADA", "LATIN"}),
    "ko": frozenset({"CJK", "HANGUL", "LATIN"}),
    "ml": frozenset({"MALAYALAM", "LATIN"}),
    "mr": frozenset({"DEVANAGARI", "LATIN"}),
    "ne": frozenset({"DEVANAGARI", "LATIN"}),
    "or": frozenset({"ORIYA", "LATIN"}),
    "pa": frozenset({"ARABIC", "GURMUKHI", "LATIN"}),
    "ru": frozenset({"CYRILLIC", "LATIN"}),
    "ta": frozenset({"TAMIL", "LATIN"}),
    "te": frozenset({"TELUGU", "LATIN"}),
    "zh": frozenset({"BOPOMOFO", "CJK", "LATIN"}),
}


class SpeechLexiconError(RuntimeError):
    """A safe build/publication failure that should block KB approval."""


@dataclass(frozen=True)
class SpeechLexiconEntry:
    entry_id: str
    canonical: str
    normalized: str
    entity_type: str
    tier: int
    priority: int
    critical: bool
    languages: tuple[str, ...]
    aliases: tuple[str, ...]
    phonetic_keys: tuple[str, ...]
    source_ids: tuple[str, ...]
    evidence_sha256: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "canonical": self.canonical,
            "normalized": self.normalized,
            "entity_type": self.entity_type,
            "tier": self.tier,
            "priority": self.priority,
            "critical": self.critical,
            "languages": list(self.languages),
            "aliases": list(self.aliases),
            "phonetic_keys": list(self.phonetic_keys),
            "source_ids": list(self.source_ids),
            "evidence_sha256": list(self.evidence_sha256),
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> SpeechLexiconEntry:
        return cls(
            entry_id=str(value.get("entry_id") or ""),
            canonical=str(value.get("canonical") or ""),
            normalized=str(value.get("normalized") or ""),
            entity_type=str(value.get("entity_type") or "other"),
            tier=max(1, min(3, int(value.get("tier") or 3))),
            priority=max(0, int(value.get("priority") or 0)),
            critical=bool(value.get("critical")),
            languages=tuple(str(item) for item in (value.get("languages") or []) if item),
            aliases=tuple(str(item) for item in (value.get("aliases") or []) if item),
            phonetic_keys=tuple(str(item) for item in (value.get("phonetic_keys") or []) if item),
            source_ids=tuple(str(item) for item in (value.get("source_ids") or []) if item),
            evidence_sha256=tuple(
                str(item) for item in (value.get("evidence_sha256") or []) if item
            ),
        )


@dataclass(frozen=True)
class SpeechLexiconBuild:
    artifact_id: UUID
    tenant_id: UUID
    knowledge_base_id: UUID
    compiler_version: str
    generated_at: datetime
    source_revision_sha256: str
    content_sha256: str
    source_revisions: tuple[dict[str, Any], ...]
    entries: tuple[SpeechLexiconEntry, ...]
    coverage: dict[str, int | float]


@dataclass(frozen=True)
class ProviderLexiconSelection:
    terms: tuple[str, ...]
    entry_ids: tuple[str, ...]
    coverage: dict[str, int | float]


@dataclass(frozen=True)
class ScriptAssessment:
    expected_language: str
    detected_scripts: tuple[str, ...]
    unexpected_scripts: tuple[str, ...]
    letter_count: int
    unexpected_letter_count: int
    unexpected_ratio: float
    is_unexpected: bool


@dataclass(frozen=True)
class EntityResolution:
    raw_text: str
    canonical: str | None
    entry_id: str | None
    entity_type: str | None
    matched_text: str | None
    confidence: float
    margin: float
    reason: str
    safe_to_apply: bool


def _stable_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256(value: object) -> str:
    if not isinstance(value, str):
        value = _stable_json(value)
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _timestamp(value: object) -> str | None:
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


def _normalized(value: object) -> str:
    return " ".join(token.casefold() for token in _TERM_TOKEN.findall(str(value or "")))


def _latin_fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value)
    return "".join(character for character in decomposed if not unicodedata.combining(character))


def _phonetic_key(value: str) -> str:
    """Return a conservative Latin name signature, never an answer alias."""

    folded = _latin_fold(value).casefold()
    if not folded or any(ord(character) > 127 and character.isalpha() for character in folded):
        return ""
    folded = re.sub(r"[^a-z]+", "", folded)
    if len(folded) < 4:
        return ""
    for source, replacement in (
        ("ph", "f"),
        ("ck", "k"),
        ("qu", "k"),
        ("sh", "s"),
        ("th", "t"),
        ("w", "v"),
    ):
        folded = folded.replace(source, replacement)
    head, tail = folded[0], folded[1:]
    tail = re.sub(r"[aeiouy]", "", tail)
    tail = re.sub(r"(.)\1+", r"\1", tail)
    return (head + tail)[:32]


def _clean_canonical(value: object, *, source_title: bool = False) -> str:
    raw = " ".join(str(value or "").split()).strip(" |,.;:")
    if not raw or _URL_OR_PATH.search(raw):
        return ""
    raw = _FILE_EXTENSION.sub("", raw).replace("_", " ").strip(" |,.;:")
    if source_title:
        parts = _TITLE_SEPARATOR.split(raw)
        useful_parts = [
            part
            for part in parts
            if any(token not in _GENERIC_TITLE_TOKENS for token in _normalized(part).split())
        ]
        raw = max(useful_parts, key=len) if useful_parts else ""
    tokens = _normalized(raw).split()
    while tokens and tokens[0] in _GENERIC_TITLE_TOKENS:
        first = _TERM_TOKEN.search(raw)
        raw = raw[first.end() :].strip(" |,.;:-–—") if first else ""
        tokens = _normalized(raw).split()
    if not raw or not tokens or all(token in _GENERIC_TITLE_TOKENS for token in tokens):
        return ""
    if len(raw) > 180 or len(tokens) > 12 or all(token.isdigit() for token in tokens):
        return ""
    return " ".join(raw.split())


def _source_revision(source: KnowledgeSource) -> dict[str, Any]:
    content = str(source.content or "")
    structured = source.structured_content if isinstance(source.structured_content, dict) else {}
    compiler = structured.get("compiler") if isinstance(structured.get("compiler"), dict) else {}
    return {
        "source_id": str(source.id),
        "source_type": str(source.source_type),
        "status": str(source.status),
        "source_content_sha256": str(source.content_sha256 or _sha256(content)),
        "retrieval_content_sha256": _sha256(content),
        "structured_content_sha256": _sha256(structured),
        "source_compiler_version": str(compiler.get("version") or "unstructured"),
        "compiled_at": _timestamp(source.compiled_at),
        "source_updated_at": _timestamp(source.updated_at),
    }


def _entry_tier(entity_type: str, *, critical: bool) -> int:
    if entity_type in _TIER_ONE_TYPES or critical:
        return 1
    if entity_type in {"service", "product"}:
        return 2
    return 3


def _entry_priority(entity_type: str, *, tier: int, occurrences: int) -> int:
    return (
        _ENTITY_PRIORITY.get(entity_type, _ENTITY_PRIORITY["other"])
        + (120 if tier == 1 else 0)
        + min(occurrences, 20) * 3
    )


def _structured_entity_candidates(source: KnowledgeSource) -> list[dict[str, Any]]:
    structured = source.structured_content if isinstance(source.structured_content, dict) else {}
    candidates = structured.get("speech_entities")
    if not isinstance(candidates, list):
        candidates = structured.get("entities")
    result: list[dict[str, Any]] = []
    if isinstance(candidates, list):
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            canonical = _clean_canonical(candidate.get("canonical") or candidate.get("name"))
            entity_type = str(candidate.get("entity_type") or "other").strip().lower()
            if entity_type not in _ENTITY_TYPES:
                entity_type = "other"
            if not canonical:
                continue
            evidence = str(candidate.get("evidence") or "")
            evidence_hash = str(candidate.get("evidence_sha256") or _sha256(evidence))
            aliases = [
                cleaned
                for value in (candidate.get("aliases") or [])
                if (cleaned := _clean_canonical(value))
                and _normalized(cleaned) != _normalized(canonical)
            ]
            result.append(
                {
                    "canonical": canonical,
                    "entity_type": entity_type,
                    "critical": bool(candidate.get("critical")),
                    "language": str(candidate.get("language") or "und").strip() or "und",
                    "aliases": aliases,
                    "evidence_sha256": evidence_hash,
                }
            )
    return result


def _content_entity_candidates(source: KnowledgeSource) -> list[dict[str, Any]]:
    """Recover conservative proper names from legacy PDF/text sources.

    New AI-compiled sources provide typed, evidence-stamped entities. This
    fallback keeps older directories useful during backfill without turning
    arbitrary URL slugs or every content token into recognition hints.
    """

    content = str(source.content or "")[:120_000]
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(match_value: str, entity_type: str, critical: bool) -> None:
        canonical = _clean_canonical(match_value)
        normalized = _normalized(canonical)
        if (
            not canonical
            or normalized in seen
            or all(token in _GENERIC_TITLE_TOKENS for token in normalized.split())
        ):
            return
        candidates.append(
            {
                "canonical": canonical,
                "entity_type": entity_type,
                "critical": critical,
                "language": "und",
                "aliases": [],
                "evidence_sha256": _sha256(match_value),
            }
        )
        seen.add(normalized)

    for pattern, entity_type, critical in (
        (_PERSON_WITH_TITLE, "person", True),
        (_ORGANIZATION_NAME, "organization", True),
        (_BRANCH_NAME, "branch", True),
        (_CAPITALIZED_SEQUENCE, "other", False),
    ):
        for match in pattern.finditer(content):
            add(match.group(1) if match.lastindex else match.group(0), entity_type, critical)
            if len(candidates) >= _MAX_FALLBACK_ENTITIES_PER_SOURCE:
                return candidates
    return candidates


def _metadata_candidates(knowledge_base: KnowledgeBase, source: KnowledgeSource) -> list[dict]:
    result: list[dict] = []
    for value, entity_type, critical in (
        (knowledge_base.scope_label, "organization", True),
        (source.name, "other", False),
    ):
        canonical = _clean_canonical(value, source_title=value == source.name)
        if canonical:
            result.append(
                {
                    "canonical": canonical,
                    "entity_type": entity_type,
                    "critical": critical,
                    "language": "und",
                    "aliases": [],
                    "evidence_sha256": _sha256(canonical),
                }
            )
    metadata = source.source_metadata if isinstance(source.source_metadata, dict) else {}
    for key in ("title", "page_title", "display_name"):
        canonical = _clean_canonical(metadata.get(key), source_title=True)
        if canonical:
            result.append(
                {
                    "canonical": canonical,
                    "entity_type": "other",
                    "critical": False,
                    "language": "und",
                    "aliases": [],
                    "evidence_sha256": _sha256(canonical),
                }
            )
    return result


def build_speech_lexicon(
    knowledge_base: KnowledgeBase,
    sources: Sequence[KnowledgeSource],
    *,
    generated_at: datetime | None = None,
) -> SpeechLexiconBuild:
    """Build a deterministic artifact for exactly one tenant and source revision."""

    tenant_id = knowledge_base.tenant_id
    knowledge_base_id = knowledge_base.id
    eligible: list[KnowledgeSource] = []
    for source in sources:
        if source.tenant_id != tenant_id or source.knowledge_base_id != knowledge_base_id:
            raise SpeechLexiconError("Speech lexicon sources must belong to the same tenant and KB")
        if source.status in _ELIGIBLE_SOURCE_STATUSES and str(source.content or "").strip():
            eligible.append(source)
    if not eligible:
        raise SpeechLexiconError("No approved searchable sources are available for speech lexicon")
    eligible.sort(key=lambda source: str(source.id))

    source_revisions = tuple(_source_revision(source) for source in eligible)
    revision_payload = {
        "knowledge_base_id": str(knowledge_base_id),
        "name": str(knowledge_base.name or ""),
        "scope_type": str(knowledge_base.scope_type or ""),
        "scope_label": str(knowledge_base.scope_label or ""),
        "languages": sorted(str(item) for item in (knowledge_base.languages or []) if item),
        "tags": sorted(str(item) for item in (knowledge_base.tags or []) if item),
        "sources": source_revisions,
    }
    source_revision_sha256 = _sha256(revision_payload)
    artifact_id = uuid5(
        NAMESPACE_URL,
        ":".join(
            (
                "vav-speech-lexicon",
                str(tenant_id),
                str(knowledge_base_id),
                source_revision_sha256,
                SPEECH_LEXICON_COMPILER_VERSION,
            )
        ),
    )

    aggregate: dict[str, dict[str, Any]] = {}
    for source in eligible:
        structured_candidates = _structured_entity_candidates(source)
        fallback_candidates = _content_entity_candidates(source)
        if structured_candidates:
            # AI extraction improves typing, but it must not become a single
            # point of failure for names present verbatim in approved content.
            # Supplement it with only deterministic high-confidence entity
            # patterns; generic capitalized phrases remain excluded here.
            candidates = [
                *structured_candidates,
                *(
                    candidate
                    for candidate in fallback_candidates
                    if candidate["entity_type"] in _TIER_ONE_TYPES
                ),
                *_metadata_candidates(knowledge_base, source),
            ]
        else:
            candidates = [
                *fallback_candidates,
                *_metadata_candidates(knowledge_base, source),
            ]
        for candidate in candidates:
            canonical = candidate["canonical"]
            normalized = _normalized(canonical)
            if not normalized:
                continue
            current = aggregate.get(normalized)
            candidate_type = candidate["entity_type"]
            candidate_critical = bool(candidate["critical"])
            candidate_tier = _entry_tier(candidate_type, critical=candidate_critical)
            if current is None:
                current = {
                    "canonical": canonical,
                    "entity_type": candidate_type,
                    "tier": candidate_tier,
                    "critical": candidate_critical,
                    "languages": set(),
                    "aliases": set(),
                    "source_ids": set(),
                    "evidence_sha256": set(),
                    "occurrences": 0,
                }
                aggregate[normalized] = current
            elif candidate_tier < current["tier"] or (
                candidate_tier == current["tier"]
                and _ENTITY_PRIORITY[candidate_type] > _ENTITY_PRIORITY[current["entity_type"]]
            ):
                current["canonical"] = canonical
                current["entity_type"] = candidate_type
                current["tier"] = candidate_tier
            current["critical"] = bool(current["critical"] or candidate_critical)
            current["languages"].add(candidate["language"])
            current["aliases"].update(candidate["aliases"])
            current["source_ids"].add(str(source.id))
            current["evidence_sha256"].add(candidate["evidence_sha256"])
            current["occurrences"] += 1

    # A generated alias that points to two canonical entities is unsafe. Drop
    # every colliding alias rather than guessing which entity the caller meant.
    alias_owners = Counter(
        _normalized(alias)
        for current in aggregate.values()
        for alias in current["aliases"]
        if _normalized(alias)
    )
    entries: list[SpeechLexiconEntry] = []
    for normalized, current in aggregate.items():
        aliases = tuple(
            sorted(
                (
                    alias
                    for alias in current["aliases"]
                    if alias_owners[_normalized(alias)] == 1 and _normalized(alias) not in aggregate
                ),
                key=lambda item: item.casefold(),
            )
        )
        phonetic_keys = tuple(
            sorted(
                {key for value in (current["canonical"], *aliases) if (key := _phonetic_key(value))}
            )
        )
        entry_id = uuid5(artifact_id, f"{normalized}:{current['entity_type']}").hex
        entries.append(
            SpeechLexiconEntry(
                entry_id=entry_id,
                canonical=current["canonical"],
                normalized=normalized,
                entity_type=current["entity_type"],
                tier=current["tier"],
                priority=_entry_priority(
                    current["entity_type"],
                    tier=current["tier"],
                    occurrences=current["occurrences"],
                ),
                critical=current["critical"],
                languages=tuple(sorted(current["languages"])),
                aliases=aliases,
                phonetic_keys=phonetic_keys,
                source_ids=tuple(sorted(current["source_ids"])),
                evidence_sha256=tuple(sorted(current["evidence_sha256"])),
            )
        )
    entries.sort(key=lambda entry: (entry.tier, -entry.priority, entry.normalized, entry.entry_id))
    selection = select_provider_terms(entries)
    content_payload = {
        "artifact_id": str(artifact_id),
        "compiler_version": SPEECH_LEXICON_COMPILER_VERSION,
        "knowledge_base_id": str(knowledge_base_id),
        "source_revision_sha256": source_revision_sha256,
        "source_revisions": source_revisions,
        "entries": [entry.as_dict() for entry in entries],
        "coverage": selection.coverage,
    }
    observed_at = generated_at or datetime.now(UTC)
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=UTC)
    return SpeechLexiconBuild(
        artifact_id=artifact_id,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        compiler_version=SPEECH_LEXICON_COMPILER_VERSION,
        generated_at=observed_at.astimezone(UTC),
        source_revision_sha256=source_revision_sha256,
        content_sha256=_sha256(content_payload),
        source_revisions=source_revisions,
        entries=tuple(entries),
        coverage=selection.coverage,
    )


def _coerce_entries(
    entries: Iterable[SpeechLexiconEntry | dict[str, Any]],
) -> tuple[SpeechLexiconEntry, ...]:
    result = []
    for value in entries:
        entry = (
            value if isinstance(value, SpeechLexiconEntry) else SpeechLexiconEntry.from_dict(value)
        )
        if entry.canonical and entry.normalized and entry.entry_id:
            result.append(entry)
    return tuple(result)


def select_provider_terms(
    entries: Iterable[SpeechLexiconEntry | dict[str, Any]],
    *,
    max_terms: int = DEFAULT_PROVIDER_TERM_LIMIT,
    max_chars: int = DEFAULT_PROVIDER_CHAR_LIMIT,
    required_terms: Iterable[str] = (),
) -> ProviderLexiconSelection:
    """Select complete terms deterministically under one provider prompt budget."""

    if max_terms < 1 or max_chars < 1:
        raise ValueError("Provider speech-lexicon budgets must be positive")
    ranked = sorted(
        _coerce_entries(entries),
        key=lambda entry: (entry.tier, -entry.priority, entry.normalized, entry.entry_id),
    )
    candidates: list[tuple[str, str | None, int, int]] = []
    for value in required_terms:
        canonical = _clean_canonical(value)
        if canonical:
            candidates.append((canonical, None, 0, 10_000))
    candidates.extend(
        (entry.canonical, entry.entry_id, entry.tier, entry.priority) for entry in ranked
    )

    terms: list[str] = []
    entry_ids: list[str] = []
    selected_norms: set[str] = set()
    selected_entry_ids: set[str] = set()
    used_chars = 0
    for canonical, entry_id, _tier, _priority in candidates:
        normalized = _normalized(canonical)
        if not normalized:
            continue
        if normalized in selected_norms:
            # A required runtime hint may be the same canonical value as a
            # published entry. It already consumes no additional provider
            # budget, but the immutable entry must still count as selected for
            # coverage and call-revision diagnostics.
            if entry_id is not None and entry_id not in selected_entry_ids:
                entry_ids.append(entry_id)
                selected_entry_ids.add(entry_id)
            continue
        added_chars = len(canonical) + (2 if terms else 0)
        if len(terms) >= max_terms or used_chars + added_chars > max_chars:
            continue
        terms.append(canonical)
        selected_norms.add(normalized)
        used_chars += added_chars
        if entry_id is not None:
            entry_ids.append(entry_id)
            selected_entry_ids.add(entry_id)

    total_weight = sum(_TIER_WEIGHT.get(entry.tier, 1) for entry in ranked)
    selected_weight = sum(
        _TIER_WEIGHT.get(entry.tier, 1) for entry in ranked if entry.entry_id in selected_entry_ids
    )
    tier_one_total = sum(entry.tier == 1 for entry in ranked)
    tier_one_selected = sum(
        entry.tier == 1 and entry.entry_id in selected_entry_ids for entry in ranked
    )
    coverage: dict[str, int | float] = {
        "provider_max_terms": max_terms,
        "provider_max_chars": max_chars,
        "total_entries": len(ranked),
        "selected_entries": len(selected_entry_ids),
        "selected_terms": len(terms),
        "selected_chars": used_chars,
        "tier_one_total": tier_one_total,
        "tier_one_selected": tier_one_selected,
        "tier_one_coverage_pct": round(
            100.0 if not tier_one_total else tier_one_selected * 100 / tier_one_total,
            2,
        ),
        "weighted_coverage_pct": round(
            100.0 if not total_weight else selected_weight * 100 / total_weight,
            2,
        ),
        "omitted_entries": len(ranked) - len(selected_entry_ids),
    }
    return ProviderLexiconSelection(tuple(terms), tuple(entry_ids), coverage)


def _unicode_script(character: str) -> str:
    if not character.isalpha():
        return "COMMON"
    name = unicodedata.name(character, "")
    for script in (
        "LATIN",
        "ARABIC",
        "BENGALI",
        "BOPOMOFO",
        "DEVANAGARI",
        "CYRILLIC",
        "GREEK",
        "GUJARATI",
        "GURMUKHI",
        "HEBREW",
        "HANGUL",
        "HIRAGANA",
        "KANNADA",
        "KATAKANA",
        "MALAYALAM",
        "ORIYA",
        "TAMIL",
        "TELUGU",
        "THAI",
        "CJK",
    ):
        if script in name:
            return script
    return "OTHER"


def detect_unexpected_script(
    transcript: str,
    *,
    expected_language: str,
    allowed_languages: Sequence[str] = (),
    ratio_threshold: float = 0.3,
    span_threshold: int = 3,
) -> ScriptAssessment:
    """Flag an unsupported script or a wrong-script span inside a fixed turn.

    A whole-utterance ratio alone misses the common ASR failure where only a
    person's name is emitted in another script inside an otherwise English
    sentence. A contiguous alphabetic span therefore also trips the guard.
    Multilingual callers may supply an explicit configured language set; only
    scripts outside that set are considered unexpected.
    """

    language = str(expected_language or "").strip().lower().split("-", 1)[0]
    configured_languages = {
        str(value or "").strip().lower().split("-", 1)[0]
        for value in allowed_languages
        if str(value or "").strip() and str(value or "").strip().lower() != "auto"
    }
    if language not in {"", "auto"}:
        configured_languages.add(language)
    counts = Counter(
        script for character in transcript if (script := _unicode_script(character)) != "COMMON"
    )
    letter_count = sum(counts.values())
    if configured_languages:
        mapped = [LANGUAGE_SCRIPT_ALLOWLIST.get(value) for value in configured_languages]
        if any(value is None for value in mapped):
            # Script blocking is a repair aid, not language validation. A new
            # provider language must remain telemetry-only until its Unicode
            # mapping ships; blocking ordinary caller speech would be worse.
            allowed = set(counts)
        else:
            allowed = set().union(*(value for value in mapped if value is not None))
    elif language in {"", "auto"}:
        allowed = set(counts)
    else:
        allowed = set(counts)
    unexpected = tuple(sorted(script for script in counts if script not in allowed))
    unexpected_letters = sum(counts[script] for script in unexpected)
    ratio = unexpected_letters / letter_count if letter_count else 0.0
    longest_unexpected_span = 0
    current_script: str | None = None
    current_length = 0
    for character in transcript:
        script = _unicode_script(character)
        if script in unexpected:
            if script != current_script:
                current_script = script
                current_length = 0
            current_length += 1
            longest_unexpected_span = max(longest_unexpected_span, current_length)
        elif character.isalpha():
            current_script = None
            current_length = 0
    return ScriptAssessment(
        expected_language=expected_language,
        detected_scripts=tuple(sorted(counts)),
        unexpected_scripts=unexpected,
        letter_count=letter_count,
        unexpected_letter_count=unexpected_letters,
        unexpected_ratio=round(ratio, 4),
        is_unexpected=bool(
            letter_count >= 3
            and unexpected
            and (ratio >= ratio_threshold or longest_unexpected_span >= max(1, span_threshold))
        ),
    )


def _candidate_windows(text: str, width: int) -> tuple[str, ...]:
    tokens = _TERM_TOKEN.findall(text)
    if not tokens:
        return ()
    widths = {max(1, width - 1), width, width + 1}
    return tuple(
        " ".join(tokens[start : start + candidate_width])
        for candidate_width in sorted(widths)
        if candidate_width <= len(tokens)
        for start in range(len(tokens) - candidate_width + 1)
    )


def _entity_match_score(candidate: str, alias: str, phonetic_keys: set[str]) -> tuple[float, str]:
    normalized_candidate = _normalized(candidate)
    normalized_alias = _normalized(alias)
    if not normalized_candidate or not normalized_alias:
        return 0.0, "none"
    if normalized_candidate == normalized_alias:
        return 1.0, "exact"
    compact_candidate = normalized_candidate.replace(" ", "")
    compact_alias = normalized_alias.replace(" ", "")
    if min(len(compact_candidate), len(compact_alias)) < 4:
        return 0.0, "none"
    edit_score = SequenceMatcher(None, compact_candidate, compact_alias).ratio()
    token_score = SequenceMatcher(None, normalized_candidate, normalized_alias).ratio()
    candidate_phonetic = _phonetic_key(candidate)
    phonetic_match = bool(candidate_phonetic and candidate_phonetic in phonetic_keys)
    score = edit_score * 0.7 + token_score * 0.3
    if phonetic_match:
        score = max(score, 0.9)
    latin_candidate = _latin_fold(compact_candidate)
    latin_alias = _latin_fold(compact_alias)
    if (
        latin_candidate.isascii()
        and latin_alias.isascii()
        and latin_candidate[:1] != latin_alias[:1]
        and not phonetic_match
    ):
        score = min(score, 0.75)
    return score, "phonetic" if phonetic_match and score >= 0.9 else "fuzzy"


def resolve_canonical_entity(
    transcript: str,
    entries: Iterable[SpeechLexiconEntry | dict[str, Any]],
    *,
    expected_entity_types: Iterable[str] = (),
    minimum_confidence: float = 0.84,
    safe_apply_confidence: float = 0.92,
    safe_apply_margin: float = 0.06,
) -> EntityResolution:
    """Return a non-mutating canonical suggestion suitable for shadow rollout."""

    raw_text = str(transcript or "").strip()
    if not raw_text:
        return EntityResolution(raw_text, None, None, None, None, 0.0, 0.0, "empty", False)
    expected = {str(value).strip().lower() for value in expected_entity_types if value}
    scores: list[tuple[float, SpeechLexiconEntry, str, str]] = []
    normalized_text = _normalized(raw_text)
    for entry in _coerce_entries(entries):
        aliases = (entry.canonical, *entry.aliases)
        best_score = 0.0
        best_match = ""
        best_reason = "none"
        for alias in aliases:
            normalized_alias = _normalized(alias)
            if normalized_alias and re.search(
                rf"(?<![^\W_]){re.escape(normalized_alias)}(?![^\W_])",
                normalized_text,
                flags=re.UNICODE,
            ):
                score, reason, matched = 1.0, "exact", alias
            else:
                score, reason, matched = 0.0, "none", ""
                width = max(1, len(_TERM_TOKEN.findall(alias)))
                for window in _candidate_windows(raw_text, width):
                    candidate_score, candidate_reason = _entity_match_score(
                        window,
                        alias,
                        set(entry.phonetic_keys),
                    )
                    if candidate_score > score:
                        score, reason, matched = candidate_score, candidate_reason, window
            if score > best_score:
                best_score, best_reason, best_match = score, reason, matched
        if expected and entry.entity_type in expected:
            best_score = min(1.0, best_score + 0.02)
        if best_score >= minimum_confidence:
            scores.append((best_score, entry, best_match, best_reason))
    scores.sort(key=lambda item: (-item[0], item[1].tier, -item[1].priority, item[1].normalized))
    if not scores:
        return EntityResolution(raw_text, None, None, None, None, 0.0, 0.0, "no_match", False)
    confidence, entry, matched_text, reason = scores[0]
    second_score = scores[1][0] if len(scores) > 1 else 0.0
    margin = max(0.0, confidence - second_score)
    safe = confidence >= safe_apply_confidence and margin >= safe_apply_margin
    return EntityResolution(
        raw_text=raw_text,
        canonical=entry.canonical,
        entry_id=entry.entry_id,
        entity_type=entry.entity_type,
        matched_text=matched_text,
        confidence=round(confidence, 4),
        margin=round(margin, 4),
        reason=reason if margin >= safe_apply_margin else "ambiguous",
        safe_to_apply=safe,
    )


def snapshot_from_artifact(artifact: KnowledgeSpeechLexicon) -> SpeechLexiconBuild:
    return SpeechLexiconBuild(
        artifact_id=artifact.id,
        tenant_id=artifact.tenant_id,
        knowledge_base_id=artifact.knowledge_base_id,
        compiler_version=artifact.compiler_version,
        generated_at=artifact.generated_at,
        source_revision_sha256=artifact.source_revision_sha256,
        content_sha256=artifact.content_sha256,
        source_revisions=tuple(artifact.source_revisions or []),
        entries=_coerce_entries(artifact.entries or []),
        coverage=dict(artifact.coverage or {}),
    )


async def publish_speech_lexicon(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    knowledge_base: KnowledgeBase,
    allow_draft_for_approval: bool = False,
) -> KnowledgeSpeechLexicon:
    """Append/reuse an immutable artifact and atomically move the KB pointer."""

    if knowledge_base.tenant_id != tenant_id:
        raise SpeechLexiconError("Knowledge base does not belong to this tenant")
    locked_knowledge_base = await db.scalar(
        select(KnowledgeBase)
        .where(KnowledgeBase.id == knowledge_base.id, KnowledgeBase.tenant_id == tenant_id)
        .options(selectinload(KnowledgeBase.sources))
        .execution_options(populate_existing=True)
        .with_for_update()
    )
    if locked_knowledge_base is None:
        raise SpeechLexiconError("Knowledge base does not belong to this tenant")
    knowledge_base = locked_knowledge_base
    if knowledge_base.approval_status != "approved" and not allow_draft_for_approval:
        raise SpeechLexiconError("Approve the knowledge base before publishing its speech lexicon")
    if knowledge_base.sync_status != "ready":
        raise SpeechLexiconError("Every source must be searchable before publishing speech lexicon")
    build = build_speech_lexicon(knowledge_base, tuple(knowledge_base.sources))
    existing = await db.scalar(
        select(KnowledgeSpeechLexicon).where(
            KnowledgeSpeechLexicon.id == build.artifact_id,
            KnowledgeSpeechLexicon.tenant_id == tenant_id,
            KnowledgeSpeechLexicon.knowledge_base_id == knowledge_base.id,
        )
    )
    if existing is not None:
        if existing.content_sha256 != build.content_sha256:
            raise SpeechLexiconError("Stored speech lexicon does not match its immutable revision")
        artifact = existing
    else:
        artifact = KnowledgeSpeechLexicon(
            id=build.artifact_id,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base.id,
            source_revision_sha256=build.source_revision_sha256,
            content_sha256=build.content_sha256,
            compiler_version=build.compiler_version,
            generated_at=build.generated_at,
            source_count=len(build.source_revisions),
            entries=[entry.as_dict() for entry in build.entries],
            source_revisions=list(build.source_revisions),
            coverage=build.coverage,
        )
        db.add(artifact)
        await db.flush()
    knowledge_base.speech_lexicon_artifact_id = artifact.id
    return artifact


async def load_agent_speech_lexicon(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    serving_revision_id: UUID | None = None,
    knowledge_base_id: UUID | None = None,
) -> SpeechLexiconBuild | None:
    """Load the lexicon from the call-pinned or current serving revision.

    A revision-only historical pin is accepted only for the knowledge base
    still bound to the agent. A previously admitted call may also supply its
    immutable knowledge-base ID; that pair is tenant-validated directly so a
    later rebind cannot cause mid-call vocabulary drift.
    """

    if knowledge_base_id is not None and serving_revision_id is None:
        raise ValueError("knowledge_base_id requires an explicit serving_revision_id")

    if serving_revision_id is not None and knowledge_base_id is not None:
        # The caller has already admitted this immutable tenant/KB/revision
        # identity. Do not consult the mutable agent binding again mid-call: a
        # later rebind or explicit revocation must not make an admitted call
        # drift to new knowledge or lose its recognition vocabulary.
        artifact = await db.scalar(
            select(KnowledgeSpeechLexicon)
            .join(
                KnowledgeServingRevision,
                KnowledgeServingRevision.speech_lexicon_artifact_id == KnowledgeSpeechLexicon.id,
            )
            .where(
                KnowledgeServingRevision.id == serving_revision_id,
                KnowledgeServingRevision.tenant_id == tenant_id,
                KnowledgeServingRevision.knowledge_base_id == knowledge_base_id,
                KnowledgeSpeechLexicon.tenant_id == tenant_id,
                KnowledgeSpeechLexicon.knowledge_base_id == knowledge_base_id,
            )
        )
        return snapshot_from_artifact(artifact) if artifact is not None else None

    revision_query = (
        select(KnowledgeSpeechLexicon)
        .join(
            KnowledgeServingRevision,
            KnowledgeServingRevision.speech_lexicon_artifact_id == KnowledgeSpeechLexicon.id,
        )
        .join(
            KnowledgeBase,
            KnowledgeBase.id == KnowledgeServingRevision.knowledge_base_id,
        )
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
            KnowledgeSpeechLexicon.tenant_id == tenant_id,
            KnowledgeSpeechLexicon.knowledge_base_id == KnowledgeBase.id,
        )
    )
    if serving_revision_id is None:
        revision_query = revision_query.where(
            KnowledgeBase.serving_revision_id == KnowledgeServingRevision.id
        )
    else:
        revision_query = revision_query.where(KnowledgeServingRevision.id == serving_revision_id)
    artifact = await db.scalar(revision_query)
    if artifact is not None:
        return snapshot_from_artifact(artifact)

    if serving_revision_id is not None:
        return None

    # Rolling-deploy fallback until the post-migration serving-revision
    # backfill has published every pre-existing approved knowledge base.
    artifact = await db.scalar(
        select(KnowledgeSpeechLexicon)
        .join(
            KnowledgeBase,
            KnowledgeBase.speech_lexicon_artifact_id == KnowledgeSpeechLexicon.id,
        )
        .join(
            AgentKnowledgeBinding,
            AgentKnowledgeBinding.knowledge_base_id == KnowledgeBase.id,
        )
        .where(
            AgentKnowledgeBinding.tenant_id == tenant_id,
            AgentKnowledgeBinding.agent_id == agent_id,
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.is_active.is_(True),
            KnowledgeBase.approval_status == "approved",
            KnowledgeSpeechLexicon.tenant_id == tenant_id,
            KnowledgeSpeechLexicon.knowledge_base_id == KnowledgeBase.id,
        )
    )
    return snapshot_from_artifact(artifact) if artifact is not None else None


async def backfill_approved_speech_lexicons(
    db: AsyncSession,
    *,
    tenant_id: UUID | None = None,
    limit: int = 500,
) -> int:
    """Publish missing artifacts for approved KBs without changing their content."""

    result = await backfill_approved_speech_lexicons_batch(
        db,
        tenant_id=tenant_id,
        limit=limit,
    )
    return result.published


@dataclass(frozen=True, slots=True)
class SpeechLexiconBackfillBatch:
    selected: int
    published: int
    failed: int


async def backfill_approved_speech_lexicons_batch(
    db: AsyncSession,
    *,
    tenant_id: UUID | None = None,
    limit: int = 500,
) -> SpeechLexiconBackfillBatch:
    """Backfill one isolated batch and quarantine malformed legacy rows."""

    query = (
        select(KnowledgeBase)
        .where(
            KnowledgeBase.is_active.is_(True),
            KnowledgeBase.approval_status == "approved",
            KnowledgeBase.sync_status == "ready",
            KnowledgeBase.speech_lexicon_artifact_id.is_(None),
        )
        .options(selectinload(KnowledgeBase.sources))
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
        try:
            async with db.begin_nested():
                await publish_speech_lexicon(
                    db,
                    tenant_id=knowledge_base.tenant_id,
                    knowledge_base=knowledge_base,
                )
        except SpeechLexiconError as exc:
            # One corrupt pre-migration row must not roll back every later KB or
            # be selected forever. Move it visibly back to repair/review.
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
                    "Immutable speech-lexicon backfill requires source repair: " + str(exc)[:500]
                )
        else:
            published += 1
    return SpeechLexiconBackfillBatch(
        selected=len(knowledge_bases),
        published=published,
        failed=failed,
    )
