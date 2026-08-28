"""Smallest.ai catalog normalization and built-in voice-agent templates."""

from __future__ import annotations

import re
from typing import Any

LANGUAGE_NAMES: dict[str, str] = {
    "ar": "Arabic",
    "bn": "Bengali",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "el": "Greek",
    "fi": "Finnish",
    "fr": "French",
    "gu": "Gujarati",
    "hi": "Hindi",
    "id": "Indonesian",
    "it": "Italian",
    "ja": "Japanese",
    "kn": "Kannada",
    "ko": "Korean",
    "ml": "Malayalam",
    "mr": "Marathi",
    "ms": "Malay",
    "nl": "Dutch",
    "no": "Norwegian",
    "or": "Odia",
    "pa": "Punjabi",
    "pl": "Polish",
    "pt": "Portuguese",
    "ru": "Russian",
    "sv": "Swedish",
    "ta": "Tamil",
    "te": "Telugu",
    "tr": "Turkish",
    "vi": "Vietnamese",
    "zh": "Chinese (Mandarin)",
}

LANGUAGE_CODES = {name.lower(): code for code, name in LANGUAGE_NAMES.items()}
LANGUAGE_CODES.update(
    {
        "brazilian portuguese": "pt",
        "chinese": "zh",
        "mandarin": "zh",
        "mandarin chinese": "zh",
        "modern standard arabic": "ar",
        "portuguese (brazilian)": "pt",
        "portuguese (brazilian + european)": "pt",
    }
)

# Provider catalogs can add languages without an application release. Keep the
# normalized value deliberately broad enough for ISO 639 and BCP 47-style tags,
# while bounding it before it is returned to a browser or sent back upstream.
_LANGUAGE_SEPARATOR_RE = re.compile(r"[\s_]+")
_SAFE_LANGUAGE_CODE_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")
_MAX_LANGUAGE_CODE_LENGTH = 63
_MAX_LANGUAGE_SUBTAG_LENGTH = 16

STANDARD_SYNTHESIZER_MODEL = "waves_lightning_v3_1"

# The current Lightning v3.1 model card publishes these standard-pool IDs. The
# live endpoint also returns voices outside this curated card and does not label
# their pool. Keep those catalog-visible but unavailable until their Atoms model
# pairing can be proven; treating every non-Pro ID as standard would silently
# misclassify a newly added Pro voice when this snapshot becomes stale.
STANDARD_VOICE_IDS = frozenset(
    {
        "aanya",
        "aarush",
        "advika",
        "alba",
        "anuja",
        "atharv",
        "avni",
        "blofeld",
        "brooke",
        "camilla",
        "chinmayi",
        "chloe",
        "christine",
        "daniel",
        "daniella",
        "david",
        "deepashri",
        "devansh",
        "dhruvit",
        "elizabeth",
        "erica",
        "felix",
        "hannah",
        "ilsa",
        "ishani",
        "jeevan",
        "johnny",
        "jordan",
        "lauren",
        "lucas",
        "magnus",
        "maithili",
        "malcolm",
        "marcos",
        "maya",
        "mia",
        "miguel",
        "mihir",
        "neel",
        "nerea",
        "nicole",
        "niharika",
        "nilesh",
        "olivia",
        "padmaja",
        "parth",
        "pranav",
        "quinn",
        "rachel",
        "rajeshwari",
        "rehan",
        "robert",
        "ronald",
        "rupali",
        "sakshi",
        "sameera",
        "sana",
        "shibi",
        "siya",
        "srihari",
        "srishti",
        "sunidhi",
        "vaisakh",
        "vanessa",
        "vivaan",
        "wasim",
        "william",
        "yuvika",
        "zorin",
    }
)

# Canonical Lightning v3.1 Pro catalog published by Smallest.ai. The live
# get_voices endpoint is a union of standard and Pro without a model marker, so
# matching this provider-owned set is the only deterministic pairing signal.
# Keep this set synchronized with the provider model card when its catalog
# changes; an ID not returned by get_voices is rejected before this is used.
PRO_VOICE_IDS = frozenset(
    {
        "rhea",
        "zariya",
        "kareena",
        "mishka",
        "inaaya",
        "saira",
        "meher",
        "aarini",
        "aviraj",
        "vyom",
        "zoravar",
        "reyansh",
        "ahan",
        "sophie",
        "ellie",
        "cressida",
        "ottilie",
        "elowen",
        "seraphina",
        "sam",
        "henry",
        "benedict",
        "cormac",
        "rupert",
        "finley",
        "kaitlyn",
        "savannah",
        "amelia",
        "zoe",
        "ruby",
        "leah",
        "jenna",
        "kate",
        "molly",
        "sara",
        "fiona",
        "blake",
        "austin",
        "jack",
        "leo",
        "luke",
        "owen",
        "mrunal",
        "manasi",
        "ketaki",
        "tejaswini",
        "mandar",
        "tushar",
        "malar",
        "nila",
        "tamilselvi",
        "mathan",
        "dinesh",
        "prabhu",
        "ezhil",
        "kavin",
        "tamizh",
        "barath",
        "sakthi",
        "murugan",
        "parvathy",
        "lakshmi",
        "vishnu",
        "sreenath",
        "unni",
        "aravindan",
        "sravani",
        "swathi",
        "naveen",
        "charan",
        "sasank",
        "bhaskar",
        "gopal",
        "manohar",
        "spoorthi",
        "rashmi",
        "varsha",
        "sahana",
        "rakshith",
        "kishore",
        "yogesh",
        "gowtham",
        "shankar",
        "basava",
        "jasleen",
        "manmeet",
        "rajdeep",
        "tejinder",
        "sukhdeep",
        "amrit",
        "gagandeep",
        "rajib",
        "tanmoy",
        "subhro",
        "arghya",
        "indranil",
        "sasmita",
        "ankita",
        "subrat",
        "debasish",
        "sambit",
        "pratik",
        "rakesh",
        "smruti",
        "krupa",
        "riddhi",
        "jignesh",
        "mit",
        "keval",
        "layla",
        "adam",
        "hazel",
        "vivian",
        "dylan",
        "silas",
        "eli",
        "nora",
        "bryce",
        "miles",
        "cole",
        "aria",
        "mila",
        "daisy",
        "jasper",
        "june",
        "sasha",
        "roman",
        "beau",
        "wes",
        "kai",
        "hanna",
        "lea",
        "petra",
        "max",
        "ben",
        "markus",
        "finn",
        "martina",
        "ines",
        "paula",
        "sebastian",
        "mateo",
        "gabriel",
        "manon",
        "juliette",
        "lucie",
        "elise",
        "amelie",
        "louis",
        "nicolas",
        "maxime",
        "raphael",
        "silvia",
        "concetta",
        "arianna",
        "davide",
        "luca",
        "leonardo",
        "juliana",
        "leticia",
        "gustavo",
        "thiago",
        "bruno",
        "catarina",
        "francisco",
        "anastasia",
        "ekaterina",
        "olga",
        "irina",
        "andrei",
        "nikolai",
        "maksim",
        "katerina",
        "dimitra",
        "athina",
        "dimitris",
        "vasilis",
        "aino",
        "helmi",
        "venla",
        "mika",
        "timo",
        "matti",
        "solveig",
        "marit",
        "kristian",
        "espen",
        "ewa",
        "joanna",
        "tomasz",
        "jakub",
    }
)


def language_code(value: str) -> str | None:
    """Return a safe, stable code for a provider-supplied language tag.

    Known display names retain their conventional short code. Unknown ISO,
    locale, or provider labels are preserved rather than silently discarded;
    whitespace and underscores become hyphens so the value remains safe to use
    as a catalog identifier. No language is added unless the provider supplied
    it.
    """

    raw = value.strip()
    if not raw:
        return None

    alias = LANGUAGE_CODES.get(raw.casefold())
    if alias:
        return alias

    normalized = _LANGUAGE_SEPARATOR_RE.sub("-", raw.casefold())
    if (
        len(normalized) > _MAX_LANGUAGE_CODE_LENGTH
        or not _SAFE_LANGUAGE_CODE_RE.fullmatch(normalized)
        or any(len(subtag) > _MAX_LANGUAGE_SUBTAG_LENGTH for subtag in normalized.split("-"))
    ):
        return None
    return normalized


def language_name(code: str) -> str:
    """Return a human-readable name without guessing provider capabilities."""

    normalized = language_code(code)
    if not normalized:
        return code.strip()
    if normalized in LANGUAGE_NAMES:
        return LANGUAGE_NAMES[normalized]

    base, *qualifiers = normalized.split("-")
    if base in LANGUAGE_NAMES:
        suffix = "-".join(
            part.upper() if len(part) in {2, 3} else part.title() for part in qualifiers
        )
        return f"{LANGUAGE_NAMES[base]} ({suffix})" if suffix else LANGUAGE_NAMES[base]

    # An unknown provider label such as `Arabic` is normalized to `arabic`.
    # For actual unknown locale codes, showing the code is more honest than
    # pretending to know a language name.
    if len(base) > 3:
        return " ".join(part.title() for part in normalized.split("-"))
    canonical_qualifiers = [
        part.title() if len(part) == 4 else part.upper() if len(part) in {2, 3} else part
        for part in qualifiers
    ]
    return "-".join([base, *canonical_qualifiers])


def _raw_voice_languages(voice: dict[str, Any], tags: dict[str, Any]) -> list[Any]:
    values: list[Any] = []
    for candidate in (
        tags.get("language"),
        tags.get("languages"),
        voice.get("language"),
        voice.get("languages"),
    ):
        if isinstance(candidate, list):
            values.extend(candidate)
        elif candidate:
            values.append(candidate)
    return values


def _voice_language_details(values: Any) -> tuple[list[str], dict[str, str]]:
    raw = values if isinstance(values, list) else [values] if values else []
    languages: list[str] = []
    names: dict[str, str] = {}
    for value in raw:
        raw_value = str(value).strip()
        code = language_code(raw_value)
        if not code:
            continue
        if code not in names:
            languages.append(code)
            normalized_spellings = {code, code.replace("-", "_")}
            names[code] = LANGUAGE_NAMES.get(code) or (
                raw_value
                if raw_value.casefold() not in normalized_spellings
                else language_name(code)
            )
    return languages, names


def _voice_languages(values: Any) -> list[str]:
    return _voice_language_details(values)[0]


def voice_synthesizer_model(voice: dict[str, Any]) -> str | None:
    """Resolve the exact Waves model required by a provider voice.

    Smallest returns standard and Pro voices from the same catalog, but their
    IDs are not interchangeable across synthesizer models. Unknown shapes stay
    unavailable rather than being silently paired with the standard model.
    """

    voice_id = voice.get("voiceId") or voice.get("id") or voice.get("_id")
    if not voice_id:
        return None
    # The unified Waves TTS API documents a Pro token, but the Atoms agent
    # draft enum currently documents only waves_lightning_v3_1. Keep Pro voices
    # visible in the catalog while refusing to guess an incompatible Atoms
    # token that could produce silence or the wrong voice.
    normalized_voice_id = str(voice_id).strip().casefold()
    if normalized_voice_id in PRO_VOICE_IDS:
        return None
    if normalized_voice_id in STANDARD_VOICE_IDS:
        return STANDARD_SYNTHESIZER_MODEL
    return None


def voice_unavailability_reason(voice: dict[str, Any]) -> str | None:
    """Explain why a catalog voice cannot safely be selected for Atoms."""
    voice_id = voice.get("voiceId") or voice.get("id") or voice.get("_id")
    if not voice_id or voice_synthesizer_model(voice):
        return None
    if str(voice_id).strip().casefold() in PRO_VOICE_IDS:
        return "Lightning v3.1 Pro is not yet documented for Atoms agents"
    return "Atoms synthesizer model pairing is unverified"


def unsupported_voice_languages(
    provider_voices: list[dict[str, Any]],
    voice_id: str,
    selected_languages: list[str],
) -> list[str]:
    """Return selected languages that a tagged public voice does not support.

    A blank voice selects the provider default. A voice with no language tags
    is intentionally treated as unspecified rather than unsupported. Public
    voice existence is enforced separately so an absent ID also produces no
    language mismatch here.
    """

    if not voice_id:
        return []

    selected_voice = next(
        (
            voice
            for voice in provider_voices
            if str(voice.get("voiceId") or voice.get("id") or "") == voice_id
        ),
        None,
    )
    if selected_voice is None:
        return []

    tags = selected_voice.get("tags") if isinstance(selected_voice.get("tags"), dict) else {}
    capabilities = set(_voice_languages(_raw_voice_languages(selected_voice, tags)))
    if not capabilities:
        return []

    unsupported: list[str] = []
    for language in selected_languages:
        normalized = language_code(language)
        if not normalized or normalized not in capabilities:
            value = normalized or language.strip().casefold()
            if value and value not in unsupported:
                unsupported.append(value)
    return unsupported


def normalize_voices(
    provider_voices: list[dict[str, Any]], cloned_voices: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for voice in provider_voices:
        voice_id = voice.get("voiceId") or voice.get("id")
        if not voice_id:
            continue
        tags = voice.get("tags") if isinstance(voice.get("tags"), dict) else {}
        languages, language_names = _voice_language_details(_raw_voice_languages(voice, tags))
        synthesizer_model = voice_synthesizer_model(voice)
        normalized[str(voice_id)] = {
            "id": str(voice_id),
            "name": str(voice.get("displayName") or voice.get("name") or voice_id),
            "languages": languages,
            "_language_names": language_names,
            "accent": tags.get("accent"),
            "gender": tags.get("gender"),
            "age": tags.get("age"),
            "use_cases": tags.get("usecases") if isinstance(tags.get("usecases"), list) else [],
            "synthesizer_model": synthesizer_model,
            "unavailability_reason": voice_unavailability_reason(voice),
            "source": "catalog",
        }

    for voice in cloned_voices:
        voice_id = voice.get("voiceId") or voice.get("id") or voice.get("_id")
        model_ids = voice.get("modelIds") or []
        if (
            not voice_id
            or voice.get("status") not in (None, "completed")
            or (model_ids and "lightning-v3.1" not in model_ids)
        ):
            continue
        clone_tags = voice.get("tags") if isinstance(voice.get("tags"), list) else []
        gender = next((tag for tag in clone_tags if tag in {"male", "female"}), None)
        languages, language_names = _voice_language_details(_raw_voice_languages(voice, {}))
        synthesizer_model = voice_synthesizer_model(voice)
        normalized[str(voice_id)] = {
            "id": str(voice_id),
            "name": str(voice.get("displayName") or voice_id),
            "languages": languages,
            "_language_names": language_names,
            "accent": voice.get("accent"),
            "gender": gender,
            "age": None,
            "use_cases": [str(tag) for tag in clone_tags if tag not in {"male", "female"}],
            "synthesizer_model": synthesizer_model,
            "unavailability_reason": voice_unavailability_reason(voice),
            "source": "cloned",
        }

    return sorted(
        normalized.values(), key=lambda voice: (voice["source"] != "cloned", voice["name"])
    )


AGENT_TEMPLATES: list[dict[str, Any]] = [
    {
        "id": "receptionist",
        "name": "AI Receptionist",
        "category": "Inbound",
        "description": (
            "Welcome callers, understand intent, answer common questions, and route requests."
        ),
        "greeting_message": "Hello, thank you for calling. How may I help you today?",
        "system_prompt": """You are the professional AI receptionist for {{company_name}}.

GOALS
- Welcome every caller warmly and identify why they are calling.
- Answer approved questions using only provided company information.
- Collect the caller's name, phone number, and a concise message when follow-up is needed.
- Transfer or escalate urgent, sensitive, or unsupported requests to a human.

CONVERSATION STYLE
- Speak naturally in short sentences and ask one question at a time.
- Confirm names, numbers, dates, and appointments before acting.
- Match the caller's language when it is one of your supported languages.
- Never invent company policies, prices, availability, or customer records.

ENDING
Summarize the agreed next step, ask whether anything else is needed, and close politely.""",
        "default_language": "en",
        "supported_languages": ["en"],
        "voice_id": "",
        "speech_rate": 1.0,
        "temperature": 0.4,
        "timezone": "Asia/Dubai",
    },
    {
        "id": "customer_support",
        "name": "Customer Support",
        "category": "Service",
        "description": "Diagnose issues, guide customers through resolutions, and escalate safely.",
        "greeting_message": (
            "Hello, you have reached customer support. What can I help you resolve today?"
        ),
        "system_prompt": """You are a calm and capable customer-support agent for {{company_name}}.

WORKFLOW
1. Ask for the customer's name and a concise description of the issue.
2. Ask focused diagnostic questions one at a time.
3. Provide only verified troubleshooting steps from the approved knowledge base.
4. Confirm whether each step solved the issue before continuing.
5. Escalate account security, payments, complaints, safety risks, or unresolved cases to a human.

RULES
- Never request passwords, one-time codes, or full payment-card details.
- Never claim an action succeeded unless a connected tool confirms it.
- Keep explanations short, empathetic, and easy to follow.
- End with the resolution, case status, and next step.""",
        "default_language": "en",
        "supported_languages": ["en"],
        "voice_id": "",
        "speech_rate": 0.95,
        "temperature": 0.3,
        "timezone": "Asia/Dubai",
    },
    {
        "id": "lead_qualification",
        "name": "Lead Qualification",
        "category": "Sales",
        "description": (
            "Qualify interest, timeline, budget, and decision process without sounding scripted."
        ),
        "greeting_message": (
            "Hello, I am calling from {{company_name}}. "
            "Is now a good time for a brief conversation?"
        ),
        "system_prompt": """You are a consultative lead-qualification agent for {{company_name}}.

OBJECTIVE
Understand the prospect's need, current approach, urgency, budget range,
decision process, and preferred next step.

FLOW
- Ask permission to continue and respect a request to stop.
- Discover the problem before describing the solution.
- Ask one open question at a time and acknowledge each answer.
- Explain the most relevant value in plain language; do not overpromise.
- If qualified, offer a meeting or human follow-up and confirm the date, time, and contact details.
- If not interested, close respectfully. If they request no further calls,
  record do-not-call immediately.

Never invent pricing, discounts, case studies, or product capabilities.""",
        "default_language": "en",
        "supported_languages": ["en"],
        "voice_id": "",
        "speech_rate": 1.0,
        "temperature": 0.6,
        "timezone": "Asia/Dubai",
    },
    {
        "id": "appointment_booking",
        "name": "Appointment Booking",
        "category": "Scheduling",
        "description": "Book, reschedule, and cancel appointments with explicit confirmation.",
        "greeting_message": (
            "Hello, I can help you with an appointment. What would you like to schedule?"
        ),
        "system_prompt": """You are the appointment coordinator for {{company_name}}.

TASKS
- Help callers book, reschedule, or cancel appointments.
- Collect the service, preferred location or staff member, date, time, name, and contact number.
- Use the scheduling tool to check real availability before offering a slot.
- Repeat the final date, time, timezone, location, and preparation instructions.

GUARDRAILS
- Never state that a booking is confirmed until the scheduling tool confirms it.
- Offer at most three suitable alternatives at a time.
- Escalate emergencies, medical questions, payment disputes, and exceptions to a human.
- Keep personal information private and collect only what the booking requires.""",
        "default_language": "en",
        "supported_languages": ["en"],
        "voice_id": "",
        "speech_rate": 0.95,
        "temperature": 0.3,
        "timezone": "Asia/Dubai",
    },
    {
        "id": "payment_reminder",
        "name": "Payment Reminder",
        "category": "Collections",
        "description": (
            "Deliver respectful reminders, verify the customer, and capture payment commitments."
        ),
        "greeting_message": (
            "Hello, may I speak with {{customer_name}}? I am calling from {{company_name}} "
            "about an account update."
        ),
        "system_prompt": """You are a respectful payment-reminder agent for {{company_name}}.

COMPLIANCE FIRST
- Confirm you are speaking with the intended customer before discussing account details.
- Follow the approved identity-verification process and disclose only the
  minimum necessary information.
- State the verified balance, due date, and available approved options without pressure or judgment.
- Record a promised payment date only after the customer clearly agrees.
- Escalate disputes, hardship, fraud, legal threats, deceased customers, and
  vulnerable-customer cases.

Never request a password, one-time code, or full card number. Honor do-not-call
and contact-time rules immediately. End by summarizing the agreed next step.""",
        "default_language": "en",
        "supported_languages": ["en"],
        "voice_id": "",
        "speech_rate": 0.9,
        "temperature": 0.2,
        "timezone": "Asia/Dubai",
    },
]


def language_catalog(voices: list[dict[str, Any]]) -> list[dict[str, str]]:
    names: dict[str, str] = {}
    for voice in voices:
        provider_names = voice.get("_language_names")
        if not isinstance(provider_names, dict):
            provider_names = {}
        for value in voice.get("languages", []):
            code = language_code(str(value))
            if code and code not in names:
                candidate = provider_names.get(code)
                names[code] = str(candidate).strip() if candidate else language_name(code)

    # An empty/untagged provider catalog stays empty. Falling back to a local
    # hardcoded list would falsely claim support the provider did not advertise.
    return [
        {"code": code, "name": names[code]}
        for code in sorted(names, key=lambda item: (names[item].casefold(), item))
    ]
