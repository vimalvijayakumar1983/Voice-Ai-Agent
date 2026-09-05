"""PostgreSQL serialization checks for native knowledge admission."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import pytest
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1.endpoints import webhooks
from app.api.v1.endpoints.knowledge import set_knowledge_approval
from app.middleware.tenant import CurrentUser
from app.models.agent import Agent, AgentRuntimeProfile, KnowledgeBase
from app.models.call import Call
from app.models.user import User
from app.schemas.knowledge import KnowledgeApprovalRequest
from app.services.knowledge_serving import (
    KnowledgeServingError,
    admit_inbound_twilio_knowledge_call,
    knowledge_call_reservation_metadata,
    pre_admit_outbound_knowledge_call,
)
from app.services.provider_credentials import (
    invalidate_active_runtimes_for_credential,
    lock_provider_runtime_boundaries,
    store_provider_config,
)
from app.services.twilio_route_security import (
    load_workspace_twilio_route_credential,
    mark_twilio_route_verified,
)
from app.services.usage_ledger import lock_agent_runtime_limits
from tests.conftest import engine
from tests.knowledge_test_utils import publish_test_knowledge

Admission = Callable[..., Awaitable[Call]]


@dataclass(frozen=True)
class _RaceFixture:
    tenant_id: UUID
    agent_id: UUID
    knowledge_base_id: UUID
    call_id: UUID
    original_generation: int


async def _native_race_fixture(
    db: AsyncSession,
    *,
    tenant_id: UUID,
    direction: str,
) -> _RaceFixture:
    agent = Agent(
        tenant_id=tenant_id,
        name=f"PostgreSQL {direction} admission race",
        system_prompt="Answer only from the admitted immutable release.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
    )
    db.add(agent)
    await db.flush()
    knowledge, revision = await publish_test_knowledge(
        db,
        tenant_id=tenant_id,
        agent=agent,
        label=f"PostgreSQL {direction}",
    )
    original_generation = knowledge.serving_revocation_generation
    assert original_generation == 0

    call = Call(
        tenant_id=tenant_id,
        agent_id=agent.id,
        direction=direction,
        status="dispatching" if direction == "outbound" else "in_progress",
        from_number="+15550102001",
        to_number="+15550102002",
        provider="twilio",
        provider_call_sid=(
            f"CA-native-admission-race-{direction}" if direction == "inbound" else None
        ),
        call_metadata={
            "speech_provider": "sarvam",
            "runtime": {
                "transport": "twilio_media_streams",
                "speech_provider": "sarvam",
                **knowledge_call_reservation_metadata(revision, original_generation),
            },
        },
    )
    db.add(call)
    await db.commit()
    return _RaceFixture(
        tenant_id=tenant_id,
        agent_id=agent.id,
        knowledge_base_id=knowledge.id,
        call_id=call.id,
        original_generation=original_generation,
    )


def _current_user(user: User) -> CurrentUser:
    return CurrentUser(
        id=user.id,
        tenant_id=user.tenant_id,
        email=user.email,
        role=user.role,
        full_name=user.full_name,
    )


async def _assert_revocation_serializes_and_wins(
    *,
    race: _RaceFixture,
    user: User,
    admission: Admission,
    expected_error: str,
) -> None:
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    revocation_flushed = asyncio.Event()
    release_revocation = asyncio.Event()
    admission_requested_knowledge_lock = asyncio.Event()
    loop = asyncio.get_running_loop()
    revocation_task: asyncio.Task | None = None
    admission_task: asyncio.Task | None = None
    listener_installed = False

    async def revoke_while_holding_knowledge_lock():
        async with session_factory() as session, session.begin():
            response = await set_knowledge_approval(
                race.knowledge_base_id,
                KnowledgeApprovalRequest(approved=False),
                _current_user(user),
                session,
            )
            revocation_flushed.set()
            await release_revocation.wait()
            return response

    async def admit_in_independent_transaction() -> Call:
        async with session_factory() as session, session.begin():
            return await admission(
                session,
                tenant_id=race.tenant_id,
                agent_id=race.agent_id,
                call_id=race.call_id,
            )

    def observe_knowledge_lock(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        normalized = str(statement).upper()
        if "FROM KNOWLEDGE_BASES" in normalized and "FOR UPDATE" in normalized:
            loop.call_soon_threadsafe(admission_requested_knowledge_lock.set)

    try:
        revocation_task = asyncio.create_task(revoke_while_holding_knowledge_lock())
        await asyncio.wait_for(revocation_flushed.wait(), timeout=5)

        event.listen(engine.sync_engine, "before_cursor_execute", observe_knowledge_lock)
        listener_installed = True
        admission_task = asyncio.create_task(admit_in_independent_transaction())
        await asyncio.wait_for(admission_requested_knowledge_lock.wait(), timeout=5)

        # The admission transaction has reached its knowledge row lock but may
        # not cross the boundary while the explicit revocation owns that row.
        await asyncio.sleep(0.1)
        assert not admission_task.done()

        release_revocation.set()
        revoked = await asyncio.wait_for(revocation_task, timeout=5)
        assert revoked.approval_status == "draft"
        with pytest.raises(KnowledgeServingError, match=expected_error):
            await asyncio.wait_for(admission_task, timeout=5)
    finally:
        release_revocation.set()
        if listener_installed:
            event.remove(engine.sync_engine, "before_cursor_execute", observe_knowledge_lock)
        for task in (revocation_task, admission_task):
            if task is not None and not task.done():
                task.cancel()
        pending = [task for task in (revocation_task, admission_task) if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async with session_factory() as verify_db:
        knowledge = await verify_db.get(KnowledgeBase, race.knowledge_base_id)
        call = await verify_db.get(Call, race.call_id)
        assert knowledge is not None
        assert call is not None
        assert knowledge.approval_status == "draft"
        assert knowledge.serving_revision_id is None
        assert knowledge.serving_revocation_generation == race.original_generation + 1
        runtime = call.call_metadata["runtime"]
        assert "knowledge_admission_state" not in runtime


@pytest.mark.asyncio
async def test_postgres_explicit_revocation_serializes_before_outbound_pre_admission(
    db: AsyncSession,
    tenant,
    user: User,
):
    if engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row-lock semantics")

    race = await _native_race_fixture(
        db,
        tenant_id=tenant.id,
        direction="outbound",
    )
    await _assert_revocation_serializes_and_wins(
        race=race,
        user=user,
        admission=pre_admit_outbound_knowledge_call,
        expected_error="Outbound knowledge was revoked before dispatch",
    )


@pytest.mark.asyncio
async def test_postgres_explicit_revocation_serializes_before_inbound_media_admission(
    db: AsyncSession,
    tenant,
    user: User,
):
    if engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row-lock semantics")

    race = await _native_race_fixture(
        db,
        tenant_id=tenant.id,
        direction="inbound",
    )
    await _assert_revocation_serializes_and_wins(
        race=race,
        user=user,
        admission=admit_inbound_twilio_knowledge_call,
        expected_error="Inbound knowledge was revoked before admission",
    )


@pytest.mark.asyncio
async def test_postgres_twilio_rotation_boundary_cannot_be_overtaken_by_inbound_admission(
    db: AsyncSession,
    tenant,
):
    """A rotation owning the provider boundary must win before a later call."""

    if engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL advisory-lock semantics")

    number = "+15550103002"
    original_token = "postgres_rotation_boundary_token_123456789"
    agent = Agent(
        tenant_id=tenant.id,
        name="PostgreSQL credential rotation race",
        system_prompt="Answer only from the admitted immutable release.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
    )
    db.add(agent)
    await db.flush()
    await store_provider_config(
        db,
        tenant.id,
        "twilio",
        {
            "account_sid": "AC" + "2" * 32,
            "auth_token": original_token,
            "default_from_number": number,
        },
    )
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider="twilio",
        primary_speech_provider="sarvam",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
        assigned_numbers=[number],
    )
    db.add(profile)
    authenticated_credential = await load_workspace_twilio_route_credential(db, tenant.id)
    assert authenticated_credential is not None
    mark_twilio_route_verified(
        profile,
        authenticated_credential,
        expected_voice_url=(
            f"{webhooks.settings.base_url.rstrip('/')}/api/v1/webhooks/twilio/voice/inbound"
        ),
    )
    await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="PostgreSQL rotation boundary",
    )
    await db.commit()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    provider_boundary_acquired = asyncio.Event()
    release_rotation = asyncio.Event()
    rotation_task: asyncio.Task | None = None
    admission_task: asyncio.Task | None = None
    call_sid = "CA-provider-boundary-rotation-race"

    async def rotate_credential() -> None:
        async with session_factory() as session, session.begin():
            await lock_provider_runtime_boundaries(session, tenant.id, "twilio")
            provider_boundary_acquired.set()
            await release_rotation.wait()
            invalidated = await invalidate_active_runtimes_for_credential(
                session,
                tenant.id,
                "twilio",
            )
            assert invalidated == [str(agent.id)]
            await store_provider_config(
                session,
                tenant.id,
                "twilio",
                {
                    "account_sid": authenticated_credential.account_sid,
                    "auth_token": "postgres_rotated_token_987654321",
                    "default_from_number": number,
                },
            )

    async def admit_from_stale_candidate() -> Call | None:
        async with session_factory() as session:
            stale_agent = await session.get(Agent, agent.id)
            stale_profile = await session.get(AgentRuntimeProfile, profile.id)
            assert stale_agent is not None
            assert stale_profile is not None
            return await webhooks._reserve_twilio_inbound_call(
                session,
                profile=stale_profile,
                agent=stale_agent,
                authenticated_route_credential=authenticated_credential,
                call_sid=call_sid,
                from_number="+15550103001",
                to_number=number,
            )

    try:
        rotation_task = asyncio.create_task(rotate_credential())
        await asyncio.wait_for(provider_boundary_acquired.wait(), timeout=5)
        admission_task = asyncio.create_task(admit_from_stale_candidate())

        await asyncio.sleep(0.1)
        assert not admission_task.done()

        release_rotation.set()
        await asyncio.wait_for(rotation_task, timeout=5)
        assert await asyncio.wait_for(admission_task, timeout=5) is None
    finally:
        release_rotation.set()
        for task in (rotation_task, admission_task):
            if task is not None and not task.done():
                task.cancel()
        pending = [task for task in (rotation_task, admission_task) if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async with session_factory() as verify_db:
        stored_profile = await verify_db.get(AgentRuntimeProfile, profile.id)
        assert stored_profile is not None
        assert stored_profile.enabled is False
        assert stored_profile.status == "draft"
        assert (
            await verify_db.scalar(select(Call).where(Call.provider_call_sid == call_sid)) is None
        )


@pytest.mark.asyncio
async def test_postgres_inbound_replay_waits_for_terminalization_and_fails_closed(
    db: AsyncSession,
    tenant,
):
    """A duplicate CallSid cannot stream from a stale pre-terminal snapshot."""

    if engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row-lock semantics")

    agent = Agent(
        tenant_id=tenant.id,
        name="PostgreSQL terminal replay race",
        system_prompt="Answer only from the admitted immutable release.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
    )
    db.add(agent)
    await db.flush()
    from_number = "+15550102001"
    to_number = "+15550102002"
    await store_provider_config(
        db,
        tenant.id,
        "twilio",
        {
            "account_sid": "AC" + "1" * 32,
            "auth_token": "postgres_terminal_replay_token_123456789",
            "default_from_number": to_number,
        },
    )
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        enabled=True,
        status="active",
        telephony_provider="twilio",
        primary_speech_provider="sarvam",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
        assigned_numbers=[to_number],
    )
    db.add(profile)
    route_credential = await load_workspace_twilio_route_credential(db, tenant.id)
    assert route_credential is not None
    mark_twilio_route_verified(
        profile,
        route_credential,
        expected_voice_url=(
            f"{webhooks.settings.base_url.rstrip('/')}/api/v1/webhooks/twilio/voice/inbound"
        ),
    )
    _knowledge, revision = await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="PostgreSQL terminal replay",
    )
    call_sid = "CA-native-terminal-replay-race"
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="inbound",
        status="in_progress",
        from_number=from_number,
        to_number=to_number,
        provider="twilio",
        provider_call_sid=call_sid,
        started_at=datetime.now(UTC),
        answered_at=datetime.now(UTC),
        call_metadata={
            "speech_provider": "sarvam",
            "runtime": {
                "transport": "twilio_media_streams",
                "speech_provider": "sarvam",
                **knowledge_call_reservation_metadata(revision, 0),
            },
        },
    )
    db.add(call)
    await db.flush()
    await admit_inbound_twilio_knowledge_call(
        db,
        tenant_id=tenant.id,
        agent_id=agent.id,
        call_id=call.id,
    )
    await db.commit()

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    terminal_flushed = asyncio.Event()
    request_terminal_capacity_lock = asyncio.Event()
    terminal_capacity_lock_acquired = asyncio.Event()
    replay_requested_call_lock = asyncio.Event()
    loop = asyncio.get_running_loop()
    terminal_task: asyncio.Task | None = None
    replay_task: asyncio.Task | None = None
    listener_installed = False

    async def terminalize_while_holding_call_lock() -> None:
        async with session_factory() as session, session.begin():
            stored = await session.scalar(select(Call).where(Call.id == call.id).with_for_update())
            assert stored is not None
            stored.status = "completed"
            stored.ended_at = datetime.now(UTC)
            await session.flush()
            terminal_flushed.set()
            await request_terminal_capacity_lock.wait()
            # Terminal billing holds Call before taking the shared capacity
            # lock. A replay that held capacity while waiting for this Call
            # would deadlock here.
            await lock_agent_runtime_limits(
                session,
                tenant_id=tenant.id,
                agent_id=agent.id,
            )
            terminal_capacity_lock_acquired.set()

    async def replay_in_independent_transaction() -> Call | None:
        async with session_factory() as session, session.begin():
            stored_agent = await session.get(Agent, agent.id)
            stored_profile = await session.get(AgentRuntimeProfile, profile.id)
            assert stored_agent is not None
            assert stored_profile is not None
            return await webhooks._reserve_twilio_inbound_call(
                session,
                profile=stored_profile,
                agent=stored_agent,
                authenticated_route_credential=route_credential,
                call_sid=call_sid,
                from_number=from_number,
                to_number=to_number,
            )

    def observe_replay_call_lock(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        normalized = str(statement).upper()
        if "FROM CALLS" in normalized and "FOR UPDATE" in normalized:
            loop.call_soon_threadsafe(replay_requested_call_lock.set)

    try:
        terminal_task = asyncio.create_task(terminalize_while_holding_call_lock())
        await asyncio.wait_for(terminal_flushed.wait(), timeout=5)

        event.listen(engine.sync_engine, "before_cursor_execute", observe_replay_call_lock)
        listener_installed = True
        replay_task = asyncio.create_task(replay_in_independent_transaction())
        await asyncio.wait_for(replay_requested_call_lock.wait(), timeout=5)

        request_terminal_capacity_lock.set()
        await asyncio.wait_for(terminal_capacity_lock_acquired.wait(), timeout=5)
        await asyncio.wait_for(terminal_task, timeout=5)
        assert await asyncio.wait_for(replay_task, timeout=5) is None
    finally:
        request_terminal_capacity_lock.set()
        if listener_installed:
            event.remove(engine.sync_engine, "before_cursor_execute", observe_replay_call_lock)
        for task in (terminal_task, replay_task):
            if task is not None and not task.done():
                task.cancel()
        pending = [task for task in (terminal_task, replay_task) if task is not None]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async with session_factory() as verify_db:
        persisted = await verify_db.get(Call, call.id)
        assert persisted is not None
        assert persisted.status == "completed"
