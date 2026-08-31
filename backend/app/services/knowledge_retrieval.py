"""Provider-independent retrieval from approved, agent-bound VAV knowledge."""

from __future__ import annotations

import re
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import AgentKnowledgeBinding, KnowledgeBase, KnowledgeSource

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_SPLIT = re.compile(r"(?:\r?\n){2,}|(?<=[.!?।])\s+")
MAX_CONTEXT_CHARS = 6000
_QUERY_STOP_WORDS = {
    "a",
    "about",
    "an",
    "and",
    "are",
    "can",
    "could",
    "do",
    "does",
    "for",
    "from",
    "have",
    "is",
    "me",
    "of",
    "please",
    "tell",
    "the",
    "what",
    "which",
    "who",
}


@dataclass(frozen=True)
class KnowledgeMatch:
    source: str
    text: str
    score: float


def _tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for raw_token in _TOKEN.findall(value):
        token = raw_token.casefold()
        if len(token) <= 1:
            continue
        tokens.add(token)
        if len(token) > 4 and token.endswith("ies"):
            tokens.add(f"{token[:-3]}y")
        elif len(token) > 3 and token.endswith("s") and not token.endswith(("is", "ss", "us")):
            tokens.add(token[:-1])
    return tokens


def _query_tokens(value: str) -> set[str]:
    tokens = _tokens(value)
    meaningful = tokens - _QUERY_STOP_WORDS
    return meaningful or tokens


def _chunks(value: str, *, max_chars: int = 900) -> list[str]:
    chunks: list[str] = []
    current = ""
    for part in _SPLIT.split(value):
        part = " ".join(part.split()).strip()
        if not part:
            continue
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
    if not query_tokens:
        return []
    matches: list[KnowledgeMatch] = []
    for source, content in documents:
        source_tokens = _tokens(source)
        source_overlap = query_tokens & source_tokens
        for chunk in _chunks(content):
            chunk_tokens = _tokens(chunk)
            content_overlap = query_tokens & chunk_tokens
            overlap = content_overlap | source_overlap
            if not overlap:
                continue
            coverage = len(overlap) / len(query_tokens)
            density = len(content_overlap) / max(len(chunk_tokens), 1)
            source_bonus = min(len(source_overlap), 2) * 0.03
            score = coverage * 0.82 + density * 0.15 + source_bonus
            matches.append(KnowledgeMatch(source, chunk, score))
    return sorted(matches, key=lambda item: -item.score)[:limit]


async def retrieve_knowledge_context(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    agent_id: UUID,
    query: str,
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
    sources = (
        await db.scalars(
            select(KnowledgeSource).where(
                KnowledgeSource.knowledge_base_id == knowledge_base.id,
                KnowledgeSource.tenant_id == tenant_id,
                KnowledgeSource.status.in_(("indexed", "local_only")),
                KnowledgeSource.content.is_not(None),
            )
        )
    ).all()
    documents = [
        (source.name, source.content)
        for source in sources
        if isinstance(source.content, str) and source.content.strip()
    ]
    if knowledge_base.content:
        documents.append((knowledge_base.name, knowledge_base.content))
    matches = rank_knowledge(query, documents)
    if not matches:
        return None
    context = "\n\n".join(f"Source: {match.source}\n{match.text}" for match in matches)
    return context[:MAX_CONTEXT_CHARS]
