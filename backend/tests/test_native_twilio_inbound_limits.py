"""Fail-closed capacity and knowledge admission for native Twilio inbound calls."""

import asyncio
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from xml.etree import ElementTree

import pytest
from sqlalchemy import func, select
from twilio.request_validator import RequestValidator

from app.api.v1.endpoints import webhooks
from app.models.agent import Agent, AgentRuntimeProfile
from app.models.billing import UsageRecord
from app.models.call import Call
from app.services.knowledge_serving import KnowledgeServingError
from app.services.twilio_route_security import (
    load_workspace_twilio_route_credential,
    mark_twilio_route_verified,
)
from tests.conftest import test_session_factory as session_factory
from tests.knowledge_test_utils import publish_test_knowledge


async def _configure_native_inbound_route(
    client,
    auth_headers,
    db,
    tenant,
    *,
    daily_call_limit: int = 100,
    max_concurrent_calls: int = 5,
    monthly_budget_cents: int = 10_000,
):
    account_sid = "AC" + "a1234567890abcdef1234567890abcde"
    auth_token = "twilio_native_inbound_limit_token_123456789"
    number = "+15551234567"
    saved = await client.put(
        "/api/v1/runtime/credentials/twilio/account",
        headers=auth_headers,
        json={
            "account_sid": account_sid,
            "auth_token": auth_token,
            "default_from_number": number,
        },
    )
    assert saved.status_code == 200
    agent = Agent(
        tenant_id=tenant.id,
        name="Capacity guarded receptionist",
        system_prompt="Help callers without exceeding runtime limits.",
        voice_provider="sarvam",
        voice_id="sarvam:ishita",
        language="en",
        supported_languages=["en"],
        max_call_duration_seconds=30,
    )
    db.add(agent)
    await db.flush()
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
        daily_call_limit=daily_call_limit,
        max_concurrent_calls=max_concurrent_calls,
        monthly_budget_cents=monthly_budget_cents,
    )
    db.add(profile)
    credential = await load_workspace_twilio_route_credential(db, tenant.id)
    assert credential is not None
    mark_twilio_route_verified(
        profile,
        credential,
        expected_voice_url="http://test/api/v1/webhooks/twilio/voice/inbound",
    )
    _knowledge, revision = await publish_test_knowledge(
        db,
        tenant_id=tenant.id,
        agent=agent,
        label="Capacity clinic",
    )
    await db.commit()
    return agent, profile, revision, account_sid, auth_token, number


async def _post_inbound(
    client,
    monkeypatch,
    *,
    account_sid: str,
    auth_token: str,
    number: str,
    call_sid: str,
):
    path = "/api/v1/webhooks/twilio/voice/inbound"
    payload = {
        "AccountSid": account_sid,
        "From": "+15557654321",
        "To": number,
        "CallSid": call_sid,
    }
    monkeypatch.setattr(webhooks.settings, "base_url", "http://test")
    monkeypatch.setattr(webhooks.settings, "twilio_auth_token", "")
    monkeypatch.setattr(webhooks, "async_session_factory", session_factory)
    signature = RequestValidator(auth_token).compute_signature(f"http://test{path}", payload)
    return await client.post(
        path,
        data=payload,
        headers={"X-Twilio-Signature": signature},
    )


def _assert_friendly_rejection(response) -> None:
    assert response.status_code == 200
    xml = ElementTree.fromstring(response.text)
    assert xml.find("./Hangup") is not None
    assert xml.find("./Connect/Stream") is None
    assert "temporarily unavailable" in response.text


async def _assert_single_failed_audit_call(db, *, call_sid: str, lifecycle_error: str) -> Call:
    call = await db.scalar(select(Call).where(Call.provider_call_sid == call_sid))
    assert call is not None
    assert call.direction == "inbound"
    assert call.provider == "twilio"
    assert call.status == "failed"
    assert call.answered_at is not None
    assert call.ended_at is not None
    assert call.duration_seconds == 1
    assert call.call_metadata["lifecycle_error"] == lifecycle_error
    runtime = call.call_metadata["runtime"]
    assert runtime["transport"] == "twilio_fallback_twiml"
    assert runtime["media_stream_started"] is False
    assert runtime["cost_state"] == "pending_provider_billing_sync"
    count = await db.scalar(
        select(func.count()).select_from(Call).where(Call.provider_call_sid == call_sid)
    )
    assert count == 1
    return call


@pytest.mark.asyncio
async def test_concurrent_native_twilio_duplicate_deliveries_share_one_reservation(
    client,
    auth_headers,
    db,
    tenant,
    monkeypatch,
):
    _agent, _profile, _revision, account_sid, token, number = await _configure_native_inbound_route(
        client, auth_headers, db, tenant
    )
    call_sid = "CA-concurrent-duplicate"

    first, second = await asyncio.gather(
        _post_inbound(
            client,
            monkeypatch,
            account_sid=account_sid,
            auth_token=token,
            number=number,
            call_sid=call_sid,
        ),
        _post_inbound(
            client,
            monkeypatch,
            account_sid=account_sid,
            auth_token=token,
            number=number,
            call_sid=call_sid,
        ),
    )

    assert first.status_code == 200
    assert second.status_code == 200
    first_stream = ElementTree.fromstring(first.text).find("./Connect/Stream")
    second_stream = ElementTree.fromstring(second.text).find("./Connect/Stream")
    assert first_stream is not None
    assert second_stream is not None
    assert first_stream.attrib["url"] == second_stream.attrib["url"]
    count = await db.scalar(
        select(func.count()).select_from(Call).where(Call.provider_call_sid == call_sid)
    )
    assert count == 1
    call = await db.scalar(select(Call).where(Call.provider_call_sid == call_sid))
    assert call is not None
    assert call.status == "in_progress"


@pytest.mark.asyncio
async def test_concurrent_distinct_native_calls_observe_one_agent_capacity_reservation(
    client,
    auth_headers,
    db,
    tenant,
    monkeypatch,
):
    _agent, _profile, _revision, account_sid, token, number = await _configure_native_inbound_route(
        client,
        auth_headers,
        db,
        tenant,
        max_concurrent_calls=1,
    )

    responses = await asyncio.gather(
        *(
            _post_inbound(
                client,
                monkeypatch,
                account_sid=account_sid,
                auth_token=token,
                number=number,
                call_sid=call_sid,
            )
            for call_sid in ("CA-distinct-capacity-a", "CA-distinct-capacity-b")
        )
    )

    streams = [
        ElementTree.fromstring(response.text).find("./Connect/Stream") for response in responses
    ]
    assert sum(stream is not None for stream in streams) == 1
    assert sum("temporarily unavailable" in response.text for response in responses) == 1
    calls = (
        await db.scalars(
            select(Call).where(
                Call.provider_call_sid.in_(("CA-distinct-capacity-a", "CA-distinct-capacity-b"))
            )
        )
    ).all()
    assert sorted(call.status for call in calls) == ["failed", "in_progress"]


@pytest.mark.asyncio
async def test_native_twilio_inbound_never_reopens_terminal_unknown_call(
    client,
    auth_headers,
    db,
    tenant,
    monkeypatch,
):
    _agent, _profile, _revision, account_sid, token, number = await _configure_native_inbound_route(
        client, auth_headers, db, tenant
    )
    call_sid = "CA-terminal-unknown-replay"
    first = await _post_inbound(
        client,
        monkeypatch,
        account_sid=account_sid,
        auth_token=token,
        number=number,
        call_sid=call_sid,
    )
    assert first.status_code == 200
    assert ElementTree.fromstring(first.text).find("./Connect/Stream") is not None

    call = await db.scalar(select(Call).where(Call.provider_call_sid == call_sid))
    assert call is not None
    call.status = "terminal_unknown"
    call.ended_at = datetime.now(UTC)
    await db.commit()

    replay = await _post_inbound(
        client,
        monkeypatch,
        account_sid=account_sid,
        auth_token=token,
        number=number,
        call_sid=call_sid,
    )

    _assert_friendly_rejection(replay)
    await db.refresh(call)
    assert call.status == "terminal_unknown"
    count = await db.scalar(
        select(func.count()).select_from(Call).where(Call.provider_call_sid == call_sid)
    )
    assert count == 1


@pytest.mark.asyncio
async def test_native_twilio_admission_race_retains_billable_failure_proxy(
    client,
    auth_headers,
    db,
    tenant,
    monkeypatch,
):
    _agent, _profile, _revision, account_sid, token, number = await _configure_native_inbound_route(
        client, auth_headers, db, tenant
    )
    call_sid = "CA-knowledge-admission-policy-race"
    monkeypatch.setattr(
        webhooks,
        "admit_inbound_twilio_knowledge_call",
        AsyncMock(side_effect=KnowledgeServingError("Knowledge was revoked before admission")),
    )

    response = await _post_inbound(
        client,
        monkeypatch,
        account_sid=account_sid,
        auth_token=token,
        number=number,
        call_sid=call_sid,
    )

    _assert_friendly_rejection(response)
    call = await _assert_single_failed_audit_call(
        db,
        call_sid=call_sid,
        lifecycle_error="immutable_knowledge_admission_failed",
    )
    assert call.call_metadata["runtime"]["duration_source"] == "minimum_answered_rejection"


@pytest.mark.asyncio
async def test_native_twilio_inbound_rejects_at_exact_concurrent_limit(
    client,
    auth_headers,
    db,
    tenant,
    monkeypatch,
):
    agent, _profile, _revision, account_sid, token, number = await _configure_native_inbound_route(
        client,
        auth_headers,
        db,
        tenant,
        max_concurrent_calls=1,
    )
    db.add(
        Call(
            tenant_id=tenant.id,
            agent_id=agent.id,
            direction="inbound",
            status="in_progress",
            from_number="+15550000001",
            to_number=number,
            provider="twilio",
            provider_call_sid="CA-existing-active",
            started_at=datetime.now(UTC),
            answered_at=datetime.now(UTC),
        )
    )
    await db.commit()

    call_sid = "CA-capacity-concurrent"
    response = await _post_inbound(
        client,
        monkeypatch,
        account_sid=account_sid,
        auth_token=token,
        number=number,
        call_sid=call_sid,
    )
    replay = await _post_inbound(
        client,
        monkeypatch,
        account_sid=account_sid,
        auth_token=token,
        number=number,
        call_sid=call_sid,
    )

    _assert_friendly_rejection(response)
    _assert_friendly_rejection(replay)
    await _assert_single_failed_audit_call(
        db,
        call_sid=call_sid,
        lifecycle_error="runtime_capacity_concurrent_limit_reached",
    )


@pytest.mark.asyncio
async def test_native_twilio_inbound_rejects_at_exact_daily_limit(
    client,
    auth_headers,
    db,
    tenant,
    monkeypatch,
):
    agent, _profile, _revision, account_sid, token, number = await _configure_native_inbound_route(
        client,
        auth_headers,
        db,
        tenant,
        daily_call_limit=1,
    )
    db.add(
        Call(
            tenant_id=tenant.id,
            agent_id=agent.id,
            direction="inbound",
            status="completed",
            from_number="+15550000002",
            to_number=number,
            provider="twilio",
            provider_call_sid="CA-existing-daily",
            started_at=datetime.now(UTC),
            ended_at=datetime.now(UTC),
            duration_seconds=0,
        )
    )
    await db.commit()

    call_sid = "CA-capacity-daily"
    response = await _post_inbound(
        client,
        monkeypatch,
        account_sid=account_sid,
        auth_token=token,
        number=number,
        call_sid=call_sid,
    )

    _assert_friendly_rejection(response)
    await _assert_single_failed_audit_call(
        db,
        call_sid=call_sid,
        lifecycle_error="runtime_capacity_daily_limit_reached",
    )


@pytest.mark.asyncio
async def test_native_twilio_inbound_rejects_when_monthly_budget_is_reserved(
    client,
    auth_headers,
    db,
    tenant,
    monkeypatch,
):
    agent, _profile, _revision, account_sid, token, number = await _configure_native_inbound_route(
        client,
        auth_headers,
        db,
        tenant,
        monthly_budget_cents=5,
    )
    now = datetime.now(UTC)
    metered_call = Call(
        tenant_id=tenant.id,
        agent_id=agent.id,
        direction="inbound",
        status="completed",
        from_number="+15550000003",
        to_number=number,
        provider="twilio",
        provider_call_sid="CA-existing-metered",
        started_at=now,
        ended_at=now,
        duration_seconds=30,
    )
    db.add(metered_call)
    await db.flush()
    db.add(
        UsageRecord(
            tenant_id=tenant.id,
            call_id=metered_call.id,
            usage_type="call_minutes",
            quantity=0.5,
            unit="minutes",
            cost_cents=3,
            period_start=now,
            period_end=now,
        )
    )
    await db.commit()

    call_sid = "CA-capacity-budget"
    response = await _post_inbound(
        client,
        monkeypatch,
        account_sid=account_sid,
        auth_token=token,
        number=number,
        call_sid=call_sid,
    )

    _assert_friendly_rejection(response)
    await _assert_single_failed_audit_call(
        db,
        call_sid=call_sid,
        lifecycle_error="runtime_capacity_monthly_budget_reached",
    )


@pytest.mark.asyncio
async def test_native_twilio_inbound_corrupt_lexicon_manifest_fails_closed(
    client,
    auth_headers,
    db,
    tenant,
    monkeypatch,
):
    _agent, _profile, revision, account_sid, token, number = await _configure_native_inbound_route(
        client, auth_headers, db, tenant
    )
    revision.manifest = {
        **revision.manifest,
        "speech_lexicon": {
            **revision.manifest["speech_lexicon"],
            "content_sha256": "0" * 64,
        },
    }
    await db.commit()

    call_sid = "CA-corrupt-lexicon-manifest"
    response = await _post_inbound(
        client,
        monkeypatch,
        account_sid=account_sid,
        auth_token=token,
        number=number,
        call_sid=call_sid,
    )
    replay = await _post_inbound(
        client,
        monkeypatch,
        account_sid=account_sid,
        auth_token=token,
        number=number,
        call_sid=call_sid,
    )

    _assert_friendly_rejection(response)
    _assert_friendly_rejection(replay)
    call = await _assert_single_failed_audit_call(
        db,
        call_sid=call_sid,
        lifecycle_error="immutable_knowledge_reservation_failed",
    )
    runtime = call.call_metadata["runtime"]
    assert runtime["knowledge_serving_revision_id"] == str(revision.id)
    assert runtime["knowledge_serving_knowledge_base_id"] == str(revision.knowledge_base_id)
    assert "speech_lexicon_artifact_id" not in runtime
