"""Auth endpoint tests."""

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.endpoints import auth as auth_endpoint
from app.core.config import settings
from app.core.security import (
    create_access_token,
    decode_token,
    generate_api_key,
    hash_api_key,
    hash_password,
)
from app.models.audit import AuditEvent
from app.models.tenant import Tenant
from app.models.user import ApiKey, RefreshSession, User, UserInvitation


@pytest.mark.asyncio
async def test_auth_rejects_declared_oversized_body_before_parsing(client: AsyncClient):
    response = await client.post(
        "/api/v1/auth/login",
        content=b"{}",
        headers={
            "Content-Type": "application/json",
            "Content-Length": str(settings.max_request_body_bytes + 1),
        },
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body is too large"}
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.asyncio
async def test_auth_rejects_chunked_oversized_body_without_content_length(
    client: AsyncClient,
):
    async def oversized_chunks():
        remaining = settings.max_request_body_bytes + 1
        chunk = b"x" * (256 * 1024)
        while remaining:
            emitted = chunk[:remaining]
            remaining -= len(emitted)
            yield emitted

    response = await client.post(
        "/api/v1/auth/login",
        content=oversized_chunks(),
        headers={"Content-Type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body is too large"}


@pytest.mark.asyncio
async def test_register(client: AsyncClient):
    response = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "new@example.com",
            "password": "securepass123",
            "full_name": "New User",
            "tenant_name": "New Corp",
            "tenant_slug": "new-corp",
        },
    )
    assert response.status_code == 201
    data = response.json()
    assert "access_token" in data
    assert "refresh_token" in data
    assert data["role"] == "owner"


@pytest.mark.asyncio
async def test_invite_only_registration_is_closed_but_invitations_remain_available(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
    db: AsyncSession,
):
    monkeypatch.setattr(auth_endpoint.settings, "registration_mode", "invite_only")

    registration = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "public@example.com",
            "password": "securepass123",
            "full_name": "Public User",
            "tenant_name": "Public Corp",
            "tenant_slug": "public-corp",
        },
    )
    assert registration.status_code == 403
    assert registration.json()["detail"] == auth_endpoint.REGISTRATION_UNAVAILABLE_DETAIL

    invitation = await client.post(
        "/api/v1/auth/invite",
        headers=auth_headers,
        json={"email": "invited@example.com", "full_name": "Invited User"},
    )
    assert invitation.status_code == 201
    assert await db.scalar(select(func.count()).select_from(User)) == 1

    policy = await client.get("/api/v1/auth/registration-policy")
    assert policy.json() == {
        "mode": "invite_only",
        "registration_available": False,
        "message": "New accounts require an invitation from a workspace owner.",
    }


@pytest.mark.asyncio
async def test_bootstrap_registration_does_not_disclose_configured_email(
    client: AsyncClient,
    monkeypatch,
    db: AsyncSession,
):
    configured_email = "bootstrap-owner@example.com"
    monkeypatch.setattr(auth_endpoint.settings, "registration_mode", "bootstrap")
    monkeypatch.setattr(auth_endpoint.settings, "bootstrap_owner_email", configured_email)

    policy = await client.get("/api/v1/auth/registration-policy")
    assert policy.status_code == 200
    assert policy.json()["mode"] == "bootstrap"
    assert policy.json()["registration_available"] is True
    assert configured_email not in policy.text

    rejected = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "someone-else@example.com",
            "password": "securepass123",
            "full_name": "Wrong Owner",
            "tenant_name": "Wrong Corp",
            "tenant_slug": "wrong-corp",
        },
    )
    assert rejected.status_code == 403
    assert rejected.json()["detail"] == auth_endpoint.REGISTRATION_UNAVAILABLE_DETAIL
    assert configured_email not in rejected.text
    assert await db.scalar(select(func.count()).select_from(User)) == 0


@pytest.mark.asyncio
async def test_bootstrap_registration_is_globally_single_use_under_concurrency(
    client: AsyncClient,
    monkeypatch,
    db: AsyncSession,
):
    configured_email = "first-owner@example.com"
    monkeypatch.setattr(auth_endpoint.settings, "registration_mode", "bootstrap")
    monkeypatch.setattr(auth_endpoint.settings, "bootstrap_owner_email", configured_email)

    def payload(slug: str) -> dict[str, str]:
        return {
            "email": "FIRST-OWNER@EXAMPLE.COM",
            "password": "securepass123",
            "full_name": "First Owner",
            "tenant_name": slug,
            "tenant_slug": slug,
        }

    first, second = await asyncio.gather(
        client.post("/api/v1/auth/register", json=payload("bootstrap-one")),
        client.post("/api/v1/auth/register", json=payload("bootstrap-two")),
    )
    assert sorted([first.status_code, second.status_code]) == [201, 403]
    loser = first if first.status_code == 403 else second
    assert loser.json()["detail"] == auth_endpoint.REGISTRATION_UNAVAILABLE_DETAIL
    assert configured_email not in loser.text
    assert await db.scalar(select(func.count()).select_from(User)) == 1
    assert await db.scalar(select(func.count()).select_from(Tenant)) == 1
    assert await db.scalar(select(func.count()).select_from(RefreshSession)) == 1

    policy = await client.get("/api/v1/auth/registration-policy")
    assert policy.json()["mode"] == "invite_only"
    assert policy.json()["registration_available"] is False

    after_bootstrap = await client.post("/api/v1/auth/register", json=payload("bootstrap-three"))
    assert after_bootstrap.status_code == 403
    assert after_bootstrap.json()["detail"] == auth_endpoint.REGISTRATION_UNAVAILABLE_DETAIL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("password", "short"),
        ("full_name", "   "),
        ("tenant_name", "   "),
        ("tenant_slug", "   "),
    ],
)
async def test_register_rejects_weak_or_blank_identity_fields(
    client: AsyncClient,
    field: str,
    value: str,
):
    payload = {
        "email": "validation@example.com",
        "password": "securepass123",
        "full_name": "Valid Name",
        "tenant_name": "Valid Corp",
        "tenant_slug": "valid-corp",
    }
    payload[field] = value
    response = await client.post("/api/v1/auth/register", json=payload)
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_login(client: AsyncClient, user, tenant):
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "test@testcorp.com", "password": "testpassword"},
    )
    assert response.status_code == 200
    data = response.json()
    assert data["user_id"] == str(user.id)
    assert data["tenant_id"] == str(tenant.id)


@pytest.mark.asyncio
async def test_unknown_login_uses_dummy_password_hash(client: AsyncClient, monkeypatch):
    checked_hashes: list[str] = []

    def fake_verify_password(_plain_password: str, password_hash: str) -> bool:
        checked_hashes.append(password_hash)
        return False

    monkeypatch.setattr(auth_endpoint, "verify_password", fake_verify_password)
    response = await client.post(
        "/api/v1/auth/login",
        json={"email": "missing@example.com", "password": "wrong-password"},
    )

    assert response.status_code == 401
    assert checked_hashes == [auth_endpoint.DUMMY_PASSWORD_HASH]


@pytest.mark.asyncio
async def test_email_identity_is_canonical_across_registration_and_login(
    client: AsyncClient,
    db: AsyncSession,
):
    registration = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "  Mixed.Case@Example.COM  ",
            "password": "securepass123",
            "full_name": "Mixed Case",
            "tenant_name": "Canonical Corp",
            "tenant_slug": "canonical-corp",
        },
    )
    assert registration.status_code == 201

    stored = (
        await db.execute(select(User).where(User.email == "mixed.case@example.com"))
    ).scalar_one()
    assert stored.email == "mixed.case@example.com"

    login = await client.post(
        "/api/v1/auth/login",
        json={"email": "MIXED.CASE@EXAMPLE.COM", "password": "securepass123"},
    )
    assert login.status_code == 200
    assert login.json()["user_id"] == str(stored.id)

    duplicate = await client.post(
        "/api/v1/auth/register",
        json={
            "email": "mixed.case@EXAMPLE.com",
            "password": "securepass123",
            "full_name": "Duplicate",
            "tenant_name": "Duplicate Corp",
            "tenant_slug": "duplicate-canonical-corp",
        },
    )
    assert duplicate.status_code == 400
    assert duplicate.json()["detail"] == "Email already registered"


@pytest.mark.asyncio
async def test_canonical_email_unique_index_is_enforced_by_sqlite_metadata(
    tenant,
    user,
    db: AsyncSession,
):
    assert user.email == "test@testcorp.com"
    duplicate = User(
        tenant_id=tenant.id,
        email="TEST@TestCorp.COM",
        hashed_password=hash_password("differentpassword"),
        full_name="Canonical Duplicate",
        role="member",
    )
    db.add(duplicate)
    with pytest.raises(IntegrityError) as exc_info:
        await db.flush()
    assert (
        "uq_users_email_canonical" in str(exc_info.value) or "UNIQUE" in str(exc_info.value).upper()
    )
    await db.rollback()


@pytest.mark.asyncio
async def test_refresh_rotation_is_hashed_race_safe_and_detects_late_reuse(
    client: AsyncClient,
    user,
    db: AsyncSession,
):
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "testpassword"},
    )
    original = login.json()["refresh_token"]
    original_claims = decode_token(original)
    assert original_claims is not None

    original_session = (
        await db.execute(
            select(RefreshSession).where(RefreshSession.id == uuid.UUID(original_claims["jti"]))
        )
    ).scalar_one()
    assert original_session.token_hash == hashlib.sha256(original.encode()).hexdigest()
    assert original not in original_session.token_hash

    first_rotation = await client.post("/api/v1/auth/refresh", json={"refresh_token": original})
    assert first_rotation.status_code == 200
    successor = first_rotation.json()["refresh_token"]
    successor_claims = decode_token(successor)
    assert successor_claims is not None
    assert successor_claims["jti"] != original_claims["jti"]
    assert successor_claims["family_id"] == original_claims["family_id"]
    assert successor_claims["exp"] == original_claims["exp"]

    # An immediate loser in a normal multi-tab race is rejected without
    # destroying the winner's successor.
    immediate_replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": original})
    assert immediate_replay.status_code == 409
    second_rotation = await client.post("/api/v1/auth/refresh", json={"refresh_token": successor})
    assert second_rotation.status_code == 200
    latest = second_rotation.json()["refresh_token"]

    # Once the short coordination grace has elapsed, reuse is a theft signal
    # and revokes the still-active descendant in this family.
    successor_session = (
        await db.execute(
            select(RefreshSession).where(RefreshSession.id == uuid.UUID(successor_claims["jti"]))
        )
    ).scalar_one()
    successor_session.revoked_at = datetime.now(UTC) - timedelta(seconds=10)
    await db.commit()

    late_replay = await client.post("/api/v1/auth/refresh", json={"refresh_token": successor})
    assert late_replay.status_code == 401
    assert "reuse detected" in late_replay.json()["detail"].lower()
    assert (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": latest})
    ).status_code == 401

    audit = (
        await db.execute(select(AuditEvent).where(AuditEvent.action == "session.reuse_detected"))
    ).scalar_one()
    assert audit.tenant_id == user.tenant_id
    assert "token" not in str(audit.details).lower()


@pytest.mark.asyncio
async def test_concurrent_refresh_race_preserves_the_winning_successor(
    client: AsyncClient,
    user,
):
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "testpassword"},
    )
    refresh_token = login.json()["refresh_token"]

    first, second = await asyncio.gather(
        client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}),
        client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token}),
    )
    assert sorted([first.status_code, second.status_code]) == [200, 409]
    winner = first if first.status_code == 200 else second
    follow_up = await client.post(
        "/api/v1/auth/refresh",
        json={"refresh_token": winner.json()["refresh_token"]},
    )
    assert follow_up.status_code == 200


@pytest.mark.asyncio
async def test_logout_revokes_family_without_access_token_and_is_idempotent(
    client: AsyncClient,
    user,
    db: AsyncSession,
):
    login = await client.post(
        "/api/v1/auth/login",
        json={"email": user.email, "password": "testpassword"},
    )
    refresh_token = login.json()["refresh_token"]

    logout = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert logout.status_code == 204
    repeated = await client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert repeated.status_code == 204
    assert (
        await client.post("/api/v1/auth/refresh", json={"refresh_token": refresh_token})
    ).status_code == 401

    events = (
        (await db.execute(select(AuditEvent).where(AuditEvent.action == "session.logged_out")))
        .scalars()
        .all()
    )
    assert len(events) == 1
    assert events[0].actor_user_id == user.id

    # Unknown tokens receive the same response and do not reveal validity.
    unknown = await client.post("/api/v1/auth/logout", json={"refresh_token": "x" * 32})
    assert unknown.status_code == 204


@pytest.mark.asyncio
async def test_get_me(client: AsyncClient, auth_headers):
    response = await client.get("/api/v1/auth/me", headers=auth_headers)
    assert response.status_code == 200
    data = response.json()
    assert data["email"] == "test@testcorp.com"
    assert data["role"] == "owner"
    assert data["tenant_name"] == "Test Corp"
    assert data["tenant_slug"] == "test-corp"


@pytest.mark.asyncio
async def test_unauthorized_without_token(client: AsyncClient):
    response = await client.get("/api/v1/auth/me")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_invite_rejects_owner_role(client: AsyncClient, auth_headers):
    response = await client.post(
        "/api/v1/auth/invite",
        headers=auth_headers,
        json={"email": "owner2@example.com", "full_name": "Second Owner", "role": "owner"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_invite_rejects_blank_name(client: AsyncClient, auth_headers):
    response = await client.post(
        "/api/v1/auth/invite",
        headers=auth_headers,
        json={"email": "blank-name@example.com", "full_name": "   ", "role": "member"},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_team_listing_is_tenant_scoped(
    client: AsyncClient,
    auth_headers,
    db: AsyncSession,
):
    other_tenant = Tenant(name="Other Corp", slug="other-corp")
    db.add(other_tenant)
    await db.flush()
    db.add(
        User(
            tenant_id=other_tenant.id,
            email="other@example.com",
            hashed_password=hash_password("otherpassword"),
            full_name="Other User",
            role="owner",
        )
    )
    await db.commit()

    response = await client.get("/api/v1/auth/users", headers=auth_headers)
    assert response.status_code == 200
    users = response.json()
    assert [item["email"] for item in users] == ["test@testcorp.com"]


@pytest.mark.asyncio
async def test_owner_can_update_member_but_not_self(
    client: AsyncClient,
    auth_headers,
    tenant,
    user,
    db: AsyncSession,
):
    member = User(
        tenant_id=tenant.id,
        email="member@example.com",
        hashed_password=hash_password("memberpassword"),
        full_name="Member User",
        role="member",
    )
    db.add(member)
    await db.commit()
    await db.refresh(member)

    response = await client.patch(
        f"/api/v1/auth/users/{member.id}",
        headers=auth_headers,
        json={"role": "viewer", "is_active": False},
    )
    assert response.status_code == 200
    assert response.json()["role"] == "viewer"
    assert response.json()["is_active"] is False

    self_response = await client.patch(
        f"/api/v1/auth/users/{user.id}",
        headers=auth_headers,
        json={"is_active": False},
    )
    assert self_response.status_code == 400


@pytest.mark.asyncio
async def test_admin_cannot_invite_admin(
    client: AsyncClient,
    tenant,
    db: AsyncSession,
):
    admin = User(
        tenant_id=tenant.id,
        email="admin@example.com",
        hashed_password=hash_password("adminpassword"),
        full_name="Admin User",
        role="admin",
    )
    db.add(admin)
    await db.commit()
    await db.refresh(admin)
    admin_headers = {
        "Authorization": f"Bearer {create_access_token(admin.id, tenant.id, admin.role)}"
    }

    response = await client.post(
        "/api/v1/auth/invite",
        headers=admin_headers,
        json={"email": "admin2@example.com", "full_name": "Admin Two", "role": "admin"},
    )
    assert response.status_code == 403


@pytest.mark.asyncio
async def test_invitation_token_is_hashed_listed_without_secret_and_accepted_once(
    client: AsyncClient,
    auth_headers,
    tenant,
    db: AsyncSession,
):
    create_response = await client.post(
        "/api/v1/auth/invite",
        headers=auth_headers,
        json={
            "email": "invitee@example.com",
            "full_name": "Invited Teammate",
            "role": "member",
        },
    )
    assert create_response.status_code == 201
    created = create_response.json()
    assert created["status"] == "pending"
    assert created["token"]

    row_result = await db.execute(
        select(UserInvitation).where(UserInvitation.id == uuid.UUID(created["id"]))
    )
    invitation = row_result.scalar_one()
    assert invitation.token_hash == hashlib.sha256(created["token"].encode()).hexdigest()
    assert invitation.token_hash != created["token"]

    list_response = await client.get("/api/v1/auth/invitations", headers=auth_headers)
    assert list_response.status_code == 200
    listed = list_response.json()
    assert [item["email"] for item in listed] == ["invitee@example.com"]
    assert "token" not in listed[0]

    accept_response = await client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": created["token"], "password": "newsecurepassword"},
    )
    assert accept_response.status_code == 200
    assert accept_response.json()["tenant_id"] == str(tenant.id)
    assert accept_response.json()["role"] == "member"

    second_accept = await client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": created["token"], "password": "anotherpassword"},
    )
    assert second_accept.status_code == 409
    assert second_accept.json()["detail"] == "Invitation has already been accepted"

    team_response = await client.get("/api/v1/auth/users", headers=auth_headers)
    assert {item["email"] for item in team_response.json()} == {
        "test@testcorp.com",
        "invitee@example.com",
    }

    audit_response = await client.get(
        "/api/v1/audit-events?resource_type=invitation", headers=auth_headers
    )
    events = audit_response.json()
    assert [event["action"] for event in events] == [
        "invitation.accepted",
        "invitation.created",
    ]
    serialized_events = str(events)
    assert created["token"] not in serialized_events
    assert invitation.token_hash not in serialized_events


@pytest.mark.asyncio
async def test_invitation_revoke_and_expiry_are_enforced(
    client: AsyncClient,
    auth_headers,
    db: AsyncSession,
):
    revoked_create = await client.post(
        "/api/v1/auth/invite",
        headers=auth_headers,
        json={"email": "revoked@example.com", "full_name": "Revoked User"},
    )
    revoked = revoked_create.json()
    revoke_response = await client.delete(
        f"/api/v1/auth/invitations/{revoked['id']}", headers=auth_headers
    )
    assert revoke_response.status_code == 204

    revoked_accept = await client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": revoked["token"], "password": "newsecurepassword"},
    )
    assert revoked_accept.status_code == 410
    assert revoked_accept.json()["detail"] == "Invitation has been revoked"

    expired_create = await client.post(
        "/api/v1/auth/invite",
        headers=auth_headers,
        json={"email": "expired@example.com", "full_name": "Expired User"},
    )
    expired = expired_create.json()
    result = await db.execute(
        select(UserInvitation).where(UserInvitation.id == uuid.UUID(expired["id"]))
    )
    invitation = result.scalar_one()
    invitation.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db.commit()

    expired_accept = await client.post(
        "/api/v1/auth/invitations/accept",
        json={"token": expired["token"], "password": "newsecurepassword"},
    )
    assert expired_accept.status_code == 410
    assert expired_accept.json()["detail"] == "Invitation has expired"

    list_response = await client.get("/api/v1/auth/invitations", headers=auth_headers)
    statuses = {item["email"]: item["status"] for item in list_response.json()}
    assert statuses == {
        "expired@example.com": "expired",
        "revoked@example.com": "revoked",
    }


@pytest.mark.asyncio
async def test_expired_invitation_is_superseded_by_canonical_reinvite(
    client: AsyncClient,
    auth_headers,
    db: AsyncSession,
):
    first = await client.post(
        "/api/v1/auth/invite",
        headers=auth_headers,
        json={"email": "ReInvite@Example.COM", "full_name": "First Invite"},
    )
    assert first.status_code == 201
    assert first.json()["email"] == "reinvite@example.com"

    invitation = (
        await db.execute(
            select(UserInvitation).where(UserInvitation.id == uuid.UUID(first.json()["id"]))
        )
    ).scalar_one()
    invitation.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db.commit()

    replacement = await client.post(
        "/api/v1/auth/invite",
        headers=auth_headers,
        json={"email": "  REINVITE@example.com ", "full_name": "Replacement Invite"},
    )
    assert replacement.status_code == 201
    assert replacement.json()["email"] == "reinvite@example.com"
    await db.refresh(invitation)
    assert invitation.revoked_at is not None
    assert invitation.resolution_reason == "expired"
    listed = await client.get("/api/v1/auth/invitations", headers=auth_headers)
    statuses = {item["id"]: item["status"] for item in listed.json()}
    assert statuses[first.json()["id"]] == "expired"


@pytest.mark.asyncio
async def test_concurrent_canonical_invites_have_one_pending_winner(
    client: AsyncClient,
    auth_headers,
    db: AsyncSession,
):
    first, second = await asyncio.gather(
        client.post(
            "/api/v1/auth/invite",
            headers=auth_headers,
            json={"email": "InviteRace@Example.com", "full_name": "First"},
        ),
        client.post(
            "/api/v1/auth/invite",
            headers=auth_headers,
            json={"email": "inviterace@EXAMPLE.COM", "full_name": "Second"},
        ),
    )
    assert sorted([first.status_code, second.status_code]) == [201, 409]
    pending = (
        (
            await db.execute(
                select(UserInvitation).where(
                    UserInvitation.email == "inviterace@example.com",
                    UserInvitation.accepted_at.is_(None),
                    UserInvitation.revoked_at.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    assert len(pending) == 1


@pytest.mark.asyncio
async def test_invitations_are_tenant_scoped(
    client: AsyncClient,
    auth_headers,
    db: AsyncSession,
):
    other_tenant = Tenant(name="Invitation Other", slug="invitation-other")
    db.add(other_tenant)
    await db.flush()
    other_owner = User(
        tenant_id=other_tenant.id,
        email="invitation-owner@example.com",
        hashed_password=hash_password("securepassword"),
        full_name="Other Owner",
        role="owner",
    )
    db.add(other_owner)
    await db.flush()
    other_invitation = UserInvitation(
        tenant_id=other_tenant.id,
        invited_by_user_id=other_owner.id,
        email="other-invitee@example.com",
        full_name="Other Invitee",
        role="member",
        token_hash=hashlib.sha256(b"other-tenant-secret-token").hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(days=1),
    )
    db.add(other_invitation)
    await db.commit()
    await db.refresh(other_invitation)

    list_response = await client.get("/api/v1/auth/invitations", headers=auth_headers)
    assert list_response.status_code == 200
    assert list_response.json() == []

    revoke_response = await client.delete(
        f"/api/v1/auth/invitations/{other_invitation.id}", headers=auth_headers
    )
    assert revoke_response.status_code == 404


@pytest.mark.asyncio
async def test_api_key_authenticates_is_revealed_once_and_can_be_revoked(
    client: AsyncClient,
    auth_headers,
    tenant,
    db: AsyncSession,
):
    create_response = await client.post(
        "/api/v1/auth/api-keys",
        headers=auth_headers,
        json={"name": "Production"},
    )
    assert create_response.status_code == 201
    created = create_response.json()
    assert created["key"].startswith("vai_")
    raw_key = created["key"]
    assert uuid.UUID(hex=raw_key.split("_", 2)[1]) == uuid.UUID(created["id"])

    stored_result = await db.execute(select(ApiKey).where(ApiKey.id == uuid.UUID(created["id"])))
    stored_key = stored_result.scalar_one()
    assert stored_key.key_hash == hashlib.sha256(raw_key.encode()).hexdigest()
    assert stored_key.key_hash == hash_api_key(raw_key)
    assert raw_key not in stored_key.key_hash

    me_response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert me_response.status_code == 200
    assert me_response.json()["email"] == "test@testcorp.com"
    assert me_response.json()["tenant_id"] == str(tenant.id)
    assert me_response.json()["role"] == "viewer"
    assert created["expires_at"] is not None

    privileged_response = await client.get(
        "/api/v1/audit-events", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert privileged_response.status_code == 403
    mutation_response = await client.post(
        "/api/v1/agents",
        headers={"Authorization": f"Bearer {raw_key}"},
        json={"name": "Forbidden API mutation", "system_prompt": "A valid blocked prompt."},
    )
    assert mutation_response.status_code == 403

    await db.refresh(stored_key)
    assert stored_key.last_used_at is not None

    list_response = await client.get("/api/v1/auth/api-keys", headers=auth_headers)
    assert list_response.status_code == 200
    listed = list_response.json()
    assert len(listed) == 1
    assert "key" not in listed[0]
    serialized_list = str(listed)
    assert raw_key not in serialized_list
    assert stored_key.key_hash not in serialized_list

    revoke_response = await client.delete(
        f"/api/v1/auth/api-keys/{created['id']}", headers=auth_headers
    )
    assert revoke_response.status_code == 204

    after_revoke = await client.get("/api/v1/auth/api-keys", headers=auth_headers)
    assert after_revoke.json()[0]["is_active"] is False

    revoked_auth = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {raw_key}"}
    )
    assert revoked_auth.status_code == 401

    audit_response = await client.get(
        "/api/v1/audit-events?resource_type=api_key", headers=auth_headers
    )
    assert audit_response.status_code == 200
    assert [event["action"] for event in audit_response.json()] == [
        "api_key.revoked",
        "api_key.created",
    ]
    events = audit_response.json()
    assert all("key" not in event["details"] for event in events)
    serialized_events = str(events)
    assert raw_key not in serialized_events
    assert stored_key.key_hash not in serialized_events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "malformed_key",
    [
        "vai_legacy-secret-without-an-id",
        "vai_not-a-uuid_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        f"vai_{uuid.uuid4().hex}_short",
    ],
)
async def test_malformed_api_keys_fail_closed(client: AsyncClient, malformed_key: str):
    response = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {malformed_key}"}
    )
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid API key"


@pytest.mark.asyncio
async def test_expired_api_key_and_inactive_owner_fail_closed(
    client: AsyncClient,
    auth_headers,
    user,
    db: AsyncSession,
):
    expired_response = await client.post(
        "/api/v1/auth/api-keys",
        headers=auth_headers,
        json={"name": "Expiring key"},
    )
    expired = expired_response.json()
    result = await db.execute(select(ApiKey).where(ApiKey.id == uuid.UUID(expired["id"])))
    api_key = result.scalar_one()
    api_key.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db.commit()

    expired_auth = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {expired['key']}"},
    )
    assert expired_auth.status_code == 401

    active_response = await client.post(
        "/api/v1/auth/api-keys",
        headers=auth_headers,
        json={"name": "Inactive owner key"},
    )
    active_key = active_response.json()["key"]
    user.is_active = False
    await db.commit()

    inactive_user_auth = await client.get(
        "/api/v1/auth/me", headers={"Authorization": f"Bearer {active_key}"}
    )
    assert inactive_user_auth.status_code == 401


@pytest.mark.asyncio
async def test_api_key_identifier_and_secret_cannot_be_mixed_across_tenants(
    client: AsyncClient,
    auth_headers,
    db: AsyncSession,
):
    current_response = await client.post(
        "/api/v1/auth/api-keys",
        headers=auth_headers,
        json={"name": "Current tenant key"},
    )
    current_key = current_response.json()["key"]
    current_secret = current_key.split("_", 2)[2]

    other_tenant = Tenant(name="API Key Other", slug="api-key-other")
    db.add(other_tenant)
    await db.flush()
    other_user = User(
        tenant_id=other_tenant.id,
        email="api-key-owner@example.com",
        hashed_password=hash_password("securepassword"),
        full_name="API Key Owner",
        role="admin",
    )
    db.add(other_user)
    await db.flush()
    other_key_id = uuid.uuid4()
    other_raw_key = generate_api_key(other_key_id)
    db.add(
        ApiKey(
            id=other_key_id,
            tenant_id=other_tenant.id,
            user_id=other_user.id,
            key_hash=hash_api_key(other_raw_key),
            name="Other tenant key",
        )
    )
    await db.commit()

    manipulated_key = f"vai_{other_key_id.hex}_{current_secret}"
    manipulated_response = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {manipulated_key}"},
    )
    assert manipulated_response.status_code == 401

    valid_other_response = await client.get(
        "/api/v1/auth/me",
        headers={"Authorization": f"Bearer {other_raw_key}"},
    )
    assert valid_other_response.status_code == 200
    assert valid_other_response.json()["tenant_id"] == str(other_tenant.id)
    assert valid_other_response.json()["role"] == "viewer"
