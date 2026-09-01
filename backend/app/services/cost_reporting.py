"""Transparent provider-cost and call-performance reporting.

Public list rates are reference estimates, never invoices.  The report keeps
the application's existing usage ledger separate so an operator can reconcile
estimated costs with provider billing without double counting either source.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.billing import UsageRecord
from app.models.call import Call, CallTranscript

USD_AED = Decimal("3.6725")
# Latest value captured from the cited CBUAE reference snapshot.  Keeping the
# dated FX input visible is safer than silently calling an unversioned FX feed.
INR_AED = Decimal("0.038377")

TWILIO_UAE_MOBILE_USD_PER_MINUTE = Decimal("0.2995")
TWILIO_UAE_LOCAL_USD_PER_MINUTE = Decimal("0.3635")
TWILIO_US_OUTBOUND_USD_PER_MINUTE = Decimal("0.0140")
TWILIO_US_INBOUND_USD_PER_MINUTE = Decimal("0.0085")
TWILIO_MEDIA_STREAM_USD_PER_MINUTE = Decimal("0.0044")
SARVAM_STT_INR_PER_HOUR = Decimal("30")
SARVAM_TTS_INR_PER_10K_CHARACTERS = Decimal("30")
ELEVENLABS_FLASH_USD_PER_1K_CHARACTERS = Decimal("0.05")

TWILIO_UAE_SOURCE = "https://www.twilio.com/en-us/voice/pricing/ae"
TWILIO_US_SOURCE = "https://www.twilio.com/en-us/voice/pricing/us"
SARVAM_SOURCE = "https://docs.sarvam.ai/api/getting-started/pricing"
ELEVENLABS_SOURCE = "https://elevenlabs.io/pricing/api"
OPENAI_SOURCE = "https://developers.openai.com/api/docs/models/gpt-4o-mini"
SMALLEST_SOURCE = "https://atoms-docs.smallest.ai/intro/admin/billing"
CBUAE_SOURCE = "https://www.centralbank.ae/en/forex-eibor/exchange-rates/"

OPENAI_RATES: dict[str, tuple[Decimal, Decimal]] = {
    "gpt-4o-mini": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4o-mini-2024-07-18": (Decimal("0.15"), Decimal("0.60")),
    "gpt-4o": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-2024-08-06": (Decimal("2.50"), Decimal("10.00")),
    "gpt-4o-2024-11-20": (Decimal("2.50"), Decimal("10.00")),
}

SUCCESSFUL_DISPOSITIONS = {
    "appointment_booked",
    "booked",
    "converted",
    "interested",
    "sale",
    "success",
    "successful",
}


def _rounded(value: Decimal, places: str = "0.000001") -> float:
    return float(value.quantize(Decimal(places), rounding=ROUND_HALF_UP))


def _money(usd: Decimal) -> dict[str, float]:
    return {"usd": _rounded(usd), "aed": _rounded(usd * USD_AED)}


def _from_native(amount: Decimal, currency: str) -> dict[str, float]:
    if currency == "USD":
        return _money(amount)
    if currency == "INR":
        aed = amount * INR_AED
        return {"usd": _rounded(aed / USD_AED), "aed": _rounded(aed)}
    raise ValueError(f"Unsupported reference currency: {currency}")


def _normalized_disposition(value: str | None) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _runtime_metadata(call: Call) -> dict[str, Any]:
    metadata = call.call_metadata if isinstance(call.call_metadata, dict) else {}
    runtime = metadata.get("runtime")
    return runtime if isinstance(runtime, dict) else {}


def _speech_provider(call: Call, agent: Agent | None) -> str | None:
    runtime = _runtime_metadata(call)
    provider = runtime.get("speech_provider")
    if isinstance(provider, str) and provider:
        return provider.lower()
    metadata = call.call_metadata if isinstance(call.call_metadata, dict) else {}
    provider = metadata.get("speech_provider")
    if isinstance(provider, str) and provider:
        return provider.lower()
    if agent and agent.voice_provider:
        return agent.voice_provider.lower()
    return None


def _assistant_characters(transcript: CallTranscript | None) -> tuple[int, str]:
    if transcript is None:
        return 0, "missing"
    turns = transcript.turns
    if not isinstance(turns, list):
        return 0, "missing"
    characters = sum(
        len(str(turn.get("content") or ""))
        for turn in turns
        if isinstance(turn, dict) and str(turn.get("role") or "").lower() == "assistant"
    )
    return characters, "transcript_derived" if characters else "missing"


def _twilio_voice_rate(call: Call) -> tuple[Decimal | None, str, str]:
    if call.direction == "outbound":
        destination = str(call.to_number or "")
        if destination.startswith("+9715"):
            return TWILIO_UAE_MOBILE_USD_PER_MINUTE, "UAE mobile outbound", TWILIO_UAE_SOURCE
        if destination.startswith("+971"):
            return TWILIO_UAE_LOCAL_USD_PER_MINUTE, "UAE local outbound", TWILIO_UAE_SOURCE
        if destination.startswith("+1"):
            return TWILIO_US_OUTBOUND_USD_PER_MINUTE, "US/Canada outbound", TWILIO_US_SOURCE
        return None, "Destination-specific Twilio rate required", TWILIO_UAE_SOURCE
    # VAV's current inbound production number is a US local Twilio number.
    if str(call.to_number or "").startswith("+1"):
        return TWILIO_US_INBOUND_USD_PER_MINUTE, "US local inbound", TWILIO_US_SOURCE
    return None, "Number-specific Twilio inbound rate required", TWILIO_UAE_SOURCE


def _rate_card(
    provider: str,
    service: str,
    amount: Decimal,
    currency: str,
    unit: str,
    source_url: str,
    notes: str,
    *,
    effective_date: str = "2026-09-01",
) -> dict[str, Any]:
    converted = _from_native(amount, currency)
    return {
        "provider": provider,
        "service": service,
        "native_amount": _rounded(amount),
        "native_currency": currency,
        "unit": unit,
        "usd": converted["usd"],
        "aed": converted["aed"],
        "source_url": source_url,
        "effective_date": effective_date,
        "notes": notes,
    }


def public_rate_cards() -> list[dict[str, Any]]:
    """Return the auditable reference catalog shown in the UI."""
    return [
        _rate_card(
            "Twilio",
            "UAE mobile outbound",
            TWILIO_UAE_MOBILE_USD_PER_MINUTE,
            "USD",
            "minute",
            TWILIO_UAE_SOURCE,
            "Destination rate; phone number, recording, and optional features are separate.",
        ),
        _rate_card(
            "Twilio",
            "UAE local outbound",
            TWILIO_UAE_LOCAL_USD_PER_MINUTE,
            "USD",
            "minute",
            TWILIO_UAE_SOURCE,
            "Destination rate; phone number, recording, and optional features are separate.",
        ),
        _rate_card(
            "Twilio",
            "Media Streams",
            TWILIO_MEDIA_STREAM_USD_PER_MINUTE,
            "USD",
            "minute",
            TWILIO_UAE_SOURCE,
            "Added for VAV realtime calls that stream audio through Twilio Media Streams.",
        ),
        _rate_card(
            "Sarvam",
            "Saaras speech to text",
            SARVAM_STT_INR_PER_HOUR,
            "INR",
            "audio hour",
            SARVAM_SOURCE,
            "Billed per second; report uses connected call duration as the audio estimate.",
        ),
        _rate_card(
            "Sarvam",
            "Bulbul v3 text to speech",
            SARVAM_TTS_INR_PER_10K_CHARACTERS,
            "INR",
            "10,000 characters",
            SARVAM_SOURCE,
            "Production licensing and taxes remain subject to the provider account.",
        ),
        _rate_card(
            "ElevenLabs",
            "Flash / Turbo text to speech",
            ELEVENLABS_FLASH_USD_PER_1K_CHARACTERS,
            "USD",
            "1,000 characters",
            ELEVENLABS_SOURCE,
            (
                "Public pay-as-you-go API reference; plan inclusions and taxes "
                "can change invoice cost."
            ),
        ),
        _rate_card(
            "OpenAI",
            "GPT-4o mini input",
            Decimal("0.15"),
            "USD",
            "1M tokens",
            OPENAI_SOURCE,
            "Standard API input-token rate; cached input and enterprise terms can differ.",
        ),
        _rate_card(
            "OpenAI",
            "GPT-4o mini output",
            Decimal("0.60"),
            "USD",
            "1M tokens",
            OPENAI_SOURCE,
            "Standard API output-token rate.",
        ),
        _rate_card(
            "Smallest.ai",
            "Atoms PAYG / Personal India calls",
            Decimal("0.09"),
            "USD",
            "minute (approximately)",
            SMALLEST_SOURCE,
            "Published approximate India call rate; Business is approximately $0.07/min.",
        ),
        _rate_card(
            "Smallest.ai",
            "Atoms PAYG / Personal US calls",
            Decimal("0.15"),
            "USD",
            "minute (approximately)",
            SMALLEST_SOURCE,
            "Published approximate US call rate; Business is approximately $0.12/min.",
        ),
        _rate_card(
            "Smallest.ai",
            "Personal platform plan",
            Decimal("49"),
            "USD",
            "month",
            SMALLEST_SOURCE,
            "Provider plan fee; usage charges remain separate.",
        ),
        _rate_card(
            "Smallest.ai",
            "Business platform plan",
            Decimal("1999"),
            "USD",
            "month",
            SMALLEST_SOURCE,
            "Provider plan fee; published regional call rates and custom terms remain separate.",
        ),
    ]


def _component(
    provider: str,
    service: str,
    quantity: Decimal,
    unit: str,
    rate_usd: Decimal,
    source_url: str,
    basis: str,
) -> dict[str, Any]:
    cost = quantity * rate_usd
    return {
        "provider": provider,
        "service": service,
        "quantity": _rounded(quantity),
        "unit": unit,
        "rate_usd": _rounded(rate_usd),
        "cost_usd": _rounded(cost),
        "cost_aed": _rounded(cost * USD_AED),
        "source_url": source_url,
        "basis": basis,
    }


def _call_components(
    call: Call,
    agent: Agent | None,
    transcript: CallTranscript | None,
) -> tuple[list[dict[str, Any]], list[str]]:
    minutes = Decimal(str(max(call.duration_seconds or 0, 0))) / Decimal("60")
    if minutes <= 0:
        return [], []
    runtime = _runtime_metadata(call)
    speech = _speech_provider(call, agent)
    components: list[dict[str, Any]] = []
    missing: list[str] = []

    if call.provider == "twilio":
        voice_rate, basis, source = _twilio_voice_rate(call)
        if voice_rate is not None:
            components.append(
                _component(
                    "Twilio",
                    "Programmable Voice",
                    minutes,
                    "minutes",
                    voice_rate,
                    source,
                    basis,
                )
            )
        else:
            missing.append("Twilio destination rate")
        components.append(
            _component(
                "Twilio",
                "Media Streams",
                minutes,
                "minutes",
                TWILIO_MEDIA_STREAM_USD_PER_MINUTE,
                TWILIO_UAE_SOURCE,
                "VAV realtime audio stream",
            )
        )

    if call.provider == "smallest":
        destination = str(call.to_number or "")
        if destination.startswith("+91"):
            components.append(
                _component(
                    "Smallest.ai",
                    "Atoms call",
                    minutes,
                    "minutes",
                    Decimal("0.09"),
                    SMALLEST_SOURCE,
                    "PAYG/Personal India public approximation",
                )
            )
        elif destination.startswith("+1"):
            components.append(
                _component(
                    "Smallest.ai",
                    "Atoms call",
                    minutes,
                    "minutes",
                    Decimal("0.15"),
                    SMALLEST_SOURCE,
                    "PAYG/Personal US public approximation",
                )
            )
        else:
            missing.append("Smallest.ai region/plan rate")

    if speech in {"sarvam", "elevenlabs"}:
        stt_usd_per_hour = (SARVAM_STT_INR_PER_HOUR * INR_AED) / USD_AED
        components.append(
            _component(
                "Sarvam",
                "Saaras speech to text",
                minutes / Decimal("60"),
                "audio hours",
                stt_usd_per_hour,
                SARVAM_SOURCE,
                "Connected duration proxy",
            )
        )

        tracked_characters = runtime.get("tts_characters")
        if isinstance(tracked_characters, (int, float)) and tracked_characters >= 0:
            tts_characters = int(tracked_characters)
            character_basis = "Runtime-metered characters"
        else:
            tts_characters, origin = _assistant_characters(transcript)
            character_basis = (
                "Assistant transcript characters (partial estimate)"
                if origin == "transcript_derived"
                else ""
            )
        if tts_characters:
            thousands = Decimal(tts_characters) / Decimal("1000")
            if speech == "elevenlabs":
                components.append(
                    _component(
                        "ElevenLabs",
                        "Flash / Turbo text to speech",
                        thousands,
                        "1,000 characters",
                        ELEVENLABS_FLASH_USD_PER_1K_CHARACTERS,
                        ELEVENLABS_SOURCE,
                        character_basis,
                    )
                )
            else:
                sarvam_usd_per_1k = (
                    SARVAM_TTS_INR_PER_10K_CHARACTERS / Decimal("10") * INR_AED / USD_AED
                )
                components.append(
                    _component(
                        "Sarvam",
                        "Bulbul v3 text to speech",
                        thousands,
                        "1,000 characters",
                        sarvam_usd_per_1k,
                        SARVAM_SOURCE,
                        character_basis,
                    )
                )
        else:
            missing.append(f"{speech.title()} TTS characters")

        llm_model = str(runtime.get("llm_model") or "gpt-4o-mini")
        input_tokens = runtime.get("llm_input_tokens")
        output_tokens = runtime.get("llm_output_tokens")
        model_rates = OPENAI_RATES.get(llm_model)
        if (
            model_rates
            and isinstance(input_tokens, (int, float))
            and isinstance(output_tokens, (int, float))
        ):
            input_rate, output_rate = model_rates
            if input_tokens:
                components.append(
                    _component(
                        "OpenAI",
                        f"{llm_model} input",
                        Decimal(str(input_tokens)) / Decimal("1000000"),
                        "1M tokens",
                        input_rate,
                        OPENAI_SOURCE,
                        "Runtime-metered input tokens",
                    )
                )
            if output_tokens:
                components.append(
                    _component(
                        "OpenAI",
                        f"{llm_model} output",
                        Decimal(str(output_tokens)) / Decimal("1000000"),
                        "1M tokens",
                        output_rate,
                        OPENAI_SOURCE,
                        "Runtime-metered output tokens",
                    )
                )
        elif runtime.get("llm_tokens"):
            missing.append("OpenAI input/output token split")

    return components, missing


async def build_cost_report(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    since: datetime,
    until: datetime,
    provider: str | None = None,
    speech_provider: str | None = None,
    agent_id: UUID | None = None,
    direction: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    predicates = [Call.tenant_id == tenant_id, Call.created_at >= since, Call.created_at <= until]
    if provider:
        predicates.append(Call.provider == provider)
    if agent_id:
        predicates.append(Call.agent_id == agent_id)
    if direction:
        predicates.append(Call.direction == direction)
    if status:
        predicates.append(Call.status == status)

    result = await db.execute(
        select(Call, Agent, CallTranscript)
        .outerjoin(Agent, and_(Agent.id == Call.agent_id, Agent.tenant_id == tenant_id))
        .outerjoin(
            CallTranscript,
            and_(CallTranscript.call_id == Call.id, CallTranscript.tenant_id == tenant_id),
        )
        .where(*predicates)
        .order_by(Call.created_at.desc())
    )
    rows = result.all()
    if speech_provider:
        rows = [row for row in rows if _speech_provider(row[0], row[1]) == speech_provider]
    selected_call_ids = {row[0].id for row in rows}

    usage_result = await db.execute(
        select(UsageRecord).where(
            UsageRecord.tenant_id == tenant_id,
            UsageRecord.created_at >= since,
            UsageRecord.created_at <= until,
        )
    )
    ledger_by_call: dict[UUID, Decimal] = defaultdict(Decimal)
    ledger_total = Decimal("0")
    for usage in usage_result.scalars().all():
        if usage.call_id not in selected_call_ids:
            continue
        amount = Decimal(usage.cost_cents or 0) / Decimal("100")
        ledger_total += amount
        ledger_by_call[usage.call_id] += amount

    calls: list[dict[str, Any]] = []
    provider_totals: dict[tuple[str, str], dict[str, Any]] = {}
    trend: dict[str, dict[str, Decimal | int]] = defaultdict(
        lambda: {"calls": 0, "minutes": Decimal("0"), "cost_usd": Decimal("0")}
    )
    total_minutes = Decimal("0")
    total_cost = Decimal("0")
    completed = answered = successful = priced = fully_priced = 0
    status_counts: dict[str, int] = defaultdict(int)
    direction_counts: dict[str, int] = defaultdict(int)

    for call, agent, transcript in rows:
        minutes = Decimal(str(max(call.duration_seconds or 0, 0))) / Decimal("60")
        components, missing = _call_components(call, agent, transcript)
        estimated = sum((Decimal(str(item["cost_usd"])) for item in components), Decimal("0"))
        ledger = ledger_by_call.get(call.id, Decimal("0"))
        if estimated > 0:
            primary_cost = estimated
            cost_state = "public_rate_estimate"
        elif ledger > 0:
            primary_cost = ledger
            cost_state = "recorded_ledger_estimate"
            components.append(
                _component(
                    "VAV ledger",
                    "Unallocated recorded estimate",
                    ledger,
                    "recorded USD",
                    Decimal("1"),
                    "",
                    "Legacy application estimate; provider allocation unavailable",
                )
            )
        elif minutes == 0:
            primary_cost = Decimal("0")
            cost_state = "zero_duration"
        else:
            primary_cost = Decimal("0")
            cost_state = "unpriced"

        if cost_state in {"public_rate_estimate", "recorded_ledger_estimate", "zero_duration"}:
            priced += 1
        if not missing and cost_state in {"public_rate_estimate", "zero_duration"}:
            fully_priced += 1
        if call.status == "completed":
            completed += 1
            if _normalized_disposition(call.disposition) in SUCCESSFUL_DISPOSITIONS:
                successful += 1
        if call.answered_at is not None or call.status in {"in_progress", "completed"}:
            answered += 1

        status_counts[call.status] += 1
        direction_counts[call.direction] += 1
        total_minutes += minutes
        total_cost += primary_cost
        day = call.created_at.date().isoformat()
        trend[day]["calls"] = int(trend[day]["calls"]) + 1
        trend[day]["minutes"] = Decimal(trend[day]["minutes"]) + minutes
        trend[day]["cost_usd"] = Decimal(trend[day]["cost_usd"]) + primary_cost

        for component in components:
            key = (component["provider"], component["service"])
            aggregate = provider_totals.setdefault(
                key,
                {
                    "provider": component["provider"],
                    "service": component["service"],
                    "calls": set(),
                    "quantity": Decimal("0"),
                    "unit": component["unit"],
                    "cost_usd": Decimal("0"),
                    "source_url": component["source_url"],
                    "basis": component["basis"],
                },
            )
            aggregate["calls"].add(call.id)
            aggregate["quantity"] += Decimal(str(component["quantity"]))
            aggregate["cost_usd"] += Decimal(str(component["cost_usd"]))

        calls.append(
            {
                "call_id": str(call.id),
                "created_at": call.created_at.isoformat(),
                "agent_id": str(call.agent_id) if call.agent_id else None,
                "agent_name": agent.name if agent else "Deleted agent",
                "direction": call.direction,
                "status": call.status,
                "disposition": call.disposition,
                "telephony_provider": call.provider,
                "speech_provider": _speech_provider(call, agent),
                "from_number": call.from_number,
                "to_number": call.to_number,
                "duration_seconds": int(call.duration_seconds or 0),
                "cost_usd": _rounded(primary_cost),
                "cost_aed": _rounded(primary_cost * USD_AED),
                "ledger_cost_usd": _rounded(ledger),
                "ledger_cost_aed": _rounded(ledger * USD_AED),
                "cost_state": cost_state,
                "pricing_completeness": "complete" if not missing else "partial",
                "missing_cost_inputs": missing,
                "components": components,
            }
        )

    total_calls = len(calls)
    provider_breakdown = []
    for aggregate in provider_totals.values():
        usd = aggregate["cost_usd"]
        provider_breakdown.append(
            {
                "provider": aggregate["provider"],
                "service": aggregate["service"],
                "calls": len(aggregate["calls"]),
                "quantity": _rounded(aggregate["quantity"]),
                "unit": aggregate["unit"],
                "cost_usd": _rounded(usd),
                "cost_aed": _rounded(usd * USD_AED),
                "source_url": aggregate["source_url"],
                "basis": aggregate["basis"],
            }
        )
    provider_breakdown.sort(key=lambda item: item["cost_usd"], reverse=True)

    trend_rows = [
        {
            "date": day,
            "calls": int(values["calls"]),
            "minutes": _rounded(Decimal(values["minutes"]), "0.001"),
            "cost_usd": _rounded(Decimal(values["cost_usd"])),
            "cost_aed": _rounded(Decimal(values["cost_usd"]) * USD_AED),
        }
        for day, values in sorted(trend.items())
    ]
    avg_cost = total_cost / total_calls if total_calls else Decimal("0")
    cost_per_minute = total_cost / total_minutes if total_minutes else Decimal("0")

    return {
        "period_start": since.isoformat(),
        "period_end": until.isoformat(),
        "currency": {
            "display": ["USD", "AED"],
            "usd_to_aed": float(USD_AED),
            "inr_to_aed": float(INR_AED),
            "fx_effective_date": "2026-05-28",
            "source_url": CBUAE_SOURCE,
            "notes": (
                "AED uses the official USD peg. INR conversion uses the dated "
                "CBUAE reference snapshot."
            ),
        },
        "summary": {
            "total_calls": total_calls,
            "answered_calls": answered,
            "completed_calls": completed,
            "successful_calls": successful,
            "total_minutes": _rounded(total_minutes, "0.001"),
            "avg_duration_seconds": _rounded(
                total_minutes * Decimal("60") / total_calls if total_calls else Decimal("0"),
                "0.1",
            ),
            "answer_rate": round(answered / total_calls, 4) if total_calls else 0,
            "success_rate": round(successful / completed, 4) if completed else 0,
            "estimated_cost_usd": _rounded(total_cost),
            "estimated_cost_aed": _rounded(total_cost * USD_AED),
            "avg_cost_per_call_usd": _rounded(avg_cost),
            "avg_cost_per_call_aed": _rounded(avg_cost * USD_AED),
            "cost_per_minute_usd": _rounded(cost_per_minute),
            "cost_per_minute_aed": _rounded(cost_per_minute * USD_AED),
            "priced_calls": priced,
            "fully_priced_calls": fully_priced,
            "unpriced_calls": total_calls - priced,
            "cost_coverage": round(priced / total_calls, 4) if total_calls else 1,
            "full_cost_coverage": round(fully_priced / total_calls, 4) if total_calls else 1,
            "ledger_estimate_usd": _rounded(ledger_total),
            "ledger_estimate_aed": _rounded(ledger_total * USD_AED),
            "calls_by_status": dict(status_counts),
            "calls_by_direction": dict(direction_counts),
        },
        "provider_breakdown": provider_breakdown,
        "trend": trend_rows,
        "calls": calls,
        "rate_cards": public_rate_cards(),
        "methodology": {
            "primary_total": (
                "Public list-rate components where measurable; otherwise the "
                "existing VAV usage-ledger estimate."
            ),
            "not_included": (
                "Taxes, credits, discounts, committed-use pricing, phone-number "
                "rental, and plan fees unless explicitly listed."
            ),
            "invoice_status": (
                "Estimates are not invoices. Reconcile with provider billing "
                "before customer quotation or accounting use."
            ),
        },
    }
