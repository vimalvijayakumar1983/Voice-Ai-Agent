"""Records: the unit of VAV knowledge shared by every source type.

A record keeps together what a page or document presents together: a
directory card (name, specialty, experience), a table row (item, price), a
list entry, a heading, or a prose paragraph.  Extractors for HTML, PDF and
pasted text all produce records; the compiler grounds facts against the
rendered records; the coverage report checks that the record-like items were
captured as facts before a source can be approved without acknowledgement.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Literal

RecordKind = Literal["heading", "paragraph", "list_item", "table_row", "card", "field"]

RECORD_SEPARATOR = " | "
RECORD_LIKE_KINDS: frozenset[str] = frozenset({"card", "table_row", "list_item", "field"})
MAX_STORED_RECORDS = 2_000
MAX_UNCOVERED_EXAMPLES = 300
_SPACE_RE = re.compile(r"\s+")
_GROUNDING_SEPARATOR_RE = re.compile(r"[^\w]+", re.UNICODE)
_BULLET_RE = re.compile(r"^\s*(?:[-*•▪◦]|\d{1,3}[.)])\s+(.*)$")
_MARKDOWN_HEADING_RE = re.compile(r"^\s*#{1,4}\s+(.*)$")
_FIELD_RE = re.compile(r"^\s*([^:|\t]{1,60}):\s+(.+)$")
# Calls to action carry no knowledge and repeat on every card.
CALL_TO_ACTION_FRAGMENTS: frozenset[str] = frozenset(
    {
        "apply",
        "apply now",
        "book",
        "book appointment",
        "book an appointment",
        "book now",
        "buy now",
        "call now",
        "click here",
        "contact us",
        "details",
        "discover more",
        "enquire now",
        "explore",
        "find out more",
        "get started",
        "know more",
        "learn more",
        "more",
        "more details",
        "more info",
        "read more",
        "see more",
        "view",
        "view all",
        "view details",
        "view more",
        "view profile",
        "whatsapp",
        # Pagination and filter controls carry no knowledge either.
        "next",
        "next page",
        "previous",
        "previous page",
        "prev",
        "load more",
        "show more",
        "see all",
        "show all",
        "all specialties",
        "all departments",
        "all services",
        "any",
        "select",
        "search",
        "filter",
        "reset",
        "submit",
        "close",
        "menu",
    }
)
# Words that name the kind of thing rather than the thing itself. A fact that
# says "Physiotherapy" has captured the record "Physiotherapy Department".
_COVERAGE_GENERIC_TOKENS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "at",
        "by",
        "for",
        "in",
        "of",
        "on",
        "or",
        "our",
        "the",
        "to",
        "with",
        "your",
        "department",
        "departments",
        "centre",
        "center",
        "clinic",
        "clinics",
        "service",
        "services",
        "unit",
        "section",
    }
)


@dataclass(frozen=True)
class KnowledgeRecord:
    kind: RecordKind
    text: str
    heading_path: tuple[str, ...] = ()
    page: int | None = None
    fragments: tuple[str, ...] = field(default=())

    def to_dict(self) -> dict:
        value: dict = {"kind": self.kind, "text": self.text}
        if self.heading_path:
            value["heading_path"] = list(self.heading_path)
        if self.page is not None:
            value["page"] = self.page
        if self.fragments:
            value["fragments"] = list(self.fragments)
        return value


def normalize_fragment(value: str) -> str:
    return _SPACE_RE.sub(" ", str(value or "")).strip()


def grounding_normalized(value: str) -> str:
    """Normalise presentation-only punctuation without changing words or digits."""
    return _SPACE_RE.sub(" ", _GROUNDING_SEPARATOR_RE.sub(" ", value)).strip().casefold()


def is_call_to_action(fragment: str) -> bool:
    folded = grounding_normalized(fragment)
    return folded in CALL_TO_ACTION_FRAGMENTS or (
        len(folded.split()) <= 3 and folded.endswith((" more", " now", " here"))
    )


def make_record(
    kind: RecordKind,
    fragments: Iterable[str],
    *,
    heading_path: Sequence[str] = (),
    page: int | None = None,
) -> KnowledgeRecord | None:
    """Build one record from text fragments, dropping calls to action and repeats."""
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in fragments:
        fragment = normalize_fragment(raw).strip(" |")
        if len(fragment) < 2 or is_call_to_action(fragment):
            continue
        key = fragment.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(fragment)
    if not cleaned:
        return None
    text = cleaned[0] if len(cleaned) == 1 else RECORD_SEPARATOR.join(cleaned)
    return KnowledgeRecord(
        kind=kind,
        text=text,
        heading_path=tuple(heading_path),
        page=page,
        fragments=tuple(cleaned) if len(cleaned) > 1 else (),
    )


def dedupe_records(records: Iterable[KnowledgeRecord]) -> list[KnowledgeRecord]:
    """Drop whole records repeated verbatim within the same heading context.

    The same row under two different headings (opening hours for Branch A and
    for Branch B) means two different things, so both occurrences are kept; a
    label shared by records is never dropped either.
    """
    unique: list[KnowledgeRecord] = []
    seen: set[tuple[tuple[str, ...], str]] = set()
    for record in records:
        key = (
            tuple(part.casefold() for part in record.heading_path),
            record.text.casefold(),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(record)
    return unique


def render_records(records: Iterable[KnowledgeRecord]) -> str:
    """Render records as the paragraph-separated text the compiler grounds against.

    Every record is its own paragraph, so a directory card stays on one line
    with its name, specialty and experience together, and a heading directly
    precedes the records it introduces.
    """
    return "\n\n".join(record.text for record in records if record.text.strip()).strip()


def records_from_text(text: str, *, page: int | None = None) -> list[KnowledgeRecord]:
    """Turn pasted or plain text into records: headings, bullets, fields, prose."""
    records: list[KnowledgeRecord] = []
    heading_path: list[str] = []
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            record = make_record(
                "paragraph", [" ".join(paragraph)], heading_path=heading_path, page=page
            )
            if record:
                records.append(record)
            paragraph.clear()

    for raw_line in text.splitlines():
        line = normalize_fragment(raw_line)
        if not line:
            flush_paragraph()
            continue
        heading = _MARKDOWN_HEADING_RE.match(raw_line)
        if heading:
            flush_paragraph()
            level = len(raw_line.lstrip()) - len(raw_line.lstrip().lstrip("#"))
            del heading_path[max(level - 1, 0) :]
            heading_path.append(normalize_fragment(heading.group(1)))
            record = make_record("heading", [heading.group(1)], heading_path=heading_path[:-1])
            if record:
                records.append(record)
            continue
        bullet = _BULLET_RE.match(raw_line)
        if bullet:
            flush_paragraph()
            record = make_record(
                "list_item", bullet.group(1).split("|"), heading_path=heading_path, page=page
            )
            if record:
                records.append(record)
            continue
        if "\t" in raw_line or " | " in raw_line:
            flush_paragraph()
            cells = re.split(r"\t+|\s\|\s", raw_line)
            record = make_record("table_row", cells, heading_path=heading_path, page=page)
            if record:
                records.append(record)
            continue
        field_match = _FIELD_RE.match(raw_line)
        if field_match and len(field_match.group(2)) <= 200 and not paragraph:
            record = make_record(
                "field",
                [f"{normalize_fragment(field_match.group(1))}: {field_match.group(2)}"],
                heading_path=heading_path,
                page=page,
            )
            if record:
                records.append(record)
            continue
        paragraph.append(line)
    flush_paragraph()
    return dedupe_records(records)


def records_to_payload(records: Sequence[KnowledgeRecord]) -> list[dict]:
    return [record.to_dict() for record in records[:MAX_STORED_RECORDS]]


def records_from_payload(payload: object) -> list[KnowledgeRecord]:
    records: list[KnowledgeRecord] = []
    if not isinstance(payload, list):
        return records
    for item in payload:
        if not isinstance(item, dict) or not str(item.get("text") or "").strip():
            continue
        kind = str(item.get("kind") or "paragraph")
        if kind not in {"heading", "paragraph", "list_item", "table_row", "card", "field"}:
            kind = "paragraph"
        page = item.get("page")
        records.append(
            KnowledgeRecord(
                kind=kind,  # type: ignore[arg-type]
                text=str(item["text"]),
                heading_path=tuple(str(part) for part in (item.get("heading_path") or [])),
                page=int(page) if isinstance(page, int) else None,
                fragments=tuple(str(part) for part in (item.get("fragments") or [])),
            )
        )
    return records


def _fragment_tokens(fragment: str) -> list[str]:
    return [
        token
        for token in grounding_normalized(fragment).split()
        if token not in _COVERAGE_GENERIC_TOKENS
    ]


def _fact_captures_record(record: KnowledgeRecord, bundle: str, bundle_tokens: set[str]) -> bool:
    """Whether one fact carries every field of a record.

    A field counts when the fact quotes it verbatim, or when every word that
    identifies it (ignoring generic nouns such as "department") appears in
    that same fact.  Words from different facts are never combined.
    """
    for fragment in record.fragments or (record.text,):
        if f" {grounding_normalized(fragment)} " in bundle:
            continue
        tokens = _fragment_tokens(fragment)
        if tokens and not all(token in bundle_tokens for token in tokens):
            return False
    return True


def uncovered_records(
    records: Sequence[KnowledgeRecord], facts: Sequence[dict]
) -> list[KnowledgeRecord]:
    """Return the record-like records that no single accepted fact captures.

    One bundle per fact: a record counts as captured only when a single fact
    carries all of its fields together. Matching fragments across unrelated
    facts would let an omitted row pass because its name appears in one fact
    and its price in another.
    """
    fact_bundles = [
        " "
        + grounding_normalized(
            " ".join(
                str(fact.get(key) or "") for key in ("subject", "predicate", "value", "evidence")
            )
        )
        + " "
        for fact in facts
        if isinstance(fact, dict)
    ]
    bundle_tokens = [set(bundle.split()) for bundle in fact_bundles]
    return [
        record
        for record in records
        if record.kind in RECORD_LIKE_KINDS
        and not any(
            _fact_captures_record(record, bundle, tokens)
            for bundle, tokens in zip(fact_bundles, bundle_tokens, strict=True)
        )
    ]


def render_focused_records(
    title: str, records: Sequence[KnowledgeRecord], *, separator: str = " › "
) -> str:
    """Render records for a second compile pass with their page context inline.

    A single-entry record such as a department name under a "Departments"
    heading carries no explicit subject of its own. Prefixing each line with
    the page title and heading path keeps the organization and the heading
    inside the record's own evidence span, so the compiler can attribute the
    entry without inferring anything the page did not present together.
    """
    context_title = " ".join(title.replace(" | ", " - ").split())
    lines = []
    for record in records:
        text = record.text.strip()
        if not text:
            continue
        context = [context_title] if context_title else []
        context.extend(record.heading_path)
        lines.append(separator.join((*context, text)) if context else text)
    return "\n\n".join(lines).strip()


def coverage_report(
    records: Sequence[KnowledgeRecord],
    structured: dict | None,
    *,
    requested_mode: str,
    effective_mode: str,
) -> dict:
    """Measure whether the record-like items became grounded facts.

    A source is ``complete`` when every card, table row, list entry and field
    is captured by at least one accepted fact, ``partial`` when some are not,
    ``not_compiled`` when AI compilation was requested but did not run, and
    ``skipped`` when the operator explicitly chose fast (deterministic) mode.
    """
    structured = structured or {}
    facts = [fact for fact in (structured.get("facts") or []) if isinstance(fact, dict)]
    entities = [entity for entity in (structured.get("entities") or []) if isinstance(entity, dict)]
    validation = structured.get("validation") or {}
    facts_accepted = int(validation.get("facts_accepted", len(facts)) or 0)
    facts_rejected = int(validation.get("facts_rejected", 0) or 0)

    record_like = [record for record in records if record.kind in RECORD_LIKE_KINDS]
    missing = {id(record) for record in uncovered_records(record_like, facts)}
    subject_blob = " ".join(
        grounding_normalized(str(item.get("subject") or item.get("name") or ""))
        for item in (*facts, *entities)
    )
    subject_blob = f" {subject_blob} "

    uncovered: list[str] = []
    covered = 0
    entities_found: list[str] = []
    entities_with_facts = 0
    for record in record_like:
        if id(record) in missing:
            uncovered.append(record.text[:200])
        else:
            covered += 1
        if record.kind in {"card", "table_row"} and record.fragments:
            entity = record.fragments[0]
            entities_found.append(entity)
            if f" {grounding_normalized(entity)} " in subject_blob:
                entities_with_facts += 1

    if requested_mode == "fast":
        status = "skipped"
    elif effective_mode != "ai_verified":
        status = "not_compiled"
    elif uncovered or (not record_like and facts_accepted == 0):
        status = "partial"
    elif not record_like:
        # Prose only: there is no denominator, so "complete" would overstate
        # what was verified.
        status = "unstructured"
    else:
        status = "complete"
    return {
        "status": status,
        "record_total": len(record_like),
        "records_covered": covered,
        "uncovered": uncovered[:MAX_UNCOVERED_EXAMPLES],
        "uncovered_total": len(uncovered),
        "entities_found": len(entities_found),
        "entities_with_facts": entities_with_facts,
        "facts_accepted": facts_accepted,
        "facts_rejected": facts_rejected,
        "requested_mode": requested_mode,
        "effective_mode": effective_mode,
    }


def coverage_blocks_approval(coverage: object) -> str | None:
    """Return why a source's coverage blocks approval outright, if it does.

    A source compiled before coverage existed carries no report; it is shown as
    unmeasured but is not blocked, so an existing workspace keeps working until
    its sources are recompiled. A source whose AI compilation was requested but
    did not run is blocked: its facts were never verified.
    """
    if not isinstance(coverage, dict):
        return None
    status = str(coverage.get("status") or "")
    if status == "not_compiled":
        return "AI compilation did not run; recompile it with an OpenAI key configured"
    return None


def coverage_issue(coverage: object) -> str | None:
    """One reviewer-facing sentence describing a source's coverage."""
    if not isinstance(coverage, dict):
        return "Coverage not measured yet. Recompile this source to measure it."
    status = str(coverage.get("status") or "")
    total = int(coverage.get("record_total") or 0)
    covered = int(coverage.get("records_covered") or 0)
    facts = int(coverage.get("facts_accepted") or 0)
    if status == "skipped":
        return "Coverage not measured: fast (deterministic) extraction was requested."
    if status == "not_compiled":
        return (
            "AI compilation did not run, so no facts were verified. Recompile with an OpenAI key."
        )
    if status == "complete":
        return (
            f"Coverage complete: {covered} of {total} records captured as {facts} verified facts."
        )
    if status == "unstructured":
        return (
            f"Prose only: {facts} verified facts were extracted, but the page has no cards, rows, "
            "lists or fields to measure against, so completeness is not verified."
        )
    examples = [str(item) for item in (coverage.get("uncovered") or [])][:3]
    remaining = int(coverage.get("uncovered_total") or len(examples)) - len(examples)
    detail = "; ".join(examples)
    if remaining > 0:
        detail += f" (+{remaining} more)"
    return (
        f"Partial coverage: {covered} of {total} records captured as verified facts. "
        f"Not captured: {detail}"
    )
