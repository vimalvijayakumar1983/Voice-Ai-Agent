"""Provider cost intelligence and call-report export tests."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.agent import Agent
from app.models.billing import UsageRecord
from app.models.call import Call, CallTranscript


@pytest.mark.asyncio
async def test_cost_report_converts_provider_components_to_usd_and_aed(
    client,
    auth_headers,
    db,
    tenant,
):
    now = datetime.now(UTC)
    vav_agent = Agent(
        tenant_id=tenant.id,
        name="AED support",
        system_prompt="Help callers.",
        voice_provider="sarvam",
    )
    smallest_agent = Agent(
        tenant_id=tenant.id,
        name="US qualification",
        system_prompt="Qualify callers.",
        voice_provider="smallest",
    )
    db.add_all([vav_agent, smallest_agent])
    await db.flush()

    vav_call = Call(
        tenant_id=tenant.id,
        agent_id=vav_agent.id,
        direction="outbound",
        status="completed",
        from_number="+14142934703",
        to_number="+971501234567",
        provider="twilio",
        answered_at=now - timedelta(minutes=1),
        duration_seconds=60,
        disposition="appointment-booked",
        call_metadata={
            "runtime": {
                "speech_provider": "sarvam",
                "llm_model": "gpt-4o-mini",
                "tts_characters": 1000,
                "llm_tokens": 1500,
                "llm_input_tokens": 1000,
                "llm_output_tokens": 500,
            }
        },
        created_at=now - timedelta(hours=2),
    )
    smallest_call = Call(
        tenant_id=tenant.id,
        agent_id=smallest_agent.id,
        direction="outbound",
        status="completed",
        from_number="provider-managed",
        to_number="+14155550123",
        provider="smallest",
        answered_at=now - timedelta(minutes=2),
        duration_seconds=120,
        created_at=now - timedelta(hours=1),
    )
    db.add_all([vav_call, smallest_call])
    await db.flush()
    db.add(
        CallTranscript(
            tenant_id=tenant.id,
            call_id=vav_call.id,
            turns=[{"role": "assistant", "content": "A" * 1000}],
            full_text="Assistant response",
        )
    )
    db.add(
        UsageRecord(
            tenant_id=tenant.id,
            call_id=vav_call.id,
            usage_type="call_minutes",
            quantity=1,
            unit="minutes",
            cost_cents=5,
            period_start=now,
            period_end=now,
            created_at=now - timedelta(hours=1),
        )
    )
    await db.commit()

    response = await client.get("/api/v1/billing/cost-report?days=7", headers=auth_headers)

    assert response.status_code == 200
    report = response.json()
    assert report["summary"]["total_calls"] == 2
    assert report["summary"]["answered_calls"] == 2
    assert report["summary"]["successful_calls"] == 1
    assert report["summary"]["estimated_cost_usd"] > 0.64
    assert report["summary"]["estimated_cost_aed"] == pytest.approx(
        report["summary"]["estimated_cost_usd"] * 3.6725,
        abs=0.00001,
    )
    assert report["summary"]["ledger_estimate_usd"] == 0.05
    assert report["summary"]["cost_coverage"] == 1
    assert {row["provider"] for row in report["provider_breakdown"]} >= {
        "Twilio",
        "Sarvam",
        "OpenAI",
        "Smallest.ai",
    }
    direct = next(row for row in report["calls"] if row["call_id"] == str(vav_call.id))
    assert direct["pricing_completeness"] == "complete"
    assert direct["ledger_cost_usd"] == 0.05
    assert any(
        rate["provider"] == "ElevenLabs" and rate["aed"] == pytest.approx(0.183625)
        for rate in report["rate_cards"]
    )


@pytest.mark.asyncio
async def test_cost_report_filters_speech_provider_and_exports_csv(
    client,
    auth_headers,
    db,
    tenant,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="ElevenLabs support",
        system_prompt="Help callers.",
        voice_provider="elevenlabs",
    )
    db.add(agent)
    await db.flush()
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="inbound",
        status="failed",
        from_number="+14155550123",
        to_number="+14142934703",
        provider="twilio",
        duration_seconds=30,
        call_metadata={"runtime": {"speech_provider": "elevenlabs", "tts_characters": 200}},
    )
    db.add(call)
    await db.commit()

    response = await client.get(
        "/api/v1/billing/cost-report?speech_provider=elevenlabs&days=30",
        headers=auth_headers,
    )
    exported = await client.get(
        "/api/v1/billing/cost-report.csv?speech_provider=elevenlabs&days=30",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["summary"]["total_calls"] == 1
    assert response.json()["calls"][0]["speech_provider"] == "elevenlabs"
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("text/csv")
    assert "estimated_cost_aed" in exported.text
    assert str(call.id) in exported.text


@pytest.mark.asyncio
async def test_cost_report_attributes_livekit_and_direct_inworld_without_carrier_markup(
    client,
    auth_headers,
    db,
    tenant,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Inworld concierge",
        system_prompt="Use approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
    )
    db.add(agent)
    await db.flush()
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="inbound",
        status="completed",
        from_number="+971501234567",
        to_number="+97141234567",
        provider="livekit_sip",
        duration_seconds=60,
        call_metadata={
            "runtime": {
                "speech_provider": "inworld",
                "llm_provider": "inworld",
                "llm_model": "openai/gpt-4o-mini",
                "tts_model": "inworld-tts-2",
                "tts_characters": 1000,
                "llm_input_tokens": 1000,
                "llm_output_tokens": 500,
                "recording_enabled": True,
            }
        },
    )
    db.add(call)
    await db.commit()

    response = await client.get(
        "/api/v1/billing/cost-report?provider=livekit_sip&speech_provider=inworld&days=30",
        headers=auth_headers,
    )

    assert response.status_code == 200
    providers = {row["provider"] for row in response.json()["provider_breakdown"]}
    assert providers >= {"LiveKit", "Inworld"}
    assert "Inworld Router" not in providers
    report = response.json()
    row = report["calls"][0]
    components = row["components"]
    services = {item["service"] for item in components}
    assert services >= {"Third-party SIP", "Recording", "Speech to text", "TTS 2"}
    assert "Agent session" not in services
    assert "Railway LiveKit worker hosting allocation" in row["missing_cost_inputs"]
    assert "Inworld Router usage/rate" in row["missing_cost_inputs"]
    assert row["pricing_completeness"] == "partial"
    assert report["summary"]["fully_priced_calls"] == 0
    assert all(rate["service"] != "Agent session" for rate in report["rate_cards"])
    assert all("e&" not in item["provider"] for item in components)
    stt = next(item for item in components if item["service"] == "Speech to text")
    tts = next(item for item in components if item["service"] == "TTS 2")
    assert stt["rate_usd"] == pytest.approx(0.15)
    assert tts["rate_usd"] == pytest.approx(0.025)
    assert "conservative public on-demand list rate" in stt["basis"]
    assert "conservative public on-demand list rate" in tts["basis"]
    inworld_rates = {
        item["service"]: item
        for item in response.json()["rate_cards"]
        if item["provider"] == "Inworld"
    }
    assert inworld_rates["Speech to text"]["native_amount"] == pytest.approx(0.15)
    assert inworld_rates["TTS 2 Flash"]["native_amount"] == pytest.approx(0.015)
    assert inworld_rates["TTS 2"]["native_amount"] == pytest.approx(0.025)
    assert all(
        item["source_url"] == "https://inworld.ai/pricing" for item in inworld_rates.values()
    )
    assert all("negotiated tiers may reduce" in item["notes"] for item in inworld_rates.values())


@pytest.mark.asyncio
async def test_inworld_auto_router_cost_is_partial_without_actual_model(
    client,
    auth_headers,
    db,
    tenant,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Auto-routed concierge",
        system_prompt="Use approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
    )
    db.add(agent)
    await db.flush()
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="inbound",
        status="completed",
        from_number="+971501234567",
        to_number="+97141234567",
        provider="livekit_sip",
        duration_seconds=60,
        call_metadata={
            "runtime": {
                "speech_provider": "inworld",
                "llm_provider": "inworld",
                "llm_model": "auto",
                "tts_model": "inworld-tts-2",
                "tts_characters": 1000,
                "llm_input_tokens": 1000,
                "llm_output_tokens": 500,
            }
        },
    )
    db.add(call)
    await db.commit()

    response = await client.get(
        "/api/v1/billing/cost-report?provider=livekit_sip&days=30",
        headers=auth_headers,
    )

    assert response.status_code == 200
    report = response.json()
    row = next(item for item in report["calls"] if item["call_id"] == str(call.id))
    assert row["pricing_completeness"] == "partial"
    assert "Inworld Router usage/rate" in row["missing_cost_inputs"]
    assert "Railway LiveKit worker hosting allocation" in row["missing_cost_inputs"]
    assert all(component["provider"] != "Inworld Router" for component in row["components"])
    assert report["summary"]["fully_priced_calls"] == 0
