"""Dialer tests never place paid calls; provider boundaries are mocked."""

from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.core.config import settings
from app.models.agent import Agent
from app.models.call import Call
from app.models.dialer import DialerCampaign, DialerCustomer, DialerJob
from app.models.tenant import Tenant
from app.schemas.dialer import CampaignInput, CustomerInput, EnqueueInput
from app.services.dialer_dispatch import claim_due_jobs, dispatch_job, reconcile
from app.services.dialer_import import parse_customers
from app.services.dialer_policy import dial_capacity, eligibility
from app.services.phone_numbers import is_number_on_tenant_dnc

NOW = datetime(2026, 9, 7, 8, tzinfo=UTC)
CSV = (
    b"name,phone_number,company,contact_allowed,consent_reference\n"
    b"Test Person,+971501234567,Test,true,Permission record 1\n"
)


def config(**overrides):
    return CampaignInput(
        name="Test campaign",
        company="Test",
        agent_id=uuid4(),
        compliance_approved=True,
        **overrides,
    ).model_dump(mode="json")


async def seed(db, tenant, user, mode="progressive", count=2, **overrides):
    agent = Agent(tenant_id=tenant.id, name="Dialer test", system_prompt="Approved survey")
    db.add(agent)
    await db.flush()
    campaign = DialerCampaign(
        tenant_id=tenant.id,
        owner_id=user.id,
        agent_id=agent.id,
        name="Test campaign",
        company="Test",
        purpose="survey",
        mode=mode,
        status="running",
        config=config(**overrides),
    )
    db.add(campaign)
    await db.flush()
    jobs = []
    for n in range(count):
        customer = DialerCustomer(
            tenant_id=tenant.id,
            company="Test",
            name=f"Person {n}",
            phone_number=f"+97150{(agent.id.int + n) % 10_000_000:07d}",
            contact_allowed=True,
            consent_reference="Recorded permission",
        )
        db.add(customer)
        await db.flush()
        job = DialerJob(
            tenant_id=tenant.id,
            customer_id=customer.id,
            campaign_id=campaign.id,
            event_key=f"test-event-{n}",
            available_at=NOW - timedelta(hours=1),
        )
        db.add(job)
        jobs.append(job)
    await db.commit()
    return campaign, jobs


def test_csv_import_validates_permission_and_duplicates():
    rows, errors, total = parse_customers("customers.csv", CSV)
    assert total == 1 and not errors and rows[0].contact_allowed
    assert parse_customers("a.csv", CSV + CSV.splitlines(keepends=True)[1])[1]
    assert parse_customers("a.csv", CSV.replace(b"Permission record 1", b""))[1]
    assert parse_customers("a.csv", CSV.replace(b"Test Person", b'=HYPERLINK("x")'))[1]
    with pytest.raises(ValueError):
        parse_customers("a.exe", CSV)
    with pytest.raises(ValueError):
        parse_customers("a.csv", b"x" * 2_000_001)


def test_xlsx_import_values_and_formula_rejection():
    from openpyxl import Workbook

    book = Workbook()
    book.active.append(["name", "phone_number", "company"])
    book.active.append(["Customer", "+971501234567", "Test"])
    out = BytesIO()
    book.save(out)
    rows, errors, _ = parse_customers("customers.xlsx", out.getvalue())
    assert rows[0].name == "Customer" and not errors
    book.active["A2"] = "=1+1"
    out = BytesIO()
    book.save(out)
    assert parse_customers("customers.xlsx", out.getvalue())[1]
    book.active.cell(row=6000, column=1, value="gap")
    out = BytesIO()
    book.save(out)
    with pytest.raises(ValueError, match="dimensions"):
        parse_customers("customers.xlsx", out.getvalue())


def test_schemas_reject_unsafe_schedules_and_capacity():
    with pytest.raises(ValidationError):
        EnqueueInput(
            customer_ids=[uuid4()], event_key="test-event", available_at="2026-09-07T12:00:00"
        )
    with pytest.raises(ValidationError):
        config(max_concurrent_calls=1, target_live_calls=2)
    with pytest.raises(ValidationError):
        config(timezone="not/a/timezone")
    with pytest.raises(ValidationError):
        CustomerInput(company="Test", name="Test", phone_number="0501234567")


@pytest.mark.parametrize(
    "mode", ["preview", "progressive", "parallel", "predictive", "scheduled", "event"]
)
def test_pacing_never_overbooks(mode):
    cfg = config(max_concurrent_calls=5, target_live_calls=4)
    for active in range(8):
        for history in ([], [False] * 20, [True] * 20):
            limit = 1 if mode == "progressive" else 5
            assert 0 <= dial_capacity(mode, cfg, active, 0, history) <= max(0, limit - active)
    assert dial_capacity("predictive", cfg, 0) == 1
    assert dial_capacity("predictive", cfg, 0, history=[False] * 20) == 5


def test_eligibility_is_company_permission_and_time_scoped():
    campaign = SimpleNamespace(
        company="Test", status="running", mode="preview", purpose="survey", config=config()
    )
    person = SimpleNamespace(
        company="Test",
        opted_out=False,
        contact_allowed=True,
        consent_reference="record",
        timezone="Asia/Dubai",
        phone_number="+971501234567",
    )
    job = SimpleNamespace(available_at=NOW, approved=False, attempts=0, state="queued")
    assert eligibility(campaign, job, person, NOW) == "preview_approval_required"
    job.approved = True
    assert eligibility(campaign, job, person, NOW) is None
    assert eligibility(campaign, job, person, NOW + timedelta(hours=12)) == "outside_calling_hours"
    person.company = "Different"
    assert eligibility(campaign, job, person, NOW) == "company_mismatch"
    person.company = "Test"
    person.opted_out = True
    assert eligibility(campaign, job, person, NOW) == "contact_permission_missing_or_revoked"
    person.opted_out = False
    campaign.purpose = "collections"
    assert "live_balance" in eligibility(campaign, job, person, NOW)


@pytest.mark.asyncio
async def test_import_is_preview_first_and_repeat_safe(client, auth_headers, db):
    r = await client.post(
        "/api/v1/dialer/customers/import",
        headers=auth_headers,
        files={"file": ("customers.csv", CSV, "text/csv")},
    )
    assert r.status_code == 200 and r.json()["valid"] == 1
    assert await db.scalar(select(func.count()).select_from(DialerCustomer)) == 0
    for expected in (1, 0):
        r = await client.post(
            "/api/v1/dialer/customers/import?commit=true",
            headers=auth_headers,
            files={"file": ("customers.csv", CSV, "text/csv")},
        )
        assert r.status_code == 200 and r.json()["created"] == expected
    assert await db.scalar(select(func.count()).select_from(Call)) == 0


@pytest.mark.asyncio
async def test_api_queue_idempotency_tenant_isolation_and_live_gate(
    client, auth_headers, db, tenant, user, monkeypatch
):
    campaign, jobs = await seed(db, tenant, user, count=1)
    monkeypatch.setattr(settings, "dialer_live_enabled", False)
    body = {"customer_ids": [str(jobs[0].customer_id)], "event_key": "external-event-123"}
    path = f"/api/v1/dialer/campaigns/{campaign.id}"
    first = await client.post(path + "/queue", headers=auth_headers, json=body)
    second = await client.post(path + "/queue", headers=auth_headers, json=body)
    assert first.status_code == 200, first.text
    assert first.json()[0]["id"] == second.json()[0]["id"]
    assert (
        await client.post(path + "/action", headers=auth_headers, json={"action": "start"})
    ).status_code == 409
    assert (await client.get(path + "/simulation", headers=auth_headers)).json()[
        "live_call_made"
    ] is False
    other = Tenant(name="Other", slug="other")
    db.add(other)
    await db.flush()
    stranger = DialerCustomer(
        tenant_id=other.id, name="Private", company="Test", phone_number="+971509999999"
    )
    db.add(stranger)
    await db.commit()
    r = await client.post(
        path + "/queue", headers=auth_headers, json={**body, "customer_ids": [str(stranger.id)]}
    )
    assert r.status_code == 404
    user.role = "member"
    await db.commit()
    assert (await client.get("/api/v1/dialer/customers", headers=auth_headers)).status_code == 403


@pytest.mark.asyncio
async def test_optout_adds_workspace_dnc(client, auth_headers, db, tenant, user):
    _, jobs = await seed(db, tenant, user, count=1)
    person = await db.get(DialerCustomer, jobs[0].customer_id)
    r = await client.patch(
        f"/api/v1/dialer/customers/{person.id}/permission",
        headers=auth_headers,
        json={"opted_out": True, "reason": "Customer request"},
    )
    assert r.status_code == 200, r.text
    assert await is_number_on_tenant_dnc(db, tenant.id, person.phone_number)


@pytest.mark.asyncio
async def test_claims_capacity_budget_and_reservation_durability(db, tenant, user, monkeypatch):
    monkeypatch.setattr(settings, "dialer_live_enabled", True)
    campaign, jobs = await seed(
        db, tenant, user, mode="parallel", count=4, max_concurrent_calls=3, max_attempts_total=2
    )
    ids = await claim_due_jobs(db, campaign.id, tenant.id, NOW)
    assert len(ids) == 2
    assert await claim_due_jobs(db, campaign.id, tenant.id, NOW) == []
    # A lost queue publication is recoverable before dispatch, without incrementing attempts.
    recovered = await claim_due_jobs(db, campaign.id, tenant.id, NOW + timedelta(minutes=6))
    assert len(recovered) == 2
    assert all(job.attempts == 0 for job in jobs)


@pytest.mark.asyncio
async def test_preview_and_future_jobs_do_not_dispatch(db, tenant, user, monkeypatch):
    monkeypatch.setattr(settings, "dialer_live_enabled", True)
    campaign, jobs = await seed(db, tenant, user, mode="preview", count=1)
    assert await claim_due_jobs(db, campaign.id, tenant.id, NOW) == []
    jobs[0].approved = True
    jobs[0].available_at = NOW + timedelta(hours=1)
    await db.commit()
    assert await claim_due_jobs(db, campaign.id, tenant.id, NOW) == []
    assert len(await claim_due_jobs(db, campaign.id, tenant.id, NOW + timedelta(hours=1))) == 1


@pytest.mark.asyncio
async def test_ambiguous_acceptance_holds_campaign_not_redial(db, tenant, user, monkeypatch):
    monkeypatch.setattr(settings, "dialer_live_enabled", True)
    campaign, jobs = await seed(db, tenant, user)
    jobs[0].state = "dispatching"
    jobs[0].claimed_at = NOW - timedelta(minutes=6)
    jobs[0].attempts = 1
    jobs[0].call_id = uuid4()
    await db.commit()
    assert await claim_due_jobs(db, campaign.id, tenant.id, NOW) == []
    assert jobs[0].state == "unknown" and campaign.status == "paused"


@pytest.mark.asyncio
async def test_no_answer_retry_requires_new_preview_approval(db, tenant, user):
    campaign, jobs = await seed(
        db, tenant, user, mode="preview", count=1, max_attempts_per_customer=2
    )
    call = Call(
        tenant_id=tenant.id,
        agent_id=campaign.agent_id,
        direction="outbound",
        status="no_answer",
        from_number="+971501111111",
        to_number="+971501234500",
        provider="livekit_sip",
    )
    db.add(call)
    await db.flush()
    job = jobs[0]
    job.call_id = call.id
    job.attempts = 1
    job.state = "calling"
    job.approved = True
    await db.commit()
    await reconcile(db, campaign, NOW)
    assert job.state == "queued" and job.approved is False
    assert job.available_at == NOW + timedelta(hours=24)


@pytest.mark.asyncio
async def test_dispatch_guard_rechecks_pause_and_does_not_redial(db, tenant, user, monkeypatch):
    from app.api.v1.endpoints import calls
    from app.services import dialer_dispatch

    monkeypatch.setattr(settings, "dialer_live_enabled", True)
    # Test the late guard independently of wall-clock calling windows.
    monkeypatch.setattr(
        dialer_dispatch, "eligibility", lambda c, j, p: None if c.status == "running" else "paused"
    )
    campaign, jobs = await seed(db, tenant, user, count=1)
    job = jobs[0]
    job.state = "reserved"
    await db.commit()
    observations = []

    async def fake_dispatch(data, key, actor, session, *, dispatch_guard):
        assert await dispatch_guard(session)
        assert data.context["company_name"] == "Test" and "notes" not in data.context
        campaign.status = "paused"
        await session.commit()
        observations.append(await dispatch_guard(session))

    mock = AsyncMock(side_effect=fake_dispatch)
    monkeypatch.setattr(calls, "dispatch_outbound_call", mock)
    await dispatch_job(db, job.id, tenant.id)
    await dispatch_job(db, job.id, tenant.id)
    assert observations == [False] and mock.await_count == 1
    await db.refresh(job)
    assert job.attempts == 1


@pytest.mark.asyncio
async def test_final_guard_shared_service_blocks_provider_io(db, tenant, user, monkeypatch):
    from app.api.v1.endpoints import calls
    from app.middleware.tenant import CurrentUser
    from app.schemas.call import CallOutbound
    from app.services.provider_credentials import store_provider_config
    from app.tasks import call_tasks
    from tests.test_calls import _seed_generic_twilio_agent

    await store_provider_config(
        db,
        tenant.id,
        "twilio",
        {
            "account_sid": "AC" + "1" * 32,
            "auth_token": "test-only-workspace-token",
            "default_from_number": "+15550110001",
        },
    )
    await db.commit()
    monkeypatch.setattr(call_tasks.reconcile_call_dispatch, "apply_async", lambda **kw: None)
    monkeypatch.setattr(call_tasks.reconcile_direct_call_terminal, "apply_async", lambda **kw: None)

    agent = await _seed_generic_twilio_agent(db, tenant.id)
    provider = AsyncMock()
    monkeypatch.setattr(calls, "get_telephony_provider", lambda **kw: provider)
    actor = CurrentUser(user.id, tenant.id, user.email, user.role, user.full_name)
    guard = AsyncMock(return_value=False)
    response = await calls.dispatch_outbound_call(
        CallOutbound(agent_id=agent.id, to_number="+971501234567"),
        "dialer-deny-boundary",
        actor,
        db,
        dispatch_guard=guard,
    )
    guard.assert_awaited_once()
    assert response.status == "cancelled"
    provider.make_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_postgres_parallel_sweeps_share_tenant_capacity(db, tenant, user, monkeypatch):
    import asyncio

    from tests.conftest import engine, test_session_factory

    if engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row locks; executed by CI")
    monkeypatch.setattr(settings, "dialer_live_enabled", True)
    monkeypatch.setattr(settings, "dialer_tenant_concurrency", 1)
    first, _ = await seed(db, tenant, user)
    second, _ = await seed(db, tenant, user)

    async def claim(identity):
        async with test_session_factory() as session:
            return await claim_due_jobs(session, identity, tenant.id, NOW)

    results = await asyncio.gather(claim(first.id), claim(second.id))
    assert sum(map(len, results)) == 1


@pytest.mark.asyncio
async def test_callback_event_retry_uses_original_time_and_cancel_clears_queue(
    client,
    auth_headers,
    db,
    tenant,
    user,
):
    campaign, jobs = await seed(db, tenant, user, mode="scheduled", count=1)
    path = f"/api/v1/dialer/campaigns/{campaign.id}"
    body = {
        "customer_ids": [str(jobs[0].customer_id)],
        "event_key": "callback-event-123",
        "available_at": NOW.isoformat(),
    }
    first = await client.post(path + "/queue", headers=auth_headers, json=body)
    assert first.status_code == 200
    job = await db.scalar(select(DialerJob).where(DialerJob.event_key.like("callback-event-123%")))
    job.available_at = NOW + timedelta(days=1)  # Scheduler/retry changed next eligibility.
    await db.commit()
    again = await client.post(path + "/queue", headers=auth_headers, json=body)
    assert again.status_code == 200 and again.json()[0]["id"] == first.json()[0]["id"]
    conflict = await client.post(
        path + "/queue",
        headers=auth_headers,
        json={**body, "available_at": (NOW + timedelta(hours=1)).isoformat()},
    )
    assert conflict.status_code == 409
    assert (
        await client.post(path + "/action", headers=auth_headers, json={"action": "cancel"})
    ).status_code == 200
    await db.refresh(job)
    assert job.state == "cancelled"


@pytest.mark.asyncio
async def test_results_use_late_disposition_and_escape_csv_formulas(
    client, auth_headers, db, tenant, user
):
    from app.models.call import CallSummary

    campaign, jobs = await seed(db, tenant, user, count=1)
    call = Call(
        tenant_id=tenant.id,
        agent_id=campaign.agent_id,
        direction="outbound",
        status="completed",
        from_number="+971501111111",
        to_number="+971501234567",
        provider="livekit_sip",
        disposition="callback",
        duration_seconds=30,
    )
    db.add(call)
    await db.flush()
    db.add(
        CallSummary(tenant_id=tenant.id, call_id=call.id, summary="=formula-like untrusted text")
    )
    jobs[0].call_id = call.id
    jobs[0].state = "completed"
    jobs[0].outcome = "completed"
    await db.commit()
    path = f"/api/v1/dialer/campaigns/{campaign.id}"
    assert (await client.get(path + "/jobs", headers=auth_headers)).json()[0][
        "outcome"
    ] == "callback"
    response = await client.get(path + "/export", headers=auth_headers)
    assert response.status_code == 200 and "callback" in response.text
    assert "'=formula-like untrusted text" in response.text
