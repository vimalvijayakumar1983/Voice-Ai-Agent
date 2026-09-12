"""Operator search across one knowledge base.

Two questions an operator asks before a call: "does the knowledge base mention
this at all?" and "what would the agent actually retrieve for this question?".
The first is a plain text scan of every source's extracted text and verified
facts; the second runs the same retrieval the runtime uses, against the
approved release when one exists and otherwise against the draft sources.
"""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import KnowledgeBase
from app.services.knowledge_retrieval import (
    _QUERY_STOP_WORDS,
    _rank_contextual_knowledge,
    _singular,
    _source_retrieval_documents,
    _specialty_forms,
    build_contextual_query_plan,
    retrieve_knowledge_context,
)

MAX_QUERY_CHARS = 200
MAX_SOURCES = 20
MAX_SNIPPETS_PER_SOURCE = 3
MAX_FACTS_PER_SOURCE = 5
MAX_HITS_PER_TERM = 50
SNIPPET_RADIUS = 120
RETRIEVAL_LIMIT = 6
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_PREFIX_MIN_CHARS = 4


@dataclass(frozen=True)
class SearchFact:
    subject: str
    predicate: str
    value: str


@dataclass
class SourceMatch:
    source_id: uuid.UUID
    name: str
    source_type: str
    matched_terms: list[str] = field(default_factory=list)
    match_count: int = 0
    snippets: list[str] = field(default_factory=list)
    facts: list[SearchFact] = field(default_factory=list)


@dataclass(frozen=True)
class RetrievalChunk:
    source: str
    text: str


@dataclass
class RetrievalPreview:
    scope: str  # "approved_release" | "draft"
    status: str  # "verified" | "no_match"
    chunks: list[RetrievalChunk] = field(default_factory=list)
    note: str | None = None


def search_terms(query: str) -> list[str]:
    """Distinct search words, in order, without question framing."""
    terms: list[str] = []
    for word in _WORD_RE.findall(query.casefold()):
        if len(word) < 2 or word in _QUERY_STOP_WORDS or word in terms:
            continue
        terms.append(word)
    return terms


def _term_pattern(term: str) -> re.Pattern[str]:
    """Match a term, its plural and its specialty word forms, as whole words.

    "urology" also finds "Urologist"; "scan" finds "scans"; a short term such
    as "ct" or "mri" must be a whole word so it never matches inside another.
    """
    variants = {term, _singular(term), *_specialty_forms(term)}
    ordered = sorted(variants, key=len, reverse=True)
    alternatives = "|".join(re.escape(variant) for variant in ordered)
    if len(term) >= _PREFIX_MIN_CHARS:
        return re.compile(
            rf"(?<![^\W_])(?:{alternatives})(?:e?s)?(?![^\W_])", re.IGNORECASE | re.UNICODE
        )
    return re.compile(rf"(?<![^\W_])(?:{alternatives})(?![^\W_])", re.IGNORECASE | re.UNICODE)


def _snippet(text: str, start: int, end: int) -> str:
    window_start = max(0, start - SNIPPET_RADIUS)
    window_end = min(len(text), end + SNIPPET_RADIUS)
    snippet = " ".join(text[window_start:window_end].split())
    prefix = "…" if window_start > 0 else ""
    suffix = "…" if window_end < len(text) else ""
    return f"{prefix}{snippet}{suffix}"


def _fact_text(fact: dict[str, Any]) -> str:
    phrases = fact.get("search_phrases")
    phrase_text = " ".join(str(item) for item in phrases) if isinstance(phrases, list) else ""
    return (
        " ".join(str(fact.get(key) or "") for key in ("subject", "predicate", "value", "evidence"))
        + f" {phrase_text}"
    )


def scan_source(source: Any, terms: list[str]) -> SourceMatch | None:
    """Where the terms appear in one source's extracted text and verified facts."""
    if not terms:
        return None
    patterns = [(term, _term_pattern(term)) for term in terms]
    text = str(source.raw_content or source.content or "")
    match = SourceMatch(
        source_id=source.id, name=str(source.name or ""), source_type=str(source.source_type or "")
    )
    hits: list[tuple[int, int, str]] = []
    for term, pattern in patterns:
        term_hits = 0
        for found in pattern.finditer(text):
            hits.append((found.start(), found.end(), term))
            term_hits += 1
            if term_hits >= MAX_HITS_PER_TERM:
                break
        if term_hits:
            match.matched_terms.append(term)
            match.match_count += term_hits

    structured = source.structured_content if isinstance(source.structured_content, dict) else {}
    facts = structured.get("facts") if isinstance(structured.get("facts"), list) else []
    for fact in facts:
        if not isinstance(fact, dict):
            continue
        searchable = _fact_text(fact)
        fact_terms = [term for term, pattern in patterns if pattern.search(searchable)]
        if not fact_terms:
            continue
        match.match_count += 1
        for term in fact_terms:
            if term not in match.matched_terms:
                match.matched_terms.append(term)
        if len(match.facts) < MAX_FACTS_PER_SOURCE:
            match.facts.append(
                SearchFact(
                    subject=" ".join(str(fact.get("subject") or "").split()),
                    predicate=" ".join(str(fact.get("predicate") or "").split()),
                    value=" ".join(str(fact.get("value") or "").split()),
                )
            )
    if not match.matched_terms:
        return None

    # One snippet per distinct term first, so a source matching two terms
    # shows both; later windows that overlap an earlier one are skipped.
    covered: list[tuple[int, int]] = []
    seen_terms: set[str] = set()
    for start, end, term in sorted(hits, key=lambda item: (item[2] in seen_terms, item[0])):
        if len(match.snippets) >= MAX_SNIPPETS_PER_SOURCE:
            break
        overlaps = any(
            start < c_end + SNIPPET_RADIUS and end > c_start - SNIPPET_RADIUS
            for c_start, c_end in covered
        )
        if overlaps:
            continue
        match.snippets.append(_snippet(text, start, end))
        covered.append((start, end))
        seen_terms.add(term)
    match.matched_terms.sort(key=terms.index)
    return match


def scan_sources(sources: list[Any], query: str) -> tuple[list[str], list[SourceMatch]]:
    terms = search_terms(query)
    matches = [match for source in sources if (match := scan_source(source, terms)) is not None]
    matches.sort(key=lambda item: (-len(item.matched_terms), -item.match_count, item.name))
    return terms, matches[:MAX_SOURCES]


def _context_chunks(context: str | None) -> tuple[list[RetrievalChunk], str | None]:
    if not context:
        return [], None
    note = None
    body = context
    marker = "Contextual terminology considered: "
    if body.startswith(marker):
        head, _, body = body.partition("\n\n")
        note = head
    chunks: list[RetrievalChunk] = []
    for part in body.split("\n\nSource: "):
        part = part.removeprefix("Source: ").strip()
        if not part:
            continue
        source, _, text = part.partition("\n")
        chunks.append(RetrievalChunk(source=source.strip(), text=text.strip() or source.strip()))
    return chunks, note


async def retrieval_preview(db: AsyncSession, kb: KnowledgeBase, query: str) -> RetrievalPreview:
    """What the runtime would retrieve for ``query`` from this knowledge base."""
    company_subject = (kb.owner_company or "").strip() or None
    if kb.serving_revision_id is not None:
        context = await retrieve_knowledge_context(
            db,
            tenant_id=kb.tenant_id,
            agent_id=uuid.UUID(int=0),
            query=query,
            knowledge_base_id=kb.id,
            serving_revision_id=kb.serving_revision_id,
            **({"company_subject": company_subject} if company_subject else {}),
        )
        scope = "approved_release"
    else:
        documents: list[tuple[str, str]] = []
        for source in kb.sources:
            if source.status not in {"indexed", "local_only", "processing"} or not source.content:
                continue
            documents.extend(
                _source_retrieval_documents(
                    name=source.name,
                    content=source.content,
                    structured_content=source.structured_content,
                    company_subject=company_subject,
                    owner_company=company_subject,
                )
            )
        tags = kb.tags if isinstance(kb.tags, list) else []
        plan = build_contextual_query_plan(query, terminology=(kb.name, kb.scope_label, *tags))
        matches = (
            await asyncio.to_thread(
                _rank_contextual_knowledge,
                plan.variants,
                documents,
                RETRIEVAL_LIMIT,
                company_subject,
            )
            if plan.variants and documents
            else []
        )
        context = "\n\n".join(f"Source: {match.source}\n{match.text}" for match in matches) or None
        if context and plan.recovered_terms:
            context = (
                "Contextual terminology considered: "
                + ", ".join(plan.recovered_terms)
                + ".\n\n"
                + context
            )
        scope = "draft"
    chunks, note = _context_chunks(context)
    return RetrievalPreview(
        scope=scope, status="verified" if chunks else "no_match", chunks=chunks, note=note
    )
