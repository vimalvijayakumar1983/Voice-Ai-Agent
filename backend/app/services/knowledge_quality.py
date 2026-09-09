"""Deterministic extraction checks; extracted bytes are not proof of answerability.

These checks identify known failures, not complete factual coverage of a website.
They never call an LLM or manufacture a missing directory entry.
"""

import hashlib
import json
import re
from urllib.parse import urlsplit

QUALITY_CHECK_VERSION = "knowledge-readiness-1"
MAX_PUBLICATION_PROBES = 24


def missing_doctor_directory(title: str, url: str, text: str) -> bool:
    path = urlsplit(url).path.casefold().rstrip("/")
    directory = path.endswith(("/doctors", "/our-doctors", "/doctors-directory")) or bool(
        re.search(r"\b(?:our doctors|best doctors|doctors directory)\b", title, re.I)
    )
    return directory and not re.search(r"\b(?:Dr\.?|Doctor)\s+[A-Z][\w'-]+", text)


def source_fingerprint(source) -> str:
    return hashlib.sha256(
        json.dumps(
            [
                source.name,
                source.location,
                source.raw_content,
                source.content,
                source.structured_content,
            ],
            sort_keys=True,
            default=str,
        ).encode()
    ).hexdigest()


def source_quality(source) -> tuple[str, list[str]]:
    content = str(source.raw_content or source.content or "").strip()
    if "\n\nSOURCE CONTENT" in content:
        content = content.split("\n\nSOURCE CONTENT", 1)[1].strip()
    if not content:
        return "needs_repair", ["No readable source content was extracted."]
    metadata = source.source_metadata or {}
    compilation = metadata.get("upload_compile") or {}
    if source.status in {"pending", "processing"} or compilation.get("status") in {
        "queued",
        "processing",
    }:
        return "processing", ["Source extraction or compilation is still running."]
    if source.status == "failed":
        return "needs_repair", [source.error_message or "Source processing failed."]
    missing_directory = source.source_type in {
        "website",
        "url",
        "sitemap",
    } and missing_doctor_directory(str(source.name or ""), str(source.location or ""), content)
    # A doctor listing page without even one named clinician is demonstrably
    # incomplete. Navigation words such as 'Doctors' do not satisfy this check.
    facts = (source.structured_content or {}).get("facts") or []
    person_entity = any(
        isinstance(e, dict) and e.get("entity_type") == "person" and e.get("evidence")
        for e in (source.structured_content or {}).get("entities", [])
    )
    if missing_directory and not person_entity:
        return "needs_repair", [
            "This looks like a doctor directory, but no named doctor entries were extracted. "
            "Headings and navigation are not a usable directory."
        ]
    if facts:
        return "not_tested", [
            "Source-backed facts extracted. Agent retrieval must be checked before publication."
        ]
    return "text_only", ["Readable text extracted; answer coverage has not been verified."]


def retrieval_probes(source, *, maximum: int = 3) -> list[tuple[str, str, str]]:
    """Bounded representative questions and exact expected source-backed values."""
    result = []
    for fact in (source.structured_content or {}).get("facts", []):
        if not isinstance(fact, dict):
            continue
        subject, value = str(fact.get("subject") or ""), str(fact.get("value") or "")
        if not subject or not value or not fact.get("evidence"):
            continue
        phrases = fact.get("search_phrases") or []
        if not phrases:
            phrases = [f"{subject} {fact.get('predicate', '')}"]
        for phrase in phrases[:maximum]:
            if isinstance(phrase, str) and phrase.strip():
                result.append((phrase, subject, value))
        if len(result) >= maximum:
            break
    return result[:maximum]
