"""VAV realtime runtime control-plane and protocol tests."""

from datetime import UTC, datetime
from unittest.mock import ANY, AsyncMock
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.api.v1.endpoints.realtime import _twilio_start_token
from app.core.config import settings
from app.core.security import create_access_token, hash_password
from app.livekit_runtime import worker as livekit_worker
from app.models.agent import (
    Agent,
    AgentKnowledgeBinding,
    AgentRuntimeProfile,
    KnowledgeBase,
    KnowledgeSource,
)
from app.models.audit import AuditEvent
from app.models.call import Call
from app.models.tenant import Tenant
from app.models.user import User
from app.providers.inworld import (
    InworldClient,
    InworldError,
    inworld_realtime_websocket_url,
)
from app.providers.openai import OpenAIProviderClient
from app.realtime.auth import create_media_token, verify_media_token
from app.realtime.sarvam_stream import is_speech_start, parse_transcript_event
from app.schemas.runtime import RuntimeProfileUpdate
from app.services.knowledge_retrieval import rank_knowledge
from app.services.knowledge_serving import publish_serving_revision
from app.services.provider_credentials import ProviderCredentialError
from app.services.speech_lexicon import publish_speech_lexicon
from app.telephony.livekit_provider import LiveKitSIPError, LiveKitSIPProvider, LiveKitSIPResult


def test_media_token_is_call_scoped_and_expires():
    from uuid import uuid4

    call_id = uuid4()
    token = create_media_token(call_id, now=1000)

    assert verify_media_token(token, call_id, now=1001)
    assert not verify_media_token(token, uuid4(), now=1001)
    assert not verify_media_token(token, call_id, now=2000)
    assert not verify_media_token(token + "tampered", call_id, now=1001)


def test_runtime_model_identifier_is_normalized_and_limited_to_production_routes():
    assert RuntimeProfileUpdate(llm_model="  gpt-4o-mini  ").llm_model == "gpt-4o-mini"
    assert (
        RuntimeProfileUpdate(llm_provider="inworld", llm_model="  openai/gpt-4o-mini  ").llm_model
        == "openai/gpt-4o-mini"
    )
    with pytest.raises(ValidationError, match="OpenAI LLM routes support only"):
        RuntimeProfileUpdate(llm_provider="openai", llm_model="openai/gpt-4o-mini")
    with pytest.raises(ValidationError, match="Inworld LLM routes support only"):
        RuntimeProfileUpdate(llm_provider="inworld", llm_model="customer-support-preview")
    native = RuntimeProfileUpdate(
        voice_runtime="inworld_realtime",
        llm_provider="inworld",
        llm_model="openai/gpt-4o-mini",
    )
    assert native.voice_runtime == "inworld_realtime"
    with pytest.raises(ValidationError, match="Native Inworld Realtime requires"):
        RuntimeProfileUpdate(voice_runtime="inworld_realtime")


def test_single_pass_policy_is_typed_native_only_and_defaults_to_control():
    assert RuntimeProfileUpdate().knowledge_turn_mode == "tool_loop"
    native = RuntimeProfileUpdate(
        voice_runtime="inworld_realtime",
        knowledge_turn_mode="single_pass_experimental",
        llm_provider="inworld",
        llm_model="openai/gpt-4o-mini",
    )
    assert native.knowledge_turn_mode == "single_pass_experimental"
    with pytest.raises(ValidationError, match="requires Native Inworld Realtime"):
        RuntimeProfileUpdate(knowledge_turn_mode="single_pass_experimental")
    with pytest.raises(ValidationError):
        RuntimeProfileUpdate(knowledge_turn_mode="single_pass")


def test_diagnostic_recording_defaults_off_and_requires_every_runtime_prerequisite():
    from app.api.v1.endpoints.runtime import _diagnostic_recording_readiness

    assert RuntimeProfileUpdate().diagnostic_recording_mode == "off"
    assert _diagnostic_recording_readiness(None) == ({}, {})

    profile = AgentRuntimeProfile(
        telephony_provider="livekit_sip",
        runtime_config={
            "diagnostic_recording_mode": "livekit_egress_explicit_consent",
        },
    )
    checks, labels = _diagnostic_recording_readiness(profile)

    assert checks == {
        "diagnostic_recording_livekit_transport": True,
        "diagnostic_recording_explicit_consent_enforced": False,
        "diagnostic_recording_egress_configured": False,
        "diagnostic_recording_storage_configured": False,
        "diagnostic_recording_retention_enforced": False,
    }
    assert set(labels) == set(checks)
    assert (
        "absence of consent is not permission"
        in labels["diagnostic_recording_explicit_consent_enforced"]
    )
    assert "does not provide VAV playback" in labels["diagnostic_recording_storage_configured"]
    with pytest.raises(ValidationError):
        RuntimeProfileUpdate(diagnostic_recording_mode="always_record")


def test_inworld_realtime_websocket_url_uses_provider_protocol():
    assert inworld_realtime_websocket_url(
        "https://api.inworld.ai/v1",
        session_id="vav-test-session",
    ) == ("wss://api.inworld.ai/api/v1/realtime/session?key=vav-test-session&protocol=realtime")


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["test", "activate"])
async def test_billable_live_readiness_is_rate_limited_per_tenant_user(
    action,
    client,
    auth_headers,
    tenant,
    user,
    monkeypatch,
):
    from app.api.v1.endpoints import runtime as runtime_endpoint

    enforce = AsyncMock()
    monkeypatch.setattr(runtime_endpoint, "enforce_rate_limit", enforce)

    response = await client.post(f"/api/v1/runtime/agents/{uuid4()}/{action}", headers=auth_headers)

    assert response.status_code == 404
    enforce.assert_awaited_once_with(
        ANY,
        scope="runtime-live-readiness",
        limit=5,
        window_seconds=60,
        subject=f"{tenant.id}:{user.id}",
        bind_to_client=False,
        limit_detail="Too many live readiness tests. Wait one minute and try again.",
        unavailable_detail="Live readiness testing is temporarily unavailable.",
    )


def test_twilio_media_token_is_read_from_start_custom_parameters():
    assert (
        _twilio_start_token(
            {
                "event": "start",
                "start": {"customParameters": {"token": "scoped-media-capability"}},
            }
        )
        == "scoped-media-capability"
    )
    assert _twilio_start_token({"event": "connected"}) is None


def test_sarvam_events_support_documented_and_nested_transcripts():
    final = parse_transcript_event(
        {
            "event": "transcript.final",
            "data": {"transcript": "I need a dermatologist", "language_code": "en-IN"},
        }
    )
    partial = parse_transcript_event(
        {"type": "transcript.partial", "data": {"transcript": {"text": "I need"}}}
    )

    assert final and final.is_final and final.language_code == "en-IN"
    assert partial and not partial.is_final and partial.text == "I need"
    assert is_speech_start({"event": "vad.speech_start"})


def test_provider_neutral_knowledge_ranking_prefers_query_coverage():
    matches = rank_knowledge(
        "doctor hair specialist",
        [
            ("PRP", "PRP may support hair rejuvenation."),
            ("Doctors", "Dr Rao is a doctor and hair specialist at the clinic."),
        ],
    )

    assert matches[0].source == "Doctors"
    assert "Dr Rao" in matches[0].text


def test_knowledge_ranking_routes_directory_questions_by_source_name():
    matches = rank_knowledge(
        "Which doctors are available?",
        [
            ("PRP_Treatment.pdf", "Appointments are available at the clinic."),
            (
                "Adam_and_Eve_Doctors_Directory.pdf",
                "Dr Rao — Dermatology. Dr Khan — Plastic Surgery.",
            ),
        ],
    )

    assert matches[0].source == "Adam_and_Eve_Doctors_Directory.pdf"
    assert "Dr Rao" in matches[0].text


@pytest.mark.asyncio
async def test_runtime_profile_requires_readiness_before_activation(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Royal concierge",
        system_prompt="Answer patients using approved clinic knowledge.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en", "hi"],
        language_switching_enabled=True,
        language_switching_mode="automatic",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Approved clinic knowledge",
        approval_status="approved",
        is_active=True,
        content="The clinic is open from nine to six.",
    )
    db.add_all([agent, knowledge])
    await db.flush()
    db.add_all(
        [
            KnowledgeSource(
                tenant_id=tenant.id,
                knowledge_base_id=knowledge.id,
                name="clinic-hours.txt",
                source_type="text",
                status="indexed",
                content="The clinic is open from nine to six.",
            ),
            AgentKnowledgeBinding(
                tenant_id=tenant.id,
                agent_id=agent.id,
                knowledge_base_id=knowledge.id,
            ),
        ]
    )
    await db.commit()

    configured = await client.put(
        f"/api/v1/runtime/agents/{agent.id}",
        headers=auth_headers,
        json={
            "assigned_numbers": ["+971501234567"],
            "telephony_provider": "twilio",
            "primary_speech_provider": "sarvam",
            "fallback_speech_provider": None,
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "stt_language": "auto",
            "max_concurrent_calls": 2,
            "daily_call_limit": 50,
            "monthly_budget_cents": 10000,
        },
    )
    activated = await client.post(
        f"/api/v1/runtime/agents/{agent.id}/activate",
        headers=auth_headers,
    )

    assert configured.status_code == 200
    assert not configured.json()["ready"]
    assert activated.status_code == 409
    assert "blockers" in activated.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("voice_provider", "voice_id", "telephony_provider", "speech_provider", "llm_provider"),
    [
        ("inworld", "inworld:Ashley", "twilio", "inworld", "openai"),
        ("sarvam", "sarvam:ishita", "livekit_sip", "sarvam", "inworld"),
    ],
)
async def test_runtime_profile_rejects_unsupported_provider_matrix(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
    voice_provider,
    voice_id,
    telephony_provider,
    speech_provider,
    llm_provider,
):
    agent = Agent(
        tenant_id=tenant.id,
        name=f"Cross-wired {voice_provider}",
        system_prompt="This route must never be activated.",
        voice_provider=voice_provider,
        voice_id=voice_id,
    )
    db.add(agent)
    await db.commit()

    response = await client.put(
        f"/api/v1/runtime/agents/{agent.id}",
        headers=auth_headers,
        json={
            "assigned_numbers": ["+971501234567"],
            "telephony_provider": telephony_provider,
            "primary_speech_provider": speech_provider,
            "fallback_speech_provider": None,
            "llm_provider": llm_provider,
            "llm_model": "auto" if llm_provider == "inworld" else "gpt-4o-mini",
            "stt_language": "auto",
            "max_concurrent_calls": 1,
            "daily_call_limit": 50,
            "monthly_budget_cents": 10000,
        },
    )

    assert response.status_code == 422
    assert "require" in response.json()["detail"].lower()


@pytest.mark.asyncio
async def test_diagnostic_recording_policy_is_serialized_audited_and_blocks_activation(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Governed diagnostic concierge",
        system_prompt="Use approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
        supported_languages=["en-GB"],
    )
    db.add(agent)
    await db.commit()
    payload = {
        "assigned_numbers": ["+971501234567"],
        "telephony_provider": "livekit_sip",
        "primary_speech_provider": "inworld",
        "fallback_speech_provider": None,
        "llm_provider": "inworld",
        "llm_model": "openai/gpt-4o-mini",
        "voice_runtime": "inworld_realtime",
        "stt_language": "en-GB",
        "stt_model": "assemblyai/u3-rt-pro",
        "tts_delivery_mode": "balanced",
        "diagnostic_recording_mode": "livekit_egress_explicit_consent",
        "max_concurrent_calls": 1,
        "daily_call_limit": 50,
        "monthly_budget_cents": 10000,
    }

    configured = await client.put(
        f"/api/v1/runtime/agents/{agent.id}", headers=auth_headers, json=payload
    )
    activation = await client.post(
        f"/api/v1/runtime/agents/{agent.id}/activate", headers=auth_headers
    )

    assert configured.status_code == 200
    assert configured.json()["diagnostic_recording_mode"] == ("livekit_egress_explicit_consent")
    assert configured.json()["ready"] is False
    assert any("explicit recording consent" in item for item in configured.json()["blockers"])
    assert any("LiveKit Egress" in item for item in configured.json()["blockers"])
    assert any("does not provide VAV playback" in item for item in configured.json()["blockers"])
    assert activation.status_code == 409

    profile = await db.scalar(
        select(AgentRuntimeProfile).where(AgentRuntimeProfile.agent_id == agent.id)
    )
    assert profile is not None
    assert profile.runtime_config["diagnostic_recording_mode"] == (
        "livekit_egress_explicit_consent"
    )
    configured_audit = await db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.action == "agent.runtime_configured",
            AuditEvent.resource_id == str(agent.id),
        )
        .order_by(AuditEvent.created_at.desc())
    )
    assert configured_audit is not None
    assert configured_audit.details["diagnostic_recording_mode"] == (
        "livekit_egress_explicit_consent"
    )
    assert configured_audit.details["diagnostic_recording_opted_in"] is True
    assert "system_prompt" not in configured_audit.details


@pytest.mark.asyncio
async def test_single_pass_policy_persists_strict_boolean_and_audit_mode(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
):
    agent = Agent(
        tenant_id=tenant.id,
        name="Single-pass canary concierge",
        system_prompt="Use approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
        supported_languages=["en-GB"],
    )
    db.add(agent)
    await db.commit()

    response = await client.put(
        f"/api/v1/runtime/agents/{agent.id}",
        headers=auth_headers,
        json={
            "assigned_numbers": ["+971501234567"],
            "telephony_provider": "livekit_sip",
            "primary_speech_provider": "inworld",
            "fallback_speech_provider": None,
            "llm_provider": "inworld",
            "llm_model": "openai/gpt-4o-mini",
            "voice_runtime": "inworld_realtime",
            "knowledge_turn_mode": "single_pass_experimental",
            "stt_language": "en-GB",
            "stt_model": "assemblyai/u3-rt-pro",
            "max_concurrent_calls": 1,
            "daily_call_limit": 50,
            "monthly_budget_cents": 10000,
        },
    )

    assert response.status_code == 200
    assert response.json()["knowledge_turn_mode"] == "single_pass_experimental"
    profile = await db.scalar(
        select(AgentRuntimeProfile).where(AgentRuntimeProfile.agent_id == agent.id)
    )
    assert profile is not None
    assert profile.runtime_config["inworld_single_pass"] is True
    configured_audit = await db.scalar(
        select(AuditEvent)
        .where(
            AuditEvent.action == "agent.runtime_configured",
            AuditEvent.resource_id == str(agent.id),
        )
        .order_by(AuditEvent.created_at.desc())
    )
    assert configured_audit is not None
    assert configured_audit.details["knowledge_turn_mode"] == ("single_pass_experimental")
    assert configured_audit.details["inworld_single_pass"] is True


@pytest.mark.asyncio
async def test_sip_credential_endpoint_rejects_unused_carrier_secrets(
    client: AsyncClient,
    auth_headers,
):
    response = await client.put(
        "/api/v1/runtime/sip/credential",
        headers=auth_headers,
        json={
            "sip_uri": "sip:trunk.example.ae",
            "username": "must-not-be-ingested",
            "password": "must-not-be-stored",
            "inbound_trunk_id": "ST_inbound",
            "dispatch_rule_id": "SDR_vav",
            "outbound_trunk_id": "ST_outbound",
            "agent_name": "vav-inworld",
        },
    )

    assert response.status_code == 422
    rejected_fields = {error["loc"][-1] for error in response.json()["detail"]}
    assert rejected_fields >= {"username", "password"}


@pytest.mark.asyncio
async def test_livekit_inworld_runtime_requires_explicit_route_ids_and_can_activate(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "base_url", "https://api.example.com")
    monkeypatch.setattr(settings, "integration_encryption_key", "i" * 40)
    monkeypatch.setattr(settings, "livekit_url", "wss://example.livekit.cloud")
    monkeypatch.setattr(settings, "livekit_api_key", "livekit-key")
    monkeypatch.setattr(settings, "livekit_api_secret", "livekit-secret-long-enough")
    monkeypatch.setattr(
        settings,
        "livekit_worker_health_url",
        "http://livekit-agent.internal:8080",
    )

    async def validate(_self):
        return None

    tts_probes: list[dict[str, object]] = []
    router_probes: list[dict[str, object]] = []

    async def synthesize_probe(_self, **kwargs):
        tts_probes.append(kwargs)

    async def router_probe(_self, **kwargs):
        router_probes.append(kwargs)

    async def verify_route(_self, **_kwargs):
        return None

    async def verify_worker(_self, **_kwargs):
        return None

    async def make_call(_self, **kwargs):
        # LiveKit dispatch starts the worker before this provider request returns.
        # Persist representative worker metadata from a separate transaction to
        # prove the late API response merges instead of replacing that state.
        async with async_sessionmaker(db.bind, expire_on_commit=False)() as worker_db:
            worker_call = await worker_db.get(Call, kwargs["call_id"])
            assert worker_call is not None
            worker_call.call_metadata = {
                **(worker_call.call_metadata or {}),
                "channel": "phone",
                "conversation_type": "telephonyOutbound",
                "runtime": {
                    **((worker_call.call_metadata or {}).get("runtime") or {}),
                    "turn_count": 1,
                },
            }
            worker_call.status = "in_progress"
            await worker_db.commit()
        return LiveKitSIPResult(
            provider_call_sid="livekit-sip-call-1",
            room_name=f"vav-call-{kwargs['call_id']}",
        )

    monkeypatch.setattr(InworldClient, "validate_connection", validate)
    monkeypatch.setattr(InworldClient, "synthesize_readiness_probe", synthesize_probe)
    monkeypatch.setattr(InworldClient, "router_readiness_probe", router_probe)
    monkeypatch.setattr(LiveKitSIPProvider, "verify_route", verify_route)
    monkeypatch.setattr(LiveKitSIPProvider, "verify_worker", verify_worker)
    monkeypatch.setattr(LiveKitSIPProvider, "make_call", make_call)
    from app.tasks import call_tasks

    monkeypatch.setattr(call_tasks.reconcile_call_dispatch, "apply_async", lambda **_kwargs: None)
    monkeypatch.setattr(
        call_tasks.reconcile_direct_call_terminal, "apply_async", lambda **_kwargs: None
    )
    agent = Agent(
        tenant_id=tenant.id,
        name="Inworld concierge",
        system_prompt="Answer using approved clinic knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
        supported_languages=["en-GB"],
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Approved Inworld knowledge",
        approval_status="approved",
        is_active=True,
        content="The clinic is open from nine to six.",
        sync_status="ready",
        source_count=1,
        indexed_source_count=1,
    )
    db.add_all([agent, knowledge])
    await db.flush()
    db.add_all(
        [
            KnowledgeSource(
                tenant_id=tenant.id,
                knowledge_base_id=knowledge.id,
                name="clinic-hours.txt",
                source_type="text",
                status="indexed",
                content="The clinic is open from nine to six.",
            ),
            AgentKnowledgeBinding(
                tenant_id=tenant.id,
                agent_id=agent.id,
                knowledge_base_id=knowledge.id,
            ),
        ]
    )
    await db.commit()

    saved_key = await client.put(
        "/api/v1/runtime/credentials/inworld",
        headers=auth_headers,
        json={"api_key": "inworld-workspace-key-123456789"},
    )
    saved_sip = await client.put(
        "/api/v1/runtime/sip/credential",
        headers=auth_headers,
        json={
            "sip_uri": "sip:trunk.example.ae",
            "inbound_trunk_id": "ST_inbound",
            "dispatch_rule_id": "SDR_vav",
            "outbound_trunk_id": "ST_outbound",
            "agent_name": "vav-inworld",
        },
    )
    other_tenant = Tenant(name="Other workspace", slug="other-workspace")
    db.add(other_tenant)
    await db.flush()
    other_user = User(
        tenant_id=other_tenant.id,
        email="other@example.com",
        hashed_password=hash_password("testpassword"),
        full_name="Other Owner",
        role="owner",
    )
    db.add(other_user)
    await db.commit()
    duplicate_route = await client.put(
        "/api/v1/runtime/sip/credential",
        headers={
            "Authorization": (
                f"Bearer {create_access_token(other_user.id, other_tenant.id, other_user.role)}"
            )
        },
        json={
            "sip_uri": "sip:other-trunk.example.ae",
            "inbound_trunk_id": "ST_inbound",
            "dispatch_rule_id": "SDR_other",
            "outbound_trunk_id": "ST_other_outbound",
            "agent_name": "vav-inworld",
        },
    )
    configured = await client.put(
        f"/api/v1/runtime/agents/{agent.id}",
        headers=auth_headers,
        json={
            "assigned_numbers": ["+97141234567"],
            "telephony_provider": "livekit_sip",
            "primary_speech_provider": "inworld",
            "fallback_speech_provider": "sarvam",
            "llm_provider": "inworld",
            "llm_model": "openai/gpt-4o-mini",
            "stt_language": "en-GB",
            "tts_delivery_mode": "creative",
            "max_concurrent_calls": 5,
            "daily_call_limit": 500,
            "monthly_budget_cents": 50000,
        },
    )
    speech_lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
    )
    serving_revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=knowledge,
        speech_lexicon=speech_lexicon,
    )
    await db.commit()
    legacy_update = await client.put(
        f"/api/v1/runtime/agents/{agent.id}",
        headers=auth_headers,
        json={
            "assigned_numbers": ["+97141234567"],
            "telephony_provider": "livekit_sip",
            "primary_speech_provider": "inworld",
            "fallback_speech_provider": "sarvam",
            "llm_provider": "inworld",
            "llm_model": "openai/gpt-4o-mini",
            "stt_language": "en-GB",
            "max_concurrent_calls": 5,
            "daily_call_limit": 500,
            "monthly_budget_cents": 50000,
        },
    )
    tested = await client.post(f"/api/v1/runtime/agents/{agent.id}/test", headers=auth_headers)
    activated = await client.post(
        f"/api/v1/runtime/agents/{agent.id}/activate", headers=auth_headers
    )
    monkeypatch.setattr(
        livekit_worker,
        "async_session_factory",
        async_sessionmaker(db.bind, expire_on_commit=False),
    )
    (
        inbound_model,
        inbound_profile,
        inbound_key,
        inbound_knowledge_pin,
    ) = await livekit_worker._resolve_inbound_runtime(
        inbound_trunk_id="ST_inbound",
        called_number="+97141234567",
    )
    assert inbound_knowledge_pin.revision_id == serving_revision.id
    assert inbound_knowledge_pin.revocation_generation == 0
    outbound = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "livekit-outbound-test-0001"},
        json={
            "agent_id": str(agent.id),
            "to_number": "+971501234567",
            "context": {"purpose": "appointment reminder"},
        },
    )

    async def no_answer(_self, **_kwargs):
        raise LiveKitSIPError(
            "callee unavailable",
            ambiguous=False,
            terminal_status="no_answer",
        )

    monkeypatch.setattr(LiveKitSIPProvider, "make_call", no_answer)
    unanswered = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "livekit-outbound-no-answer-0001"},
        json={
            "agent_id": str(agent.id),
            "to_number": "+971501234568",
            "context": {"purpose": "appointment reminder"},
        },
    )
    await db.refresh(knowledge)
    knowledge.serving_revocation_generation += 1
    knowledge.serving_revision_id = None
    knowledge.speech_lexicon_artifact_id = None
    knowledge.approval_status = "draft"
    await db.commit()
    with pytest.raises(RuntimeError, match="immutable published knowledge revision"):
        await livekit_worker._resolve_inbound_runtime(
            inbound_trunk_id="ST_inbound",
            called_number="+97141234567",
        )
    provider_call_after_revoke = AsyncMock(
        return_value=LiveKitSIPResult(
            provider_call_sid="must-not-dispatch",
            room_name="must-not-exist",
        )
    )
    monkeypatch.setattr(LiveKitSIPProvider, "make_call", provider_call_after_revoke)
    rejected_after_revoke = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "livekit-outbound-revoked-0001"},
        json={
            "agent_id": str(agent.id),
            "to_number": "+971501234569",
            "context": {"purpose": "appointment reminder"},
        },
    )

    assert saved_key.status_code == 200
    assert saved_sip.status_code == 200
    assert saved_sip.json()["route_recorded"] is True
    assert saved_sip.json()["gateway_provisioned"] is False
    assert duplicate_route.status_code == 409
    assert configured.json()["ready"] is False
    assert any("immutable serving revision" in blocker for blocker in configured.json()["blockers"])
    assert configured.json()["tts_delivery_mode"] == "creative"
    assert legacy_update.status_code == 200
    assert legacy_update.json()["ready"] is True
    assert legacy_update.json()["tts_delivery_mode"] == "creative"
    assert tested.json()["ready"] is True
    assert tested.json()["checks"]["tts_provider_live"] is True
    assert tested.json()["checks"]["llm_provider_live"] is True
    assert activated.json()["enabled"] is True
    assert tts_probes == [
        {
            "voice_id": "Ashley",
            "model_id": "inworld-tts-2",
        },
        {
            "voice_id": "Ashley",
            "model_id": "inworld-tts-2",
        },
    ]
    assert router_probes == [
        {"model_id": "openai/gpt-4o-mini"},
        {"model_id": "openai/gpt-4o-mini"},
    ]
    assert inbound_model.id == agent.id
    assert inbound_profile.agent_id == agent.id
    assert inbound_profile.runtime_config == {
        "tts_delivery_mode": "creative",
        "inworld_single_pass": False,
    }
    assert inbound_key.speech == "inworld-workspace-key-123456789"
    assert inbound_key.llm == "inworld-workspace-key-123456789"
    assert serving_revision.id is not None
    assert outbound.status_code == 201
    assert outbound.json()["provider"] == "livekit_sip"
    assert outbound.json()["provider_call_sid"] == "livekit-sip-call-1"
    assert outbound.json()["status"] == "in_progress"
    assert outbound.json()["call_metadata"]["channel"] == "phone"
    assert outbound.json()["call_metadata"]["conversation_type"] == "telephonyOutbound"
    assert outbound.json()["call_metadata"]["runtime"]["turn_count"] == 1
    db.expire_all()
    outbound_call = await db.get(Call, UUID(outbound.json()["id"]))
    assert outbound_call.call_metadata["livekit_room"] == f"vav-call-{outbound.json()['id']}"
    assert outbound_call.call_metadata["runtime"]["turn_count"] == 1
    assert unanswered.status_code == 201
    assert unanswered.json()["status"] == "no_answer"
    assert rejected_after_revoke.status_code == 409
    assert "Approve and publish" in rejected_after_revoke.json()["detail"]
    provider_call_after_revoke.assert_not_awaited()
    assert (
        await db.scalar(
            select(func.count()).select_from(Call).where(Call.to_number == "+971501234569")
        )
        == 0
    )


@pytest.mark.asyncio
async def test_inworld_readiness_reports_tts_and_router_failures_independently(
    tenant,
    db,
    monkeypatch,
):
    from app.api.v1.endpoints import runtime as runtime_endpoint

    agent = Agent(
        tenant_id=tenant.id,
        name="Probe failure concierge",
        system_prompt="Use approved knowledge for every answer.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="ar",
        supported_languages=["ar"],
    )
    db.add(agent)
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        telephony_provider="twilio",
        primary_speech_provider="inworld",
        llm_provider="inworld",
        llm_model="inworld/customer-support",
    )

    async def static_readiness(*_args):
        return [], {
            "provider_compatibility": True,
            "tts_credential": True,
            "voice_selection": True,
            "llm_credential": True,
        }

    async def provider_config(*_args):
        return {"api_key": "inworld-workspace-key-123456789"}

    async def tts_failure(_self, **kwargs):
        assert kwargs == {
            "voice_id": "Ashley",
            "model_id": "inworld-tts-2",
        }
        raise InworldError("selected voice cannot synthesize the probe", status_code=422)

    async def router_failure(_self, **kwargs):
        assert kwargs == {"model_id": "inworld/customer-support"}
        raise InworldError("selected Router model was not found", status_code=404)

    monkeypatch.setattr(runtime_endpoint, "runtime_readiness", static_readiness)
    monkeypatch.setattr(runtime_endpoint, "load_provider_config", provider_config)
    monkeypatch.setattr(InworldClient, "synthesize_readiness_probe", tts_failure)
    monkeypatch.setattr(InworldClient, "router_readiness_probe", router_failure)

    blockers, checks = await runtime_endpoint.live_runtime_readiness(db, agent, profile)

    assert checks["tts_provider_live"] is False
    assert checks["llm_provider_live"] is False
    assert blockers == [
        "Inworld live TTS synthesis failed: selected voice cannot synthesize the probe",
        "Inworld Router tool-calling check failed: selected Router model was not found",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("single_pass", [False, True])
async def test_native_inworld_readiness_probes_one_complete_realtime_route(
    tenant,
    db,
    monkeypatch,
    single_pass,
):
    from app.api.v1.endpoints import runtime as runtime_endpoint

    agent = Agent(
        tenant_id=tenant.id,
        name="Native realtime concierge",
        system_prompt="Use approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
        supported_languages=["en-GB"],
    )
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        # Isolate the provider probe here; LiveKit SIP has separate route tests.
        telephony_provider="twilio",
        primary_speech_provider="inworld",
        llm_provider="inworld",
        llm_model="openai/gpt-4o-mini",
        stt_language="en-GB",
        runtime_config={
            "voice_runtime": "inworld_realtime",
            "stt_model": "auto",
            "inworld_single_pass": single_pass,
        },
    )
    probes = []

    async def static_readiness(*_args):
        return [], {
            "provider_compatibility": True,
            "tts_credential": True,
            "voice_selection": True,
            "llm_credential": True,
        }

    async def provider_config(*_args):
        return {"api_key": "inworld-workspace-key-123456789"}

    async def realtime_probe(_self, **kwargs):
        probes.append(kwargs)

    tts_probe = AsyncMock()
    router_probe = AsyncMock()
    monkeypatch.setattr(runtime_endpoint, "runtime_readiness", static_readiness)
    monkeypatch.setattr(runtime_endpoint, "load_provider_config", provider_config)
    monkeypatch.setattr(InworldClient, "realtime_readiness_probe", realtime_probe)
    monkeypatch.setattr(InworldClient, "synthesize_readiness_probe", tts_probe)
    monkeypatch.setattr(InworldClient, "router_readiness_probe", router_probe)

    blockers, checks = await runtime_endpoint.live_runtime_readiness(db, agent, profile)

    assert blockers == []
    assert checks["realtime_provider_live"] is True
    assert checks["tts_provider_live"] is True
    assert checks["llm_provider_live"] is True
    if single_pass:
        assert checks["knowledge_single_pass_provider_live"] is True
    else:
        assert "knowledge_single_pass_provider_live" not in checks
    expected_probe = {
        "model_id": "openai/gpt-4o-mini",
        "voice_id": "Ashley",
        "stt_model_id": "assemblyai/u3-rt-pro",
        "stt_language": "en-GB",
    }
    if single_pass:
        expected_probe["single_pass"] = True
    assert probes == [expected_probe]
    tts_probe.assert_not_awaited()
    router_probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_native_readiness_uses_the_production_multilingual_auto_stt_payload(
    tenant,
    db,
    monkeypatch,
):
    from app.api.v1.endpoints import runtime as runtime_endpoint
    from app.livekit_runtime import worker as livekit_worker

    agent = Agent(
        tenant_id=tenant.id,
        name="Multilingual native concierge",
        system_prompt="Use approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
        language="en-GB",
        supported_languages=["en-GB", "ar-AE", "hi-IN"],
        language_switching_enabled=True,
    )
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        telephony_provider="twilio",
        primary_speech_provider="inworld",
        llm_provider="inworld",
        llm_model="openai/gpt-4o-mini",
        stt_language="auto",
        runtime_config={
            "voice_runtime": "inworld_realtime",
            "stt_model": "auto",
            "inworld_single_pass": False,
        },
    )
    probes = []

    async def static_readiness(*_args):
        return [], {
            "provider_compatibility": True,
            "tts_credential": True,
            "voice_selection": True,
            "llm_credential": True,
        }

    async def provider_config(*_args):
        return {"api_key": "inworld-workspace-key-123456789"}

    async def realtime_probe(_self, **kwargs):
        probes.append(kwargs)

    monkeypatch.setattr(runtime_endpoint, "runtime_readiness", static_readiness)
    monkeypatch.setattr(runtime_endpoint, "load_provider_config", provider_config)
    monkeypatch.setattr(InworldClient, "realtime_readiness_probe", realtime_probe)

    blockers, checks = await runtime_endpoint.live_runtime_readiness(db, agent, profile)
    production = livekit_worker._build_inworld_realtime_model(
        model=agent,
        profile=profile,
        api_key="inworld-workspace-key-123456789",
    )
    transcription = production._opts.input_audio_transcription

    assert blockers == []
    assert checks["realtime_provider_live"] is True
    assert probes == [
        {
            "model_id": profile.llm_model,
            "voice_id": "Ashley",
            "stt_model_id": transcription.model,
            "stt_language": transcription.language,
        }
    ]
    assert transcription.model == "soniox/stt-rt-v4"
    assert transcription.language is None


@pytest.mark.asyncio
async def test_inworld_speech_openai_route_runs_live_tool_capability_probe(
    tenant,
    db,
    monkeypatch,
):
    from app.api.v1.endpoints import runtime as runtime_endpoint

    agent = Agent(
        tenant_id=tenant.id,
        name="Hybrid concierge",
        system_prompt="Use the knowledge tool.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
    )
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        telephony_provider="twilio",
        primary_speech_provider="inworld",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
    )
    probed_models = []

    async def static_readiness(*_args):
        return [], {
            "provider_compatibility": True,
            "tts_credential": True,
            "voice_selection": True,
            "llm_credential": True,
        }

    async def provider_config(_db, _tenant_id, provider):
        return {"api_key": f"tenant-{provider}-key"}

    async def tool_probe(_self, *, model_id):
        probed_models.append(model_id)

    monkeypatch.setattr(runtime_endpoint, "runtime_readiness", static_readiness)
    monkeypatch.setattr(runtime_endpoint, "load_provider_config", provider_config)
    monkeypatch.setattr(InworldClient, "synthesize_readiness_probe", AsyncMock())
    monkeypatch.setattr(OpenAIProviderClient, "tool_readiness_probe", tool_probe)

    blockers, checks = await runtime_endpoint.live_runtime_readiness(db, agent, profile)

    assert blockers == []
    assert checks["tts_provider_live"] is True
    assert checks["llm_provider_live"] is True
    assert probed_models == ["gpt-4o-mini"]


@pytest.mark.asyncio
async def test_readiness_never_falls_back_when_workspace_inworld_key_is_unreadable(
    tenant,
    db,
    monkeypatch,
):
    from app.api.v1.endpoints import runtime as runtime_endpoint

    agent = Agent(
        tenant_id=tenant.id,
        name="Fail-closed Inworld concierge",
        system_prompt="Use approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
    )
    db.add(agent)
    await db.flush()
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        telephony_provider="livekit_sip",
        primary_speech_provider="inworld",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
    )
    monkeypatch.setattr(settings, "inworld_api_key", "platform-inworld-key")
    monkeypatch.setattr(settings, "openai_api_key", "platform-openai-key")

    async def provider_config(_db, _tenant_id, provider):
        if provider == "inworld":
            raise ProviderCredentialError("cannot decrypt")
        return None

    monkeypatch.setattr(runtime_endpoint, "load_provider_config", provider_config)
    _blockers, checks = await runtime_endpoint.runtime_readiness(db, agent, profile)

    assert checks["stt_credential"] is False
    assert checks["tts_credential"] is False
    assert checks["llm_credential"] is True


@pytest.mark.asyncio
async def test_live_readiness_never_falls_back_when_workspace_inworld_key_is_unreadable(
    tenant,
    db,
    monkeypatch,
):
    from app.api.v1.endpoints import runtime as runtime_endpoint

    agent = Agent(
        tenant_id=tenant.id,
        name="Fail-closed live Inworld concierge",
        system_prompt="Use approved knowledge.",
        voice_provider="inworld",
        voice_id="inworld:Ashley",
    )
    profile = AgentRuntimeProfile(
        tenant_id=tenant.id,
        agent_id=agent.id,
        telephony_provider="twilio",
        primary_speech_provider="inworld",
        llm_provider="openai",
        llm_model="gpt-4o-mini",
    )

    async def static_readiness(*_args):
        return [], {
            "provider_compatibility": True,
            "tts_credential": True,
            "voice_selection": True,
            "llm_credential": True,
        }

    async def provider_config(_db, _tenant_id, provider):
        if provider == "inworld":
            raise ProviderCredentialError("cannot decrypt")
        return {"api_key": f"tenant-{provider}-key"}

    synthesize = AsyncMock()
    monkeypatch.setattr(settings, "inworld_api_key", "platform-inworld-key")
    monkeypatch.setattr(runtime_endpoint, "runtime_readiness", static_readiness)
    monkeypatch.setattr(runtime_endpoint, "load_provider_config", provider_config)
    monkeypatch.setattr(InworldClient, "synthesize_readiness_probe", synthesize)

    blockers, checks = await runtime_endpoint.live_runtime_readiness(db, agent, profile)

    assert checks["tts_provider_live"] is False
    assert checks["llm_provider_live"] is False
    assert blockers == ["Inworld workspace credential became unavailable during live readiness."]
    synthesize.assert_not_awaited()


@pytest.mark.asyncio
async def test_ready_runtime_can_be_activated(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "sarvam_api_key", "sarvam-test-key-long-enough")
    monkeypatch.setattr(settings, "openai_api_key", "openai-test-key")
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "twilio-test-token")
    monkeypatch.setattr(settings, "base_url", "https://api.example.com")
    agent = Agent(
        tenant_id=tenant.id,
        name="Active concierge",
        system_prompt="Answer patients using approved clinic knowledge.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
        language_switching_enabled=False,
        language_switching_mode="disabled",
        last_synced_at=datetime.now(UTC),
    )
    db.add(agent)
    await db.commit()
    payload = {
        "assigned_numbers": ["+971501234567"],
        "telephony_provider": "twilio",
        "primary_speech_provider": "sarvam",
        "fallback_speech_provider": None,
        "llm_provider": "openai",
        "llm_model": "gpt-4o-mini",
        "stt_language": "auto",
        "max_concurrent_calls": 2,
        "daily_call_limit": 50,
        "monthly_budget_cents": 10000,
    }

    configured = await client.put(
        f"/api/v1/runtime/agents/{agent.id}", headers=auth_headers, json=payload
    )
    tested = await client.post(f"/api/v1/runtime/agents/{agent.id}/test", headers=auth_headers)
    activated = await client.post(
        f"/api/v1/runtime/agents/{agent.id}/activate", headers=auth_headers
    )
    retested = await client.post(f"/api/v1/runtime/agents/{agent.id}/test", headers=auth_headers)
    active_profile = await client.get(f"/api/v1/runtime/agents/{agent.id}", headers=auth_headers)

    assert configured.json()["ready"] is True
    assert tested.json()["ready"] is True
    assert activated.status_code == 200
    assert activated.json()["enabled"] is True
    assert activated.json()["status"] == "active"
    assert retested.status_code == 200
    assert retested.json()["ready"] is True
    assert active_profile.json()["enabled"] is True
    assert active_profile.json()["status"] == "active"

    monkeypatch.setattr(settings, "sarvam_api_key", "")
    degraded = await client.post(f"/api/v1/runtime/agents/{agent.id}/test", headers=auth_headers)
    still_active = await client.get(f"/api/v1/runtime/agents/{agent.id}", headers=auth_headers)

    assert degraded.status_code == 200
    assert degraded.json()["ready"] is False
    assert still_active.json()["enabled"] is True
    assert still_active.json()["status"] == "active"
    assert still_active.json()["blockers"]

    failed_activation = await client.post(
        f"/api/v1/runtime/agents/{agent.id}/activate", headers=auth_headers
    )
    fail_closed_profile = await client.get(
        f"/api/v1/runtime/agents/{agent.id}", headers=auth_headers
    )

    assert failed_activation.status_code == 409
    assert fail_closed_profile.json()["enabled"] is False
    assert fail_closed_profile.json()["status"] == "blocked"


@pytest.mark.asyncio
async def test_runtime_readiness_blocks_number_owned_by_another_active_agent(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "sarvam_api_key", "sarvam-test-key-long-enough")
    monkeypatch.setattr(settings, "openai_api_key", "openai-test-key")
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "twilio-test-token")
    monkeypatch.setattr(settings, "base_url", "https://api.example.com")
    active_agent = Agent(
        tenant_id=tenant.id,
        name="Existing phone owner",
        system_prompt="Answer calls.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
    )
    new_agent = Agent(
        tenant_id=tenant.id,
        name="Conflicting phone owner",
        system_prompt="Answer calls.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
    )
    db.add_all([active_agent, new_agent])
    await db.flush()
    db.add(
        AgentRuntimeProfile(
            tenant_id=tenant.id,
            agent_id=active_agent.id,
            enabled=True,
            status="active",
            telephony_provider="twilio",
            assigned_numbers=["+971501234567"],
        )
    )
    await db.commit()

    configured = await client.put(
        f"/api/v1/runtime/agents/{new_agent.id}",
        headers=auth_headers,
        json={
            "assigned_numbers": ["+971501234567"],
            "telephony_provider": "twilio",
            "primary_speech_provider": "sarvam",
            "fallback_speech_provider": None,
            "llm_provider": "openai",
            "llm_model": "gpt-4o-mini",
            "stt_language": "auto",
            "max_concurrent_calls": 1,
            "daily_call_limit": 50,
            "monthly_budget_cents": 10000,
        },
    )
    activated = await client.post(
        f"/api/v1/runtime/agents/{new_agent.id}/activate",
        headers=auth_headers,
    )

    assert configured.status_code == 200
    assert configured.json()["ready"] is False
    assert any("other active agent" in blocker for blocker in configured.json()["blockers"])
    assert activated.status_code == 409


@pytest.mark.asyncio
async def test_runtime_readiness_blocks_bound_pdf_without_searchable_text(
    client: AsyncClient,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "sarvam_api_key", "sarvam-test-key-long-enough")
    monkeypatch.setattr(settings, "openai_api_key", "openai-test-key")
    monkeypatch.setattr(settings, "twilio_account_sid", "ACtest")
    monkeypatch.setattr(settings, "twilio_auth_token", "twilio-test-token")
    monkeypatch.setattr(settings, "base_url", "https://api.example.com")
    agent = Agent(
        tenant_id=tenant.id,
        name="Knowledge-gated concierge",
        system_prompt="Use the bound knowledge base.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
    )
    knowledge = KnowledgeBase(
        tenant_id=tenant.id,
        name="Clinic knowledge",
        provider="smallest",
        sync_status="ready",
        approval_status="approved",
    )
    db.add_all([agent, knowledge])
    await db.flush()
    db.add_all(
        [
            AgentKnowledgeBinding(
                tenant_id=tenant.id,
                agent_id=agent.id,
                knowledge_base_id=knowledge.id,
                provider="sarvam",
                sync_status="synced",
            ),
            KnowledgeSource(
                tenant_id=tenant.id,
                knowledge_base_id=knowledge.id,
                source_type="file",
                name="directory.pdf",
                status="indexed",
                content=None,
            ),
        ]
    )
    await db.commit()
    payload = {
        "assigned_numbers": ["+971501234567"],
        "telephony_provider": "twilio",
        "primary_speech_provider": "sarvam",
        "fallback_speech_provider": None,
        "llm_provider": "openai",
        "llm_model": "gpt-4o-mini",
        "stt_language": "auto",
        "max_concurrent_calls": 1,
        "daily_call_limit": 50,
        "monthly_budget_cents": 10000,
    }

    configured = await client.put(
        f"/api/v1/runtime/agents/{agent.id}", headers=auth_headers, json=payload
    )

    assert configured.status_code == 200
    assert configured.json()["ready"] is False
    assert any("searchable text" in blocker for blocker in configured.json()["blockers"])
