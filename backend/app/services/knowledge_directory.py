"""Source-qualified directory summaries, never counts of top-k search hits."""

import json
import re
from dataclasses import dataclass


def _key(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


@dataclass(frozen=True)
class DirectoryRequest:
    operation: str
    role: str


def directory_request(query: str, owner: str) -> DirectoryRequest | None:
    """Parse operation and scope separately; never discard unknown filters.

    Directory/list wording describes the source, not a specialty or a location.
    A count and a list reformulation may share a scope, but GP and all-doctor
    requests must never share an aggregate. Unknown qualifiers use normal search.
    """
    text = re.sub(r"\b" + re.escape(owner.casefold()) + r"\b", " ", query.casefold())
    operation = "count" if re.search(r"\b(?:how many|total|number of|count)\b", text) else "list"
    if operation == "list" and not re.search(r"\b(?:which|who|list|names?|directory)\b", text):
        return None
    role = (
        "general_practitioner"
        if re.search(r"\b(?:general practitioners?|gps?)\b", text)
        else "doctor"
    )
    if role == "general_practitioner":
        text = re.sub(r"\b(?:general practitioners?|gps?)\b", " doctors ", text)
    words = set(_key(text).split())
    framing = set(
        (
            "how many what is total number count of doctor doctors do does you have are there "
            "in at the your our currently please tell me all altogether "
            "listed listing directory directories published website site on from "
            "which who list name names can i consult a an"
        ).split()
    )
    # Preserve filters, dates, locations and comparisons by declining them.
    if not words & {"doctor", "doctors"} or words - framing:
        return None
    return DirectoryRequest(operation=operation, role=role)


def requests_doctor_count(query: str, owner: str) -> bool:
    return directory_request(query, owner) == DirectoryRequest("count", "doctor")


def shared_directory_request(queries: tuple[str, ...], owner: str) -> DirectoryRequest | None:
    requests = [directory_request(query, owner) for query in queries]
    if not requests or any(request is None for request in requests):
        return None
    if len({request.role for request in requests}) != 1:
        return None
    return DirectoryRequest(
        "count" if any(request.operation == "count" for request in requests) else "list",
        requests[0].role,
    )


def published_doctor_names(
    sources: list[tuple[str, dict]], *, max_chars: int = 3600, role: str = "doctor"
) -> str | None:
    """Count explicitly titled, evidence-backed names, not inferred professions.

    No clinical synonym inference, fuzzy name merging or 'current staff' claim.
    Missing extraction/aliases cannot make this a verified real-world headcount.
    """
    if role not in {"doctor", "general_practitioner"}:
        return None
    names: dict[str, dict] = {}
    for source, data in sources:
        if not isinstance(data, dict):
            continue
        entities = data.get("entities")
        if not isinstance(entities, list):
            continue
        if len(entities) > 2000:
            return None
        for entity in entities:
            if not isinstance(entity, dict) or entity.get("entity_type") != "person":
                continue
            name = str(entity.get("name") or "").strip()
            evidence = str(entity.get("evidence") or "").strip()
            if not re.match(r"^(?:dr\.?|doctor)\s+", name, re.I):
                continue
            if not name or not evidence or f" {_key(name)} " not in f" {_key(evidence)} ":
                continue
            if role == "general_practitioner" and not re.search(
                r"\bgeneral practitioner\b", evidence, re.I
            ):
                continue
            identity = re.sub(r"^(?:dr|doctor)\s+", "", _key(name))
            if len(identity.split()) < 1:
                continue
            entry = names.setdefault(identity, {"name": name, "sources": []})
            if source not in entry["sources"]:
                entry["sources"].append(source)
    if not names or len(names) > 20_000:
        return None
    summary = {
        "directory_scope": "Distinct explicitly doctor-titled names in the approved sources",
        "published_name_count": len(names),
        "role_filter": role,
        "complete_current_staff_count_verified": False,
        "qualification": "This counts published names, not a confirmed current staff total. "
        "The sources may omit doctors or contain name variants. "
        "State the published-name count with this limitation. "
        "Listed names are examples when entries_omitted is nonzero, not the complete roster.",
        "entries": [names[key] for key in sorted(names)[:5]],
    }
    while True:
        summary["entries_omitted"] = len(names) - len(summary["entries"])
        encoded = json.dumps(summary, ensure_ascii=False)
        if len(encoded) <= max_chars:
            return encoded
        if not summary["entries"]:
            return None
        summary["entries"].pop()
