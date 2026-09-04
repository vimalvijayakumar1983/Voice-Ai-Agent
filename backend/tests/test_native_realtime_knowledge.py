"""Immutable knowledge contracts for VAV's native realtime providers."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select

from app.core.config import settings
from app.models.agent import Agent, AgentKnowledgeBinding, AgentRuntimeProfile, KnowledgeBase
from app.models.call import Call
from app.realtime import session as realtime_session
from app.services.knowledge_serving import (
    INBOUND_KNOWLEDGE_ADMISSION_STATE,
    admit_inbound_twilio_knowledge_call,
    knowledge_admission_is_durable,
    knowledge_call_reservation_metadata,
    load_agent_serving_revision_identity,
)
from tests.conftest import engine
from tests.conftest import test_session_factory as session_factory
from tests.knowledge_test_utils import publish_test_knowledge


@pytest.mark.asyncio
async def test_native_media_capability_has_one_durable_session_owner(
    db,
    tenant,
    monkeypatch,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Single-owner receptionist",
        system_prompt="Answer only from approved knowledge.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
    )
    db.add(agent)
    await db.flush()
    db.add(
        AgentRuntimeProfile(
            tenant_id=tenant.id,
            agent_id=agent.id,
            enabled=True,
            status="active",
            telephony_provider="twilio",
            primary_speech_provider="sarvam",
            llm_provider="openai",
            llm_model="gpt-4o-mini",
            stt_language="en",
        )
    )
    _knowledge, revision = await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="Single owner",
    )
    call_sid = "CA-native-media-single-owner"
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="inbound",
        status="in_progress",
        from_number="+15550100001",
        to_number="+15550100002",
        provider="twilio",
        provider_call_sid=call_sid,
        started_at=datetime.now(UTC),
        answered_at=datetime.now(UTC),
        call_metadata={
            "runtime": {
                "transport": "twilio_media_streams",
                "speech_provider": "sarvam",
                **knowledge_call_reservation_metadata(revision, 0),
            }
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
    call_id = call.id

    monkeypatch.setattr(settings, "sarvam_api_key", "sarvam-test-key")
    monkeypatch.setattr(settings, "openai_api_key", "openai-test-key")
    monkeypatch.setattr(realtime_session, "async_session_factory", session_factory)

    results = await asyncio.gather(
        realtime_session.claim_runtime_media_session(
            call_id,
            stream_sid="MZ-owner-a",
            provider_call_sid=call_sid,
        ),
        realtime_session.claim_runtime_media_session(
            call_id,
            stream_sid="MZ-owner-b",
            provider_call_sid=call_sid,
        ),
        return_exceptions=True,
    )
    claims = [result for result in results if isinstance(result, UUID)]
    rejected = [
        result
        for result in results
        if isinstance(result, realtime_session.RuntimeMediaSessionAlreadyClaimedError)
    ]
    assert len(claims) == 1
    assert len(rejected) == 1

    winner = await realtime_session.load_runtime_session(
        call_id,
        media_session_claim_id=claims[0],
    )
    assert winner is not None
    assert winner.media_session_claim_id == claims[0]
    assert winner.media_stream_sid in {"MZ-owner-a", "MZ-owner-b"}
    assert (
        await realtime_session.load_runtime_session(
            call_id,
            media_session_claim_id=uuid4(),
        )
        is None
    )

    stale = replace(
        winner,
        media_session_claim_id=uuid4(),
        media_stream_sid="MZ-replayed-owner",
    )
    await asyncio.gather(
        realtime_session._store_metrics(stale, {"turn_count": 999}),
        realtime_session._store_metrics(winner, {"turn_count": 1, "llm_tokens": 3}),
    )
    db.expire_all()
    persisted = await db.get(Call, call_id)
    assert persisted is not None
    runtime = persisted.call_metadata["runtime"]
    assert persisted.status == "in_progress"
    assert runtime["media_session_claim"]["id"] == str(claims[0])
    assert runtime["turn_count"] == 1
    assert runtime["llm_tokens"] == 3


@pytest.mark.asyncio
async def test_native_admission_and_session_start_never_load_compiled_source_bodies(
    db,
    tenant,
    monkeypatch,
):
    """Large website bodies stay off the call-answer and greeting hot path."""

    agent = Agent(
        tenant_id=tenant.id,
        name="Large-site receptionist",
        system_prompt="Answer only from approved knowledge.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
    )
    db.add(agent)
    await db.flush()
    db.add(
        AgentRuntimeProfile(
            tenant_id=tenant.id,
            agent_id=agent.id,
            enabled=True,
            status="active",
            telephony_provider="twilio",
            primary_speech_provider="sarvam",
            llm_provider="openai",
            llm_model="gpt-4o-mini",
            stt_language="en",
        )
    )
    knowledge, revision = await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="Large site",
        content=(
            "Large Site Medical Centre telephone is +971 2 555 0100. "
            + ("approved website information " * 75_000)
        ),
    )
    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="inbound",
        status="in_progress",
        from_number="+15550100011",
        to_number="+15550100012",
        provider="twilio",
        provider_call_sid="CA-large-native-startup",
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
    await db.commit()

    monkeypatch.setattr(settings, "sarvam_api_key", "sarvam-test-key")
    monkeypatch.setattr(settings, "openai_api_key", "openai-test-key")
    monkeypatch.setattr(realtime_session, "async_session_factory", session_factory)

    statements: list[str] = []

    def capture_statement(
        _connection,
        _cursor,
        statement,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        statements.append(str(statement).casefold())

    event.listen(engine.sync_engine, "before_cursor_execute", capture_statement)
    try:
        async with session_factory() as admission_db:
            identity, generation = await load_agent_serving_revision_identity(
                admission_db,
                tenant_id=tenant.id,
                agent_id=agent.id,
                include_sources=False,
            )
            assert identity is not None
            assert identity.id == revision.id
            assert identity.knowledge_base_id == knowledge.id
            assert generation == 0
            await admit_inbound_twilio_knowledge_call(
                admission_db,
                tenant_id=tenant.id,
                agent_id=agent.id,
                call_id=call.id,
            )
            await admission_db.commit()
        config = await realtime_session.load_runtime_session(call.id)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", capture_statement)

    assert config is not None
    assert not any("knowledge_serving_revision_sources" in sql for sql in statements)
    lexicon_payload_queries = [
        sql for sql in statements if "knowledge_speech_lexicons.entries" in sql
    ]
    assert len(lexicon_payload_queries) == 1
    assert "knowledge_speech_lexicons.source_revisions" in lexicon_payload_queries[0]

    async with session_factory() as terminal_db:
        terminal_call = await terminal_db.get(Call, call.id)
        assert terminal_call is not None
        terminal_call.status = "terminal_unknown"
        terminal_call.ended_at = datetime.now(UTC)
        await terminal_db.commit()

    await realtime_session._finalize_inbound_call(config)
    async with session_factory() as terminal_db:
        terminal_call = await terminal_db.get(Call, call.id)
        assert terminal_call is not None
        assert terminal_call.status == "terminal_unknown"
    assert await realtime_session.load_runtime_session(call.id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("speech_provider", "voice_id"),
    [("sarvam", "sarvam:ishita"), ("elevenlabs", "elevenlabs:voice-1")],
)
async def test_native_session_keeps_exact_admitted_release_and_metrics_preserve_pin(
    client,
    auth_headers,
    db,
    tenant,
    monkeypatch,
    speech_provider,
    voice_id,
):
    """Rebinding an agent cannot move a running native call to newer knowledge."""

    agent = Agent(
        tenant_id=tenant.id,
        name="Pinned receptionist",
        system_prompt="Answer only from approved knowledge.",
        voice_provider=speech_provider,
        voice_id=voice_id,
        language="en",
        supported_languages=["en"],
    )
    db.add(agent)
    await db.flush()
    db.add(
        AgentRuntimeProfile(
            tenant_id=tenant.id,
            agent_id=agent.id,
            enabled=True,
            status="active",
            telephony_provider="twilio",
            primary_speech_provider=speech_provider,
            llm_provider="openai",
            llm_model="gpt-4o-mini",
            stt_language="en",
        )
    )
    knowledge_a, revision_a = await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="Alpha clinic",
    )

    call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="inbound",
        status="in_progress",
        from_number="+15550100001",
        to_number="+15550100002",
        provider="twilio",
        provider_call_sid=f"CA-{speech_provider}-pinned",
        started_at=datetime.now(UTC),
        answered_at=datetime.now(UTC),
        call_metadata={
            "runtime": {
                "transport": "twilio_media_streams",
                "speech_provider": speech_provider,
                **knowledge_call_reservation_metadata(revision_a, 0),
            }
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

    replacement_agent = Agent(
        tenant_id=tenant.id,
        name="Replacement knowledge owner",
        system_prompt="Test only.",
        voice_provider=speech_provider,
        voice_id=voice_id,
    )
    db.add(replacement_agent)
    await db.flush()
    knowledge_b, _revision_b = await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=replacement_agent,
        label="Beta clinic",
    )
    binding = await db.scalar(
        select(AgentKnowledgeBinding).where(AgentKnowledgeBinding.agent_id == agent.id)
    )
    assert binding is not None
    binding.knowledge_base_id = knowledge_b.id
    await db.commit()

    monkeypatch.setattr(settings, "sarvam_api_key", "sarvam-test-key")
    monkeypatch.setattr(settings, "elevenlabs_api_key", "elevenlabs-test-key")
    monkeypatch.setattr(settings, "openai_api_key", "openai-test-key")
    monkeypatch.setattr(realtime_session, "async_session_factory", session_factory)

    config = await realtime_session.load_runtime_session(call.id)
    assert config is not None
    assert config.knowledge_serving_revision_id == revision_a.id
    assert config.knowledge_serving_knowledge_base_id == knowledge_a.id
    assert config.knowledge_serving_revocation_generation == 0
    context = await realtime_session._retrieve_session_knowledge(
        config,
        "What is the telephone number?",
    )
    assert context is not None
    assert "Alpha clinic Medical Centre" in context
    assert "Beta clinic Medical Centre" not in context

    await realtime_session._store_metrics(
        config,
        {
            "turn_count": 2,
            "llm_tokens": 12,
            "turn_latency_p50_ms": 350,
        },
    )

    replacement = KnowledgeBase(
        tenant_id=tenant.id,
        name=f"Replacement {speech_provider} knowledge",
        provider=speech_provider,
        sync_status="ready",
        approval_status="approved",
        source_count=0,
        indexed_source_count=0,
        is_active=True,
    )
    db.add(replacement)
    await db.flush()
    rebound = await db.scalar(
        select(AgentKnowledgeBinding).where(AgentKnowledgeBinding.agent_id == agent.id)
    )
    assert rebound is not None
    rebound.knowledge_base_id = replacement.id
    await db.commit()

    blocked_delete = await client.delete(
        f"/api/v1/knowledge/{knowledge_a.id}",
        headers=auth_headers,
    )
    assert blocked_delete.status_code == 409
    assert "reserved by an active call" in blocked_delete.json()["detail"]

    async with session_factory() as verify_db:
        persisted = await verify_db.get(Call, call.id)
        assert persisted is not None
        runtime = persisted.call_metadata["runtime"]
        assert knowledge_admission_is_durable(persisted.call_metadata)
        assert runtime["knowledge_admission_state"] == INBOUND_KNOWLEDGE_ADMISSION_STATE
        assert runtime["knowledge_serving_revision_id"] == str(revision_a.id)
        assert runtime["knowledge_serving_knowledge_base_id"] == str(knowledge_a.id)
        assert runtime["knowledge_serving_content_sha256"] == revision_a.content_sha256
        assert runtime["knowledge_source_revision_sha256"] == revision_a.source_revision_sha256
        assert runtime["knowledge_serving_revocation_generation"] == 0
        assert runtime["speech_lexicon_artifact_id"] == str(revision_a.speech_lexicon_artifact_id)
        assert runtime["speech_lexicon_content_sha256"] == revision_a.entity_revision_sha256
        assert runtime["turn_count"] == 2

        # An integrity mismatch is never allowed to fall back to the agent's
        # current binding or latest publication.
        tampered_runtime = {
            **runtime,
            "knowledge_serving_content_sha256": "0" * 64,
        }
        persisted.call_metadata = {
            **persisted.call_metadata,
            "runtime": tampered_runtime,
        }
        await verify_db.commit()

    assert await realtime_session.load_runtime_session(call.id) is None
    assert await realtime_session.fail_inbound_runtime_start(
        call.id,
        reason="runtime_configuration_unavailable",
    )
    assert not await realtime_session.fail_inbound_runtime_start(
        call.id,
        reason="runtime_configuration_unavailable",
    )

    async with session_factory() as terminal_db:
        terminal_call = await terminal_db.get(Call, call.id)
        assert terminal_call is not None
        assert terminal_call.status == "failed"
        assert terminal_call.answered_at is not None
        assert terminal_call.ended_at is not None
        assert terminal_call.duration_seconds >= 1
        assert terminal_call.call_metadata["lifecycle_error"] == (
            "runtime_configuration_unavailable"
        )
        runtime = terminal_call.call_metadata["runtime"]
        assert runtime["runtime_start_failure"] == "runtime_configuration_unavailable"
        assert runtime["media_stream_started"] is True
        assert runtime["cost_state"] == "pending_provider_billing_sync"
        assert runtime["duration_source"] == "minimum_answered_runtime_start_failure"
        assert runtime["knowledge_serving_revision_id"] == str(revision_a.id)
