"""Source-qualified directory summaries, never counts of top-k search hits."""

import json
import re


def _key(value: str) -> str:
    return " ".join(re.findall(r"\w+", value.casefold()))


def requests_doctor_count(query: str, owner: str) -> bool:
    text = query.casefold().replace(owner.casefold(), " ")
    if not re.search(r"\b(?:how many|total|number of)\b", text):
        return False
    words = set(_key(text).split())
    framing = set(
        (
            "how many what is total number of doctor doctors do does you have are there "
            "in at the your our currently please tell me all altogether"
        ).split()
    )
    # Preserve filters, dates, locations and comparisons by declining them.
    return bool(words & {"doctor", "doctors"}) and not (words - framing)


def published_doctor_names(sources: list[tuple[str, dict]]) -> str | None:
    """Count explicitly titled, evidence-backed names, not inferred professions.

    No clinical synonym inference, fuzzy name merging or 'current staff' claim.
    Missing extraction/aliases cannot make this a verified real-world headcount.
    """
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
            identity = re.sub(r"^(?:dr|doctor)\s+", "", _key(name))
            if len(identity.split()) < 1:
                continue
            entry = names.setdefault(identity, {"name": name, "sources": []})
            if source not in entry["sources"]:
                entry["sources"].append(source)
    if not names or len(names) > 200:
        return None
    return json.dumps(
        {
            "directory_scope": "Distinct explicitly doctor-titled names in the approved sources",
            "published_name_count": len(names),
            "complete_current_staff_count_verified": False,
            "qualification": "This counts published names, not a confirmed current staff total. "
            "The sources may omit doctors or contain name variants. "
            "State the published-name count with this limitation.",
            "entries": [names[key] for key in sorted(names)],
        },
        ensure_ascii=False,
    )
