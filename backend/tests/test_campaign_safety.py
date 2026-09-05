"""Campaign tenant isolation, compliance, scheduling, and provider routing tests."""

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy import func, select

from app.core import database
from app.core.config import settings
from app.models.agent import Agent
from app.models.call import Call
from app.models.campaign import Campaign, CampaignContact
from app.models.compliance import DncEntry
from app.models.tenant import Tenant
from app.models.workflow import Workflow
from app.services.phone_numbers import normalize_e164
from app.tasks import campaign_tasks
from app.telephony.base import CallResult
from tests.conftest import test_session_factory as session_factory


def _agent(tenant_id, *, provider: str = "smallest", provider_agent_id: str | None = None):
    return Agent(
        tenant_id=tenant_id,
        name=f"{provider.title()} Agent",
        system_prompt="Help the customer safely and concisely.",
        voice_provider=provider,
        provider_agent_id=provider_agent_id,
        provider_revision_id="published-revision" if provider_agent_id else None,
        last_synced_at=datetime.now(UTC) if provider_agent_id else None,
        sync_status="synced" if provider_agent_id else "local_only",
    )


def _campaign(tenant_id, agent_id, **overrides):
    values = {
        "tenant_id": tenant_id,
        "agent_id": agent_id,
        "name": "Safe campaign",
        "status": "running",
        "calling_hours_start": None,
        "calling_hours_end": None,
        "timezone": "UTC",
        "max_concurrent_calls": 5,
        "retry_attempts": 0,
    }
    values.update(overrides)
    return Campaign(**values)


def test_e164_rejects_an_invalid_zero_country_code():
    assert normalize_e164("+0501234567") is None


@pytest.mark.asyncio
async def test_campaign_api_rejects_cross_tenant_agent_and_workflow(
    client,
    auth_headers,
    tenant,
    db,
):
    own_agent_response = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Owned Agent", "system_prompt": "Help customers with their request."},
    )
    assert own_agent_response.status_code == 201
    own_agent_id = own_agent_response.json()["id"]

    other_tenant = Tenant(name="Other Corp", slug="other-corp")
    db.add(other_tenant)
    await db.flush()
    other_agent = _agent(other_tenant.id)
    other_workflow = Workflow(
        tenant_id=other_tenant.id,
        agent_id=other_agent.id,
        name="Foreign workflow",
        trigger_type="campaign",
    )
    db.add_all([other_agent, other_workflow])
    await db.commit()

    foreign_agent_response = await client.post(
        "/api/v1/campaigns",
        headers=auth_headers,
        json={"name": "Blocked campaign", "agent_id": str(other_agent.id)},
    )
    assert foreign_agent_response.status_code == 404
    assert foreign_agent_response.json()["detail"] == "Agent not found"

    foreign_workflow_response = await client.post(
        "/api/v1/campaigns",
        headers=auth_headers,
        json={
            "name": "Blocked workflow campaign",
            "agent_id": own_agent_id,
            "workflow_id": str(other_workflow.id),
        },
    )
    assert foreign_workflow_response.status_code == 404
    assert foreign_workflow_response.json()["detail"] == "Workflow not found"

    campaign_response = await client.post(
        "/api/v1/campaigns",
        headers=auth_headers,
        json={
            "name": "Owned campaign",
            "agent_id": own_agent_id,
            "contacts": [{"phone_number": "+971 (50) 123-4567"}],
        },
    )
    assert campaign_response.status_code == 201
    campaign_id = campaign_response.json()["id"]

    update_agent_response = await client.patch(
        f"/api/v1/campaigns/{campaign_id}",
        headers=auth_headers,
        json={"agent_id": str(other_agent.id)},
    )
    assert update_agent_response.status_code == 404

    update_workflow_response = await client.patch(
        f"/api/v1/campaigns/{campaign_id}",
        headers=auth_headers,
        json={"workflow_id": str(other_workflow.id)},
    )
    assert update_workflow_response.status_code == 404

    contacts_response = await client.get(
        f"/api/v1/campaigns/{campaign_id}/contacts",
        headers=auth_headers,
    )
    assert contacts_response.status_code == 200
    assert contacts_response.json()[0]["phone_number"] == "+971501234567"
    assert str(tenant.id) == campaign_response.json()["tenant_id"]


@pytest.mark.asyncio
async def test_dnc_endpoints_store_check_and_deduplicate_canonical_numbers(
    client,
    auth_headers,
):
    created = await client.post(
        "/api/v1/compliance/dnc",
        headers=auth_headers,
        json={"phone_number": "00971 50 123 4567", "reason": "customer_request"},
    )
    assert created.status_code == 201
    assert created.json()["phone_number"] == "+971501234567"

    checked = await client.get(
        "/api/v1/compliance/dnc/check",
        headers=auth_headers,
        params={"phone_number": "+971 (50) 123-4567"},
    )
    assert checked.status_code == 200
    assert checked.json() == {"phone_number": "+971501234567", "is_on_dnc": True}

    duplicate = await client.post(
        "/api/v1/compliance/dnc",
        headers=auth_headers,
        json={"phone_number": "+971501234567"},
    )
    assert duplicate.status_code == 400


def test_next_dispatch_time_enforces_schedule_and_calling_window():
    campaign = SimpleNamespace(
        scheduled_start=None,
        scheduled_end=None,
        calling_hours_start="09:00",
        calling_hours_end="17:00",
        timezone="Asia/Dubai",
    )
    before_window = datetime(2026, 8, 27, 4, 0, tzinfo=UTC)  # 08:00 in Dubai
    assert campaign_tasks._next_dispatch_time(campaign, before_window) == datetime(
        2026, 8, 27, 5, 0, tzinfo=UTC
    )

    inside_window = datetime(2026, 8, 27, 8, 0, tzinfo=UTC)
    assert campaign_tasks._next_dispatch_time(campaign, inside_window) == inside_window

    campaign.scheduled_end = datetime(2026, 8, 27, 4, 30, tzinfo=UTC)
    assert campaign_tasks._next_dispatch_time(campaign, before_window) is None


@pytest.mark.asyncio
async def test_worker_scopes_campaign_and_contacts_to_task_tenant(tenant, db, monkeypatch):
    tenant_id = tenant.id
    other_tenant = Tenant(name="Other Corp", slug="campaign-worker-other")
    db.add(other_tenant)
    await db.flush()
    agent = _agent(tenant.id, provider_agent_id="remote-agent")
    db.add(agent)
    await db.flush()
    campaign = _campaign(tenant.id, agent.id)
    db.add(campaign)
    await db.flush()
    foreign_contact = CampaignContact(
        tenant_id=other_tenant.id,
        campaign_id=campaign.id,
        phone_number="+971501234567",
    )
    db.add(foreign_contact)
    await db.commit()
    campaign_id = campaign.id
    foreign_contact_id = foreign_contact.id
    other_tenant_id = other_tenant.id

    monkeypatch.setattr(database, "async_session_factory", session_factory)
    apply_async = Mock()
    monkeypatch.setattr(campaign_tasks.run_campaign, "apply_async", apply_async)

    await campaign_tasks._run_campaign_async(str(campaign_id), str(other_tenant_id))

    db.expire_all()
    unchanged_campaign = await db.get(Campaign, campaign_id)
    unchanged_contact = await db.get(CampaignContact, foreign_contact_id)
    assert unchanged_campaign.status == "running"
    assert unchanged_contact.status == "pending"
    assert await db.scalar(select(func.count()).select_from(Call)) == 0

    await campaign_tasks._run_campaign_async(str(campaign_id), str(tenant_id))

    db.expire_all()
    completed_campaign = await db.get(Campaign, campaign_id)
    unchanged_contact = await db.get(CampaignContact, foreign_contact_id)
    assert completed_campaign.status == "completed"
    assert unchanged_contact.status == "pending"
    assert await db.scalar(select(func.count()).select_from(Call)) == 0
    apply_async.assert_not_called()


@pytest.mark.asyncio
async def test_worker_pauses_for_cross_tenant_agent_and_workflow(tenant, db, monkeypatch):
    tenant_id = tenant.id
    other_tenant = Tenant(name="Other Corp", slug="campaign-reference-other")
    db.add(other_tenant)
    await db.flush()
    own_agent = _agent(tenant.id, provider_agent_id="own-remote-agent")
    foreign_agent = _agent(other_tenant.id, provider_agent_id="foreign-remote-agent")
    db.add_all([own_agent, foreign_agent])
    await db.flush()
    foreign_workflow = Workflow(
        tenant_id=other_tenant.id,
        agent_id=foreign_agent.id,
        name="Foreign workflow",
        trigger_type="campaign",
    )
    db.add(foreign_workflow)
    await db.flush()
    agent_campaign = _campaign(tenant.id, foreign_agent.id, name="Foreign agent")
    workflow_campaign = _campaign(
        tenant.id,
        own_agent.id,
        workflow_id=foreign_workflow.id,
        name="Foreign workflow",
    )
    db.add_all([agent_campaign, workflow_campaign])
    await db.commit()
    agent_campaign_id = agent_campaign.id
    workflow_campaign_id = workflow_campaign.id

    monkeypatch.setattr(database, "async_session_factory", session_factory)

    await campaign_tasks._run_campaign_async(str(agent_campaign_id), str(tenant_id))
    await campaign_tasks._run_campaign_async(str(workflow_campaign_id), str(tenant_id))

    db.expire_all()
    assert (await db.get(Campaign, agent_campaign_id)).status == "paused"
    assert (await db.get(Campaign, workflow_campaign_id)).status == "paused"
    assert await db.scalar(select(func.count()).select_from(Call)) == 0


@pytest.mark.asyncio
async def test_worker_uses_tenant_dnc_and_routes_smallest_agents(tenant, db, monkeypatch):
    tenant_id = tenant.id
    agent = _agent(tenant.id, provider="smallest", provider_agent_id="smallest-agent")
    db.add(agent)
    await db.flush()
    campaign = _campaign(tenant.id, agent.id)
    db.add(campaign)
    await db.flush()
    blocked_contact = CampaignContact(
        tenant_id=tenant.id,
        campaign_id=campaign.id,
        phone_number="+971501234567",
    )
    allowed_contact = CampaignContact(
        tenant_id=tenant.id,
        campaign_id=campaign.id,
        phone_number="+971551234567",
        context_data={"customer_name": "Maya"},
    )
    db.add_all(
        [
            blocked_contact,
            allowed_contact,
            DncEntry(tenant_id=tenant.id, phone_number="00971 50 123 4567"),
        ]
    )
    await db.commit()
    campaign_id = campaign.id
    blocked_contact_id = blocked_contact.id
    allowed_contact_id = allowed_contact.id

    smallest_client = SimpleNamespace(
        start_outbound_call=AsyncMock(return_value="smallest-conversation")
    )
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    monkeypatch.setattr(campaign_tasks, "get_smallest_client", lambda: smallest_client)
    monkeypatch.setattr(
        campaign_tasks,
        "get_telephony_provider",
        lambda **_kwargs: pytest.fail("Twilio must not be used for a Smallest.ai agent"),
    )
    apply_async = Mock()
    monkeypatch.setattr(campaign_tasks.run_campaign, "apply_async", apply_async)

    await campaign_tasks._run_campaign_async(str(campaign_id), str(tenant_id))

    db.expire_all()
    blocked = await db.get(CampaignContact, blocked_contact_id)
    allowed = await db.get(CampaignContact, allowed_contact_id)
    call = (await db.execute(select(Call))).scalar_one()
    assert blocked.status == "dnc"
    assert allowed.status == "calling"
    assert call.provider == "smallest"
    assert call.status == "ringing"
    assert call.provider_call_sid == "smallest-conversation"
    assert (await db.get(Campaign, campaign_id)).successful_contacts == 0
    smallest_client.start_outbound_call.assert_awaited_once()
    provider_request = smallest_client.start_outbound_call.await_args.kwargs
    assert provider_request["agent_id"] == "smallest-agent"
    assert provider_request["phone_number"] == "+971551234567"
    assert provider_request["variables"]["customer_name"] == "Maya"
    assert provider_request["variables"]["_vav_call_id"] == str(call.id)
    assert provider_request["variables"]["_voice_ai_attempt_id"]
    assert "from_product_id" not in provider_request
    assert provider_request["version_id"] == "published-revision"
    # Accepted calls normally advance through signed callbacks, with one
    # bounded watchdog wake-up if that terminal lifecycle is lost.
    apply_async.assert_called_once()
    watchdog = apply_async.call_args.kwargs
    assert watchdog["args"] == [str(campaign_id), str(tenant_id)]
    assert watchdog["eta"] > datetime.now(UTC)


@pytest.mark.asyncio
async def test_worker_routes_twilio_only_for_twilio_agent_and_records_failure(
    tenant,
    db,
    monkeypatch,
):
    tenant_id = tenant.id
    twilio_agent = _agent(tenant.id, provider="twilio")
    unsupported_agent = _agent(tenant.id, provider="custom")
    db.add_all([twilio_agent, unsupported_agent])
    await db.flush()
    twilio_campaign = _campaign(tenant.id, twilio_agent.id, name="Twilio campaign")
    unsupported_campaign = _campaign(tenant.id, unsupported_agent.id, name="Custom campaign")
    db.add_all([twilio_campaign, unsupported_campaign])
    await db.flush()
    twilio_contact = CampaignContact(
        tenant_id=tenant.id,
        campaign_id=twilio_campaign.id,
        phone_number="+971501111111",
    )
    unsupported_contact = CampaignContact(
        tenant_id=tenant.id,
        campaign_id=unsupported_campaign.id,
        phone_number="+971502222222",
    )
    db.add_all([twilio_contact, unsupported_contact])
    await db.commit()
    twilio_campaign_id = twilio_campaign.id
    unsupported_campaign_id = unsupported_campaign.id
    unsupported_contact_id = unsupported_contact.id

    twilio_provider = SimpleNamespace(
        make_call=AsyncMock(
            return_value=CallResult(provider_call_sid="twilio-sid", status="queued")
        )
    )
    monkeypatch.setattr(database, "async_session_factory", session_factory)
    monkeypatch.setattr(settings, "twilio_default_from_number", "+971503333333")
    monkeypatch.setattr(
        campaign_tasks,
        "get_telephony_provider",
        lambda **_kwargs: twilio_provider,
    )
    monkeypatch.setattr(
        campaign_tasks,
        "get_smallest_client",
        lambda: pytest.fail("Smallest.ai must not be used for a Twilio agent"),
    )
    monkeypatch.setattr(campaign_tasks.run_campaign, "apply_async", Mock())

    await campaign_tasks._run_campaign_async(str(twilio_campaign_id), str(tenant_id))
    await campaign_tasks._run_campaign_async(str(unsupported_campaign_id), str(tenant_id))

    db.expire_all()
    twilio_call = await db.scalar(select(Call).where(Call.campaign_id == twilio_campaign_id))
    failed_call = await db.scalar(select(Call).where(Call.campaign_id == unsupported_campaign_id))
    failed_contact = await db.get(CampaignContact, unsupported_contact_id)
    assert twilio_call.provider == "twilio"
    assert twilio_call.provider_call_sid == "twilio-sid"
    assert twilio_call.status == "ringing"
    assert failed_call is None
    assert failed_contact.status == "pending"
    unsupported_campaign = await db.get(Campaign, unsupported_campaign_id)
    assert unsupported_campaign.status == "paused"
    assert "not supported" in unsupported_campaign.settings["last_dispatch_error"]
    assert (await db.get(Campaign, unsupported_campaign_id)).successful_contacts == 0
    twilio_provider.make_call.assert_awaited_once()
