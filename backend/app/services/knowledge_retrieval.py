"""Provider-independent retrieval from approved, agent-bound VAV knowledge."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from urllib.parse import unquote, urlsplit
from uuid import UUID

from sqlalchemy import Text, case, cast, func, literal_column, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentKnowledgeBinding, KnowledgeBase, KnowledgeSource

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_SPLIT = re.compile(r"(?:\r?\n){2,}|(?<=[.!?।])\s+")
_PARAGRAPH_SPLIT = re.compile(r"(?:\r?\n){2,}")
MAX_CONTEXT_CHARS = 6000
MAX_SOURCE_CANDIDATES = 48
MAX_RANKING_CORPUS_CHARS = 96_000
MAX_RANKING_SOURCE_CHARS = 8_000
MAX_CONTEXTUAL_QUERY_VARIANTS = 5
MAX_TERMINOLOGY_SOURCES = 256
MAX_TERMINOLOGY_VALUES = 1_024
MAX_TERMINOLOGY_CONTENT_CHARS = 16_000
MAX_CONTENT_TERMS_PER_SOURCE = 24
_FTS_CANDIDATE_LIMIT = 32
_TITLE_CANDIDATE_LIMIT = 8
_CONTACT_CANDIDATE_LIMIT = 12
_POSTGRES_HEADLINE_OPTIONS = (
    "MaxWords=120, MinWords=20, ShortWord=2, MaxFragments=8, HighlightAll=FALSE"
)
_ELIGIBLE_SOURCE_STATUSES = ("processing", "indexed", "local_only")
_QUERY_STOP_WORDS = {
    "a",
    "about",
    "actually",
    "again",
    "also",
    "an",
    "and",
    "anyway",
    "are",
    "basically",
    "can",
    "could",
    "do",
    "does",
    "for",
    "from",
    "give",
    "have",
    "hello",
    "hey",
    "hi",
    "is",
    "it",
    "just",
    "kind",
    "know",
    "like",
    "maybe",
    "me",
    "more",
    "now",
    "of",
    "ok",
    "okay",
    "please",
    "right",
    "so",
    "something",
    "tell",
    "thank",
    "thanks",
    "that",
    "the",
    "their",
    "theirs",
    "them",
    "they",
    "then",
    "there",
    "thing",
    "this",
    "to",
    "us",
    "want",
    "we",
    "well",
    "what",
    "which",
    "who",
    "would",
    "yeah",
    "yes",
    "you",
    "your",
    "he",
    "her",
    "hers",
    "him",
    "his",
    "its",
    "she",
    "share",
}
_SOURCE_NOISE_TOKENS = {
    "base",
    "content",
    "converted",
    "copy",
    "crawl",
    "crawled",
    "doc",
    "document",
    "docx",
    "extracted",
    "final",
    "html",
    "indexed",
    "knowledge",
    "local",
    "page",
    "pages",
    "pdf",
    "searchable",
    "source",
    "text",
    "txt",
    "vav",
    "voice",
    "web",
    "website",
}
_BROAD_QUERY_TOKENS = {
    "about",
    "business",
    "company",
    "corporate",
    "division",
    "group",
    "organisation",
    "organization",
    "overview",
    "profile",
}
_OVERVIEW_SOURCE_TOKENS = {"about", "homepage", "overview", "profile"}
_DIVISIONS_SOURCE_TOKENS = {"business", "division"}
_DIRECTORY_QUERY_TOKENS = {
    "consultant",
    "dermatologist",
    "doctor",
    "physician",
    "specialist",
    "surgeon",
}
_DIRECTORY_SOURCE_TOKENS = _DIRECTORY_QUERY_TOKENS | {"directory", "medical", "team"}
_CONTACT_QUERY_TOKENS = {
    "address",
    "call",
    "contact",
    "email",
    "fax",
    "location",
    "mobile",
    "phone",
    "telephone",
}
_QUERY_INTENT_TOKENS = (
    _BROAD_QUERY_TOKENS
    | _DIRECTORY_QUERY_TOKENS
    | _CONTACT_QUERY_TOKENS
    | {
        "appointment",
        "availability",
        "available",
        "book",
        "booking",
        "consultation",
        "cost",
        "detail",
        "hour",
        "hierarchy",
        "information",
        "location",
        "leadership",
        "management",
        "chairman",
        "open",
        "opening",
        "option",
        "price",
        "pricing",
        "service",
        "treatment",
    }
)
_NAVIGATION_NOISE_TOKENS = {
    "cookie",
    "copyright",
    "facebook",
    "footer",
    "home",
    "instagram",
    "linkedin",
    "login",
    "menu",
    "navigation",
    "next",
    "previous",
    "privacy",
    "share",
    "subscribe",
    "twitter",
    "youtube",
}
_ENTITY_SEQUENCE = re.compile(
    r"\b(?:[A-Z][\w&'’-]{2,})(?:\s+(?:(?:and|of|the)\s+)?[A-Z][\w&'’-]{2,}){1,3}\b",
    re.UNICODE,
)
_UPPERCASE_ENTITY = re.compile(r"\b[A-Z][A-Z0-9&'’-]{3,}\b", re.UNICODE)
_CONTACT_SOURCE_MARKERS = (
    "contact",
    "phone",
    "telephone",
    "mobile",
    "email",
    "address",
    "tel:",
    "mailto:",
    "@",
)
_PHONE_TERMS = {"mobile", "phone", "tel", "telephone"}
_PHONE_NUMBER = re.compile(r"(?<!\w)\+?\d[\d\s().-]{6,}\d(?!\w)")


@dataclass(frozen=True)
class KnowledgeMatch:
    source: str
    text: str
    score: float


@dataclass(frozen=True)
class ContextualQueryPlan:
    """Auditable retrieval queries derived without changing the raw transcript."""

    primary_query: str
    variants: tuple[str, ...]
    recovered_terms: tuple[str, ...]


def _base_tokens(value: str) -> list[str]:
    return [token.casefold() for token in _TOKEN.findall(value) if len(token) > 1]


def _singular(token: str) -> str:
    if len(token) > 4 and token.endswith("ies"):
        return f"{token[:-3]}y"
    if len(token) > 3 and token.endswith("s") and not token.endswith(("is", "ss", "us")):
        return token[:-1]
    return token


def _tokens(value: str) -> set[str]:
    return _token_forms(_base_tokens(value))


def _token_forms(base_tokens: list[str]) -> set[str]:
    tokens: set[str] = set()
    for token in base_tokens:
        tokens.add(token)
        tokens.add(_singular(token))
    return tokens


def _query_tokens(value: str) -> set[str]:
    return {_singular(token) for token in _base_tokens(value)} - _QUERY_STOP_WORDS


def _is_phone_query(value: str, query_tokens: set[str]) -> bool:
    """Return whether the caller is asking for a telephone contact.

    ``number`` remains substantive for account, invoice, order, and other
    identifiers.  It becomes a harmless qualifier only when the query also
    contains an explicit phone/contact-number signal.
    """

    return bool(query_tokens & _PHONE_TERMS) or bool(
        re.search(r"\bcontact\s+(?:phone\s+)?number\b", value.casefold())
    )


def _has_phone_evidence(value: str, tokens: set[str]) -> bool:
    return bool(tokens & _PHONE_TERMS) or bool(_PHONE_NUMBER.search(value))


def _deduplicated_queries(values: Iterable[str]) -> tuple[str, ...]:
    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        candidate = " ".join(str(value or "").split()).strip()
        normalized = " ".join(_base_tokens(candidate))
        if not normalized or normalized in seen:
            continue
        selected.append(candidate)
        seen.add(normalized)
        if len(selected) >= MAX_CONTEXTUAL_QUERY_VARIANTS:
            break
    return tuple(selected)


def _terminology_text(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    if "://" in raw:
        parsed = urlsplit(raw)
        raw = " ".join((parsed.hostname or "", unquote(parsed.path)))
    raw = re.sub(r"\.(?:docx?|html?|pdf|pptx?|txt|xlsx?)\b", " ", raw, flags=re.IGNORECASE)
    raw = re.sub(r"[_/|:]+", " ", raw)
    raw = re.sub(r"\s+-\s+", " ", raw)
    return " ".join(raw.split())


def _terminology_phrases(values: Iterable[object]) -> dict[str, str]:
    """Build a bounded correction vocabulary from approved source metadata.

    Source titles and URL slugs are authoritative enough to repair recognition,
    while arbitrary document prose is deliberately excluded so ordinary words
    cannot become accidental aliases.
    """
    phrases: dict[str, str] = {}
    ignored = _SOURCE_NOISE_TOKENS | _QUERY_STOP_WORDS | _QUERY_INTENT_TOKENS
    for value in values:
        text = _terminology_text(value)
        tokens = _TOKEN.findall(text)
        if not tokens:
            continue
        bounded_tokens = tokens[:24]
        for width in range(1, min(4, len(bounded_tokens)) + 1):
            for start in range(len(bounded_tokens) - width + 1):
                display_tokens = bounded_tokens[start : start + width]
                normalized_tokens = [token.casefold() for token in display_tokens]
                if any(token.isdigit() for token in normalized_tokens):
                    continue
                distinctive = [
                    token for token in normalized_tokens if len(token) >= 4 and token not in ignored
                ]
                if not distinctive:
                    continue
                if width == 1 and len(normalized_tokens[0]) < 5:
                    continue
                normalized = " ".join(normalized_tokens)
                phrases.setdefault(normalized, " ".join(display_tokens))
                if len(phrases) >= MAX_TERMINOLOGY_VALUES:
                    return phrases
    return phrases


def _content_entity_terminology(value: str) -> tuple[str, ...]:
    """Extract only high-confidence proper names from approved source prose.

    Capitalized multi-word names and all-uppercase brands are sufficiently narrow
    to help speech-recognition recovery. Ordinary prose remains excluded so a
    similar medical or operational word cannot silently become an alias.
    """
    bounded = str(value or "")[:MAX_TERMINOLOGY_CONTENT_CHARS]
    selected: list[str] = []
    seen: set[str] = set()
    matches = list(_ENTITY_SEQUENCE.finditer(bounded)) + list(_UPPERCASE_ENTITY.finditer(bounded))
    for match in sorted(matches, key=lambda item: item.start()):
        candidate = " ".join(match.group(0).split()).strip(" -|,.;:")
        normalized_tokens = _base_tokens(candidate)
        distinctive = [
            token
            for token in normalized_tokens
            if len(token) >= 4
            and token not in _QUERY_STOP_WORDS
            and token not in _QUERY_INTENT_TOKENS
            and token not in _SOURCE_NOISE_TOKENS
        ]
        folded = candidate.casefold()
        if not distinctive or folded in seen:
            continue
        selected.append(candidate)
        seen.add(folded)
        if len(selected) >= MAX_CONTENT_TERMS_PER_SOURCE:
            break
    return tuple(selected)


def _spoken_compound_similarity(query_token: str, canonical_tokens: Sequence[str]) -> float:
    """Score one uncertain STT token against a multi-word proper name.

    Streaming transcription often joins a spoken brand (``Saint Gobain`` ->
    ``Sengobin``). This intentionally applies only to long single tokens and
    metadata-derived proper names; it is never used for arbitrary content words.
    """
    if len(query_token) < 6 or not 2 <= len(canonical_tokens) <= 3:
        return 0.0
    compact = "".join(canonical_tokens)
    if query_token[:1] != compact[:1] or abs(len(query_token) - len(compact)) > 5:
        return 0.0
    raw_score = SequenceMatcher(None, query_token, compact).ratio()
    query_spoken = query_token.replace("saint", "sen").replace("ai", "i")
    canonical_spoken = compact.replace("saint", "sen").replace("ai", "i")
    spoken_score = SequenceMatcher(None, query_spoken, canonical_spoken).ratio()
    best_score = max(raw_score, spoken_score)
    if best_score < 0.7:
        return 0.0
    return best_score * 0.88


def _phrase_similarity(query_tokens: Sequence[str], canonical_tokens: Sequence[str]) -> float:
    if not query_tokens or not canonical_tokens:
        return 0.0
    if len(query_tokens) == 1 and len(canonical_tokens) > 1:
        return _spoken_compound_similarity(query_tokens[0], canonical_tokens)
    if len(query_tokens) != len(canonical_tokens):
        return 0.0
    token_scores = [
        SequenceMatcher(None, query_token, canonical_token).ratio()
        for query_token, canonical_token in zip(query_tokens, canonical_tokens, strict=True)
    ]
    exact_count = sum(
        query_token == canonical_token
        for query_token, canonical_token in zip(query_tokens, canonical_tokens, strict=True)
    )
    if len(query_tokens) == 1:
        if query_tokens[0][:1] != canonical_tokens[0][:1] or token_scores[0] < 0.86:
            return 0.0
        return token_scores[0]
    same_initials = all(
        query_token[:1] == canonical_token[:1]
        for query_token, canonical_token in zip(query_tokens, canonical_tokens, strict=True)
    )
    compact_score = SequenceMatcher(
        None,
        "".join(query_tokens),
        "".join(canonical_tokens),
    ).ratio()
    average_score = sum(token_scores) / len(token_scores)
    if exact_count == 0 and not same_initials:
        return 0.0
    one_uncertain_word = len(query_tokens) >= 3 and exact_count >= len(query_tokens) - 1
    minimum_score = 0.7 if one_uncertain_word else 0.78
    if compact_score < minimum_score or average_score < minimum_score:
        return 0.0
    return average_score * 0.65 + compact_score * 0.35


def _recover_terminology(query: str, terminology: Iterable[object]) -> tuple[str, str] | None:
    matches = list(_TOKEN.finditer(query))
    if not matches:
        return None
    query_tokens = [match.group(0).casefold() for match in matches]
    best: tuple[float, int, int, str] | None = None
    for normalized, display in _terminology_phrases(terminology).items():
        canonical_tokens = normalized.split()
        canonical_width = len(canonical_tokens)
        if normalized in " ".join(query_tokens):
            continue
        query_widths = {canonical_width}
        if canonical_width > 1:
            query_widths.add(1)
        for width in sorted(query_widths):
            if width > len(query_tokens):
                continue
            for start in range(len(query_tokens) - width + 1):
                window = query_tokens[start : start + width]
                score = _phrase_similarity(window, canonical_tokens)
                if score <= 0:
                    continue
                candidate = (score, start, width, display)
                if best is None or candidate[0] > best[0]:
                    best = candidate
    if best is None:
        return None
    _score, start, width, display = best
    prefix = query[: matches[start].start()]
    suffix = query[matches[start + width - 1].end() :]
    corrected = f"{prefix}{display}{suffix}"
    return " ".join(corrected.split()), display


def build_contextual_query_plan(
    query: str,
    *,
    supplied_variants: Iterable[str] = (),
    terminology: Iterable[object] = (),
) -> ContextualQueryPlan:
    """Create safe retrieval alternatives while keeping ``query`` immutable."""
    base_variants = _deduplicated_queries((query, *supplied_variants))
    if not base_variants:
        return ContextualQueryPlan(primary_query=query, variants=(), recovered_terms=())
    recovered_variants: list[str] = []
    recovered_terms: list[str] = []
    for variant in base_variants:
        recovered = _recover_terminology(variant, terminology)
        if recovered is None:
            continue
        recovered_query, recovered_term = recovered
        recovered_variants.append(recovered_query)
        recovered_terms.append(recovered_term)
    variants = _deduplicated_queries((*base_variants, *recovered_variants))
    return ContextualQueryPlan(
        primary_query=base_variants[0],
        variants=variants,
        recovered_terms=tuple(dict.fromkeys(recovered_terms)),
    )


def _compound_terms(tokens: list[str]) -> set[str]:
    compounds: set[str] = set()
    for width in (2, 3):
        for start in range(len(tokens) - width + 1):
            compound = "".join(tokens[start : start + width])
            if 5 <= len(compound) <= 32:
                compounds.add(compound)
    return compounds


def _source_terms(value: str) -> set[str]:
    base_tokens = _source_base_tokens(value)
    return set(base_tokens) | _compound_terms(base_tokens)


def _source_base_tokens(value: str) -> list[str]:
    return [
        _singular(token)
        for token in _base_tokens(value)
        if token not in _SOURCE_NOISE_TOKENS and not token.isdigit()
    ]


def _source_compounds(value: str) -> set[str]:
    tokens = [_singular(token) for token in _base_tokens(value)]
    compounds: set[str] = set()
    for width in (2, 3):
        for start in range(len(tokens) - width + 1):
            parts = tokens[start : start + width]
            if any(token in _SOURCE_NOISE_TOKENS or token.isdigit() for token in parts):
                continue
            compound = "".join(parts)
            if 5 <= len(compound) <= 32:
                compounds.add(compound)
    return compounds


def _best_fuzzy_source_match(
    query_token: str, source_compounds: set[str]
) -> tuple[float, str | None]:
    if len(query_token) < 5:
        return 0.0, None

    best_score = 0.0
    best_candidate: str | None = None
    for candidate in source_compounds:
        if len(candidate) < 5 or abs(len(query_token) - len(candidate)) > 2:
            continue
        # Requiring a shared four-character prefix keeps fuzzy matching narrow while
        # recovering common streaming-STT truncations such as "Alzab" -> "Al Zaabi".
        if query_token[:4] != candidate[:4]:
            continue
        shorter, longer = sorted((query_token, candidate), key=len)
        if longer.startswith(shorter):
            score = 0.84
        else:
            similarity = SequenceMatcher(None, query_token, candidate).ratio()
            if similarity < 0.82:
                continue
            score = similarity * 0.9
        if score > best_score:
            best_score = score
            best_candidate = candidate
    return best_score, best_candidate


def _best_source_match(
    query_token: str,
    source_terms: set[str],
    source_compounds: set[str],
) -> tuple[float, str | None]:
    if query_token in source_terms:
        return 1.0, query_token
    return _best_fuzzy_source_match(query_token, source_compounds)


def _ubiquitous_source_terms(documents: list[tuple[str, str]]) -> set[str]:
    terms_by_source: dict[str, set[str]] = {}
    for source, _content in documents:
        terms_by_source.setdefault(source.casefold(), _source_terms(source))
    if len(terms_by_source) < 2:
        return set()
    frequencies = Counter(term for terms in terms_by_source.values() for term in terms)
    threshold = max(2, (len(terms_by_source) * 3 + 4) // 5)
    return {term for term, frequency in frequencies.items() if frequency >= threshold}


def _chunk_quality(value: str, *, words: list[str] | None = None) -> float:
    if words is None:
        words = _base_tokens(value)
    word_count = len(words)
    navigation_heavy = False
    if word_count < 4:
        quality = 0.35
    elif word_count < 8:
        quality = 0.65
    elif word_count < 14:
        quality = 0.82
    else:
        quality = 1.0

    if words:
        useful_ratio = sum(word not in _NAVIGATION_NOISE_TOKENS for word in words) / word_count
        if useful_ratio < 0.5:
            quality *= 0.55
            navigation_heavy = True
        elif useful_ratio < 0.7:
            quality *= 0.78
        if word_count >= 12 and len(set(words)) / word_count < 0.35:
            quality *= 0.75

    separators = sum(value.count(separator) for separator in ("|", "•", "›", "»"))
    if separators >= 3 and separators >= max(word_count // 3, 1):
        quality *= 0.7
        if navigation_heavy:
            quality *= 0.5
    if len(re.findall(r"https?://|www\.", value.casefold())) >= 2:
        quality *= 0.75
    return max(quality, 0.2)


def _is_broad_query(query: str, query_tokens: set[str]) -> bool:
    raw_tokens = {_singular(token) for token in _base_tokens(query)}
    return bool(raw_tokens & _BROAD_QUERY_TOKENS) and len(query_tokens - _BROAD_QUERY_TOKENS) <= 2


def _is_group_overview_source(source: str) -> bool:
    title_tokens = [_singular(token) for token in _base_tokens(source)]
    return title_tokens[:2] == ["the", "group"]


def _intent_content(value: str, *, phone_query: bool) -> str:
    """Prefer verified subject associations for multi-entity phone lookups."""

    if not phone_query:
        return value
    marker = "VERIFIED STRUCTURED FACTS"
    source_marker = "\n\nSOURCE CONTENT"
    start = value.find(marker)
    if start < 0:
        return value
    end = value.find(source_marker, start)
    structured = value[start : end if end >= 0 else len(value)].strip()
    structured_tokens = _token_forms(_base_tokens(structured))
    return structured if _has_phone_evidence(structured, structured_tokens) else value


def _chunks(
    value: str,
    *,
    max_chars: int = 900,
    preserve_paragraphs: bool = False,
) -> list[str]:
    chunks: list[str] = []
    current = ""
    splitter = _PARAGRAPH_SPLIT if preserve_paragraphs else _SPLIT
    for part in splitter.split(value):
        part = " ".join(part.split()).strip()
        if not part:
            continue
        if preserve_paragraphs and current:
            chunks.append(current)
            current = ""
        if current and len(current) + len(part) + 1 > max_chars:
            chunks.append(current)
            current = ""
        if len(part) > max_chars:
            for start in range(0, len(part), max_chars):
                chunks.append(part[start : start + max_chars])
        else:
            current = f"{current} {part}".strip()
    if current:
        chunks.append(current)
    return chunks


def rank_knowledge(
    query: str,
    documents: list[tuple[str, str]],
    *,
    limit: int = 6,
) -> list[KnowledgeMatch]:
    query_tokens = _query_tokens(query)
    phone_query = _is_phone_query(query, query_tokens)
    if phone_query:
        # ``phone number`` and ``telephone number`` describe one contact
        # concept.  Do not require the source to contain the literal word
        # ``number`` when it supplies the value as ``Tel: +971 ...``.
        query_tokens.discard("number")
    phone_subject_tokens = (
        query_tokens - _CONTACT_QUERY_TOKENS - {"detail", "information"} if phone_query else set()
    )
    if not query_tokens or limit <= 0:
        return []
    ordered_query_tokens = sorted(query_tokens)
    ubiquitous_source_terms = _ubiquitous_source_terms(documents)
    broad_query = _is_broad_query(query, query_tokens)
    directory_query = bool(query_tokens & _DIRECTORY_QUERY_TOKENS)
    matches: list[KnowledgeMatch] = []
    for source, content in documents:
        source_terms = _source_terms(source)
        source_compounds = _source_compounds(source)
        ranked_content = _intent_content(content, phone_query=phone_query)
        for chunk in _chunks(ranked_content, preserve_paragraphs=phone_query):
            chunk_base_tokens = _base_tokens(chunk)
            chunk_tokens = _token_forms(chunk_base_tokens)
            chunk_has_phone = _has_phone_evidence(chunk, chunk_tokens)
            if phone_query and not chunk_has_phone:
                # A contact-page title alone must not satisfy a request for a
                # phone number.  Require an explicit telephone label or a
                # number-shaped value in the returned evidence.
                continue
            if chunk_has_phone:
                # Treat provider and website vocabulary as equivalent without
                # rewriting the stored source or weakening unrelated intents.
                chunk_tokens.update(_PHONE_TERMS)
            if phone_subject_tokens and not phone_subject_tokens <= chunk_tokens:
                # Contact pages commonly list several companies.  A source-title
                # match is not enough: the returned phone evidence must name the
                # organization requested by the caller.  Contextual spelling
                # recovery supplies a corrected query variant when necessary.
                continue
            quality = _chunk_quality(chunk, words=chunk_base_tokens)
            content_scores: list[float] = []
            source_scores: list[float] = []
            combined_scores: list[float] = []
            for query_token in ordered_query_tokens:
                # Content is exact-only: a medical term must never be recovered from a
                # similar operational word (for example, cancer from cancellation).
                content_score = 1.0 if query_token in chunk_tokens else 0.0
                source_score, source_term = _best_source_match(
                    query_token, source_terms, source_compounds
                )
                source_weight = 0.18 if source_term in ubiquitous_source_terms else 1.0
                effective_content_score = content_score * quality
                effective_source_score = source_score * source_weight * (0.55 + quality * 0.45)
                content_scores.append(effective_content_score)
                source_scores.append(effective_source_score)
                combined_scores.append(max(effective_content_score, effective_source_score))

            if not any(combined_scores):
                continue
            if len(query_tokens) >= 3:
                if sum(score > 0 for score in combined_scores) < 2:
                    continue
                # A long query usually contains an entity plus the requested fact.
                # Entity/title evidence alone must not make an unrelated source look
                # authoritative. Require exact content evidence for every term that
                # the title could not explain (for example, ``cancer Alzab group``).
                unexplained_topic_indexes = [
                    index for index, source_score in enumerate(source_scores) if source_score == 0
                ]
                if unexplained_topic_indexes:
                    substantive_topic_indexes = [
                        index
                        for index in unexplained_topic_indexes
                        if ordered_query_tokens[index] not in _QUERY_INTENT_TOKENS
                    ]
                    if substantive_topic_indexes:
                        if not all(
                            content_scores[index] > 0 for index in substantive_topic_indexes
                        ):
                            continue
                    elif not any(content_scores):
                        continue
            coverage = sum(combined_scores) / len(query_tokens)
            content_coverage = sum(content_scores) / len(query_tokens)
            density = sum(content_scores) / max(len(chunk_tokens), 1)
            source_bonus = min(sum(source_scores) / len(query_tokens), 1.0) * 0.06
            topic_bonus = 0.0
            if broad_query:
                if source_terms & _OVERVIEW_SOURCE_TOKENS or _is_group_overview_source(source):
                    topic_bonus = 0.18
                elif source_terms & _DIVISIONS_SOURCE_TOKENS:
                    topic_bonus = 0.12
            if directory_query and source_terms & _DIRECTORY_SOURCE_TOKENS:
                topic_bonus = max(topic_bonus, 0.18)
            score = (
                coverage * 0.76
                + content_coverage * 0.18
                + min(density, 0.25) * 0.24
                + source_bonus
                + topic_bonus
            )
            matches.append(KnowledgeMatch(source, chunk, score))

    diversified: list[KnowledgeMatch] = []
    chunks_per_source: Counter[str] = Counter()
    for match in sorted(matches, key=lambda item: -item.score):
        source_key = " ".join(_base_tokens(match.source)) or match.source.casefold()
        if chunks_per_source[source_key] >= 2:
            continue
        diversified.append(match)
        chunks_per_source[source_key] += 1
        if len(diversified) >= limit:
            break
    return diversified


def _query_aware_excerpt(content: str, query_tokens: set[str], *, limit: int) -> str:
    if len(content) <= limit:
        return content
    # ``str.find``/``rfind`` run in C and avoid a full Python-level regex walk over
    # large crawled documents. Keep both ends so repeated navigation near the start
    # cannot hide a substantive answer near the end.
    folded_content = content.casefold()
    anchors: set[int] = set()
    for token in sorted(query_tokens, key=lambda item: (-len(item), item))[:16]:
        anchors.update(_bounded_token_positions(folded_content, token))
    if not anchors:
        return content[:limit]

    ordered_anchors = sorted(anchors)
    if len(ordered_anchors) > 128:
        last_index = len(ordered_anchors) - 1
        ordered_anchors = [ordered_anchors[round(index * last_index / 127)] for index in range(128)]

    best_score: tuple[int, float, int, int] = (-1, -1.0, -1, -1)
    best_excerpt = content[:limit]
    for anchor in ordered_anchors:
        start = max(0, min(anchor - limit // 3, len(content) - limit))
        excerpt = content[start : start + limit]
        excerpt_tokens = _token_forms(_base_tokens(excerpt))
        local_start = max(0, anchor - 180)
        local_evidence = content[local_start : min(len(content), anchor + 420)]
        local_words = _base_tokens(local_evidence)
        informative_words = {
            word
            for word in local_words
            if word not in query_tokens
            and word not in _QUERY_STOP_WORDS
            and word not in _NAVIGATION_NOISE_TOKENS
        }
        score = (
            len(query_tokens & excerpt_tokens),
            _chunk_quality(local_evidence, words=local_words),
            len(informative_words),
            # On an otherwise equal score, later evidence is less likely to be
            # duplicated header navigation and more likely to be page body text.
            anchor,
        )
        if score > best_score:
            best_score = score
            best_excerpt = excerpt
    return best_excerpt


def _bounded_token_position(value: str, token: str, *, reverse: bool = False) -> int | None:
    """Find a whole token without accepting substrings such as ``hair`` in ``chair``."""
    positions = _bounded_token_positions(value, token, per_direction=1)
    if not positions:
        return None
    return max(positions) if reverse else min(positions)


def _bounded_token_positions(
    value: str,
    token: str,
    *,
    per_direction: int = 16,
) -> set[int]:
    """Sample whole-token occurrences from both ends of a potentially large document."""
    positions: set[int] = set()
    cursor = 0
    while len(positions) < per_direction:
        position = value.find(token, cursor)
        if position < 0:
            break
        end = position + len(token)
        if (position == 0 or not value[position - 1].isalnum()) and (
            end == len(value) or not value[end].isalnum()
        ):
            positions.add(position)
        cursor = end

    reverse_count = 0
    cursor = len(value)
    while reverse_count < per_direction:
        position = value.rfind(token, 0, cursor)
        if position < 0:
            break
        end = position + len(token)
        if (position == 0 or not value[position - 1].isalnum()) and (
            end == len(value) or not value[end].isalnum()
        ):
            positions.add(position)
            reverse_count += 1
        cursor = position
    return positions


def _bounded_ranking_documents(
    query: str,
    documents: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    eligible = [(source, content) for source, content in documents if content.strip()]
    if not eligible:
        return []

    per_source_limits = [min(len(content), MAX_RANKING_SOURCE_CHARS) for _, content in eligible]
    fair_share = max(1, MAX_RANKING_CORPUS_CHARS // len(eligible))
    allocations = [min(limit, fair_share) for limit in per_source_limits]
    remaining = MAX_RANKING_CORPUS_CHARS - sum(allocations)
    while remaining > 0:
        expandable = [
            index for index, limit in enumerate(per_source_limits) if allocations[index] < limit
        ]
        if not expandable:
            break
        next_share = max(1, remaining // len(expandable))
        for index in expandable:
            additional = min(per_source_limits[index] - allocations[index], next_share, remaining)
            allocations[index] += additional
            remaining -= additional
            if remaining == 0:
                break

    query_tokens = _query_tokens(query)
    return [
        (source, _query_aware_excerpt(content, query_tokens, limit=allocation))
        for (source, content), allocation in zip(eligible, allocations, strict=True)
        if allocation > 0
    ]


def _rank_bounded_knowledge(
    query: str,
    documents: list[tuple[str, str]],
    limit: int = 6,
) -> list[KnowledgeMatch]:
    return rank_knowledge(query, _bounded_ranking_documents(query, documents), limit=limit)


def _rank_contextual_knowledge(
    queries: Sequence[str],
    documents: list[tuple[str, str]],
    limit: int,
) -> list[KnowledgeMatch]:
    """Merge independently ranked alternatives without weakening match safety."""
    best_matches: dict[tuple[str, str], KnowledgeMatch] = {}
    for query_index, query in enumerate(queries):
        # Prefer a recovered/contextual query only when its evidence is at least
        # as strong as the literal transcript. The tiny penalty is deterministic
        # and keeps an exact raw-query result ahead on a tie.
        variant_penalty = query_index * 0.004
        for match in _rank_bounded_knowledge(query, documents, limit=max(limit, 6)):
            key = (match.source.casefold(), match.text)
            adjusted = KnowledgeMatch(match.source, match.text, match.score - variant_penalty)
            existing = best_matches.get(key)
            if existing is None or adjusted.score > existing.score:
                best_matches[key] = adjusted

    diversified: list[KnowledgeMatch] = []
    chunks_per_source: Counter[str] = Counter()
    for match in sorted(best_matches.values(), key=lambda item: -item.score):
        source_key = " ".join(_base_tokens(match.source)) or match.source.casefold()
        if chunks_per_source[source_key] >= 2:
            continue
        diversified.append(match)
        chunks_per_source[source_key] += 1
        if len(diversified) >= limit:
            break
    return diversified


def _eligible_source_filters(*, tenant_id: UUID, knowledge_base_id: UUID) -> tuple:
    return (
        KnowledgeSource.knowledge_base_id == knowledge_base_id,
        KnowledgeSource.tenant_id == tenant_id,
        KnowledgeSource.status.in_(_ELIGIBLE_SOURCE_STATUSES),
        KnowledgeSource.content.is_not(None),
    )


async def load_agent_knowledge_terminology(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    hints: Iterable[object] = (),
) -> tuple[str, ...]:
    """Load a bounded proper-name vocabulary for one bound agent.

    Source metadata remains the primary vocabulary. A bounded source-content
    window contributes only high-confidence proper names so spoken brand names
    can be recovered without treating ordinary prose as aliases.
    """
    row = (
        await db.execute(
            select(
                KnowledgeBase.id,
                KnowledgeBase.name,
                KnowledgeBase.scope_label,
                KnowledgeBase.tags,
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
            )
        )
    ).one_or_none()
    values: list[object] = list(hints)
    if row is None:
        return tuple(str(value) for value in values if str(value or "").strip())
    knowledge_base_id, name, scope_label, tags = row
    values.extend((name, scope_label))
    if isinstance(tags, list):
        values.extend(tags)
    source_rows = (
        await db.execute(
            select(
                KnowledgeSource.name,
                KnowledgeSource.location,
                KnowledgeSource.source_metadata,
                func.substr(
                    KnowledgeSource.content,
                    1,
                    MAX_TERMINOLOGY_CONTENT_CHARS,
                ),
            )
            .where(
                *_eligible_source_filters(
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                )
            )
            .order_by(KnowledgeSource.updated_at.desc(), KnowledgeSource.id)
            .limit(MAX_TERMINOLOGY_SOURCES)
        )
    ).all()
    for source_name, location, source_metadata, source_content in source_rows:
        values.extend((source_name, location))
        if isinstance(source_metadata, dict):
            values.extend(
                source_metadata.get(key)
                for key in ("title", "page_title", "display_name")
                if source_metadata.get(key)
            )
        if isinstance(source_content, str):
            values.extend(_content_entity_terminology(source_content))
    selected: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _terminology_text(value)
        folded = normalized.casefold()
        if not normalized or folded in seen:
            continue
        selected.append(normalized)
        seen.add(folded)
        if len(selected) >= MAX_TERMINOLOGY_VALUES:
            break
    return tuple(selected)


def _postgres_fts_terms(query: str) -> list[str]:
    return sorted(
        token
        for token in _tokens(query)
        if _singular(token) not in _QUERY_STOP_WORDS and token.isalnum()
    )[:16]


def _postgres_ts_query(query: str):
    return func.to_tsquery(
        literal_column("'simple'::regconfig"),
        " | ".join(_postgres_fts_terms(query)),
    )


def _postgres_source_excerpt(query: str):
    headline = func.ts_headline(
        literal_column("'simple'::regconfig"),
        KnowledgeSource.content,
        _postgres_ts_query(query),
        _POSTGRES_HEADLINE_OPTIONS,
    )
    return func.substr(headline, 1, MAX_RANKING_SOURCE_CHARS)


def _bounded_unique_ids(*candidate_groups: list[UUID]) -> list[UUID]:
    selected: list[UUID] = []
    seen: set[UUID] = set()
    for candidates in candidate_groups:
        for candidate_id in candidates:
            if candidate_id in seen:
                continue
            selected.append(candidate_id)
            seen.add(candidate_id)
            if len(selected) >= MAX_SOURCE_CANDIDATES:
                return selected
    return selected


async def _broad_title_candidate_ids(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    knowledge_base_id: UUID,
) -> list[UUID]:
    lower_name = func.lower(KnowledgeSource.name)
    broad_title = or_(
        lower_name.like("the group%"),
        lower_name.contains("overview"),
        lower_name.contains("about"),
        lower_name.contains("profile"),
        lower_name.contains("division"),
    )
    priority = case(
        (lower_name.like("the group%"), 0),
        (lower_name.contains("overview"), 1),
        (lower_name.contains("about"), 1),
        (lower_name.contains("profile"), 2),
        (lower_name.contains("division"), 3),
        else_=4,
    )
    return list(
        (
            await db.scalars(
                select(KnowledgeSource.id)
                .where(
                    *_eligible_source_filters(
                        tenant_id=tenant_id,
                        knowledge_base_id=knowledge_base_id,
                    ),
                    broad_title,
                )
                .order_by(priority, KnowledgeSource.updated_at.desc(), KnowledgeSource.id)
                .limit(_TITLE_CANDIDATE_LIMIT)
            )
        ).all()
    )


def _is_contact_query(query: str) -> bool:
    return bool(_query_tokens(query) & _CONTACT_QUERY_TOKENS)


async def _contact_candidate_source_ids(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    knowledge_base_id: UUID,
) -> list[UUID]:
    """Find bounded contact-bearing sources independently of transcript spelling."""
    lower_name = func.lower(KnowledgeSource.name)
    lower_location = func.lower(func.coalesce(KnowledgeSource.location, ""))
    lower_content = func.lower(KnowledgeSource.content)
    contact_predicate = or_(
        *(lower_name.contains(marker) for marker in _CONTACT_QUERY_TOKENS),
        *(lower_location.contains(marker) for marker in ("contact", "location")),
        *(lower_content.contains(marker) for marker in _CONTACT_SOURCE_MARKERS),
    )
    title_priority = case(
        (lower_name.contains("contact"), 0),
        (lower_location.contains("contact"), 1),
        (lower_name.contains("location"), 2),
        else_=3,
    )
    return list(
        (
            await db.scalars(
                select(KnowledgeSource.id)
                .where(
                    *_eligible_source_filters(
                        tenant_id=tenant_id,
                        knowledge_base_id=knowledge_base_id,
                    ),
                    contact_predicate,
                )
                .order_by(title_priority, KnowledgeSource.updated_at.desc(), KnowledgeSource.id)
                .limit(_CONTACT_CANDIDATE_LIMIT)
            )
        ).all()
    )


async def _postgres_candidate_source_ids(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    knowledge_base_id: UUID,
    query: str,
    query_tokens: set[str],
) -> list[UUID]:
    empty_text = literal_column("''", type_=Text())
    search_text = (
        func.coalesce(cast(KnowledgeSource.name, Text), empty_text)
        + literal_column("' '", type_=Text())
        + func.coalesce(KnowledgeSource.content, empty_text)
    )
    search_vector = func.to_tsvector(
        literal_column("'simple'::regconfig"),
        search_text,
    )
    fts_terms = _postgres_fts_terms(query)
    fts_ids: list[UUID] = []
    if fts_terms:
        ts_query = _postgres_ts_query(query)
        rank = func.ts_rank_cd(search_vector, ts_query)
        fts_ids = list(
            (
                await db.scalars(
                    select(KnowledgeSource.id)
                    .where(
                        *_eligible_source_filters(
                            tenant_id=tenant_id,
                            knowledge_base_id=knowledge_base_id,
                        ),
                        search_vector.op("@@")(ts_query),
                    )
                    .order_by(rank.desc(), KnowledgeSource.updated_at.desc(), KnowledgeSource.id)
                    .limit(_FTS_CANDIDATE_LIMIT)
                )
            ).all()
        )

    broad_ids: list[UUID] = []
    if _is_broad_query(query, query_tokens):
        broad_ids = await _broad_title_candidate_ids(
            db,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
        )

    prefixes = sorted({token[:4] for token in query_tokens if len(token) >= 5})[:8]
    fuzzy_title_ids: list[UUID] = []
    if prefixes and not fts_ids:
        normalized_name = func.regexp_replace(
            func.lower(cast(KnowledgeSource.name, Text)),
            "[^[:alnum:]]+",
            "",
            "g",
        )
        fuzzy_title_ids = list(
            (
                await db.scalars(
                    select(KnowledgeSource.id)
                    .where(
                        *_eligible_source_filters(
                            tenant_id=tenant_id,
                            knowledge_base_id=knowledge_base_id,
                        ),
                        or_(*(normalized_name.contains(prefix) for prefix in prefixes)),
                    )
                    .order_by(KnowledgeSource.updated_at.desc(), KnowledgeSource.id)
                    .limit(_TITLE_CANDIDATE_LIMIT)
                )
            ).all()
        )
    return _bounded_unique_ids(broad_ids, fuzzy_title_ids, fts_ids)


async def _fallback_candidate_source_ids(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    knowledge_base_id: UUID,
    query: str,
    query_tokens: set[str],
) -> list[UUID]:
    lower_name = func.lower(KnowledgeSource.name)
    lower_content = func.lower(KnowledgeSource.content)
    exact_terms = sorted(_tokens(query) - _QUERY_STOP_WORDS)[:16]
    exact_ids: list[UUID] = []
    if exact_terms:
        exact_ids = list(
            (
                await db.scalars(
                    select(KnowledgeSource.id)
                    .where(
                        *_eligible_source_filters(
                            tenant_id=tenant_id,
                            knowledge_base_id=knowledge_base_id,
                        ),
                        or_(
                            *(
                                predicate
                                for token in exact_terms
                                for predicate in (
                                    lower_name.contains(token),
                                    lower_content.contains(token),
                                )
                            )
                        ),
                    )
                    .order_by(KnowledgeSource.updated_at.desc(), KnowledgeSource.id)
                    .limit(_FTS_CANDIDATE_LIMIT)
                )
            ).all()
        )

    broad_ids: list[UUID] = []
    if _is_broad_query(query, query_tokens):
        broad_ids = await _broad_title_candidate_ids(
            db,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
        )

    # SQLite has no production-equivalent regexp/GIN path. Inspect a bounded metadata-only
    # window so unit tests and local development retain fuzzy title recovery without loading
    # an unbounded content corpus into the process.
    recent_rows = (
        await db.execute(
            select(KnowledgeSource.id, KnowledgeSource.name)
            .where(
                *_eligible_source_filters(
                    tenant_id=tenant_id,
                    knowledge_base_id=knowledge_base_id,
                )
            )
            .order_by(KnowledgeSource.updated_at.desc(), KnowledgeSource.id)
            .limit(_TITLE_CANDIDATE_LIMIT)
        )
    ).all()
    fuzzy_title_ids = [
        source_id
        for source_id, source_name in recent_rows
        if any(
            _best_fuzzy_source_match(query_token, _source_compounds(source_name))[0] > 0
            for query_token in query_tokens
        )
    ]
    return _bounded_unique_ids(broad_ids, fuzzy_title_ids, exact_ids)


async def _candidate_source_ids(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    knowledge_base_id: UUID,
    query: str,
) -> list[UUID]:
    query_tokens = _query_tokens(query)
    if not query_tokens:
        return []
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        return await _postgres_candidate_source_ids(
            db,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base_id,
            query=query,
            query_tokens=query_tokens,
        )
    return await _fallback_candidate_source_ids(
        db,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base_id,
        query=query,
        query_tokens=query_tokens,
    )


async def retrieve_knowledge_context(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    query: str,
    query_variants: Iterable[str] = (),
    terminology: Iterable[object] = (),
    limit: int = 6,
    max_context_chars: int = MAX_CONTEXT_CHARS,
) -> str | None:
    binding = await db.scalar(
        select(AgentKnowledgeBinding).where(
            AgentKnowledgeBinding.tenant_id == tenant_id,
            AgentKnowledgeBinding.agent_id == agent_id,
        )
    )
    if binding is None:
        return None
    knowledge_base = await db.scalar(
        select(KnowledgeBase).where(
            KnowledgeBase.id == binding.knowledge_base_id,
            KnowledgeBase.tenant_id == tenant_id,
            KnowledgeBase.is_active.is_(True),
            KnowledgeBase.approval_status == "approved",
        )
    )
    if knowledge_base is None:
        return None
    query_plan = build_contextual_query_plan(
        query,
        supplied_variants=query_variants,
        terminology=(
            *terminology,
            knowledge_base.name,
            knowledge_base.scope_label,
            *(knowledge_base.tags if isinstance(knowledge_base.tags, list) else []),
        ),
    )
    if not query_plan.variants:
        return None
    combined_query = " ".join(query_plan.variants)
    candidate_ids = await _candidate_source_ids(
        db,
        tenant_id=tenant_id,
        knowledge_base_id=knowledge_base.id,
        query=combined_query,
    )
    contact_query = any(_is_contact_query(value) for value in query_plan.variants)
    if contact_query:
        contact_ids = await _contact_candidate_source_ids(
            db,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base.id,
        )
        candidate_ids = _bounded_unique_ids(contact_ids, candidate_ids)
    if any(_is_broad_query(value, _query_tokens(value)) for value in query_plan.variants):
        broad_ids = await _broad_title_candidate_ids(
            db,
            tenant_id=tenant_id,
            knowledge_base_id=knowledge_base.id,
        )
        candidate_ids = _bounded_unique_ids(broad_ids, candidate_ids)
    source_rows = []
    if candidate_ids:
        content_expression = KnowledgeSource.content
        if db.get_bind().dialect.name == "postgresql" and not contact_query:
            content_expression = _postgres_source_excerpt(combined_query)
        source_rows = (
            await db.execute(
                select(
                    KnowledgeSource.id,
                    KnowledgeSource.name,
                    content_expression.label("content"),
                ).where(
                    *_eligible_source_filters(
                        tenant_id=tenant_id,
                        knowledge_base_id=knowledge_base.id,
                    ),
                    KnowledgeSource.id.in_(candidate_ids),
                )
            )
        ).all()
    sources_by_id = {
        source_id: (
            source_name,
            source_content.replace("<b>", "").replace("</b>", ""),
        )
        for source_id, source_name, source_content in source_rows
        if isinstance(source_content, str) and source_content.strip()
    }
    documents = [
        sources_by_id[source_id] for source_id in candidate_ids if source_id in sources_by_id
    ]
    if knowledge_base.content:
        documents.append((knowledge_base.name, knowledge_base.content))
    matches = await asyncio.to_thread(
        _rank_contextual_knowledge,
        query_plan.variants,
        documents,
        limit,
    )
    if not matches:
        return None
    interpretation = ""
    if query_plan.recovered_terms:
        interpretation = (
            "Contextual terminology considered: "
            + ", ".join(query_plan.recovered_terms)
            + ". Verify the intended term against the evidence below; ask a brief "
            "clarifying question if it would materially change the answer.\n\n"
        )
    context = interpretation + "\n\n".join(
        f"Source: {match.source}\n{match.text}" for match in matches
    )
    return context[:max_context_chars]
