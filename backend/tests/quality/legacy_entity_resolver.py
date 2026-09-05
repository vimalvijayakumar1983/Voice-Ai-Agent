"""Frozen dbe2e45 resolver: differential oracle, never imported by runtime."""

import re
from collections.abc import Iterable
from difflib import SequenceMatcher
from typing import Any

from app.services.speech_lexicon import (
    _TERM_TOKEN,
    EntityResolution,
    SpeechLexiconEntry,
    _coerce_entries,
    _latin_fold,
    _normalized,
    _phonetic_key,
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
