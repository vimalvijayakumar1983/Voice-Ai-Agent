"""Explicit, call-local company selection. Never derive authority from an agent name."""

import re

from pydantic import BaseModel, ConfigDict, Field, model_validator


def company_key(value: str) -> str:
    return " ".join(re.findall(r"[^\W_]+", value.casefold()))


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
