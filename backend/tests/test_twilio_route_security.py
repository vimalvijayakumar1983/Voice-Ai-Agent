"""Security-boundary tests for tenant-owned Twilio DID routing."""

import asyncio
import time

import httpx
import pytest

from app.models.agent import AgentRuntimeProfile
from app.services.twilio_route_security import (
    TwilioRouteCredential,
    TwilioRouteVerificationError,
    mark_twilio_route_verified,
    twilio_route_lock_key,
    twilio_route_verification_is_current,
    verify_twilio_route_ownership,
)

ACCOUNT_SID = "AC" + "a" * 32
AUTH_TOKEN = "route-verification-auth-token"
NUMBER = "+15551234567"
VOICE_URL = "https://voice.example.com/api/v1/webhooks/twilio/voice/inbound"


def _transport_for_record(record: dict) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "api.twilio.com"
        assert request.url.params["PhoneNumber"] == NUMBER
        return httpx.Response(200, json={"incoming_phone_numbers": [record]})

    return httpx.MockTransport(handler)


def _number_record(**overrides) -> dict:
    return {
        "account_sid": ACCOUNT_SID,
        "phone_number": NUMBER,
        "voice_url": VOICE_URL,
        "voice_method": "POST",
        "voice_application_sid": None,
        **overrides,
    }


@pytest.mark.asyncio
async def test_twilio_route_probe_verifies_account_did_and_direct_post_handler():
    await verify_twilio_route_ownership(
        credential=TwilioRouteCredential(ACCOUNT_SID, AUTH_TOKEN),
        assigned_numbers=[NUMBER],
        expected_voice_url=VOICE_URL,
        transport=_transport_for_record(_number_record()),
    )


@pytest.mark.asyncio
async def test_twilio_route_probe_rejects_an_empty_assignment():
    with pytest.raises(TwilioRouteVerificationError, match="No Twilio number is assigned"):
        await verify_twilio_route_ownership(
            credential=TwilioRouteCredential(ACCOUNT_SID, AUTH_TOKEN),
            assigned_numbers=[],
            expected_voice_url=VOICE_URL,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [401, 403])
async def test_twilio_route_probe_rejects_invalid_account_credentials(status_code):
    transport = httpx.MockTransport(lambda _request: httpx.Response(status_code))

    with pytest.raises(TwilioRouteVerificationError, match="rejected.*account SID"):
        await verify_twilio_route_ownership(
            credential=TwilioRouteCredential(ACCOUNT_SID, AUTH_TOKEN),
            assigned_numbers=[NUMBER],
            expected_voice_url=VOICE_URL,
            transport=transport,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "records",
    [
        [],
        [_number_record(account_sid="AC" + "b" * 32)],
        [_number_record(phone_number="+15557654321")],
    ],
)
async def test_twilio_route_probe_rejects_empty_or_mismatched_did_records(records):
    transport = httpx.MockTransport(
        lambda _request: httpx.Response(200, json={"incoming_phone_numbers": records})
    )

    with pytest.raises(TwilioRouteVerificationError, match="is not owned"):
        await verify_twilio_route_ownership(
            credential=TwilioRouteCredential(ACCOUNT_SID, AUTH_TOKEN),
            assigned_numbers=[NUMBER],
            expected_voice_url=VOICE_URL,
            transport=transport,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [429, 500, 503])
async def test_twilio_route_probe_fails_closed_on_provider_errors(status_code):
    transport = httpx.MockTransport(lambda _request: httpx.Response(status_code))

    with pytest.raises(TwilioRouteVerificationError, match=f"HTTP {status_code}"):
        await verify_twilio_route_ownership(
            credential=TwilioRouteCredential(ACCOUNT_SID, AUTH_TOKEN),
            assigned_numbers=[NUMBER],
            expected_voice_url=VOICE_URL,
            transport=transport,
        )


@pytest.mark.asyncio
async def test_twilio_route_probe_fails_closed_on_network_or_malformed_response():
    def network_failure(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Twilio is unreachable", request=request)

    with pytest.raises(TwilioRouteVerificationError, match="could not reach Twilio"):
        await verify_twilio_route_ownership(
            credential=TwilioRouteCredential(ACCOUNT_SID, AUTH_TOKEN),
            assigned_numbers=[NUMBER],
            expected_voice_url=VOICE_URL,
            transport=httpx.MockTransport(network_failure),
        )

    malformed = httpx.MockTransport(
        lambda _request: httpx.Response(
            200,
            content=b"{not-json",
            headers={"content-type": "application/json"},
        )
    )
    with pytest.raises(TwilioRouteVerificationError, match="invalid response"):
        await verify_twilio_route_ownership(
            credential=TwilioRouteCredential(ACCOUNT_SID, AUTH_TOKEN),
            assigned_numbers=[NUMBER],
            expected_voice_url=VOICE_URL,
            transport=malformed,
        )


@pytest.mark.asyncio
async def test_twilio_route_probe_uses_one_deadline_and_cancels_all_did_requests():
    numbers = [NUMBER, "+15557654321"]
    started: set[str] = set()
    cancelled: set[str] = set()

    async def stalled(request: httpx.Request) -> httpx.Response:
        number = request.url.params["PhoneNumber"]
        started.add(number)
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.add(number)
            raise
        raise AssertionError("The route probe did not enforce its total deadline")

    before = time.monotonic()
    with pytest.raises(TwilioRouteVerificationError, match="timed out"):
        await verify_twilio_route_ownership(
            credential=TwilioRouteCredential(ACCOUNT_SID, AUTH_TOKEN),
            assigned_numbers=numbers,
            expected_voice_url=VOICE_URL,
            timeout=1,
            transport=httpx.MockTransport(stalled),
        )
    elapsed = time.monotonic() - before

    assert elapsed < 2
    assert started == set(numbers)
    assert cancelled == set(numbers)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"voice_url": "https://wrong.example.com/inbound"}, "VAV inbound POST webhook"),
        ({"voice_method": "GET"}, "VAV inbound POST webhook"),
        ({"voice_method": None}, "VAV inbound POST webhook"),
        ({"voice_application_sid": "AP" + "1" * 32}, "TwiML Application"),
    ],
)
async def test_twilio_route_probe_rejects_wrong_or_indirect_handler(overrides, message):
    with pytest.raises(TwilioRouteVerificationError, match=message):
        await verify_twilio_route_ownership(
            credential=TwilioRouteCredential(ACCOUNT_SID, AUTH_TOKEN),
            assigned_numbers=[NUMBER],
            expected_voice_url=VOICE_URL,
            transport=_transport_for_record(_number_record(**overrides)),
        )


def test_twilio_route_marker_is_bound_to_secret_dids_and_callback():
    credential = TwilioRouteCredential(ACCOUNT_SID, AUTH_TOKEN)
    profile = AgentRuntimeProfile(assigned_numbers=[NUMBER], runtime_config={})
    mark_twilio_route_verified(
        profile,
        credential,
        expected_voice_url=VOICE_URL,
    )

    assert twilio_route_verification_is_current(
        profile,
        credential,
        expected_voice_url=VOICE_URL,
    )
    assert not twilio_route_verification_is_current(
        profile,
        TwilioRouteCredential(ACCOUNT_SID, "rotated-auth-token"),
        expected_voice_url=VOICE_URL,
    )
    assert not twilio_route_verification_is_current(
        profile,
        credential,
        expected_voice_url="https://new.example.com/api/v1/webhooks/twilio/voice/inbound",
    )
    profile.assigned_numbers = [NUMBER, "+15557654321"]
    assert not twilio_route_verification_is_current(
        profile,
        credential,
        expected_voice_url=VOICE_URL,
    )


def test_twilio_route_lock_identity_is_cross_tenant_account_and_did_scoped():
    assert twilio_route_lock_key(ACCOUNT_SID, NUMBER) == (f"twilio-route:{ACCOUNT_SID}:{NUMBER}")
