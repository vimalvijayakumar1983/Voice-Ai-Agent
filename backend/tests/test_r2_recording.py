"""Browser consent, server-side R2 identity, lifecycle and authenticated playback."""

import base64
import hashlib
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from google.protobuf.json_format import MessageToJson
from livekit import api
from pydantic import ValidationError

from app.api.v1.endpoints import call_recordings as endpoint
from app.core.config import Settings, settings
from app.models.call import Call
from app.services import r2_recording as r2
from app.services.call_metadata import public_call_metadata
from app.services.recordings import RecordingError


@pytest.fixture
def enabled(monkeypatch):
    for key, value in {
        "recording_enabled": True,
        "recording_s3_access_key_id": "test-access",
        "recording_s3_secret_access_key": "test-secret",
        "recording_s3_bucket": "test-bucket",
        "recording_s3_endpoint": "https://test.r2.cloudflarestorage.com",
        "livekit_api_key": "test-livekit",
        "livekit_api_secret": "test-livekit-secret-at-least-32-characters",
    }.items():
        monkeypatch.setattr(settings, key, value)


@pytest.fixture
async def call(db, tenant, user):
    call_id = uuid4()
    row = Call(
        id=call_id,
        tenant_id=tenant.id,
        provider="livekit_webrtc",
        direction="inbound",
        status="in_progress",
        from_number="browser",
        to_number="agent",
        call_metadata={
            "browser_user_id": str(user.id),
            "agent_configuration": {},
            "livekit_room": f"vav-browser-{call_id}",
        },
    )
    db.add(row)
    await db.commit()
    return row


def info(call, status=api.EgressStatus.EGRESS_ACTIVE):
    return api.EgressInfo(egress_id="EG_test", room_name=f"vav-browser-{call.id}", status=status)


@pytest.fixture
def provider(monkeypatch, call):
    lk = SimpleNamespace(
        room=SimpleNamespace(
            list_participants=AsyncMock(
                return_value=SimpleNamespace(
                    participants=[SimpleNamespace(identity=f"browser-{call.id}")]
                )
            )
        ),
        egress=SimpleNamespace(
            start_egress=AsyncMock(return_value=info(call)),
            stop_egress=AsyncMock(return_value=info(call, api.EgressStatus.EGRESS_ENDING)),
            list_egress=AsyncMock(return_value=SimpleNamespace(items=[info(call)])),
        ),
        aclose=AsyncMock(),
    )
    monkeypatch.setattr(endpoint, "client", lambda: lk)
    return lk


def consent():
    return {"consent": True, "notice_version": r2.NOTICE_VERSION}


@pytest.mark.asyncio
async def test_consent_start_stop_and_private_playback(
    client, auth_headers, call, provider, enabled, monkeypatch
):
    url = f"/api/v1/call-recordings/{call.id}"
    response = await client.post(url + "/start", json=consent(), headers=auth_headers)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "recording"
    assert "secret" not in response.text and "EG_test" not in response.text
    request = provider.egress.start_egress.await_args.args[0]
    assert request.template.audio_only
    assert request.storage.s3.force_path_style
    assert request.outputs[0].file.filepath == r2.object_key(call.tenant_id, call.id)
    assert request.storage.s3.secret == "test-secret"
    assert request.webhooks[0].url.endswith("/api/v1/call-recordings/webhook")
    again = await client.post(url + "/start", json=consent(), headers=auth_headers)
    assert again.status_code == 200
    provider.egress.start_egress.assert_awaited_once()
    stopped = await client.post(url + "/stop", headers=auth_headers)
    assert stopped.json()["state"] == "processing"
    provider.egress.list_egress.return_value.items = [info(call, api.EgressStatus.EGRESS_COMPLETE)]
    ready = await client.get(url + "/status", headers=auth_headers)
    assert ready.json()["available"] is True
    storage = Mock()
    storage.get_object.return_value = {"Body": BytesIO(b"OggS" + b"test"), "ContentLength": 8}
    monkeypatch.setattr(r2, "storage_client", lambda: storage)
    audio = await client.get(f"/api/v1/calls/{call.id}/recording", headers=auth_headers)
    assert audio.status_code == 200, audio.text
    assert audio.content.startswith(b"OggS")
    assert "no-store" in audio.headers["cache-control"]
    storage.get_object.assert_called_once_with(
        Bucket="test-bucket", Key=r2.object_key(call.tenant_id, call.id)
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"consent": False, "notice_version": r2.NOTICE_VERSION},
        {"consent": True, "notice_version": "old"},
    ],
)
async def test_consent_required(client, auth_headers, call, enabled, provider, payload):
    response = await client.post(
        f"/api/v1/call-recordings/{call.id}/start", json=payload, headers=auth_headers
    )
    assert response.status_code == 422
    provider.egress.start_egress.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change,expected", [("caller", 403), ("tenant", 404), ("sip", 409), ("completed", 409)]
)
async def test_access_and_transport_boundaries(
    client, auth_headers, call, db, enabled, provider, change, expected
):
    if change == "caller":
        call.call_metadata = {**call.call_metadata, "browser_user_id": str(uuid4())}
    elif change == "tenant":
        # A valid but non-existent call ID cannot cross a tenant boundary either.
        from app.models.tenant import Tenant

        other = Tenant(name="Other", slug="other")
        db.add(other)
        await db.flush()
        call.tenant_id = other.id
    elif change == "sip":
        call.provider = "livekit_sip"
    else:
        call.status = "completed"
    await db.commit()
    response = await client.post(
        f"/api/v1/call-recordings/{call.id}/start", json=consent(), headers=auth_headers
    )
    assert response.status_code == expected, response.text
    provider.egress.start_egress.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_start_never_retries(client, auth_headers, call, enabled, provider):
    provider.egress.start_egress.side_effect = TimeoutError("secret-provider-detail")
    url = f"/api/v1/call-recordings/{call.id}/start"
    response = await client.post(url, json=consent(), headers=auth_headers)
    assert response.status_code == 502
    assert "secret-provider-detail" not in response.text
    response = await client.post(url, json=consent(), headers=auth_headers)
    assert response.json()["state"] == "unconfirmed"
    provider.egress.start_egress.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_audio_denied_without_storage(call, monkeypatch):
    call.call_metadata = {
        "private_recording": {
            "state": "ready",
            "expires_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
        }
    }
    storage = Mock()
    monkeypatch.setattr(r2, "storage_client", storage)
    with pytest.raises(RecordingError):
        await r2.fetch_audio(call)
    storage.assert_not_called()


@pytest.mark.asyncio
async def test_unsigned_webhook_rejected(client, enabled):
    response = await client.post(
        "/api/v1/call-recordings/webhook", content='{"event":"egress_ended"}'
    )
    assert response.status_code == 401


def test_terminal_state_does_not_regress():
    state = {"state": "ready"}
    assert (
        r2.apply_provider_state(state, SimpleNamespace(status=api.EgressStatus.EGRESS_ACTIVE))
        == state
    )


def test_public_metadata_contains_no_private_recording_identity():
    metadata = {
        "agent_configuration": {},
        "runtime": {"recording_state": "blocked"},
        "private_recording": {
            "state": "ready",
            "egress_id": "EG_secret",
            "consent_user_id": "private",
            "expires_at": (datetime.now(UTC) + timedelta(days=90)).isoformat(),
        },
    }
    public = public_call_metadata(metadata)
    assert public["runtime"]["recording_state"] == "ready"
    assert public["recording"]["available"] is True
    assert "EG_secret" not in str(public) and "consent_user_id" not in str(public)


@pytest.mark.asyncio
async def test_signed_webhook_finalizes_only_matching_job(
    client, auth_headers, call, enabled, provider
):
    url = f"/api/v1/call-recordings/{call.id}"
    await client.post(url + "/start", json=consent(), headers=auth_headers)

    async def send(job):
        payload = MessageToJson(api.WebhookEvent(event="egress_ended", egress_info=job))
        digest = base64.b64encode(hashlib.sha256(payload.encode()).digest()).decode()
        token = (
            api.AccessToken(settings.livekit_api_key, settings.livekit_api_secret)
            .with_sha256(digest)
            .to_jwt()
        )
        return await client.post(
            "/api/v1/call-recordings/webhook", content=payload, headers={"Authorization": token}
        )

    wrong = info(call, api.EgressStatus.EGRESS_COMPLETE)
    wrong.egress_id = "EG_other"
    assert (await send(wrong)).status_code == 200
    status = await client.get(url + "/status", headers=auth_headers)
    assert status.json()["available"] is False
    assert (await send(info(call, api.EgressStatus.EGRESS_COMPLETE))).status_code == 200
    status = await client.get(url + "/status", headers=auth_headers)
    assert status.json()["available"] is True
    # Delayed provider events must not undo completed capture.
    assert (await send(info(call))).status_code == 200
    status = await client.get(url + "/status", headers=auth_headers)
    assert status.json()["state"] == "ready"


@pytest.mark.asyncio
async def test_lost_start_response_recovered_by_exact_request(
    client, auth_headers, call, enabled, provider
):
    provider.egress.start_egress.side_effect = TimeoutError()
    url = f"/api/v1/call-recordings/{call.id}"
    await client.post(url + "/start", json=consent(), headers=auth_headers)
    recovered = info(call, api.EgressStatus.EGRESS_COMPLETE)
    recovered.egress.CopyFrom(r2.egress_request(call.tenant_id, call.id, f"vav-browser-{call.id}"))
    provider.egress.list_egress.return_value.items = [recovered]
    status = await client.get(url + "/status", headers=auth_headers)
    assert status.json()["available"] is True
    provider.egress.start_egress.assert_awaited_once()


@pytest.mark.asyncio
async def test_preflight_failure_can_retry_without_duplicate_egress(
    client, auth_headers, call, enabled, provider
):
    provider.room.list_participants.side_effect = TimeoutError()
    url = f"/api/v1/call-recordings/{call.id}"
    first = await client.post(url + "/start", json=consent(), headers=auth_headers)
    assert first.status_code == 502
    provider.egress.start_egress.assert_not_awaited()
    status = await client.get(url + "/status", headers=auth_headers)
    assert status.json()["state"] == "retryable"
    provider.egress.list_egress.assert_not_awaited()
    provider.room.list_participants.side_effect = None
    second = await client.post(url + "/start", json=consent(), headers=auth_headers)
    assert second.json()["state"] == "recording"
    provider.egress.start_egress.assert_awaited_once()


def test_retention_cannot_silently_disagree_with_consent_and_bucket_policy(monkeypatch):
    monkeypatch.setenv("RECORDING_RETENTION_DAYS", "90")
    assert Settings().recording_retention_days == 90
    with pytest.raises(ValidationError, match="recording_retention_days"):
        Settings(recording_retention_days=30)
