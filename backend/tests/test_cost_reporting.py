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
