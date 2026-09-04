"""Deterministic retrieval for small, high-value facts used during voice calls.

The general knowledge ranker is intentionally broad.  This module provides a
separate Tier-1 path for facts where a wrong entity or value is materially
worse than returning no answer: contact details, opening hours, leadership and
service offerings.  It consumes only compiler-verified structured facts and
returns an explicit response action plus stable evidence identifiers.

No model call is made here.  Index construction is deterministic and the
immutable result can be cached safely within an API or realtime worker process.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections import OrderedDict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from threading import Lock
from time import monotonic, perf_counter
from typing import Any
from uuid import UUID

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import (
    AgentKnowledgeBinding,
    KnowledgeBase,
    KnowledgeServingRevision,
    KnowledgeServingRevisionSource,
    KnowledgeSource,
)
from app.services.exact_fact_protocol import ExactFactWireFact, encode_exact_fact_evidence

MIN_EVIDENCE_CONTEXT_CHARS = 600
DEFAULT_EVIDENCE_CONTEXT_CHARS = 800
MAX_EVIDENCE_CONTEXT_CHARS = 900
# A website crawl is capped at 500 pages.  The exact-fact lane must cover the
# same bounded corpus: a smaller, silent cap can turn a valid fact on page 257
# into a false "not found" refusal.
MAX_INDEX_SOURCES = 500
MAX_FACTS_PER_SOURCE = 200
MAX_INDEX_FACTS = 2_048
MAX_RESPONSE_FACTS = 5

_ELIGIBLE_SOURCE_STATUSES = ("processing", "indexed", "local_only")
_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_GROUNDING_RE = re.compile(r"[^\w]+", re.UNICODE)
_PHONE_RE = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")
_SAFE_EXTERNAL_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}")


class ExactFactType(StrEnum):
    PHONE = "phone"
    ADDRESS = "address"
    HOURS = "hours"
    LEADERSHIP = "leadership"
    SERVICES = "services"
    FOUNDING = "founding"


class ExactFactResponseAction(StrEnum):
    """The safe next step for a caller-facing orchestration layer."""

    ANSWER = "answer"
    CLARIFY = "clarify"
    REFUSE = "refuse"
    FALLBACK = "fallback"


@dataclass(frozen=True)
class ExactFactProvenance:
    source_id: str
    source_name: str
    source_url: str | None
    content_sha256: str | None
    compiled_at: str | None
    compiler_version: str | None


@dataclass(frozen=True)
class ExactFact:
    evidence_id: str
    fact_type: ExactFactType
    subject: str
    predicate: str
    value: str
    evidence: str
    search_phrases: tuple[str, ...]
    aliases: tuple[str, ...]
    provenance: ExactFactProvenance


@dataclass(frozen=True)
class ExactFactEvidence:
    evidence_id: str
    fact_type: ExactFactType
    subject: str
    predicate: str
    value: str
    quote: str
    provenance: ExactFactProvenance


@dataclass(frozen=True)
class ExactFactIndex:
    revision: str
    facts: tuple[ExactFact, ...]
    knowledge_base_id: str | None = None
    eligible_source_count: int = 0
    indexed_source_count: int = 0
    index_truncated: bool = False
    truncation_reasons: tuple[str, ...] = ()
    absence_authoritative: bool = True

    @property
    def fact_count(self) -> int:
        return len(self.facts)


@dataclass(frozen=True)
class ExactFactLoadDiagnostics:
    binding_lookup_ms: float = 0.0
    revision_lookup_ms: float = 0.0
    cache_lookup_ms: float = 0.0
    source_load_ms: float = 0.0
    index_build_ms: float = 0.0
    total_ms: float = 0.0
    cache_hit: bool = False
    source_count: int = 0
    eligible_source_count: int = 0
    indexed_source_count: int = 0
    fact_count: int = 0
    index_truncated: bool = False
    truncation_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExactFactIndexLoad:
    index: ExactFactIndex | None
    diagnostics: ExactFactLoadDiagnostics


@dataclass(frozen=True)
class ExactFactDiagnostics:
    preclassification_ms: float
    resolution_ms: float
    total_ms: float
    skipped_database: bool
    load: ExactFactLoadDiagnostics | None = None


@dataclass(frozen=True)
class ExactFactResolution:
    response_action: ExactFactResponseAction
    intents: tuple[ExactFactType, ...]
    evidence: tuple[ExactFactEvidence, ...]
    evidence_context: str | None
    reason: str
    candidate_count: int = 0
    diagnostics: ExactFactDiagnostics | None = None

    @property
    def evidence_ids(self) -> tuple[str, ...]:
        return tuple(item.evidence_id for item in self.evidence)


# A concise alias is useful to callers that prefer "result" terminology.
ExactFactResult = ExactFactResolution


@dataclass(frozen=True)
class ExactFactSource:
    source_id: str | UUID
    source_name: str
    structured_content: Mapping[str, Any] | None
    source_url: str | None = None
    content_sha256: str | None = None
    compiled_at: datetime | str | None = None
    updated_at: datetime | str | None = None


_QUERY_STOP_WORDS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "at",
    "can",
    "could",
    "did",
    "do",
    "does",
    "for",
    "from",
    "give",
    "has",
    "have",
    "how",
    "i",
    "in",
    "is",
    "it",
    "me",
    "my",
    "of",
    "on",
    "our",
    "please",
    "tell",
    "that",
    "the",
    "their",
    "them",
    "they",
    "this",
    "to",
    "us",
    "was",
    "we",
    "what",
    "when",
    "where",
    "which",
    "who",
    "would",
    "you",
    "your",
}
_GENERIC_ENTITY_TOKENS = {
    "business",
    "company",
    "corporation",
    "group",
    "main",
    "official",
    "organisation",
    "organization",
    "primary",
}
_INTENT_QUERY_TOKENS = {
    ExactFactType.PHONE: {
        "call",
        "contact",
        "dial",
        "mobile",
        "number",
        "phone",
        "reach",
        "telephone",
    },
    ExactFactType.ADDRESS: {
        "address",
        "based",
        "direction",
        "directions",
        "find",
        "located",
        "location",
        "office",
        "visit",
    },
    ExactFactType.HOURS: {
        "close",
        "closed",
        "closing",
        "hour",
        "hours",
        "open",
        "opening",
        "schedule",
        "time",
        "times",
        "timing",
        "timings",
        "working",
    },
    ExactFactType.LEADERSHIP: {
        "board",
        "ceo",
        "cfo",
        "chair",
        "chairman",
        "chairperson",
        "chairwoman",
        "chief",
        "director",
        "executive",
        "founder",
        "head",
        "leader",
        "leadership",
        "management",
        "manager",
        "president",
        "role",
        "run",
        "runs",
    },
    ExactFactType.SERVICES: {
        "do",
        "offer",
        "offering",
        "offers",
        "provide",
        "provides",
        "service",
        "services",
        "specialise",
        "specialises",
        "specialize",
        "specializes",
        "treatment",
        "treatments",
    },
    ExactFactType.FOUNDING: {
        "began",
        "begin",
        "established",
        "establishment",
        "formed",
        "formation",
        "founded",
        "founding",
        "incorporated",
        "inception",
        "launched",
        "started",
        "year",
    },
}
_ALL_INTENT_TOKENS = set().union(*_INTENT_QUERY_TOKENS.values())

_INTENT_PATTERNS: tuple[tuple[ExactFactType, tuple[re.Pattern[str], ...]], ...] = (
    (
        ExactFactType.PHONE,
        (
            re.compile(r"\b(?:phone|telephone|mobile)(?:\s+number)?\b"),
            re.compile(r"\bcontact\s+(?:phone\s+)?number\b"),
            re.compile(
                r"\b(?:call|dial|reach)\b.{0,80}\b"
                r"(?:company|group|office|clinic|centre|center|number|phone|telephone)\b"
            ),
        ),
    ),
    (
        ExactFactType.ADDRESS,
        (
            re.compile(r"\b(?:address|location|directions?)\b"),
            re.compile(r"\bwhere\b.*\b(?:based|find|located|office|visit)\b"),
        ),
    ),
    (
        ExactFactType.HOURS,
        (
            re.compile(r"\b(?:business|opening|office|working)\s+(?:hours?|times?|timings?)\b"),
            re.compile(r"\b(?:close|closed|closing|hours?|schedule|timings?)\b"),
            re.compile(r"\b(?:are|is|when|what\s+time)\b.{0,80}\bopen\b"),
        ),
    ),
    (
        ExactFactType.LEADERSHIP,
        (
            re.compile(
                r"\b(?:board|ceo|cfo|chairman|chairperson|chairwoman|director|founder|"
                r"leadership|management|manager|president)\b"
            ),
            re.compile(r"\bwho\s+(?:runs?|leads?|manages?|founded)\b"),
            re.compile(r"\b(?:chief\s+(?:executive|financial)|head\s+of)\b"),
        ),
    ),
    (
        ExactFactType.SERVICES,
        (
            re.compile(r"\b(?:services?|offerings?|treatments?)\b"),
            re.compile(
                r"\b(?:offer|offers|provide|provides|speciali[sz]e|speciali[sz]es)\b"
                r".{0,80}\b(?:care|procedure|product|solution)\b"
            ),
            re.compile(r"\bwhat\s+(?:do|does)\b.*\bdo\b"),
        ),
    ),
    (
        ExactFactType.FOUNDING,
        (
            re.compile(r"\b(?:founding|inception|establishment|formation)\s+(?:date|year)\b"),
            re.compile(
                r"\b(?:when|what\s+year|which\s+year)\b.{0,100}\b"
                r"(?:began|begin|established|formed|founded|incorporated|launched|started)\b"
            ),
            re.compile(
                r"\b(?:was|is)\b.{0,100}\b"
                r"(?:established|formed|founded|incorporated|launched|started)\s+(?:in|on)\b"
            ),
        ),
    ),
)

_FACT_TYPE_TERMS: dict[ExactFactType, tuple[str, ...]] = {
    ExactFactType.PHONE: (
        "phone",
        "telephone",
        "mobile",
        "contact number",
        "primary tel",
    ),
    ExactFactType.ADDRESS: (
        "address",
        "physical address",
        "location",
        "located",
        "headquarters",
        "registered office",
    ),
    ExactFactType.HOURS: (
        "hours",
        "opening",
        "closing",
        "open time",
        "business time",
        "working time",
        "schedule",
        "timing",
    ),
    ExactFactType.LEADERSHIP: (
        "board member",
        "ceo",
        "cfo",
        "chairman",
        "chairperson",
        "chairwoman",
        "chief executive",
        "chief financial",
        "director",
        "founder",
        "leadership",
        "management",
        "managing director",
        "president",
        "role",
    ),
    ExactFactType.SERVICES: (
        "service",
        "offering",
        "provides",
        "treatment",
        "speciality",
        "specialty",
        "capability",
    ),
    ExactFactType.FOUNDING: (
        "established in",
        "establishment date",
        "establishment year",
        "formation date",
        "formation year",
        "founded in",
        "founding date",
        "founding year",
        "incorporated in",
        "incorporation date",
        "inception date",
        "inception year",
        "launched in",
        "started in",
    ),
}
_FOUNDING_YEAR_RE = re.compile(r"(?:18|19|20)\d{2}")
_FOUNDING_PREDICATES = frozenset(
    {
        "establishment year",
        "formation year",
        "founding year",
        "incorporation year",
        "inception year",
        "year established",
        "year formed",
        "year founded",
        "year incorporated",
    }
)

_LEADERSHIP_ROLE_PATTERN = (
    r"(?:board\s+member|ceo|cfo|chairman|chairperson|chairwoman|chief\s+executive(?:\s+officer)?|"
    r"chief\s+financial(?:\s+officer)?|director|founder|managing\s+director|president)"
)

_NON_NAME_QUESTION_PREFIXES = {
    "a",
    "an",
    "my",
    "our",
    "the",
    "their",
    "this",
    "your",
}
_NON_NAME_QUESTION_TERMS = {
    "business",
    "company",
    "insurance",
    "organisation",
    "organization",
    "provider",
    "responsible",
    "service",
}


def _clean_text(value: object, *, limit: int = 4_000) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()[:limit]


def _validate_evidence_context_limit(max_evidence_chars: int) -> None:
    if not MIN_EVIDENCE_CONTEXT_CHARS <= max_evidence_chars <= MAX_EVIDENCE_CONTEXT_CHARS:
        raise ValueError(
            "max_evidence_chars must be between "
            f"{MIN_EVIDENCE_CONTEXT_CHARS} and {MAX_EVIDENCE_CONTEXT_CHARS}"
        )


def _normalized(value: object) -> str:
    return _clean_text(unicodedata.normalize("NFKC", str(value or ""))).casefold()


def _grounding_normalized(value: object) -> str:
    return _SPACE_RE.sub(" ", _GROUNDING_RE.sub(" ", _normalized(value))).strip()


def _tokens(value: object) -> set[str]:
    return {token.casefold() for token in _TOKEN_RE.findall(_normalized(value)) if len(token) > 1}


def _isoformat(value: datetime | str | None) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    cleaned = _clean_text(value, limit=100)
    return cleaned or None


def _deduplicated_text(values: Iterable[object], *, limit: int) -> tuple[str, ...]:
    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = _clean_text(value, limit=500)
        folded = _normalized(cleaned)
        if not cleaned or not folded or folded in seen:
            continue
        selected.append(cleaned)
        seen.add(folded)
        if len(selected) >= limit:
            break
    return tuple(selected)


def _list_values(value: object) -> tuple[object, ...]:
    if isinstance(value, (list, tuple, set)):
        return tuple(value)
    if value is None:
        return ()
    return (value,)


def _value_is_grounded(value: str, evidence: str) -> bool:
    normalized_value = _grounding_normalized(value)
    normalized_evidence = _grounding_normalized(evidence)
    if normalized_value and normalized_value in normalized_evidence:
        return True
    value_digits = "".join(character for character in value if character.isdigit())
    evidence_digits = "".join(character for character in evidence if character.isdigit())
    return len(value_digits) >= 7 and value_digits in evidence_digits


def _subject_is_grounded(subject: str, evidence: str) -> bool:
    normalized_subject = _grounding_normalized(subject)
    normalized_evidence = _grounding_normalized(evidence)
    return bool(normalized_subject and normalized_subject in normalized_evidence)


def _founding_relationship_is_grounded(*, subject: str, value: str, evidence: str) -> bool:
    """Prove a bounded organization -> founding-year clause in the quoted evidence."""

    if _FOUNDING_YEAR_RE.fullmatch(_normalized(value)) is None:
        return False
    normalized_subject = _grounding_normalized(subject)
    subject_pattern = re.escape(normalized_subject)
    year_pattern = re.escape(_normalized(value))
    # Split raw punctuation first. Grounding normalization intentionally drops
    # punctuation, which would otherwise let a different company's statement
    # in the next sentence appear to support this subject.
    for raw_clause in re.split(r"[.!?;\n]+", str(evidence or "")):
        clause = _grounding_normalized(raw_clause)
        if not clause:
            continue
        patterns = (
            # "Acme was established/founded/incorporated in 2003."
            rf"\b{subject_pattern}\b\s+(?:was|is)\s+(?:officially\s+)?"
            rf"(?:established|formed|founded|incorporated|launched)\s+"
            rf"(?:in\s+)?(?:the\s+year\s+)?{year_pattern}\b",
            # "Acme's inception year is 2003" and the production wording
            # "Acme has operated since its inception in 2003."
            rf"\b{subject_pattern}\b(?:\s+s)?\s+"
            rf"(?:has\s+operated\s+since\s+)?(?:its\s+)?"
            rf"(?:establishment|formation|founding|incorporation|inception)"
            rf"(?:\s+(?:date|year))?\s+(?:was\s+|is\s+)?(?:in\s+)?{year_pattern}\b",
            # Active voice is accepted only for the organization's own
            # operations, never an object it founded or launched.
            rf"\b{subject_pattern}\b\s+"
            rf"(?:began|commenced|started)\s+(?:its\s+)?operations?\s+"
            rf"(?:in\s+)?{year_pattern}\b",
        )
        if any(re.search(pattern, clause) for pattern in patterns):
            return True
    return False


def _explicit_fact_type(fact: Mapping[str, Any]) -> ExactFactType | None:
    for key in ("fact_type", "category", "kind"):
        try:
            return ExactFactType(_normalized(fact.get(key)))
        except ValueError:
            continue
    return None


def classify_structured_fact(fact: Mapping[str, Any]) -> ExactFactType | None:
    """Map a compiler fact to one supported exact-fact type."""

    explicit = _explicit_fact_type(fact)
    predicate = _normalized(fact.get("predicate"))
    value = _normalized(fact.get("value"))
    if explicit == ExactFactType.FOUNDING:
        # An AI-produced category is only a hint. A controlled founding
        # predicate plus a bounded year/date value is required before this
        # high-consequence fact can bypass generative retrieval.
        return (
            ExactFactType.FOUNDING
            if predicate in _FOUNDING_PREDICATES and _FOUNDING_YEAR_RE.fullmatch(value) is not None
            else None
        )
    if explicit is not None:
        return explicit
    searchable = f"{predicate} {value}"
    if "fax" not in predicate and any(
        term in searchable for term in _FACT_TYPE_TERMS[ExactFactType.PHONE]
    ):
        return ExactFactType.PHONE
    if predicate in _FOUNDING_PREDICATES and _FOUNDING_YEAR_RE.fullmatch(value) is not None:
        return ExactFactType.FOUNDING
    for fact_type in (
        ExactFactType.ADDRESS,
        ExactFactType.HOURS,
        ExactFactType.LEADERSHIP,
        ExactFactType.SERVICES,
    ):
        if any(term in searchable for term in _FACT_TYPE_TERMS[fact_type]):
            return fact_type
    # Unknown predicates stay on the general grounded-retrieval path. Subject
    # and value appearing in the same evidence excerpt does not prove the
    # predicate-to-value relationship, and compiler-proposed search phrases
    # are not caller-facing authority by themselves.
    return None


def classify_exact_fact_intents(query: str) -> tuple[ExactFactType, ...]:
    normalized = _normalized(query)
    if not normalized:
        return ()
    selected: list[ExactFactType] = []
    # "contact details" commonly means both ways to call and where to visit.
    if re.search(r"\bcontact\s+(?:details?|information|info)\b", normalized):
        selected.extend((ExactFactType.PHONE, ExactFactType.ADDRESS))
    for fact_type, patterns in _INTENT_PATTERNS:
        if fact_type not in selected and any(pattern.search(normalized) for pattern in patterns):
            selected.append(fact_type)
    # Preserve named-person questions even when STT lowercases the transcript,
    # without routing generic questions such as "who is your provider?" through
    # the exact-fact index. Two to four non-generic words is a deliberately
    # conservative proper-name signal; aliases in non-Latin scripts are handled
    # after loading the index.
    named_person = re.search(r"\bwho\s+is\s+(.+?)(?:[?.!]|$)", normalized)
    if ExactFactType.LEADERSHIP not in selected and named_person:
        candidate_tokens = _TOKEN_RE.findall(named_person.group(1))
        candidate_set = set(candidate_tokens)
        if (
            2 <= len(candidate_tokens) <= 4
            and candidate_tokens[0] not in _NON_NAME_QUESTION_PREFIXES
            and not candidate_set.intersection(_NON_NAME_QUESTION_TERMS)
        ):
            selected.append(ExactFactType.LEADERSHIP)
    # "Can you provide the address?" is an address question, not a services
    # question.  Require an explicit service noun when another Tier-1 intent is
    # already present.
    if (
        ExactFactType.SERVICES in selected
        and len(selected) > 1
        and not re.search(r"\b(?:services?|offerings?|treatments?)\b", normalized)
    ):
        selected.remove(ExactFactType.SERVICES)
    return tuple(selected)


def _preclassified_intents(queries: Iterable[str]) -> tuple[ExactFactType, ...]:
    selected: list[ExactFactType] = []
    for query in queries:
        for intent in classify_exact_fact_intents(query):
            if intent not in selected:
                selected.append(intent)
    return tuple(selected)


def _may_require_cross_script_alias(queries: Iterable[str]) -> bool:
    """Return whether an unclassified query may need a published alias lookup."""

    for query in queries:
        for character in query:
            if not character.isalpha():
                continue
            if "LATIN" not in unicodedata.name(character, ""):
                return True
    return False


def _source_url(source: ExactFactSource, structured: Mapping[str, Any]) -> str | None:
    structured_source = structured.get("source")
    if isinstance(structured_source, Mapping):
        url = _clean_text(structured_source.get("url"), limit=1_000)
        if url:
            return url
    return _clean_text(source.source_url, limit=1_000) or None


def _stable_evidence_id(
    *,
    source_id: str,
    subject: str,
    predicate: str,
    value: str,
    evidence: str,
    explicit_id: object = None,
) -> str:
    explicit = _clean_text(explicit_id, limit=96)
    if explicit and _SAFE_EXTERNAL_ID_RE.fullmatch(explicit):
        return f"ef1:{source_id}:{explicit}"
    payload = "\x1f".join(
        (
            source_id,
            _grounding_normalized(subject),
            _grounding_normalized(predicate),
            _grounding_normalized(value),
            _grounding_normalized(evidence),
        )
    )
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"ef1:{source_id}:{digest}"


def _source_revision(sources: Sequence[ExactFactSource]) -> str:
    digest = hashlib.sha256()
    for source in sorted(sources, key=lambda item: str(item.source_id)):
        digest.update(
            "\x1f".join(
                (
                    str(source.source_id),
                    source.content_sha256 or "",
                    _isoformat(source.compiled_at) or "",
                    _isoformat(source.updated_at) or "",
                )
            ).encode("utf-8")
        )
    return digest.hexdigest()


def build_exact_fact_index(
    sources: Iterable[ExactFactSource],
    *,
    knowledge_base_id: str | UUID | None = None,
    revision: str | None = None,
    eligible_source_count: int | None = None,
) -> ExactFactIndex:
    """Build an immutable, deterministic index from verified structured facts.

    Coverage limits are explicit in the returned artifact.  Callers must not
    treat a truncated index as authoritative; ``resolve_exact_fact`` therefore
    falls back instead of answering or refusing from a partial corpus.
    """

    source_items = tuple(sources)
    bounded_sources = source_items[:MAX_INDEX_SOURCES]
    declared_eligible_count = max(
        len(source_items),
        max(0, int(eligible_source_count or 0)),
    )
    truncation_reasons: set[str] = set()
    if len(source_items) > MAX_INDEX_SOURCES or declared_eligible_count > len(bounded_sources):
        truncation_reasons.add(
            "source_limit"
            if len(bounded_sources) >= MAX_INDEX_SOURCES
            else "source_coverage_mismatch"
        )
    facts: list[ExactFact] = []
    completely_structured_sources = 0
    absence_authoritative = True
    seen_ids: set[str] = set()
    for source in bounded_sources:
        structured = source.structured_content
        if not isinstance(structured, Mapping):
            truncation_reasons.add("incomplete_structured_coverage")
            continue
        raw_facts = structured.get("facts")
        if not isinstance(raw_facts, list):
            truncation_reasons.add("incomplete_structured_coverage")
            continue
        coverage = structured.get("exact_fact_coverage")
        validation = structured.get("validation")
        if isinstance(coverage, Mapping):
            # ``returned_facts_validated`` makes the extracted facts safe to
            # answer from, but deliberately does not claim that an LLM found
            # every fact in the source. Missing exact matches always continue
            # to the wider approved-content retriever below.
            coverage_is_complete = coverage.get("complete") is True or (
                coverage.get("returned_facts_validated") is True
                and coverage.get("absence_authoritative") is False
            )
            source_absence_authoritative = (
                coverage.get("complete") is True and coverage.get("absence_authoritative") is True
            )
        elif isinstance(validation, Mapping):
            # Legacy validation proves only that returned facts are grounded;
            # it cannot prove the extractor found every source fact. Valid
            # returned facts may still use Tier 1, while every miss falls back.
            coverage_is_complete = (
                validation.get("all_evidence_source_grounded") is True
                and int(validation.get("facts_rejected") or 0) == 0
            )
            source_absence_authoritative = False
        else:
            # Imported facts are revalidated individually below. They can serve
            # a named match, but a miss must always continue to approved prose.
            coverage_is_complete = "deterministic_contacts" not in structured
            source_absence_authoritative = False
        absence_authoritative = absence_authoritative and source_absence_authoritative
        if not coverage_is_complete:
            truncation_reasons.add("incomplete_structured_coverage")
        else:
            completely_structured_sources += 1
        if len(raw_facts) > MAX_FACTS_PER_SOURCE:
            truncation_reasons.add("per_source_fact_limit")
        provenance = ExactFactProvenance(
            source_id=str(source.source_id),
            source_name=_clean_text(source.source_name, limit=255) or "Unnamed source",
            source_url=_source_url(source, structured),
            content_sha256=_clean_text(source.content_sha256, limit=64) or None,
            compiled_at=_isoformat(source.compiled_at),
            compiler_version=_clean_text(structured.get("schema_version"), limit=100) or None,
        )
        for raw_fact in raw_facts[:MAX_FACTS_PER_SOURCE]:
            if len(facts) >= MAX_INDEX_FACTS:
                truncation_reasons.add("index_fact_limit")
                continue
            if not isinstance(raw_fact, Mapping):
                continue
            subject = _clean_text(raw_fact.get("subject"), limit=200)
            predicate = _clean_text(raw_fact.get("predicate"), limit=100)
            value = _clean_text(raw_fact.get("value"), limit=1_000)
            evidence = _clean_text(raw_fact.get("evidence"), limit=1_500)
            fact_type = classify_structured_fact(raw_fact)
            if not all((subject, predicate, value, evidence, fact_type)):
                truncation_reasons.add("invalid_structured_fact")
                continue
            # Re-check the compiler's most important invariant at the runtime
            # boundary.  An edited/corrupt JSON value must never become Tier-1.
            if not _value_is_grounded(value, evidence) or not _subject_is_grounded(
                subject, evidence
            ):
                truncation_reasons.add("invalid_structured_fact")
                continue
            if fact_type == ExactFactType.FOUNDING and not _founding_relationship_is_grounded(
                subject=subject,
                value=value,
                evidence=evidence,
            ):
                truncation_reasons.add("invalid_structured_fact")
                continue
            aliases = _deduplicated_text(
                (
                    *_list_values(raw_fact.get("aliases")),
                    *_list_values(raw_fact.get("spoken_aliases")),
                    *_list_values(raw_fact.get("cross_script_aliases")),
                ),
                limit=24,
            )
            search_phrases = _deduplicated_text(
                _list_values(raw_fact.get("search_phrases")),
                limit=12,
            )
            evidence_id = _stable_evidence_id(
                source_id=provenance.source_id,
                subject=subject,
                predicate=predicate,
                value=value,
                evidence=evidence,
                explicit_id=raw_fact.get("evidence_id"),
            )
            if evidence_id in seen_ids:
                continue
            facts.append(
                ExactFact(
                    evidence_id=evidence_id,
                    fact_type=fact_type,
                    subject=subject,
                    predicate=predicate,
                    value=value,
                    evidence=evidence,
                    search_phrases=search_phrases,
                    aliases=aliases,
                    provenance=provenance,
                )
            )
            seen_ids.add(evidence_id)
    facts.sort(
        key=lambda fact: (
            fact.fact_type.value,
            _normalized(fact.subject),
            _normalized(fact.predicate),
            _normalized(fact.value),
            fact.evidence_id,
        )
    )
    return ExactFactIndex(
        revision=revision or _source_revision(bounded_sources),
        facts=tuple(facts),
        knowledge_base_id=str(knowledge_base_id) if knowledge_base_id is not None else None,
        eligible_source_count=declared_eligible_count,
        indexed_source_count=completely_structured_sources,
        index_truncated=bool(truncation_reasons),
        truncation_reasons=tuple(sorted(truncation_reasons)),
        absence_authoritative=absence_authoritative,
    )


def _fact_tokens(fact: ExactFact) -> set[str]:
    return _tokens(
        " ".join(
            (
                fact.subject,
                fact.predicate,
                fact.value,
                *fact.aliases,
            )
        )
    )


def _requested_tokens(query: str, intents: Iterable[ExactFactType]) -> set[str]:
    ignored = _QUERY_STOP_WORDS | _GENERIC_ENTITY_TOKENS | _ALL_INTENT_TOKENS
    for intent in intents:
        ignored |= _INTENT_QUERY_TOKENS[intent]
    return _tokens(query) - ignored


def _leadership_claim_tokens(query: str) -> set[str]:
    """Return a named person in a yes/no leadership claim, if present."""

    normalized = _normalized(query)
    patterns = (
        re.compile(rf"\bis\s+(.{{2,80}}?)\s+(?:the\s+)?{_LEADERSHIP_ROLE_PATTERN}\b"),
        re.compile(rf"\b(.{{2,80}}?)\s+is\s+(?:the\s+)?{_LEADERSHIP_ROLE_PATTERN}\b"),
    )
    for pattern in patterns:
        match = pattern.search(normalized)
        if match is None:
            continue
        tokens = (
            _tokens(match.group(1))
            - _QUERY_STOP_WORDS
            - {
                "heard",
                "correct",
                "really",
                "think",
            }
        )
        # A confirmation claim with a long preamble should use only the most
        # recent name-sized suffix rather than treating the whole sentence as
        # an entity.
        ordered = [token for token in _TOKEN_RE.findall(match.group(1)) if token in tokens]
        return set(ordered[-5:])
    return set()


def _direct_alias_match(query: str, fact: ExactFact) -> bool:
    """Match governed entity aliases, never compiler-proposed question text.

    Search phrases remain useful ranking vocabulary after the query already has
    a recognized Tier-1 intent. They must not manufacture an intent or bypass
    entity checks: those phrases are model-generated and are not independently
    proven by their supporting source.
    """

    normalized_query = _normalized(query)
    if not normalized_query:
        return False
    return any(
        len(normalized_alias) >= 4
        and (normalized_query == normalized_alias or normalized_alias in normalized_query)
        for alias in fact.aliases
        if (normalized_alias := _normalized(alias))
    )


def _fact_score(
    query: str,
    fact: ExactFact,
    *,
    intents: tuple[ExactFactType, ...],
) -> int | None:
    if fact.fact_type not in intents:
        return None
    fact_tokens = _fact_tokens(fact)
    requested = _requested_tokens(query, intents)
    if fact.fact_type == ExactFactType.LEADERSHIP:
        claimed_name = _leadership_claim_tokens(query)
        if claimed_name and not claimed_name <= fact_tokens:
            return None
    direct_alias = _direct_alias_match(query, fact)
    if fact.fact_type == ExactFactType.FOUNDING and requested:
        identity_tokens = _tokens(" ".join((fact.subject, *fact.aliases)))
        if not requested <= identity_tokens and not direct_alias:
            return None
    if requested and not requested <= fact_tokens and not direct_alias:
        return None
    score = 100 if direct_alias else 0
    subject = _normalized(fact.subject)
    value = _normalized(fact.value)
    normalized_query = _normalized(query)
    if subject and subject in normalized_query:
        score += 40
    if value and value in normalized_query:
        score += 35
    score += 8 * len(requested & fact_tokens)
    score += 3 * len(_tokens(query) & _tokens(fact.predicate))
    # Generic requests remain valid only when the index makes them unambiguous.
    return score or 1


def _semantic_fact_key(fact: ExactFact) -> tuple[str, str, str]:
    return (
        fact.fact_type.value,
        _grounding_normalized(fact.subject),
        _grounding_normalized(fact.value),
    )


def _rank_candidates(
    *,
    queries: Sequence[str],
    facts: Sequence[ExactFact],
    intents: tuple[ExactFactType, ...],
) -> list[tuple[int, ExactFact]]:
    scored: dict[tuple[str, str, str], tuple[int, ExactFact]] = {}
    for fact in facts:
        best_score: int | None = None
        for variant_index, query in enumerate(queries):
            score = _fact_score(query, fact, intents=intents)
            if score is None:
                continue
            adjusted = score - variant_index
            best_score = adjusted if best_score is None else max(best_score, adjusted)
        if best_score is None:
            continue
        key = _semantic_fact_key(fact)
        existing = scored.get(key)
        if existing is None or (best_score, fact.evidence_id) > (
            existing[0],
            existing[1].evidence_id,
        ):
            scored[key] = (best_score, fact)
    return sorted(scored.values(), key=lambda item: (-item[0], item[1].evidence_id))


def _bounded_quote(evidence: str, value: str, *, limit: int) -> str:
    cleaned = _clean_text(evidence, limit=4_000)
    if len(cleaned) <= limit:
        return cleaned
    value_index = cleaned.casefold().find(value.casefold())
    if value_index < 0:
        return cleaned[: max(1, limit - 1)].rstrip() + "…"
    start = max(0, min(value_index - limit // 3, len(cleaned) - limit))
    end = min(len(cleaned), start + limit)
    excerpt = cleaned[start:end].strip()
    if start > 0:
        excerpt = "…" + excerpt[1:]
    if end < len(cleaned):
        excerpt = excerpt[:-1] + "…"
    return excerpt[:limit]


def _compact(value: str | None, *, limit: int) -> str:
    cleaned = _clean_text(value, limit=limit)
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(1, limit - 1)].rstrip() + "…"


def _render_evidence_context(
    *,
    action: ExactFactResponseAction,
    evidence: Sequence[ExactFactEvidence],
    candidate_count: int,
    max_chars: int,
) -> str | None:
    if not evidence:
        return None
    return encode_exact_fact_evidence(
        response_action=action.value,
        facts=tuple(
            ExactFactWireFact(
                evidence_id=item.evidence_id,
                fact_type=item.fact_type.value,
                subject=item.subject,
                predicate=item.predicate,
                value=item.value,
                quote=item.quote,
                source_name=item.provenance.source_name,
                source_url=item.provenance.source_url or "",
            )
            for item in evidence[:MAX_RESPONSE_FACTS]
        ),
        max_chars=max_chars,
        candidate_count=candidate_count,
    )


def _resolution(
    *,
    action: ExactFactResponseAction,
    intents: tuple[ExactFactType, ...],
    facts: Sequence[ExactFact] = (),
    candidate_count: int | None = None,
    reason: str,
    max_evidence_chars: int,
) -> ExactFactResolution:
    evidence = tuple(
        ExactFactEvidence(
            evidence_id=fact.evidence_id,
            fact_type=fact.fact_type,
            subject=fact.subject,
            predicate=fact.predicate,
            value=fact.value,
            quote=_bounded_quote(fact.evidence, fact.value, limit=max_evidence_chars),
            provenance=fact.provenance,
        )
        for fact in facts[:MAX_RESPONSE_FACTS]
    )
    total_candidates = max(len(facts), int(candidate_count or 0))
    return ExactFactResolution(
        response_action=action,
        intents=intents,
        evidence=evidence,
        evidence_context=_render_evidence_context(
            action=action,
            evidence=evidence,
            candidate_count=total_candidates,
            max_chars=max_evidence_chars,
        ),
        reason=reason,
        candidate_count=total_candidates,
    )


def resolve_exact_fact(
    index: ExactFactIndex,
    *,
    query: str,
    query_variants: Iterable[str] = (),
    max_evidence_chars: int = DEFAULT_EVIDENCE_CONTEXT_CHARS,
) -> ExactFactResolution:
    """Resolve a caller query to an explicit, evidence-bearing action."""

    _validate_evidence_context_limit(max_evidence_chars)
    queries = _deduplicated_text((query, *query_variants), limit=8)
    intents: list[ExactFactType] = []
    for candidate_query in queries:
        for intent in classify_exact_fact_intents(candidate_query):
            if intent not in intents:
                intents.append(intent)
    # Most exact-fact families fail closed on any partial corpus. A founding
    # match is the narrow exception: it has an exact predicate/year grammar and
    # a clause-level relationship proof, and its spoken rendering is always
    # explicitly source-qualified. A miss still falls back to broad retrieval.
    source_qualified_founding = bool(intents) and set(intents) == {ExactFactType.FOUNDING}
    if index.index_truncated and not source_qualified_founding:
        return _resolution(
            action=ExactFactResponseAction.FALLBACK,
            intents=tuple(intents),
            reason="exact_fact_index_truncated",
            max_evidence_chars=max_evidence_chars,
        )
    # A compiler-provided cross-script alias can identify both the entity and
    # the intent even when this English-first classifier cannot read the script.
    for fact in index.facts:
        if any(_direct_alias_match(candidate_query, fact) for candidate_query in queries):
            if fact.fact_type not in intents:
                intents.append(fact.fact_type)
    intent_tuple = tuple(intents)
    if not queries or not intent_tuple:
        return _resolution(
            action=ExactFactResponseAction.FALLBACK,
            intents=intent_tuple,
            reason="not_a_tier1_exact_fact_query",
            max_evidence_chars=max_evidence_chars,
        )
    ranked = _rank_candidates(
        queries=queries,
        facts=index.facts,
        intents=intent_tuple,
    )
    if not ranked:
        return _resolution(
            action=ExactFactResponseAction.FALLBACK,
            intents=intent_tuple,
            reason=(
                "exact_fact_index_truncated"
                if index.index_truncated
                else "no_exact_fact_match_use_approved_retrieval"
            ),
            max_evidence_chars=max_evidence_chars,
        )

    if not index.absence_authoritative and not any(
        _grounding_normalized(fact.subject) in _grounding_normalized(candidate_query)
        or any(
            _grounding_normalized(alias) in _grounding_normalized(candidate_query)
            for alias in fact.aliases
            if _grounding_normalized(alias)
        )
        for _score, fact in ranked
        for candidate_query in queries
    ):
        return _resolution(
            action=ExactFactResponseAction.FALLBACK,
            intents=intent_tuple,
            reason=(
                "exact_fact_index_truncated"
                if index.index_truncated
                else "generic_query_requires_complete_exact_fact_coverage"
            ),
            max_evidence_chars=max_evidence_chars,
        )

    matched_intents = {fact.fact_type for _, fact in ranked}
    if any(intent not in matched_intents for intent in intent_tuple):
        return _resolution(
            action=ExactFactResponseAction.FALLBACK,
            intents=intent_tuple,
            reason="partial_exact_fact_match_use_approved_retrieval",
            max_evidence_chars=max_evidence_chars,
        )

    selected: list[ExactFact] = []
    ambiguous: list[ExactFact] = []
    for intent in intent_tuple:
        intent_ranked = [(score, fact) for score, fact in ranked if fact.fact_type == intent]
        top_score = intent_ranked[0][0]
        top_facts = [fact for score, fact in intent_ranked if score == top_score]
        # Broad service questions intentionally return a short verified list.
        if intent == ExactFactType.SERVICES:
            subjects = {_grounding_normalized(fact.subject) for fact in top_facts}
            if len(subjects) == 1:
                selected.extend(top_facts)
                continue
        if len(top_facts) > 1:
            ambiguous.extend(top_facts)
        else:
            selected.extend(top_facts)
    if ambiguous:
        return _resolution(
            action=ExactFactResponseAction.CLARIFY,
            intents=intent_tuple,
            facts=(*selected, *ambiguous),
            candidate_count=len(selected) + len(ambiguous),
            reason="ambiguous_verified_exact_facts",
            max_evidence_chars=max_evidence_chars,
        )
    return _resolution(
        action=ExactFactResponseAction.ANSWER,
        intents=intent_tuple,
        facts=selected,
        reason=(
            "source_qualified_founding_fact"
            if ExactFactType.FOUNDING in intent_tuple
            else "verified_exact_fact"
            if len(selected) == 1
            else "verified_exact_facts"
        ),
        max_evidence_chars=max_evidence_chars,
    )


class ExactFactIndexCache:
    """Bounded process-local TTL/LRU cache for immutable fact indexes."""

    def __init__(
        self,
        *,
        max_entries: int = 32,
        ttl_seconds: float = 300,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, ExactFactIndex]] = OrderedDict()
        self._lock = Lock()

    def remember(self, key: str, index: ExactFactIndex) -> None:
        if not key:
            raise ValueError("cache key is required")
        with self._lock:
            self._entries[key] = (self._clock(), index)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)

    def get(self, key: str) -> ExactFactIndex | None:
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stored_at, index = entry
            if self._clock() - stored_at >= self._ttl_seconds:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return index

    def invalidate(self, prefix: str | None = None) -> None:
        with self._lock:
            if prefix is None:
                self._entries.clear()
                return
            for key in tuple(self._entries):
                if key.startswith(prefix):
                    del self._entries[key]


agent_exact_fact_cache = ExactFactIndexCache()


def _database_revision(
    *,
    knowledge_base_id: UUID,
    knowledge_base_updated_at: datetime,
    eligible_source_count: int,
    rows: Sequence[Any],
) -> str:
    digest = hashlib.sha256(
        (
            f"{knowledge_base_id}\x1f{knowledge_base_updated_at.isoformat()}"
            f"\x1f{eligible_source_count}"
        ).encode()
    )
    for row in rows:
        digest.update(
            "\x1f".join(
                (
                    str(row.id),
                    row.content_sha256 or "",
                    _isoformat(row.compiled_at) or "",
                    _isoformat(row.updated_at) or "",
                )
            ).encode("utf-8")
        )
    return digest.hexdigest()


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1_000, 3)


async def load_agent_exact_fact_index_with_diagnostics(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    serving_revision_id: UUID | None = None,
    knowledge_base_id: UUID | None = None,
    cache: ExactFactIndexCache | None = agent_exact_fact_cache,
) -> ExactFactIndexLoad:
    """Load a pinned/current exact-fact index with content-free timings.

    Revision-only historical pins remain valid only for the same tenant and
    the knowledge base currently bound to the agent. An admitted call may also
    supply its immutable knowledge-base ID and bypass that mutable binding.
    A bad pin produces no index; it never falls forward to a newer release.
    """

    total_started = perf_counter()
    binding_started = perf_counter()
    if knowledge_base_id is not None and serving_revision_id is None:
        raise ValueError("knowledge_base_id requires an explicit serving_revision_id")
    if serving_revision_id is not None and knowledge_base_id is not None:
        # This explicit tenant/KB/revision identity has already crossed the
        # runtime admission fence. Avoid the mutable agent binding so a later
        # rebind cannot make the in-flight call lose or change exact facts.
        bound = (knowledge_base_id, None, serving_revision_id)
    else:
        binding_query = (
            select(
                KnowledgeBase.id,
                KnowledgeBase.updated_at,
                KnowledgeBase.serving_revision_id,
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
            )
        )
        if serving_revision_id is None:
            binding_query = binding_query.where(
                or_(
                    KnowledgeBase.serving_revision_id.is_not(None),
                    KnowledgeBase.approval_status == "approved",
                )
            )
        bound = (await db.execute(binding_query)).one_or_none()
    binding_lookup_ms = _elapsed_ms(binding_started)
    if bound is None:
        return ExactFactIndexLoad(
            index=None,
            diagnostics=ExactFactLoadDiagnostics(
                binding_lookup_ms=binding_lookup_ms,
                total_ms=_elapsed_ms(total_started),
            ),
        )
    knowledge_base_id, knowledge_base_updated_at, current_serving_revision_id = bound
    effective_serving_revision_id = serving_revision_id or current_serving_revision_id
    revision_started = perf_counter()
    if effective_serving_revision_id is not None:
        serving_revision = await db.scalar(
            select(KnowledgeServingRevision).where(
                KnowledgeServingRevision.id == effective_serving_revision_id,
                KnowledgeServingRevision.tenant_id == tenant_id,
                KnowledgeServingRevision.knowledge_base_id == knowledge_base_id,
            )
        )
        if serving_revision is None:
            return ExactFactIndexLoad(
                index=None,
                diagnostics=ExactFactLoadDiagnostics(
                    binding_lookup_ms=binding_lookup_ms,
                    revision_lookup_ms=_elapsed_ms(revision_started),
                    total_ms=_elapsed_ms(total_started),
                ),
            )
        revision_source_filters = (
            KnowledgeServingRevisionSource.tenant_id == tenant_id,
            KnowledgeServingRevisionSource.serving_revision_id == effective_serving_revision_id,
        )
        source_metadata_rows = (
            await db.execute(
                select(
                    KnowledgeServingRevisionSource.original_source_id.label("id"),
                    KnowledgeServingRevisionSource.content_sha256,
                    KnowledgeServingRevisionSource.compiled_at,
                    KnowledgeServingRevisionSource.created_at.label("updated_at"),
                    func.count(KnowledgeServingRevisionSource.id)
                    .over()
                    .label("eligible_source_count"),
                )
                .where(*revision_source_filters)
                .order_by(
                    KnowledgeServingRevisionSource.created_at.desc(),
                    KnowledgeServingRevisionSource.original_source_id,
                )
                .limit(MAX_INDEX_SOURCES)
            )
        ).all()
        eligible_source_count = (
            int(source_metadata_rows[0].eligible_source_count) if source_metadata_rows else 0
        )
        # Facts can change after a compiler upgrade even when visible source
        # text does not. The aggregate release hash covers fact/entity/chunk
        # artifacts and is therefore the only safe cache identity.
        revision = serving_revision.content_sha256
    else:
        source_filters = (
            KnowledgeSource.tenant_id == tenant_id,
            KnowledgeSource.knowledge_base_id == knowledge_base_id,
            KnowledgeSource.status.in_(_ELIGIBLE_SOURCE_STATUSES),
        )
        source_metadata_rows = (
            await db.execute(
                select(
                    KnowledgeSource.id,
                    KnowledgeSource.content_sha256,
                    KnowledgeSource.compiled_at,
                    KnowledgeSource.updated_at,
                    func.count(KnowledgeSource.id).over().label("eligible_source_count"),
                )
                .where(*source_filters)
                .order_by(KnowledgeSource.updated_at.desc(), KnowledgeSource.id)
                .limit(MAX_INDEX_SOURCES)
            )
        ).all()
        # COUNT OVER is evaluated before LIMIT, giving exact corpus coverage
        # with no additional database round-trip on this latency-sensitive path.
        eligible_source_count = (
            int(source_metadata_rows[0].eligible_source_count) if source_metadata_rows else 0
        )
        revision = _database_revision(
            knowledge_base_id=knowledge_base_id,
            knowledge_base_updated_at=knowledge_base_updated_at,
            eligible_source_count=eligible_source_count,
            rows=source_metadata_rows,
        )
    revision_lookup_ms = _elapsed_ms(revision_started)
    cache_key = f"{tenant_id}:{agent_id}:{knowledge_base_id}:{revision}"
    cache_started = perf_counter()
    cached = cache.get(cache_key) if cache is not None else None
    cache_lookup_ms = _elapsed_ms(cache_started)
    if cached is not None:
        return ExactFactIndexLoad(
            index=cached,
            diagnostics=ExactFactLoadDiagnostics(
                binding_lookup_ms=binding_lookup_ms,
                revision_lookup_ms=revision_lookup_ms,
                cache_lookup_ms=cache_lookup_ms,
                total_ms=_elapsed_ms(total_started),
                cache_hit=True,
                source_count=cached.indexed_source_count,
                eligible_source_count=cached.eligible_source_count,
                indexed_source_count=cached.indexed_source_count,
                fact_count=cached.fact_count,
                index_truncated=cached.index_truncated,
                truncation_reasons=cached.truncation_reasons,
            ),
        )
    source_ids = [row.id for row in source_metadata_rows]
    source_started = perf_counter()
    if source_ids and effective_serving_revision_id is not None:
        source_rows = (
            await db.execute(
                select(
                    KnowledgeServingRevisionSource.original_source_id.label("id"),
                    KnowledgeServingRevisionSource.name,
                    KnowledgeServingRevisionSource.location,
                    KnowledgeServingRevisionSource.source_metadata,
                    KnowledgeServingRevisionSource.structured_content,
                    KnowledgeServingRevisionSource.content_sha256,
                    KnowledgeServingRevisionSource.compiled_at,
                    KnowledgeServingRevisionSource.created_at.label("updated_at"),
                )
                .where(
                    KnowledgeServingRevisionSource.tenant_id == tenant_id,
                    KnowledgeServingRevisionSource.serving_revision_id
                    == effective_serving_revision_id,
                    KnowledgeServingRevisionSource.original_source_id.in_(source_ids),
                )
                .order_by(
                    KnowledgeServingRevisionSource.created_at.desc(),
                    KnowledgeServingRevisionSource.original_source_id,
                )
            )
        ).all()
    elif source_ids:
        source_rows = (
            await db.execute(
                select(
                    KnowledgeSource.id,
                    KnowledgeSource.name,
                    KnowledgeSource.location,
                    KnowledgeSource.source_metadata,
                    KnowledgeSource.structured_content,
                    KnowledgeSource.content_sha256,
                    KnowledgeSource.compiled_at,
                    KnowledgeSource.updated_at,
                )
                .where(
                    *source_filters,
                    KnowledgeSource.id.in_(source_ids),
                )
                .order_by(KnowledgeSource.updated_at.desc(), KnowledgeSource.id)
            )
        ).all()
    else:
        source_rows = []
    source_load_ms = _elapsed_ms(source_started)
    sources = []
    for row in source_rows:
        source_url = row.location
        if isinstance(row.source_metadata, Mapping):
            source_url = (
                row.source_metadata.get("url")
                or row.source_metadata.get("canonical_url")
                or source_url
            )
        sources.append(
            ExactFactSource(
                source_id=row.id,
                source_name=row.name,
                source_url=source_url,
                structured_content=row.structured_content,
                content_sha256=row.content_sha256,
                compiled_at=row.compiled_at,
                updated_at=row.updated_at,
            )
        )
    build_started = perf_counter()
    index = build_exact_fact_index(
        sources,
        knowledge_base_id=knowledge_base_id,
        revision=revision,
        eligible_source_count=eligible_source_count,
    )
    index_build_ms = _elapsed_ms(build_started)
    if cache is not None:
        cache.remember(cache_key, index)
    return ExactFactIndexLoad(
        index=index,
        diagnostics=ExactFactLoadDiagnostics(
            binding_lookup_ms=binding_lookup_ms,
            revision_lookup_ms=revision_lookup_ms,
            cache_lookup_ms=cache_lookup_ms,
            source_load_ms=source_load_ms,
            index_build_ms=index_build_ms,
            total_ms=_elapsed_ms(total_started),
            cache_hit=False,
            source_count=index.indexed_source_count,
            eligible_source_count=index.eligible_source_count,
            indexed_source_count=index.indexed_source_count,
            fact_count=index.fact_count,
            index_truncated=index.index_truncated,
            truncation_reasons=index.truncation_reasons,
        ),
    )


async def load_agent_exact_fact_index(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    serving_revision_id: UUID | None = None,
    knowledge_base_id: UUID | None = None,
    cache: ExactFactIndexCache | None = agent_exact_fact_cache,
) -> ExactFactIndex | None:
    """Load or reuse the exact-fact index for an approved, bound knowledge base."""

    loaded = await load_agent_exact_fact_index_with_diagnostics(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        serving_revision_id=serving_revision_id,
        knowledge_base_id=knowledge_base_id,
        cache=cache,
    )
    return loaded.index


async def retrieve_exact_fact(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    query: str,
    query_variants: Iterable[str] = (),
    max_evidence_chars: int = DEFAULT_EVIDENCE_CONTEXT_CHARS,
    serving_revision_id: UUID | None = None,
    knowledge_base_id: UUID | None = None,
    cache: ExactFactIndexCache | None = agent_exact_fact_cache,
) -> ExactFactResolution:
    """Load an agent's approved index and resolve one deterministic Tier-1 query."""

    _validate_evidence_context_limit(max_evidence_chars)
    total_started = perf_counter()
    preclassification_started = perf_counter()
    queries = _deduplicated_text((query, *query_variants), limit=8)
    preclassified_intents = _preclassified_intents(queries)
    may_need_alias_index = _may_require_cross_script_alias(queries)
    preclassification_ms = _elapsed_ms(preclassification_started)
    if not preclassified_intents and not may_need_alias_index:
        result = _resolution(
            action=ExactFactResponseAction.FALLBACK,
            intents=(),
            reason="not_a_tier1_exact_fact_query",
            max_evidence_chars=max_evidence_chars,
        )
        return replace(
            result,
            diagnostics=ExactFactDiagnostics(
                preclassification_ms=preclassification_ms,
                resolution_ms=0.0,
                total_ms=_elapsed_ms(total_started),
                skipped_database=True,
            ),
        )

    loaded = await load_agent_exact_fact_index_with_diagnostics(
        db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        serving_revision_id=serving_revision_id,
        knowledge_base_id=knowledge_base_id,
        cache=cache,
    )
    if loaded.index is None:
        result = _resolution(
            action=ExactFactResponseAction.FALLBACK,
            intents=preclassified_intents,
            reason="no_approved_exact_fact_index_use_approved_retrieval",
            max_evidence_chars=max_evidence_chars,
        )
        return replace(
            result,
            diagnostics=ExactFactDiagnostics(
                preclassification_ms=preclassification_ms,
                resolution_ms=0.0,
                total_ms=_elapsed_ms(total_started),
                skipped_database=False,
                load=loaded.diagnostics,
            ),
        )
    resolution_started = perf_counter()
    result = resolve_exact_fact(
        loaded.index,
        query=query,
        query_variants=queries[1:],
        max_evidence_chars=max_evidence_chars,
    )
    resolution_ms = _elapsed_ms(resolution_started)
    return replace(
        result,
        diagnostics=ExactFactDiagnostics(
            preclassification_ms=preclassification_ms,
            resolution_ms=resolution_ms,
            total_ms=_elapsed_ms(total_started),
            skipped_database=False,
            load=loaded.diagnostics,
        ),
    )
