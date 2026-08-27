"""Smallest.ai catalog normalization and built-in voice-agent templates."""

from __future__ import annotations

from typing import Any

LANGUAGE_NAMES: dict[str, str] = {
    "bn": "Bengali",
    "de": "German",
    "en": "English",
    "es": "Spanish",
    "fr": "French",
    "gu": "Gujarati",
    "hi": "Hindi",
    "it": "Italian",
    "kn": "Kannada",
    "ml": "Malayalam",
    "mr": "Marathi",
    "nl": "Dutch",
    "or": "Odia",
    "pa": "Punjabi",
    "pl": "Polish",
    "pt": "Portuguese",
    "ru": "Russian",
    "sv": "Swedish",
    "ta": "Tamil",
    "te": "Telugu",
}

LANGUAGE_CODES = {name.lower(): code for code, name in LANGUAGE_NAMES.items()}


def language_code(value: str) -> str | None:
    normalized = value.strip().lower().replace("_", "-")
    if normalized in LANGUAGE_NAMES:
        return normalized
    if normalized in LANGUAGE_CODES:
        return LANGUAGE_CODES[normalized]
    base = normalized.split("-", 1)[0]
    return base if base in LANGUAGE_NAMES else None


def _voice_languages(values: Any) -> list[str]:
    raw = values if isinstance(values, list) else [values] if values else []
    return list(dict.fromkeys(code for value in raw if (code := language_code(str(value)))))


def normalize_voices(
    provider_voices: list[dict[str, Any]], cloned_voices: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for voice in provider_voices:
        voice_id = voice.get("voiceId") or voice.get("id")
        if not voice_id:
            continue
        tags = voice.get("tags") if isinstance(voice.get("tags"), dict) else {}
        normalized[str(voice_id)] = {
            "id": str(voice_id),
            "name": str(voice.get("displayName") or voice.get("name") or voice_id),
            "languages": _voice_languages(tags.get("language")),
            "accent": tags.get("accent"),
            "gender": tags.get("gender"),
            "age": tags.get("age"),
            "use_cases": tags.get("usecases") if isinstance(tags.get("usecases"), list) else [],
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
        normalized[str(voice_id)] = {
            "id": str(voice_id),
            "name": str(voice.get("displayName") or voice_id),
            "languages": _voice_languages(voice.get("language")),
            "accent": voice.get("accent"),
            "gender": gender,
            "age": None,
            "use_cases": [str(tag) for tag in clone_tags if tag not in {"male", "female"}],
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
    available = {code for voice in voices for code in voice["languages"]}
    codes = sorted(available or LANGUAGE_NAMES, key=lambda code: LANGUAGE_NAMES[code])
    return [{"code": code, "name": LANGUAGE_NAMES[code]} for code in codes]
