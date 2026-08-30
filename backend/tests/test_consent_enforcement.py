"""Outbound consent is enforced at dispatch boundaries."""

import pytest
from sqlalchemy import func, select

from app.models.audit import AuditEvent
from app.models.call import Call


@pytest.mark.asyncio
async def test_explicit_outbound_revocation_blocks_direct_call(
    client,
    auth_headers,
    db,
):
    agent_response = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Consent agent",
            "system_prompt": "Respect the contact's consent status.",
        },
    )
    assert agent_response.status_code == 201

    revoked = await client.post(
        "/api/v1/compliance/consent",
        headers=auth_headers,
        json={
            "phone_number": "+971 (50) 123-4567",
            "consent_type": "outbound_call",
            "status": "revoked",
            "evidence": {"method": "customer_request"},
        },
    )
    assert revoked.status_code == 201
    assert revoked.json()["phone_number"] == "+971501234567"
    audit_event = await db.scalar(
        select(AuditEvent).where(
            AuditEvent.resource_type == "consent_record",
            AuditEvent.resource_id == revoked.json()["id"],
        )
    )
    assert audit_event is not None
    assert audit_event.actor_user_id is not None
    assert audit_event.action == "consent.revoked"
    assert audit_event.details == {
        "consent_type": "outbound_call",
        "status": "revoked",
    }
    assert "+971501234567" not in str(audit_event.details)

    blocked = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "consent-revoked-call-0001"},
        json={
            "agent_id": agent_response.json()["id"],
            "to_number": "+971501234567",
        },
    )

    assert blocked.status_code == 409
    assert blocked.json()["detail"] == "Outbound consent has been revoked for this phone number"
    assert await db.scalar(select(func.count()).select_from(Call)) == 0


@pytest.mark.asyncio
async def test_later_grant_supersedes_revocation_for_same_scope(client, auth_headers):
    agent_response = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Consent renewal agent",
            "system_prompt": "Use only the latest consent decision.",
        },
    )
    for consent_status in ("revoked", "granted"):
        response = await client.post(
            "/api/v1/compliance/consent",
            headers=auth_headers,
            json={
                "phone_number": "+971501234568",
                "consent_type": "outbound_call",
                "status": consent_status,
                "evidence": {"method": "audited_test"},
            },
        )
        assert response.status_code == 201

    response = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "consent-renewed-call-0001"},
        json={
            "agent_id": agent_response.json()["id"],
            "to_number": "+971501234568",
        },
    )

    # The request passes consent enforcement and reaches the independent
    # provider-readiness gate for this intentionally unprovisioned test agent.
    assert response.status_code == 409
    assert "Provision this agent" in response.json()["detail"]


@pytest.mark.asyncio
async def test_consent_endpoint_rejects_unknown_scope_and_status(client, auth_headers):
    response = await client.post(
        "/api/v1/compliance/consent",
        headers=auth_headers,
        json={
            "phone_number": "+971501234569",
            "consent_type": "anything",
            "status": "maybe",
        },
    )

    assert response.status_code == 422


@pytest.mark.asyncio
async def test_consent_history_is_bounded_newest_first_and_includes_audit_timestamps(
    client,
    auth_headers,
):
    created_ids = []
    for suffix in range(3):
        response = await client.post(
            "/api/v1/compliance/consent",
            headers=auth_headers,
            json={
                "phone_number": f"+97150123457{suffix}",
                "consent_type": "data_processing",
                "status": "granted",
                "evidence": {"method": "audited_test", "reference": f"case-{suffix}"},
            },
        )
        assert response.status_code == 201
        created_ids.append(response.json()["id"])

    response = await client.get(
        "/api/v1/compliance/consent?page=1&page_size=2",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == list(reversed(created_ids[-2:]))
    assert all(item["granted_at"] and item["created_at"] for item in response.json())
