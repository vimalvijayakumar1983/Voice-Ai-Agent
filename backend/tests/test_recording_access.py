"""Recording playback stays tenant-bound, consent-aware, and provider-private."""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.api.v1.endpoints import calls as calls_endpoint
from app.core.config import settings
from app.core.security import create_access_token
from app.models.audit import AuditEvent
from app.models.call import Call
from app.models.compliance import ConsentRecord
from app.models.tenant import Tenant
from app.services.recordings import (
    MAX_RECORDING_BYTES,
    RecordingAudio,
    RecordingError,
    fetch_call_recording,
)

MP3_AUDIO = b"ID3" + b"\x00" * 64


def make_call(tenant_id, **overrides) -> Call:
    values = {
        "tenant_id": tenant_id,
        "agent_id": None,
        "campaign_id": None,
        "direction": "outbound",
        "status": "completed",
        "from_number": "+971500000001",
        "to_number": "+971500000002",
        "provider": "smallest",
        "provider_call_sid": f"CALL-{uuid4()}",
        "provider_recording_url": "https://provider.invalid/private?secret=value",
    }
    values.update(overrides)
    return Call(**values)


@pytest.mark.asyncio
async def test_smallest_recording_is_fetched_server_side_without_forwarding_api_key(tenant):
    second_hop_headers: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        second_hop_headers.update(request.headers)
        return httpx.Response(200, headers={"Content-Type": "audio/mpeg"}, content=MP3_AUDIO)

    provider = SimpleNamespace(
        get_recording_download_url=AsyncMock(
            return_value="https://recordings.s3.amazonaws.com/call.mp3?X-Amz-Signature=test"
        )
    )
    call = make_call(tenant.id)

    recording = await fetch_call_recording(
        call,
        smallest_client=provider,
        transport=httpx.MockTransport(handler),
    )

    provider.get_recording_download_url.assert_awaited_once_with(call_id=call.provider_call_sid)
    assert recording == RecordingAudio(MP3_AUDIO, "audio/mpeg", "mp3")
    assert "authorization" not in second_hop_headers


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://recordings.s3.amazonaws.com/call.mp3",
        "https://127.0.0.1/internal",
        "https://amazonaws.com.evil.test/call.mp3",
        "https://user:password@recordings.s3.amazonaws.com/call.mp3",
        "https://sts.amazonaws.com/call.mp3",
        "https://voice.execute-api.us-east-1.amazonaws.com/call.mp3",
        "https://s3.execute-api.us-east-1.amazonaws.com/call.mp3",
        "https://voice.s3.execute-api.us-east-1.amazonaws.com/call.mp3",
        "https://recordings.s3.not-a-region.amazonaws.com/call.mp3",
        "https://recordings.s3-website.us-east-1.amazonaws.com/call.mp3",
    ],
)
async def test_smallest_recording_rejects_unsafe_second_hop_before_network(tenant, unsafe_url):
    provider = SimpleNamespace(get_recording_download_url=AsyncMock(return_value=unsafe_url))
    network = AsyncMock()

    with pytest.raises(RecordingError, match="invalid media location"):
        await fetch_call_recording(
            make_call(tenant.id),
            smallest_client=provider,
            transport=httpx.MockTransport(network),
        )

    network.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "safe_url",
    [
        "https://s3.amazonaws.com/recordings/call.mp3?X-Amz-Signature=test",
        "https://recordings.s3.us-east-1.amazonaws.com/call.mp3?X-Amz-Signature=test",
        "https://recordings.s3-us-west-2.amazonaws.com/call.mp3?X-Amz-Signature=test",
        "https://recordings.s3.dualstack.eu-west-1.amazonaws.com/call.mp3",
        "https://recordings.s3-accelerate.amazonaws.com/call.mp3",
        (
            "https://recording-ap-123456789012.s3-accesspoint.ap-south-1.amazonaws.com/"
            "call.mp3?X-Amz-Signature=test"
        ),
    ],
)
async def test_smallest_recording_accepts_valid_s3_second_hop_shapes(tenant, safe_url):
    provider = SimpleNamespace(get_recording_download_url=AsyncMock(return_value=safe_url))

    recording = await fetch_call_recording(
        make_call(tenant.id),
        smallest_client=provider,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                headers={"Content-Type": "audio/mpeg"},
                content=MP3_AUDIO,
            )
        ),
    )

    assert recording.content == MP3_AUDIO


@pytest.mark.asyncio
async def test_recording_download_rejects_redirect_and_oversized_body(tenant):
    provider = SimpleNamespace(
        get_recording_download_url=AsyncMock(
            return_value="https://recordings.s3.amazonaws.com/call.mp3?signature=test"
        )
    )

    with pytest.raises(RecordingError, match="unsafe redirect"):
        await fetch_call_recording(
            make_call(tenant.id),
            smallest_client=provider,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(302, headers={"Location": "https://evil.test"})
            ),
        )

    with pytest.raises(RecordingError, match="secure playback limit"):
        await fetch_call_recording(
            make_call(tenant.id),
            smallest_client=provider,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    headers={
                        "Content-Length": str(MAX_RECORDING_BYTES + 1),
                        "Content-Type": "audio/mpeg",
                    },
                )
            ),
        )


@pytest.mark.asyncio
async def test_recording_download_rejects_declared_or_sniffed_non_audio(tenant):
    provider = SimpleNamespace(
        get_recording_download_url=AsyncMock(
            return_value="https://recordings.s3.amazonaws.com/call.mp3?signature=test"
        )
    )

    with pytest.raises(RecordingError, match="unexpected media type"):
        await fetch_call_recording(
            make_call(tenant.id),
            smallest_client=provider,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    headers={"Content-Type": "text/html"},
                    content=MP3_AUDIO,
                )
            ),
        )

    with pytest.raises(RecordingError, match="invalid audio"):
        await fetch_call_recording(
            make_call(tenant.id),
            smallest_client=provider,
            transport=httpx.MockTransport(
                lambda _request: httpx.Response(
                    200,
                    headers={"Content-Type": "audio/mpeg"},
                    content=b"<html>not audio</html>",
                )
            ),
        )


@pytest.mark.asyncio
async def test_twilio_recording_uses_server_credentials_and_strict_resource_url(
    tenant,
    monkeypatch,
):
    account_sid = "AC" + "a" * 32
    recording_sid = "RE" + "b" * 32
    monkeypatch.setattr(settings, "twilio_account_sid", account_sid)
    monkeypatch.setattr(settings, "twilio_auth_token", "server-auth-token")
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["authorization"] = request.headers.get("authorization")
        return httpx.Response(200, headers={"Content-Type": "audio/mpeg"}, content=MP3_AUDIO)

    recording = await fetch_call_recording(
        make_call(
            tenant.id,
            provider="twilio",
            provider_call_sid="CA" + "c" * 32,
            provider_recording_url=(
                f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}"
                f"/Recordings/{recording_sid}"
            ),
        ),
        transport=httpx.MockTransport(handler),
    )

    assert recording.content == MP3_AUDIO
    assert captured["url"].endswith(f"/Recordings/{recording_sid}.mp3")
    assert captured["authorization"].startswith("Basic ")


@pytest.mark.asyncio
async def test_call_responses_hide_raw_url_and_recording_endpoint_is_audited(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    call = make_call(tenant.id, provider_recording_url=None)
    db.add(call)
    await db.commit()

    listing = await client.get("/api/v1/calls", headers=auth_headers)
    assert listing.status_code == 200
    item = listing.json()[0]
    # Smallest audio is fetched by provider conversation ID; it does not depend
    # on a callback exposing a provider media URL.
    assert item["recording_available"] is True
    assert "provider_recording_url" not in item
    assert "provider.invalid" not in listing.text

    provider_fetch = AsyncMock(return_value=RecordingAudio(MP3_AUDIO, "audio/mpeg", "mp3"))
    monkeypatch.setattr(calls_endpoint, "fetch_call_recording", provider_fetch)

    response = await client.get(f"/api/v1/calls/{call.id}/recording", headers=auth_headers)

    assert response.status_code == 200
    assert response.content == MP3_AUDIO
    assert response.headers["cache-control"] == "private, no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "provider.invalid" not in response.text
    provider_fetch.assert_awaited_once()
    audit = await db.scalar(
        select(AuditEvent).where(AuditEvent.action == "call.recording_accessed")
    )
    assert audit is not None
    assert audit.resource_id == str(call.id)
    assert audit.details == {"provider": "smallest", "bytes": len(MP3_AUDIO)}


@pytest.mark.asyncio
async def test_recording_availability_is_provider_specific_and_hides_raw_urls(
    client,
    auth_headers,
    tenant,
    db,
):
    smallest_without_callback_url = make_call(
        tenant.id,
        provider="smallest",
        provider_call_sid="smallest-conversation-id",
        provider_recording_url=None,
    )
    twilio_without_recording = make_call(
        tenant.id,
        provider="twilio",
        provider_call_sid="CA" + "a" * 32,
        provider_recording_url=None,
    )
    twilio_with_recording = make_call(
        tenant.id,
        provider="twilio",
        provider_call_sid="CA" + "b" * 32,
        provider_recording_url="https://api.twilio.com/private-recording?secret=value",
    )
    db.add_all(
        [
            smallest_without_callback_url,
            twilio_without_recording,
            twilio_with_recording,
        ]
    )
    await db.commit()

    response = await client.get("/api/v1/calls", headers=auth_headers)

    assert response.status_code == 200
    by_provider_sid = {item["provider_call_sid"]: item for item in response.json()}
    assert by_provider_sid["smallest-conversation-id"]["recording_available"] is True
    assert by_provider_sid["CA" + "a" * 32]["recording_available"] is False
    assert by_provider_sid["CA" + "b" * 32]["recording_available"] is True
    assert all("provider_recording_url" not in item for item in response.json())
    assert "private-recording" not in response.text


@pytest.mark.asyncio
async def test_recording_endpoint_does_not_reveal_another_tenants_call(
    client,
    auth_headers,
    db,
    monkeypatch,
):
    other_tenant = Tenant(name="Other Workspace", slug=f"other-{uuid4().hex[:12]}")
    db.add(other_tenant)
    await db.flush()
    call = make_call(other_tenant.id)
    db.add(call)
    await db.commit()
    provider_fetch = AsyncMock(return_value=RecordingAudio(MP3_AUDIO, "audio/mpeg", "mp3"))
    monkeypatch.setattr(calls_endpoint, "fetch_call_recording", provider_fetch)

    response = await client.get(f"/api/v1/calls/{call.id}/recording", headers=auth_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Call not found"
    provider_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_viewer_cannot_download_recording_bytes(
    client,
    tenant,
    user,
    db,
    monkeypatch,
):
    user.role = "viewer"
    call = make_call(tenant.id)
    db.add(call)
    await db.commit()
    headers = {"Authorization": f"Bearer {create_access_token(user.id, tenant.id, 'viewer')}"}
    provider_fetch = AsyncMock(return_value=RecordingAudio(MP3_AUDIO, "audio/mpeg", "mp3"))
    monkeypatch.setattr(calls_endpoint, "fetch_call_recording", provider_fetch)

    response = await client.get(f"/api/v1/calls/{call.id}/recording", headers=headers)

    assert response.status_code == 403
    provider_fetch.assert_not_awaited()


@pytest.mark.asyncio
async def test_latest_recording_revocation_blocks_bytes_and_is_audited(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    call = make_call(tenant.id)
    db.add(call)
    await db.commit()
    revoked = await client.post(
        "/api/v1/compliance/consent",
        headers=auth_headers,
        json={
            "phone_number": call.to_number,
            "consent_type": "recording",
            "status": "revoked",
            "evidence": {"method": "customer_request"},
        },
    )
    assert revoked.status_code == 201
    provider_fetch = AsyncMock(return_value=RecordingAudio(MP3_AUDIO, "audio/mpeg", "mp3"))
    monkeypatch.setattr(calls_endpoint, "fetch_call_recording", provider_fetch)

    response = await client.get(f"/api/v1/calls/{call.id}/recording", headers=auth_headers)

    assert response.status_code == 403
    assert response.json()["detail"] == (
        "Recording playback is blocked by an explicit customer revocation"
    )
    provider_fetch.assert_not_awaited()
    audit = await db.scalar(
        select(AuditEvent).where(AuditEvent.action == "call.recording_access_blocked")
    )
    assert audit is not None
    assert audit.details == {"reason": "recording_consent_revoked"}


@pytest.mark.asyncio
async def test_revocation_committed_during_provider_fetch_blocks_release(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    call = make_call(tenant.id)
    db.add(call)
    await db.commit()

    async def fetch_then_revoke(_call):
        db.add(
            ConsentRecord(
                tenant_id=tenant.id,
                phone_number=call.to_number,
                consent_type="recording",
                status="revoked",
                evidence={"method": "concurrent_customer_request"},
            )
        )
        await db.commit()
        return RecordingAudio(MP3_AUDIO, "audio/mpeg", "mp3")

    monkeypatch.setattr(calls_endpoint, "fetch_call_recording", fetch_then_revoke)

    response = await client.get(f"/api/v1/calls/{call.id}/recording", headers=auth_headers)

    assert response.status_code == 403
    assert response.content != MP3_AUDIO
    accessed = await db.scalar(
        select(AuditEvent).where(AuditEvent.action == "call.recording_accessed")
    )
    assert accessed is None


@pytest.mark.asyncio
async def test_no_recording_consent_record_is_policy_neutral(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    call = make_call(tenant.id)
    db.add(call)
    await db.commit()
    provider_fetch = AsyncMock(return_value=RecordingAudio(MP3_AUDIO, "audio/mpeg", "mp3"))
    monkeypatch.setattr(calls_endpoint, "fetch_call_recording", provider_fetch)

    response = await client.get(f"/api/v1/calls/{call.id}/recording", headers=auth_headers)

    assert response.status_code == 200
    provider_fetch.assert_awaited_once()


@pytest.mark.asyncio
async def test_latest_recording_grant_supersedes_an_earlier_revocation(
    client,
    auth_headers,
    tenant,
    db,
    monkeypatch,
):
    call = make_call(tenant.id)
    db.add(call)
    await db.commit()
    for status in ("revoked", "granted"):
        response = await client.post(
            "/api/v1/compliance/consent",
            headers=auth_headers,
            json={
                "phone_number": call.to_number,
                "consent_type": "recording",
                "status": status,
                "evidence": {"method": "audited_test"},
            },
        )
        assert response.status_code == 201

    provider_fetch = AsyncMock(return_value=RecordingAudio(MP3_AUDIO, "audio/mpeg", "mp3"))
    monkeypatch.setattr(calls_endpoint, "fetch_call_recording", provider_fetch)

    playback = await client.get(f"/api/v1/calls/{call.id}/recording", headers=auth_headers)

    assert playback.status_code == 200
    provider_fetch.assert_awaited_once()
