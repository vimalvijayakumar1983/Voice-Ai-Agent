"""Typed wire format for compiler-verified exact-fact evidence.

Evidence crosses a trust boundary before it reaches the realtime model.  A
line-oriented format is unsafe here because source-controlled subjects and
values may themselves contain separators, newlines, or text that resembles a
control directive.  This module owns a small, versioned JSON envelope shared
by the retrieval and playout paths.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

EXACT_FACT_EVIDENCE_SCHEMA = "vav_exact_fact_evidence_v1"
EXACT_FACT_EVIDENCE_PREFIX = "VAV_EXACT_FACT_EVIDENCE_V1:"
_SPACE_RE = re.compile(r"\s+")
_VALID_ACTIONS = frozenset({"answer", "clarify"})


@dataclass(frozen=True)
class ExactFactWireFact:
    evidence_id: str
    fact_type: str
    subject: str
    predicate: str
    value: str
    quote: str = ""
    source_name: str = ""
    source_url: str = ""


@dataclass(frozen=True)
class ExactFactWireEnvelope:
    response_action: str
    facts: tuple[ExactFactWireFact, ...]
    candidate_count: int


def _text(value: object, *, limit: int) -> str:
    """Return printable, single-line data without interpreting its contents."""

    normalized = _SPACE_RE.sub(" ", str(value or "")).strip()
    return normalized[:limit]


def _fact_payload(fact: ExactFactWireFact, *, detail: str) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "evidence_id": _text(fact.evidence_id, limit=96),
        "fact_type": _text(fact.fact_type, limit=24),
        "subject": _text(fact.subject, limit=80),
        "predicate": _text(fact.predicate, limit=48),
        "value": _text(fact.value, limit=144),
    }
    if detail in {"full", "compact"}:
        payload["quote"] = _text(fact.quote, limit=220 if detail == "full" else 80)
        payload["source"] = {
            "name": _text(fact.source_name, limit=80 if detail == "full" else 48),
            "url": _text(fact.source_url, limit=120 if detail == "full" else 0),
        }
    return payload


def _serialize(
    action: str,
    facts: Sequence[Mapping[str, Any]],
    *,
    candidate_count: int,
) -> str:
    document = {
        "schema": EXACT_FACT_EVIDENCE_SCHEMA,
        "response_action": action,
        "candidate_count": candidate_count,
        "facts": list(facts),
    }
    return EXACT_FACT_EVIDENCE_PREFIX + json.dumps(
        document,
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _minimal_fact_payload(fact: ExactFactWireFact) -> dict[str, Any]:
    """Retain every identity-bearing field in a tightly bounded shape."""

    return {
        "evidence_id": _text(fact.evidence_id, limit=24),
        "fact_type": _text(fact.fact_type, limit=12),
        "subject": _text(fact.subject, limit=24),
        "predicate": _text(fact.predicate, limit=18),
        "value": _text(fact.value, limit=48),
    }


def encode_exact_fact_evidence(
    *,
    response_action: str,
    facts: Sequence[ExactFactWireFact],
    max_chars: int,
    candidate_count: int | None = None,
) -> str | None:
    """Encode as valid bounded JSON; never truncate the serialized document."""

    action = str(response_action).strip().casefold()
    if action not in _VALID_ACTIONS or not facts:
        return None

    bounded_facts = tuple(facts[:5])
    total_candidates = max(
        len(facts),
        int(candidate_count or 0),
    )
    if action == "clarify":
        # Ambiguity is meaningful only when every candidate survives the wire
        # bound. Never turn a two-option clarification into a misleading
        # one-option prompt merely because a later fact did not fit.
        if len(bounded_facts) < 2 or total_candidates < 2:
            return None
        for detail in ("full", "compact", "core"):
            rendered = _serialize(
                action,
                tuple(_fact_payload(fact, detail=detail) for fact in bounded_facts),
                candidate_count=total_candidates,
            )
            if len(rendered) <= max_chars:
                return rendered
        # Spoken clarification is deliberately generic, so two compact facts
        # are sufficient proof that the result is genuinely ambiguous. The
        # explicit total prevents this bounded subset from masquerading as the
        # complete candidate list.
        rendered = _serialize(
            action,
            tuple(_minimal_fact_payload(fact) for fact in bounded_facts[:2]),
            candidate_count=total_candidates,
        )
        return rendered if len(rendered) <= max_chars else None

    # Preserve the number of verified answer facts before preserving quotes or
    # provenance detail.  Otherwise a verbose first fact can consume the wire
    # budget and silently drop the second half of a two-role correction.
    for detail in ("full", "compact", "core"):
        rendered = _serialize(
            action,
            tuple(_fact_payload(fact, detail=detail) for fact in bounded_facts),
            candidate_count=total_candidates,
        )
        if len(rendered) <= max_chars:
            return rendered

    minimal_facts = tuple(_minimal_fact_payload(fact) for fact in bounded_facts)
    rendered = _serialize(action, minimal_facts, candidate_count=total_candidates)
    if len(rendered) <= max_chars:
        return rendered

    # The public caller enforces at least 600 characters. Retain a deterministic
    # valid prefix at smaller future bounds, while candidate_count keeps the
    # omitted count observable to the consumer.
    for count in range(len(minimal_facts) - 1, 0, -1):
        rendered = _serialize(
            action,
            minimal_facts[:count],
            candidate_count=total_candidates,
        )
        if len(rendered) <= max_chars:
            return rendered
    return None


def decode_exact_fact_evidence(value: str) -> ExactFactWireEnvelope | None:
    """Validate and decode the exact versioned envelope, failing closed."""

    if not isinstance(value, str) or not value.startswith(EXACT_FACT_EVIDENCE_PREFIX):
        return None
    try:
        document = json.loads(value[len(EXACT_FACT_EVIDENCE_PREFIX) :])
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("schema") != EXACT_FACT_EVIDENCE_SCHEMA:
        return None
    action = document.get("response_action")
    raw_facts = document.get("facts")
    if action not in _VALID_ACTIONS or not isinstance(raw_facts, list) or not raw_facts:
        return None
    if action == "clarify" and len(raw_facts) < 2:
        return None
    raw_candidate_count = document.get("candidate_count", len(raw_facts))
    if (
        not isinstance(raw_candidate_count, int)
        or isinstance(raw_candidate_count, bool)
        or raw_candidate_count < len(raw_facts)
        or raw_candidate_count > 10_000
    ):
        return None
    if action == "clarify" and raw_candidate_count < 2:
        return None

    facts: list[ExactFactWireFact] = []
    for raw in raw_facts[:5]:
        if not isinstance(raw, dict):
            return None
        required = {
            key: _text(raw.get(key), limit=limit)
            for key, limit in (
                ("evidence_id", 96),
                ("fact_type", 24),
                ("subject", 80),
                ("predicate", 48),
                ("value", 144),
            )
        }
        if not all(required.values()):
            return None
        source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
        facts.append(
            ExactFactWireFact(
                **required,
                quote=_text(raw.get("quote"), limit=220),
                source_name=_text(source.get("name"), limit=80),
                source_url=_text(source.get("url"), limit=120),
            )
        )
    return ExactFactWireEnvelope(
        response_action=action,
        facts=tuple(facts),
        candidate_count=raw_candidate_count,
    )
