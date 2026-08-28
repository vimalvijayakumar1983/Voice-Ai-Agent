"""Analytics aggregation and outcome correctness tests."""

from datetime import UTC, datetime, timedelta

import pytest

from app.models.agent import Agent
from app.models.billing import UsageRecord
from app.models.call import Call
from app.models.tenant import Tenant


@pytest.mark.asyncio
async def test_overview_uses_tenant_scoped_usage_ledger_cost_without_double_counting(
    client,
    auth_headers,
    db,
    tenant,
):
    now = datetime.now(UTC)
    agent = Agent(
        tenant_id=tenant.id,
        name="Cost agent",
        system_prompt="Keep authoritative cost records.",
    )
    other_tenant = Tenant(name="Other Corp", slug="other-corp")
    db.add_all([agent, other_tenant])
    await db.flush()

    # This legacy/provider value must not be added to the usage ledger.
    db.add(
        Call(
            tenant_id=tenant.id,
            agent_id=agent.id,
            direction="outbound",
            status="completed",
            from_number="provider-managed",
            to_number="+971501234567",
            provider="smallest",
            duration_seconds=60,
            cost_cents=999,
        )
    )
    db.add_all(
        [
            UsageRecord(
                tenant_id=tenant.id,
                usage_type="call_minutes",
                quantity=1,
                unit="minutes",
                cost_cents=13,
                period_start=now,
                period_end=now,
                created_at=now - timedelta(days=1),
            ),
            UsageRecord(
                tenant_id=tenant.id,
                usage_type="ai_tokens",
                quantity=100,
                unit="tokens",
                cost_cents=4,
                period_start=now,
                period_end=now,
                created_at=now - timedelta(days=1),
            ),
            UsageRecord(
                tenant_id=tenant.id,
                usage_type="call_minutes",
                quantity=2,
                unit="minutes",
                cost_cents=800,
                period_start=now - timedelta(days=60),
                period_end=now - timedelta(days=60),
                created_at=now - timedelta(days=60),
            ),
            UsageRecord(
                tenant_id=other_tenant.id,
                usage_type="call_minutes",
                quantity=2,
                unit="minutes",
                cost_cents=700,
                period_start=now,
                period_end=now,
                created_at=now - timedelta(days=1),
            ),
        ]
    )
    await db.commit()

    response = await client.get("/api/v1/analytics/overview?days=30", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()["total_cost_cents"] == 17


@pytest.mark.asyncio
async def test_timeseries_honors_week_and_month_periods(client, auth_headers, db, tenant):
    agent = Agent(
        tenant_id=tenant.id,
        name="Analytics agent",
        system_prompt="Measure outcomes accurately.",
    )
    db.add(agent)
    await db.flush()
    for created_at, duration in (
        (datetime(2026, 8, 3, 9, tzinfo=UTC), 60),
        (datetime(2026, 8, 5, 9, tzinfo=UTC), 120),
        (datetime(2026, 8, 10, 9, tzinfo=UTC), 180),
    ):
        db.add(
            Call(
                tenant_id=tenant.id,
                agent_id=agent.id,
                direction="outbound",
                status="completed",
                from_number="provider-managed",
                to_number="+971501234567",
                provider="smallest",
                duration_seconds=duration,
                created_at=created_at,
            )
        )
    await db.commit()

    weekly = await client.get(
        "/api/v1/analytics/timeseries?days=365&period=week",
        headers=auth_headers,
    )
    monthly = await client.get(
        "/api/v1/analytics/timeseries?days=365&period=month",
        headers=auth_headers,
    )

    assert weekly.status_code == 200
    assert weekly.json() == {
        "period": "week",
        "data": [
            {"date": "2026-08-03", "calls": 2, "minutes": 3.0},
            {"date": "2026-08-10", "calls": 1, "minutes": 3.0},
        ],
    }
    assert monthly.status_code == 200
    assert monthly.json() == {
        "period": "month",
        "data": [{"date": "2026-08-01", "calls": 3, "minutes": 6.0}],
    }


@pytest.mark.asyncio
async def test_agent_success_rate_uses_successful_completed_outcomes(
    client,
    auth_headers,
    db,
    tenant,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Outcome agent",
        system_prompt="Report real outcomes.",
    )
    db.add(agent)
    await db.flush()
    for status, disposition in (
        ("completed", "  APPOINTMENT-BOOKED  "),
        ("completed", "not_interested"),
        # A provider can report a provisional disposition before terminal
        # completion. It must never inflate the completed-call success rate.
        ("failed", " SUCCESS "),
    ):
        db.add(
            Call(
                tenant_id=tenant.id,
                agent_id=agent.id,
                direction="outbound",
                status=status,
                from_number="provider-managed",
                to_number="+971501234567",
                provider="smallest",
                duration_seconds=60,
                disposition=disposition,
                sentiment_score=0.0,
            )
        )
    await db.commit()

    response = await client.get("/api/v1/analytics/agents?days=30", headers=auth_headers)

    assert response.status_code == 200
    assert response.json()[0]["total_calls"] == 3
    assert response.json()[0]["success_rate"] == 0.5
    assert response.json()[0]["avg_sentiment"] == 0.0
