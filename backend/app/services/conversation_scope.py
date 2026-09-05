"""Explicit, call-local company selection. Never derive authority from an agent name."""

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator


def company_key(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", value.casefold().replace("&", " and ")))


def routing_text(value: str) -> str:
    """Remove discourse noise without changing stored/provider transcripts or constraints."""
    value = value.strip().replace("’", "'")
    value = re.sub(r"\b(who|what|where|when|how)'s\b", r"\1 is", value, flags=re.I)
    value = re.sub(r"\b(?:uh|um|erm)\b[,\s]*", "", value, flags=re.I)
    value = re.sub(r"^(?:(?:sorry|okay|ok|well|so|please)[,\s]+)+", "", value, flags=re.I)
    value = re.sub(r"^and\s+(?=(?:who|what|how|where|when)\b)", "", value, flags=re.I)
    value = re.sub(
        r"^(?:i (?:want|wanted|would like) to (?:know|get)|i'd like to know)\s+",
        "",
        value,
        flags=re.I,
    )
    return " ".join(value.split()).strip()


_LABEL_GENERIC = {
    "the",
    "a",
    "an",
    "and",
    "company",
    "group",
    "medical",
    "center",
    "centre",
    "llc",
    "limited",
}


def candidate_companies(
    text: str, scope: "KnowledgeCompanyScope", pending: tuple[str, ...] = ()
) -> tuple[str, ...]:
    """Resolve only allowed companies. Shared prefixes ask a choice, never grant access."""
    exact = mentioned_companies(text, scope)
    if exact:
        return exact
    normalized = company_key(text)
    if pending:
        terms = set(normalized.split()) - _LABEL_GENERIC - {"one", "please", "mean", "i"}
        choices = tuple(
            c.name
            for c in scope.companies
            if c.name in pending
            and terms
            and terms <= (set(company_key(c.name).split()) - _LABEL_GENERIC)
        )
        if choices:
            return choices
    prefixes: dict[str, list[str]] = {}
    for company in scope.companies:
        tokens = company_key(company.name).split()
        for width in range(2, len(tokens)):
            prefix = " ".join(tokens[:width])
            if len(set(tokens[:width]) - _LABEL_GENERIC) >= 2:
                prefixes.setdefault(prefix, []).append(company.name)
    for prefix in sorted(prefixes, key=len, reverse=True):
        if len(prefixes[prefix]) > 1 and re.search(r"\b" + re.escape(prefix) + r"\b", normalized):
            return tuple(prefixes[prefix])
    return ()


def company_request_remainder(text: str, company: "KnowledgeCompany") -> str:
    remainder = company_key(text)
    for label in sorted([company.name, *company.aliases], key=len, reverse=True):
        remainder = re.sub(r"\b" + re.escape(company_key(label)) + r"\b", "", remainder)
    return " ".join(remainder.split())


def person_company_directory(
    sources: list[dict], scope: "KnowledgeCompanyScope"
) -> dict[str, tuple[str, ...]]:
    """Read only approved company-owned profiles; never infer membership from co-occurrence."""
    allowed = {company_key(c.name): c.name for c in scope.companies}
    directory: dict[str, list[str]] = {}
    for source in sources:
        for fact in (source or {}).get("facts", []):
            if not isinstance(fact, dict):
                continue
            company = allowed.get(company_key(str(fact.get("subject") or "")))
            predicate = str(fact.get("predicate") or "")
            if not company or not predicate.casefold().startswith("person profile:"):
                continue
            name = predicate.split(":", 1)[1].strip()
            evidence = " " + company_key(str(fact.get("evidence") or "")) + " "
            if (
                not name
                or not fact.get("value")
                or any(
                    " " + company_key(v) + " " not in evidence
                    for v in [name, company, str(fact.get("value") or "")]
                )
            ):
                continue
            directory.setdefault(name, []).append(company)
    return {name: tuple(dict.fromkeys(companies)) for name, companies in directory.items()}


class KnowledgeCompany(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    name: str = Field(min_length=2, max_length=160)
    aliases: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def valid_labels(self):
        labels = [self.name, *self.aliases]
        if any(not company_key(label) or len(label) > 160 for label in labels):
            raise ValueError(
                "Company names and aliases must be nonempty and at most 160 characters"
            )
        return self


class KnowledgeCompanyScope(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    semantic_retrieval_enabled: bool = False
    default_company: str | None = Field(None, max_length=160)
    companies: list[KnowledgeCompany] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def unique_companies(self):
        owners: dict[str, str] = {}
        for company in self.companies:
            for label in [company.name, *company.aliases]:
                key = company_key(label)
                if key in owners and owners[key] != company.name:
                    raise ValueError("Company names and aliases must identify one company only")
                owners[key] = company.name
        names = [company.name for company in self.companies]
        if len(set(map(company_key, names))) != len(names):
            raise ValueError("Duplicate companies are not allowed")
        if self.default_company is not None and self.default_company not in names:
            raise ValueError("Default company must match an allowed company name")
        return self


def mentioned_companies(text: str, scope: KnowledgeCompanyScope) -> tuple[str, ...]:
    """Literal configured aliases only; longer nested names win, never fuzzy authority."""
    normalized = f" {company_key(text)} "
    matches: list[tuple[int, int, str]] = []
    for company in scope.companies:
        for label in [company.name, *company.aliases]:
            term = f" {company_key(label)} "
            for match in re.finditer(re.escape(term), normalized):
                matches.append((match.start(), match.end(), company.name))
    selected = [
        name
        for start, end, name in matches
        if not any(s <= start and e >= end and e - s > end - start for s, e, _ in matches)
    ]
    return tuple(dict.fromkeys(selected))


SCOPE_REPLY_PREFIX = "VAV_SCOPE_REPLY:"
SPOKEN_REPEAT_PREFIX = "VAV_SPOKEN_REPEAT:"


def scope_reply(message: str) -> str:
    return SCOPE_REPLY_PREFIX + message


def repeat_spoken(content: str, *, slow: bool) -> str:
    # Preserve punctuation, country codes and extension boundaries; slow only digit runs.
    if slow:
        content = re.sub(r"\d{2,}", lambda match: ", ".join(match.group()), content)
    return SPOKEN_REPEAT_PREFIX + content
