"""PostgreSQL serialization for cross-tenant Twilio DID ownership."""

import asyncio
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.models.agent import Agent, AgentRuntimeProfile
from app.models.tenant import Tenant
from app.services.provider_credentials import store_provider_config
from app.services.twilio_route_security import (
    active_twilio_route_conflicts,
    load_workspace_twilio_route_credential,
    lock_twilio_route_claims,
    mark_twilio_route_verified,
    twilio_route_verification_is_current,
)
from tests.conftest import engine


@pytest.mark.asyncio
async def test_postgres_same_account_did_can_activate_for_only_one_tenant(db, tenant):
    if engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL advisory-lock semantics")

    other_tenant = Tenant(
        name="Concurrent Twilio claimant",
        slug=f"twilio-claim-{uuid4().hex[:12]}",
    )
    db.add(other_tenant)
    await db.flush()
    account_sid = "AC" + "9" * 32
    auth_token = "shared-provider-account-token"
    number = "+15551234567"
    callback_url = "https://voice.example.com/api/v1/webhooks/twilio/voice/inbound"
    agents: list[Agent] = []
    profiles: list[AgentRuntimeProfile] = []
    for owner in (tenant, other_tenant):
        agent = Agent(
            tenant_id=owner.id,
            name=f"Twilio claimant {owner.slug}",
            system_prompt="Use approved knowledge.",
            voice_provider="sarvam",
            voice_id="sarvam:ishita",
        )
        db.add(agent)
        await db.flush()
        profile = AgentRuntimeProfile(
            tenant_id=owner.id,
            agent_id=agent.id,
            enabled=False,
            status="draft",
            telephony_provider="twilio",
            primary_speech_provider="sarvam",
            llm_provider="openai",
            assigned_numbers=[number],
        )
        db.add(profile)
        await store_provider_config(
            db,
            owner.id,
            "twilio",
            {"account_sid": account_sid, "auth_token": auth_token},
        )
        mark_twilio_route_verified(
            profile,
            await load_workspace_twilio_route_credential(db, owner.id),
            expected_voice_url=callback_url,
        )
        agents.append(agent)
        profiles.append(profile)
    await db.commit()
    identities = [(agent.id, agent.tenant_id) for agent in agents]
    profile_ids = [profile.id for profile in profiles]

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    first_has_route_lock = asyncio.Event()
    release_first = asyncio.Event()

    async def activate(agent_id, tenant_id, *, hold: bool) -> bool:
        async with session_factory() as session, session.begin():
            agent = await session.get(Agent, agent_id)
            profile = await session.scalar(
                select(AgentRuntimeProfile).where(
                    AgentRuntimeProfile.agent_id == agent_id,
                    AgentRuntimeProfile.tenant_id == tenant_id,
                )
            )
            credential = await load_workspace_twilio_route_credential(session, tenant_id)
            assert agent is not None
            assert profile is not None
            assert credential is not None
            await lock_twilio_route_claims(
                session,
                credential=credential,
                assigned_numbers=[number],
            )
            conflicts = await active_twilio_route_conflicts(
                session,
                agent_id=agent_id,
                account_sid=account_sid,
                assigned_numbers=[number],
                expected_voice_url=callback_url,
            )
            if conflicts:
                return False
            profile.enabled = True
            profile.status = "active"
            await session.flush()
            if hold:
                first_has_route_lock.set()
                await release_first.wait()
            return True

    first = asyncio.create_task(activate(*identities[0], hold=True))
    await asyncio.wait_for(first_has_route_lock.wait(), timeout=5)
    second = asyncio.create_task(activate(*identities[1], hold=False))
    await asyncio.sleep(0.1)
    assert not second.done()
    release_first.set()
    assert await asyncio.wait_for(first, timeout=5) is True
    assert await asyncio.wait_for(second, timeout=5) is False

    db.expire_all()
    active_count = 0
    for profile_id in profile_ids:
        stored = await db.get(AgentRuntimeProfile, profile_id)
        active_count += int(bool(stored and stored.enabled and stored.status == "active"))
    assert active_count == 1


@pytest.mark.asyncio
async def test_postgres_concurrent_active_readiness_refresh_mints_one_current_claim(db, tenant):
    if engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL advisory-lock semantics")

    from app.api.v1.endpoints import runtime as runtime_endpoint

    other_tenant = Tenant(
        name="Concurrent stale Twilio claimant",
        slug=f"twilio-refresh-{uuid4().hex[:12]}",
    )
    db.add(other_tenant)
    await db.flush()
    account_sid = "AC" + "8" * 32
    auth_token = "shared-readiness-refresh-token"
    number = "+15559876543"
    identities: list[tuple] = []
    profile_ids = []
    for owner in (tenant, other_tenant):
        agent = Agent(
            tenant_id=owner.id,
            name=f"Active stale route {owner.slug}",
            system_prompt="Refresh the existing route safely.",
            voice_provider="sarvam",
            voice_id="sarvam:ishita",
        )
        db.add(agent)
        await db.flush()
        profile = AgentRuntimeProfile(
            tenant_id=owner.id,
            agent_id=agent.id,
            enabled=True,
            status="active",
            telephony_provider="twilio",
            primary_speech_provider="sarvam",
            llm_provider="openai",
            assigned_numbers=[number],
            runtime_config={
                "twilio_route_verification": {
                    "version": 1,
                    "fingerprint": "stale",
                    "verified_at": "2026-01-01T00:00:00+00:00",
                }
            },
        )
        db.add(profile)
        await store_provider_config(
            db,
            owner.id,
            "twilio",
            {"account_sid": account_sid, "auth_token": auth_token},
        )
        identities.append((agent.id, owner.id))
        profile_ids.append(profile.id)
    await db.commit()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    first_has_route_lock = asyncio.Event()
    release_first = asyncio.Event()

    async def refresh(agent_id, tenant_id, *, hold: bool) -> bool:
        async with session_factory() as session, session.begin():
            agent = await session.get(Agent, agent_id)
            profile = await session.scalar(
                select(AgentRuntimeProfile).where(
                    AgentRuntimeProfile.agent_id == agent_id,
                    AgentRuntimeProfile.tenant_id == tenant_id,
                )
            )
            credential = await load_workspace_twilio_route_credential(
                session,
                tenant_id,
                for_update=True,
            )
            assert agent is not None
            assert profile is not None
            assert credential is not None
            conflicts = await runtime_endpoint._claim_twilio_route_verification(
                session,
                agent,
                profile,
                credential,
            )
            await session.flush()
            if conflicts:
                return False
            if hold:
                first_has_route_lock.set()
                await release_first.wait()
            return True

    first = asyncio.create_task(refresh(*identities[0], hold=True))
    await asyncio.wait_for(first_has_route_lock.wait(), timeout=5)
    second = asyncio.create_task(refresh(*identities[1], hold=False))
    await asyncio.sleep(0.1)
    assert not second.done()
    release_first.set()
    assert await asyncio.wait_for(first, timeout=5) is True
    assert await asyncio.wait_for(second, timeout=5) is False

    db.expire_all()
    current_claims = 0
    for profile_id, (_agent_id, tenant_id) in zip(profile_ids, identities, strict=True):
        stored = await db.get(AgentRuntimeProfile, profile_id)
        credential = await load_workspace_twilio_route_credential(db, tenant_id)
        assert stored is not None
        assert credential is not None
        current_claims += int(
            twilio_route_verification_is_current(
                stored,
                credential,
                expected_voice_url=runtime_endpoint._twilio_inbound_voice_url(),
            )
        )
    assert current_claims == 1
