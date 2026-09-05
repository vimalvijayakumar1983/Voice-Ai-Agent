"""Call-local routing state. Names are search keys, never factual evidence.

Only the caller's configured company scope and approved person ownership can
select a company. New questions never inherit an earlier query merely because
they contain a pronoun. No model request is added to the ordinary routing path.
"""

import re
from dataclasses import dataclass

from app.services.conversation_scope import (
    KnowledgeCompanyScope,
    candidate_companies,
    company_key,
    company_request_remainder,
    mentioned_companies,
    routing_text,
)

# Shared by routing and the upstream transcript-fragment filter. A complete
# control command must not be cancelled as an incomplete one-word question.
LIST_CONTROLS = frozenset(
    {
        "next",
        "next please",
        "continue",
        "go on",
        "the rest",
        "show more",
        "more",
        "remaining",
        "remaining entries",
        "start again",
        "start over",
        "from the beginning",
        "repeat",
        "repeat that",
        "say again",
        "repeat slowly",
    }
)


@dataclass(frozen=True)
class TurnPlan:
    action: str
    company: str | None
    query: str = ""
    message: str = ""


def person_reference(text: str) -> tuple[str, bool] | None:
    """Extract a person reference, not a caller's proposed role or company fact."""
    patterns = (
        (r"who is (.+)", True),
        (r"what does (.+?) (?:hold|do)", True),
        (r"is (?:this |that )?(.+?) (?:someone|working|a person) in (?:this|the) company", True),
        (r"is (?:this|that) (.+)", True),
        (r"(?:more |tell me more )?about (.+?)(?:,? in .+)?", True),
        (r"with (.+)", False),
        (r"(?:what|which) (?:position|role) does (.+?) (?:hold|have)", True),
        (r"(?:tell me about|what about) (.+)", True),
        (r"(?:it is|it's|that is|that's|the name is) (.+)", False),
    )
    for pattern, inquiry in patterns:
        match = re.fullmatch(pattern, text.strip(" .?!"), re.I)
        if match:
            return match[1], inquiry
    return None


def conversation_control(text: str) -> str | None:
    """Classify bounded conversational acts before retrieval, preserving mixed questions."""
    words = company_key(text).split()
    if (
        words
        and set(words)
        <= {
            "one",
            "a",
            "just",
            "minute",
            "moment",
            "second",
            "please",
            "wait",
            "hold",
            "on",
            "hang",
            "give",
            "me",
            "for",
        }
        and set(words) & {"minute", "moment", "second", "wait", "hold", "hang"}
    ):
        return "hold"
    if (
        words
        and len(words) <= 16
        and set(words)
        <= {
            "it",
            "s",
            "is",
            "that",
            "all",
            "okay",
            "ok",
            "fine",
            "thank",
            "thanks",
            "you",
            "very",
            "much",
            "so",
            "again",
            "and",
            "bye",
            "goodbye",
            "alright",
            "great",
            "well",
            "oh",
            "for",
            "your",
            "help",
            "today",
        }
        and set(words) & {"thank", "thanks", "goodbye", "bye"}
    ):
        return "courtesy"
    return None


def match_people(reference: str, directory: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """Resolve spacing and omitted middle names without fuzzy company authority.

    Partial matches must begin with the approved person's first name. Ambiguous
    first names remain choices. Matching a surname shared by a company is not enough.
    """
    tokens = company_key(reference).split()
    if not tokens or tokens[0] in {"the", "a", "an", "he", "she", "it", "they"}:
        return ()
    compact = "".join(tokens)
    exact = tuple(n for n in directory if "".join(company_key(n).split()) == compact)
    if exact:
        return exact
    matches = []
    for name in directory:
        parts = company_key(name).split()
        if not parts or tokens[0] != parts[0] or len(tokens[0]) < 3:
            continue
        remaining = iter(parts)
        if all(any(part == token for part in remaining) for token in tokens):
            matches.append(name)
    return tuple(matches)


def explicit_attribute_request(question: str, company: str) -> bool:
    """A clear attribute with no evidence is a gap, not an unclear question.

    Only used after scoped retrieval failed and the optional interpreter returned
    clarify. Never converts a provider timeout into an evidence gap.
    """
    clause = company_key(question.split("?", 1)[0])
    match = re.fullmatch(r"(?:what (?:is|are)|how much (?:is|are)) (.+)", clause)
    if not match:
        return False
    words = set(match[1].split()) - set(company_key(company).split())
    if words & {"it", "this", "that", "he", "she", "thing", "detail", "something"}:
        return False
    words -= {"the", "a", "an", "your", "our", "its", "their", "of", "s", "company", "group"}
    return bool(words) and not words <= {
        "name",
        "number",
        "contact",
        "leader",
        "leadership",
        "position",
        "role",
        "information",
        "price",
        "cost",
        "fee",
        "rate",
        "current",
        "new",
        "old",
    }


@dataclass
class ConversationState:
    company: str | None = None
    topic_query: str | None = None
    person: str | None = None
    requested_detail: str | None = None
    pending_companies: tuple[str, ...] = ()
    pending_query: str | None = None
    pending_people: tuple[str, ...] = ()
    clarification_count: int = 0

    def _clarify(self, message: str) -> TurnPlan:
        self.clarification_count += 1
        if self.clarification_count > 1:
            message = message.rstrip("?") + ". Please say the specific name so I can continue."
        if self.clarification_count > 2:
            message = (
                "I haven't been able to resolve that choice. "
                "Please give the full company or person name and your question together."
            )
        return TurnPlan("clarify", self.company, message=message)

    def _lookup(self, query: str, company: str | None) -> TurnPlan:
        if not company:
            self.pending_query = query
            return self._clarify("Which configured company is this question about?")
        self.company = company
        self.topic_query = query
        if company_key(query) != company_key(f"Who is {self.person}?"):
            self.person = None
        self.pending_companies = ()
        self.pending_query = None
        self.pending_people = ()
        self.clarification_count = 0
        return TurnPlan("lookup", company, query=query)

    def plan(
        self,
        text: str,
        scope: KnowledgeCompanyScope,
        directory: dict[str, tuple[str, ...]],
        person_hint: str | None = None,
        allow_natural_selection: bool = False,
    ) -> TurnPlan:
        text = routing_text(text)
        normalized = company_key(text)
        # Discourse corrections carry a requested slot, not the prior company's facts.
        selection = re.sub(r"^(?:i mean|i meant|instead i mean)\s+", "", text, flags=re.I)
        if re.fullmatch(r"what (?:did i|was the (?:question i)) ask(?: you)? before", normalized):
            return TurnPlan(
                "recall",
                self.company,
                message=f"You asked: {self.topic_query}"
                if self.topic_query
                else "I don't have an earlier question to repeat yet.",
            )
        if normalized in {"the company i asked about before", "the same company", "same company"}:
            if self.company:
                return self._lookup(
                    self.pending_query or self.topic_query or "Company overview", self.company
                )
            return self._clarify("Please name the company you mean?")

        # Exact configured company names win over person interpretation. Shared
        # company prefixes do not: "It's Jane Harbour" may be a person's name.
        exact_companies = mentioned_companies(selection, scope)
        if exact_companies and re.search(r"\b(?:not|except|without)\b", selection, re.I):
            return self._clarify("Please name the company you want information about?")
        reference_text = text
        for label in scope.companies:
            if label.name in exact_companies:
                reference_text = re.sub(
                    r"\s+in\s+" + re.escape(label.name) + r"[.?!]*$", "", reference_text, flags=re.I
                )
        reference = person_reference(reference_text)
        people = match_people(reference[0], directory) if reference else ()
        # A versioned lexicon may offer a spelling correction. It is usable
        # only if that exact canonical person has approved company ownership.
        if reference and not exact_companies and not people and person_hint in directory:
            people = (person_hint,)
        if self.pending_people and not people:
            people = match_people(
                text, {n: directory[n] for n in self.pending_people if n in directory}
            )
            if people:
                reference = (text, True)
        if people:
            if len(people) > 1:
                self.pending_people = people
                return self._clarify("Which person do you mean: " + " or ".join(people) + "?")
            person = people[0]
            self.person = person
            if reference and not reference[1]:
                # A statement is not a question and must not overwrite the active topic.
                return TurnPlan(
                    "person_reference",
                    self.company,
                    message=f"What would you like to know about {person}?",
                )
            owners = tuple(n for n in directory[person] if n in {c.name for c in scope.companies})
            if exact_companies:
                # Never move an explicit company question to another person's employer.
                owners = tuple(n for n in owners if n in exact_companies)
                if not owners:
                    return self._lookup(text, exact_companies[0])
            if len(owners) > 1:
                self.pending_companies, self.pending_query = owners, f"Who is {person}?"
                return self._clarify("Which company do you mean: " + " or ".join(owners) + "?")
            if owners:
                return self._lookup(f"Who is {person}?", owners[0])

        # A recognized person inquiry with an unknown name must not silently be
        # answered as an unrelated company's role. Ordinary role questions still
        # go to scoped retrieval (e.g. "Who is the chairman?").
        if reference and re.match(
            r"^(?:who is|what position|what role|which position|which role)\b", text, re.I
        ):
            ref = company_key(reference[0])
            if ref in {"he", "she", "him", "her", "they"} and self.person:
                owners = directory.get(self.person, ())
                if len(owners) == 1:
                    return self._lookup(f"Who is {self.person}?", owners[0])

        companies = exact_companies or candidate_companies(selection, scope, self.pending_companies)
        selection_words = set(company_key(selection).split())
        labels = [c for c in scope.companies if c.name in companies]
        # Strip only known labels and structural selection words. A new address,
        # revenue, price, date, negation etc. remains a new question.
        remainder = selection
        for label in labels:
            remainder = company_request_remainder(remainder, label)
        selection_vocabulary = {
            "what",
            "how",
            "about",
            "and",
            "the",
            "one",
            "please",
            "for",
            "actually",
            "i",
            "mean",
            "meant",
            "instead",
            "centre",
            "center",
            "so",
            "it",
            "s",
            "is",
            "yes",
            "could",
            "be",
        }
        if allow_natural_selection:
            # Only neutral selection framing is accepted. A new requested detail,
            # person's name, action or negation remains substantive input.
            selection_vocabulary |= {"am", "m", "talking", "referring", "to", "yeah"}
        selection_only = (
            bool(companies) and set(company_key(remainder).split()) <= selection_vocabulary
        )
        if len(labels) > 1:
            common = set.intersection(*(set(company_key(c.name).split()) for c in labels))
            selection_only |= selection_words <= common | {
                "what",
                "how",
                "about",
                "and",
                "the",
                "please",
                "i",
                "mean",
                "meant",
            }
        # A pending short choice such as "Cosmetic Centre" is not the full label.
        if len(labels) == 1 and self.pending_companies:
            selection_only |= selection_words <= set(company_key(labels[0].name).split()) | {
                "the",
                "one",
                "please",
                "centre",
                "i",
                "mean",
                "meant",
            }
        query = text
        if selection_only:
            query = self.pending_query or self.topic_query or ""
            if query and self.company:
                old_label = next(c for c in scope.companies if c.name == self.company)
                query = company_request_remainder(query, old_label)
            # Carry an attribute, not the wording of the old company's request.
            if re.search(r"\b(?:phone|telephone|contact number)\b", query, re.I) and set(
                company_key(query).split()
            ) <= {
                "what",
                "is",
                "the",
                "phone",
                "telephone",
                "contact",
                "number",
                "just",
                "give",
                "tell",
                "me",
                "please",
                "your",
                "their",
                "of",
                "office",
            }:
                query = "What is the phone number?"
        if len(companies) > 1:
            self.pending_companies = companies
            if not selection_only:
                # The company choice is resolved separately from the requested
                # detail. Remove the shared name, not the branch/service filter.
                pending = text
                for label in labels:
                    pending = company_request_remainder(pending, label)
                prefixes = [company_key(c.name).split() for c in labels]
                common_prefix = []
                for tokens in zip(*prefixes):
                    if len(set(tokens)) != 1:
                        break
                    common_prefix.append(tokens[0])
                if len(common_prefix) >= 2:
                    pending = re.sub(
                        r"\b" + re.escape(" ".join(common_prefix)) + r"\b", "", pending
                    ).strip()
                query = pending
            self.pending_query = query or None
            return self._clarify("Which company do you mean: " + " or ".join(companies) + "?")
        if companies:
            if not query:
                self.company = companies[0]
                return TurnPlan(
                    "selection",
                    self.company,
                    message=f"Okay, {self.company}. What would you like to know?",
                )
            return self._lookup(query, companies[0])
        if re.match(r"^(?:i mean|i meant|about|for)\s+", text, re.I):
            self.pending_query = self.topic_query
            return self._clarify("Which of this agent's configured companies do you mean?")

        # A follow-up is a grammatical referential request, not a bag of words.
        # In particular "What is revenue? Is it one billion?" is self-contained.
        if normalized in {"tell me more", "what about it", "what else", "give me that"}:
            if self.topic_query:
                return self._lookup(self.topic_query, self.company)
            return self._clarify("Which topic would you like me to look up?")
        if re.fullmatch(r"(?:is|was|are|were) (?:it|they) .+", normalized):
            if self.topic_query:
                # STT may finalize "What is annual revenue?" separately from
                # "Is it one billion?". This is a grammatical confirmation,
                # not a new topic and not evidence that the claim is true.
                topic = self.topic_query.split("?", 1)[0].rstrip(" .!")
                return self._lookup(f"{topic}? {text}", self.company)
            return self._clarify("Which detail are you asking me to verify?")
        if (
            self.pending_companies
            and len(normalized.split()) <= 3
            and not re.match(r"^(?:who|what|where|when|how|list)\b", normalized)
        ):
            return self._clarify("Please choose " + " or ".join(self.pending_companies) + "?")
        return self._lookup(text, self.company)
