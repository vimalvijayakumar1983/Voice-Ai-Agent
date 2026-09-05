"""Company-owned list retrieval, separate from top-k question answering.

Counts describe the published index, never completeness of a real-world roster.
Records enter through the exact index's evidence validation and revision fence.
"""

import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.services.conversation_scope import company_key

COLLECTION_PREFIX = "VAV_COLLECTION_V1:"
PAGE_SIZE = 5
PAGE_CHAR_BUDGET = 1000


@dataclass(frozen=True)
class CollectionRecord:
    subject: str
    category: str
    name: str
    role: str
    evidence_id: str


def collection_record(*, subject, predicate, value, evidence, evidence_id):
    """Classify already grounded atomic facts; never infer cross-company membership."""
    role = predicate
    name = value
    normalized = company_key(predicate)
    if predicate.casefold().startswith("person profile:"):
        name = predicate.split(":", 1)[1].strip()
        role = value
        if not name or f" {company_key(name)} " not in f" {company_key(evidence)} ":
            return None
        normalized = company_key(role)
    if re.search(r"\b(?:former|previous|retired|not|future|proposed)\b", normalized):
        return None
    if re.search(
        r"\b(?:director|chairman|chairperson|president|ceo|cfo|chief executive|chief financial)\b",
        normalized,
    ):
        role_words = set(normalized.split())
        allowed_role_words = set(
            "director managing executive ceo cfo chief financial officer chairman chairperson "
            "president board member and group vice senior deputy co founder".split()
        )
        if role_words - allowed_role_words or len(name.split()) > 12:
            return None
        # The role itself, not only the person's name, must occur in evidence.
        if f" {company_key(role)} " not in f" {company_key(evidence)} ":
            return None
        category = "leadership"
    elif normalized in {"business segment", "division", "service division", "business division"}:
        category = "divisions"
    elif normalized in {
        "service",
        "service offering",
        "offering",
        "treatment",
        "available service",
    }:
        category = "services"
    elif normalized in {"branch", "branch name", "office branch", "branch location"}:
        category = "branches"
    else:
        return None
    return CollectionRecord(subject, category, name, role, evidence_id)


_CATEGORIES = {
    "directors": r"\b(?:board of )?directors\b|\b(?:all|every|each) director\b|\bboard members\b",
    "leadership": r"\bleadership(?: team)?\b|\bmanagement team\b",
    "divisions": r"\b(?:business )?divisions\b|\bbusiness segments\b",
    "services": r"\bservices\b|\btreatments\b",
    "branches": r"\bbranches\b|\b(?:all|every|each) branch\b|\boffice locations\b",
}
_REQUEST_WORDS = set(
    (
        "who what which where how many are is the a an all every complete full list show tell me "
        "give us your their our its of for at about and please do does you they have offer provide "
        "operate can could would like to know name names available currently current published "
        "entire enumerate more this"
    ).split()
)


def collection_request(query: str, company: str) -> tuple[str | None, str | None]:
    # Possession identifies the owner; it is not a geographic or content filter.
    query = re.sub(r"[’']s\b", "", query)
    text = company_key(query)
    text = re.sub(r"\b" + re.escape(company_key(company)) + r"\b", "", text)
    text = re.sub(r"\b(?:the )?(?:group|company|organisation|organization)\b", "", text)
    categories = [key for key, pattern in _CATEGORIES.items() if re.search(pattern, text)]
    if not categories or re.search(r"\b(?:not|except|without)\b", text):
        return None, None
    if len(categories) > 1:
        return None, "Which list would you like first: " + " or ".join(categories) + "?"
    category = categories[0]
    remainder = re.sub(_CATEGORIES[category], "", text)
    if set(remainder.split()) - _REQUEST_WORDS:
        # Do not ignore filters, dates, or specific service names by answering
        # with an unfiltered collection. Existing descriptive retrieval handles it.
        if re.search(r"\b(?:all|every|list|how many|complete|full)\b", text):
            return None, (
                f"I can list the published {category} for {company}, "
                "but I can't apply that filter reliably. Please ask for the unfiltered list "
                "or a specific entry."
            )
        return None, None
    return category, None


class CollectionItem(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=1000)
    roles: list[str] = Field(default_factory=list, max_length=30)
    evidence_ids: list[str] = Field(min_length=1, max_length=100)


class CollectionPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str
    category: str
    query: str
    revision: str
    offset: int = Field(ge=0)
    next_offset: int = Field(ge=0)
    total: int = Field(ge=0)
    items: list[CollectionItem] = Field(max_length=PAGE_SIZE)
    coverage: str = "indexed_only"
    blocked: bool = False
    count_only: bool = False
    related_category: bool = False

    def encode(self) -> str:
        return COLLECTION_PREFIX + self.model_dump_json()


@dataclass
class CollectionPlayback:
    """An active page exists before speech completes; only heard entries advance.

    If a late callback arrives after a new page was dispatched it must be ignored
    by the owner. Repeating an uncertain entry is preferable to skipping it.
    """

    page: CollectionPage
    confirmed_offset: int

    def observe(self, content: str) -> None:
        expected = collection_reply(self.page)
        heard = company_key(content)
        if heard == company_key(expected):
            self.confirmed_offset = self.page.next_offset
            return
        end = 0
        completed = 0
        for item in self.page.items:
            entry = item.name + (" — " + ", ".join(item.roles) if item.roles else "")
            position = expected.find(entry, end)
            if position < 0:
                break
            end = position + len(entry)
            prefix = company_key(expected[:end])
            if heard == prefix or heard.startswith(prefix + " "):
                completed += 1
            else:
                break
        self.confirmed_offset = max(self.confirmed_offset, self.page.offset + completed)


def decode_collection(value: str | None) -> CollectionPage | None:
    if not value or not value.startswith(COLLECTION_PREFIX):
        return None
    try:
        return CollectionPage.model_validate_json(value[len(COLLECTION_PREFIX) :])
    except (ValidationError, ValueError):
        return None


def retrieve_collection(records, *, company, category, query, revision, offset=0, blocked=False):
    grouped: dict[str, CollectionItem] = {}
    for record in records:
        if company_key(record.subject) != company_key(company):
            continue
        if category == "directors":
            if record.category != "leadership" or not re.search(
                r"\bdirector\b", company_key(record.role)
            ):
                continue
        elif record.category != category:
            continue
        key = company_key(record.name)
        item = grouped.setdefault(
            key, CollectionItem(name=record.name, roles=[], evidence_ids=[record.evidence_id])
        )
        if record.category == "leadership" and record.role.casefold() not in {
            r.casefold() for r in item.roles
        }:
            item.roles.append(record.role)
        if record.evidence_id not in item.evidence_ids and len(item.evidence_ids) < 100:
            item.evidence_ids.append(record.evidence_id)
    if not grouped and category == "services" and not blocked:
        alternative = retrieve_collection(
            records,
            company=company,
            category="divisions",
            query=query,
            revision=revision,
            offset=offset,
            blocked=blocked,
        )
        if alternative.total:
            alternative.related_category = True
            return alternative
    items = sorted(grouped.values(), key=lambda item: company_key(item.name))
    for item in items:
        item.roles = sorted(
            [
                role
                for role in item.roles
                if not any(
                    role != other and f" {company_key(role)} " in f" {company_key(other)} "
                    for other in item.roles
                )
            ],
            key=company_key,
        )
    selected = []
    chars = 0
    for item in items[offset : offset + PAGE_SIZE]:
        size = len(item.name) + sum(map(len, item.roles))
        if selected and chars + size > PAGE_CHAR_BUDGET:
            break
        selected.append(item)
        chars += size
    return CollectionPage(
        company=company,
        category=category,
        query=query,
        revision=revision,
        offset=offset,
        next_offset=offset + len(selected),
        total=len(items),
        items=selected,
        blocked=blocked,
        count_only=bool(re.search(r"\bhow many\b", company_key(query))),
    )


def collection_reply(page: CollectionPage) -> str:
    if page.blocked:
        return (
            "This knowledge list is not fully loaded. "
            "Please narrow the request before I list its entries."
        )
    if not page.total:
        return f"I don't have a published {page.category} list for {page.company}."
    if page.count_only:
        return (
            f"The published knowledge lists {page.total} "
            f"{page.category} entries for {page.company}."
        )
    if not page.items:
        return f"That is the end of the published {page.category} list for {page.company}."
    entries = [
        item.name + (" — " + ", ".join(item.roles) if item.roles else "") for item in page.items
    ]
    prefix = (
        f"The published {page.category} list for {page.company} includes: "
        if page.offset == 0
        else "The next entries are: "
    )
    message = prefix + "; ".join(entries) + "."
    if page.related_category and page.offset == 0:
        message = (
            "The published information identifies divisions rather than a detailed services list. "
            + message
        )
    if page.next_offset < page.total:
        message += (
            f" These are entries {page.offset + 1} to {page.next_offset} "
            f"of {page.total}. Say next for the rest."
        )
    return message
