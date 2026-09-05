"""Scoped conversation primitives. No business facts or provider calls live here."""

import re
from dataclasses import dataclass, field

from app.services.conversation_scope import (
    KnowledgeCompany,
    KnowledgeCompanyScope,
    candidate_companies,
    company_key,
    company_request_remainder,
    mentioned_companies,
)
from app.services.conversation_state import ConversationState, TurnPlan, match_people

FOUNDATION_FLAG = "conversation_foundation_v1"
ROLE_PATTERN = (
    r"chairman|chairperson|president|ceo|cfo|chief executive officer|"
    r"chief financial officer|managing director|executive director|director"
)


def company_alias_scope(scope: KnowledgeCompanyScope) -> KnowledgeCompanyScope:
    """Derive unique descriptive suffixes only within the authorised directory.

    No fuzzy match, external company discovery, or new authority. Shared labels
    remain ambiguous; the canonical scope is not persisted or rewritten.
    """
    owners: dict[str, set[str]] = {}
    labels: dict[str, set[str]] = {}
    generic = set("the and company group medical center llc limited".split())
    for company in scope.companies:
        values = {company.name, *company.aliases}
        words = company_key(company.name).replace("centre", "center").split()
        for i in range(len(words) - 1):
            suffix = words[i:]
            if set(suffix) - generic:
                values.add(" ".join(suffix))
                if "medical" in suffix and "center" in suffix:
                    values.add(" ".join(w for w in suffix if w != "medical"))
        labels[company.name] = values
        for value in values:
            owners.setdefault(company_key(value), set()).add(company.name)
    return KnowledgeCompanyScope(
        default_company=scope.default_company,
        semantic_retrieval_enabled=scope.semantic_retrieval_enabled,
        companies=[
            KnowledgeCompany(
                name=c.name,
                aliases=list(
                    dict.fromkeys(
                        [
                            *c.aliases,
                            *sorted(
                                v
                                for v in labels[c.name]
                                if v != c.name and owners[company_key(v)] == {c.name}
                            ),
                        ]
                    )
                )[:20],
            )
            for c in scope.companies
        ],
    )


def canonical_company_text(text: str, scope: KnowledgeCompanyScope) -> str:
    """Expand a directory alias without deleting requested details or constraints."""
    text = re.sub(r"[’']s\b", "", text, flags=re.I)
    text = re.sub(r"\bcentre\b", "center", text, flags=re.I)
    # Longest labels first in a single substitution: never replace inside an
    # already expanded company name, or re-expand a nested alias.
    labels = {company_key(v): c.name for c in scope.companies for v in (c.name, *c.aliases)}
    pattern = (
        r"\b(?:"
        + "|".join(
            r"\s+".join("(?:and|&)" if w == "and" else re.escape(w) for w in v.split())
            for v in sorted(labels, key=len, reverse=True)
        )
        + r")\b"
    )
    return re.sub(pattern, lambda m: labels[company_key(m[0])], text, flags=re.I)


def spoken_control(text: str) -> str | None:
    words = set(company_key(text).split())
    if words & {"repeat", "say", "read"} and words <= {
        "repeat",
        "say",
        "read",
        "that",
        "the",
        "number",
        "phone",
        "answer",
        "again",
        "slowly",
        "slow",
        "please",
        "can",
        "could",
        "you",
        "me",
        "to",
        "it",
    }:
        return "repeat_slow" if words & {"slow", "slowly"} else "repeat"
    return None


def capability_question(text: str) -> bool:
    return bool(
        re.search(
            r"\b(?:can|could) you (?:actually )?(?:book|schedule|"
            r"make (?:an? )?(?:actual )?appointment|change (?:my |an? )?appointment)\b|"
            r"\bwhat can (?:you|this agent) do\b",
            text,
            re.I,
        )
    )


def person_mentions(text: str, directory: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    words = company_key(text).split()
    starts = {company_key(name).split()[0] for name in directory if company_key(name)}
    matches = []
    for i, word in enumerate(words):
        if word not in starts:
            continue
        for width in range(min(8, len(words) - i), 1, -1):
            candidates = match_people(" ".join(words[i : i + width]), directory)
            if candidates:
                matches.extend(candidates)
                break
    return tuple(dict.fromkeys(matches))


def negative_company_prefix(text: str, scope: KnowledgeCompanyScope) -> bool:
    normalized = company_key(text).replace("centre", "center")
    match = re.fullmatch(r"not (?:the )?(.+)", normalized)
    if not match:
        return False
    candidates = candidate_companies(match[1], scope, tuple(c.name for c in scope.companies))
    return any(
        set(match[1].split()) <= set(company_key(c.name).replace("centre", "center").split())
        for c in scope.companies
        if c.name in candidates
    )


def negative_detail_control(text: str) -> str | None:
    match = re.fullmatch(
        r"(?:not|no) (?:the )?(phone number|telephone number|address|email|price|role|position)",
        company_key(text),
    )
    if not match:
        return None
    return {"phone number": "phone", "telephone number": "phone", "position": "role"}.get(
        match[1], match[1]
    )


def named_identity_request(text: str, scope: KnowledgeCompanyScope, company: str) -> bool:
    label = next((c for c in scope.companies if c.name == company), None)
    if label is None:
        return False
    match = re.fullmatch(r"who is (.+)", company_request_remainder(text, label))
    if not match:
        return False
    words = set(match[1].split()) - set(
        "he she it they this that the person one someone man woman leader doctor dr "
        "at in of for".split()
    )
    return bool(words)


def incomplete_request(
    text: str, people: tuple[str, ...] = (), scope: KnowledgeCompanyScope | None = None
) -> bool:
    normalized = company_key(text)
    if not normalized or spoken_control(text):
        return False
    if scope and negative_company_prefix(text, scope):
        return True
    if normalized in {
        "what about",
        "how about",
        "i mean",
        "i meant",
        "can you",
        "could you",
        "i want to",
        "please tell me",
    }:
        return True
    if re.search(r"\b(?:of|for|about|with|the|and|or|at|in)\s*$", normalized):
        return True
    # A copula followed only by an approved name has no predicate yet. Identity
    # questions ("Is this Cara?") and "Who is Cara?" remain immediate.
    match = re.fullmatch(r"(?:is|was|does|did) (.+)", normalized)
    return bool(match and match_people(match[1], {name: () for name in people}))


def fragment_continues(previous: str, current: str) -> bool:
    current_key = company_key(current)
    if (
        not current_key
        or spoken_control(current)
        or current_key in {"stop", "wait", "goodbye", "thank you"}
    ):
        return False
    if re.match(
        r"^(?:who|what|where|when|how|is|does|can|could|give|tell|list|no|actually)\b", current_key
    ):
        return False
    return True


def positive_correction(text: str, scope: KnowledgeCompanyScope) -> str:
    # Accept the explicit positive side of a correction, never the negated side.
    match = re.search(
        r"^\s*((?:no|not)\b.*?)\b(?:i mean|i meant|i am talking about|"
        r"i'm talking about|instead|actually)\s+(.+)$",
        text,
        re.I,
    )
    if (
        match
        and (company_key(match[1]) in {"no", "not"} or negative_company_prefix(match[1], scope))
        and len(mentioned_companies(match[2], scope)) == 1
        and not re.search(r"\bnot\b", match[2], re.I)
    ):
        return "I mean " + match[2]
    return text


def contextual_plan(
    text: str,
    state: ConversationState,
    scope: KnowledgeCompanyScope,
    directory: dict[str, tuple[str, ...]],
) -> TurnPlan | None:
    explicit = mentioned_companies(text, scope)
    if len(explicit) > 1:
        state.pending_companies = explicit
        state.pending_query = text
        return state._clarify("Which company do you mean: " + " or ".join(explicit) + "?")
    if not explicit:
        suffixes: dict[str, list[str]] = {}
        for label in scope.companies:
            words = company_key(label.name).replace("centre", "center").split()
            for i in range(1, len(words) - 1):
                suffixes.setdefault(" ".join(words[i:]), []).append(label.name)
        normalized = company_key(text).replace("centre", "center")
        for suffix in sorted(suffixes, key=len, reverse=True):
            choices = tuple(dict.fromkeys(suffixes[suffix]))
            if len(choices) > 1 and re.search(r"\b" + re.escape(suffix) + r"\b", normalized):
                state.pending_companies = choices
                state.pending_query = re.sub(r"\b" + re.escape(suffix) + r"\b", "", normalized)
                return state._clarify("Which company do you mean: " + " or ".join(choices) + "?")
    company = explicit[0] if explicit else state.company
    office = re.fullmatch(rf"who is (?:the )?({ROLE_PATTERN})", company_key(text))
    if office and company:
        return state._lookup(f"Who is the {office[1]} of {company}?", company)
    for label in scope.companies:
        if label.name in explicit and any(
            re.search(r"\bnot (?:the )?" + re.escape(company_key(alias)) + r"\b", company_key(text))
            for alias in (label.name, *label.aliases)
        ):
            return None
    people = person_mentions(text, directory)
    person = people[0] if len(people) == 1 else None
    if not people and state.person and re.search(r"\b(?:he|she|his|her|him)\b", text, re.I):
        person = state.person
    if not person and re.search(r"\b(?:his|her|their) (?:role|position)\b", text, re.I):
        state.pending_query = text
        return state._clarify("Whose role would you like me to check?")
    if not person or person not in directory:
        return None
    # Canonical slot rewrites are allowed only for a closed structural vocabulary.
    # Extra constraints (salary, date, branch, another role...) keep their original
    # wording on the normal retrieval path instead of disappearing in a rewrite.
    remaining = set(company_key(text).split()) - set(company_key(person).split())
    for label in scope.companies:
        if label.name in explicit:
            for alias in (label.name, *label.aliases):
                remaining -= set(company_key(alias).split())
    framing = set(
        "i am was told heard think believe is that correct right really please no mean tell me "
        "his her their he she the a an of at in for role position not phone telephone number "
        "chairman chairperson president ceo cfo chief executive financial officer "
        "managing director".split()
    )
    safe_slot = remaining <= framing
    # Role correction is a new detail, not a negated company. Never drop other
    # substantive constraints such as salary, dates or contact details.
    if safe_slot and re.search(r"\b(?:his|her|their) (?:role|position)\b", text, re.I):
        result = state._lookup(f"Who is {person}?", company)
        state.person = person
        state.requested_detail = "person_role"
        return result
    role = re.search(rf"\bis (?:the )?({ROLE_PATTERN})\b", text, re.I)
    if safe_slot and role and not re.search(r"\b(?:not|never|phone)\b", text, re.I):
        # Retrieve the asserted office, not a generic biography that might hide
        # a caller's incorrect claim. Only the evidence may name its holder.
        result = state._lookup(f"Who is the {role[1]} of {company}?", company)
        state.person = person
        state.requested_detail = "person_role"
        return result
    if re.search(r"\b(?:work|works|working|employed)\b", text, re.I) or (
        explicit and re.search(r"\b(?:is|was)\b.+\bin\b", text, re.I)
    ):
        query = re.sub(r"\b(?:he|she|him|his|her)\b", person, text, flags=re.I)
        affiliation_words = set(company_key(text).split()) - set(company_key(person).split())
        if (
            not explicit
            and len(directory[person]) == 1
            and "where" in affiliation_words
            and affiliation_words
            <= set("so is does work works working employed where he she now".split())
        ):
            company = directory[person][0]
            query = f"Who is {person}?"
        result = state._lookup(query, company)
        state.person = person
        state.requested_detail = "person_affiliation"
        return result
    return None


def plural_companies(
    text: str, scope: KnowledgeCompanyScope, recent: tuple[str, ...]
) -> tuple[str, ...]:
    words = set(company_key(text).replace("centres", "centers").replace("centre", "center").split())
    if not words & {"both", "each", "all"}:
        return ()
    explicit = mentioned_companies(text, scope)
    if len(explicit) > 1:
        return explicit if len(explicit) <= 4 else ()
    descriptors = words - {
        "can",
        "you",
        "give",
        "me",
        "the",
        "phone",
        "telephone",
        "number",
        "numbers",
        "of",
        "both",
        "each",
        "all",
        "and",
        "say",
        "which",
        "is",
        "please",
        "companies",
        "company",
        "centers",
        "center",
        "for",
        "their",
        "address",
        "addresses",
        "hours",
    }
    matched = tuple(
        c.name
        for c in scope.companies
        if descriptors and descriptors <= set(company_key(c.name).split())
    )
    if 1 < len(matched) <= 4:
        return matched
    if not descriptors and "both" in words and len(recent) == 2:
        return recent
    return ()


@dataclass
class RequestLedger:
    """Content-free outcome accounting; clarification only resolves with its answer."""

    states: dict[int, str] = field(default_factory=dict)
    active: int | None = None
    sequence: int = 0
    overflow_unresolved: int = 0

    def begin(self, *, resumes: bool = False) -> int:
        if resumes and self.active and self.states.get(self.active) in {"pending", "clarification"}:
            self.states[self.active] = "pending"
            return self.active
        # A new topic does not silently erase an unfinished request. A late
        # committed response may resolve its own ID, never the newer request.
        self.sequence += 1
        self.active = self.sequence
        self.states[self.active] = "pending"
        if len(self.states) > 256:
            oldest = next(iter(self.states))
            self.overflow_unresolved += self.states.pop(oldest) in {
                "pending",
                "clarification",
                "failed",
            }
        return self.active

    def complete(self, request_id: int | None, status: str) -> None:
        if request_id in self.states and self.states[request_id] != "cancelled":
            self.states[request_id] = status

    def metrics(self) -> dict:
        return {
            "conversation_ledger_version": "v1",
            "conversation_requests_total": self.sequence,
            "conversation_requests_unresolved": self.overflow_unresolved
            + sum(s in {"pending", "clarification", "failed"} for s in self.states.values()),
            "conversation_requests_answered": sum(
                s in {"answered", "refused"} for s in self.states.values()
            ),
            "conversation_requests_cancelled": sum(s == "cancelled" for s in self.states.values()),
        }
