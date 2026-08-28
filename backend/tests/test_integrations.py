"""Integration control-plane security tests."""

import asyncio
import json
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.v1.endpoints.integrations import (
    _locked_tenant_integration_statement,
    update_integration,
)
from app.core.config import settings
from app.core.security import create_access_token, hash_password
from app.middleware.tenant import CurrentUser
from app.models.integration import Integration
from app.models.tenant import Tenant
from app.models.user import User
from app.schemas.integration import IntegrationUpdate
from app.services.integration_security import (
    INTEGRATION_CONFIG_STORAGE_VERSION,
    PUBLIC_URL_REDACTION_PLACEHOLDER,
    IntegrationConfigUnavailableError,
    decrypt_integration_config,
    encrypt_integration_config,
    load_integration_config,
)


def test_envelope_key_derivation_prefers_dedicated_key_with_secret_key_fallback(monkeypatch):
    config = {"api_key": "key-derivation-secret", "region": "global"}

    monkeypatch.setattr(settings, "integration_encryption_key", "")
    monkeypatch.setattr(settings, "secret_key", "compatibility-secret-key")
    fallback_envelope = encrypt_integration_config(config)
    assert decrypt_integration_config(fallback_envelope) == config
    assert "key-derivation-secret" not in fallback_envelope

    monkeypatch.setattr(settings, "integration_encryption_key", "dedicated-key-one")
    assert decrypt_integration_config(fallback_envelope) == config
    dedicated_envelope = encrypt_integration_config(config)
    monkeypatch.setattr(settings, "integration_encryption_key", "dedicated-key-two")
    with pytest.raises(IntegrationConfigUnavailableError):
        decrypt_integration_config(dedicated_envelope)


async def _create_webhook(client: AsyncClient, auth_headers: dict) -> dict:
    response = await client.post(
        "/api/v1/integrations",
        headers=auth_headers,
        json={
            "name": "Production webhook",
            "integration_type": "WEBHOOK",
            "config": {
                "url": (
                    "https://hooks.vendor.com/services/team/path-secret-value/events"
                    "?opaque=opaque-query-secret&token=url-secret&workspace=acme"
                ),
                "events": ["call.completed"],
                "signing_secret": "signing-secret-value",
                "api_key": "api-secret-value",
                "secretKey": "generic-secret-value",
                "access_key_id": "unknown-access-key-secret",
                "client_certificate": "unknown-certificate-secret",
                "provider_blob": "opaque-provider-secret",
                "headers": {
                    "Authorization": "Bearer header-secret-value",
                    "X-Trace-Id": "visible-value",
                },
            },
        },
    )
    assert response.status_code == 201
    return response.json()


@pytest.mark.asyncio
async def test_integration_secrets_are_write_only(
    client: AsyncClient,
    auth_headers,
    db: AsyncSession,
):
    created = await _create_webhook(client, auth_headers)

    assert created["integration_type"] == "webhook"
    assert created["config"] == {
        "url": PUBLIC_URL_REDACTION_PLACEHOLDER,
        "events": ["call.completed"],
    }
    assert created["secret_fields"] == [
        "api_key",
        "headers.Authorization",
        "secretKey",
        "signing_secret",
        "url",
    ]
    serialized = str(created)
    assert "signing-secret-value" not in serialized
    assert "api-secret-value" not in serialized
    assert "header-secret-value" not in serialized
    assert "generic-secret-value" not in serialized
    assert "url-secret" not in serialized
    assert "path-secret-value" not in serialized
    assert "opaque-query-secret" not in serialized
    assert "unknown-access-key-secret" not in serialized
    assert "unknown-certificate-secret" not in serialized
    assert "opaque-provider-secret" not in serialized

    result = await db.execute(select(Integration).where(Integration.id == UUID(created["id"])))
    stored = result.scalar_one()
    assert stored.config == created["config"]
    assert stored.encrypted_config is not None
    assert stored.encrypted_config.startswith("fernet:v1:")
    stored_json = json.dumps(stored.config, sort_keys=True)
    for secret in (
        "signing-secret-value",
        "api-secret-value",
        "header-secret-value",
        "generic-secret-value",
        "url-secret",
        "path-secret-value",
        "opaque-query-secret",
        "unknown-access-key-secret",
        "unknown-certificate-secret",
        "opaque-provider-secret",
    ):
        assert secret not in stored_json
        assert secret not in stored.encrypted_config

    hydrated = load_integration_config(stored.config, stored.encrypted_config)
    assert hydrated["api_key"] == "api-secret-value"
    assert hydrated["signing_secret"] == "signing-secret-value"
    assert hydrated["secretKey"] == "generic-secret-value"
    assert hydrated["headers"]["Authorization"] == "Bearer header-secret-value"
    assert hydrated["headers"]["X-Trace-Id"] == "visible-value"
    assert "token=url-secret" in hydrated["url"]
    assert "/path-secret-value/" in hydrated["url"]
    assert "opaque=opaque-query-secret" in hydrated["url"]
    assert stored.config_encryption_version == INTEGRATION_CONFIG_STORAGE_VERSION

    listed = await client.get("/api/v1/integrations", headers=auth_headers)
    assert listed.status_code == 200
    assert listed.json() == [created]


@pytest.mark.asyncio
async def test_secret_update_masks_preserve_and_explicit_clear_removes(
    client: AsyncClient,
    auth_headers,
    db: AsyncSession,
):
    created = await _create_webhook(client, auth_headers)

    masked_update = await client.patch(
        f"/api/v1/integrations/{created['id']}",
        headers=auth_headers,
        json={
            "config": {
                "url": created["config"]["url"],
                "api_key": "********",
                "signing_secret": "",
                "headers": {"Authorization": "[REDACTED]"},
                "events": ["call.completed", "call.analytics_updated"],
            }
        },
    )
    assert masked_update.status_code == 200
    assert masked_update.json()["secret_fields"] == created["secret_fields"]

    result = await db.execute(select(Integration).where(Integration.id == UUID(created["id"])))
    stored = result.scalar_one()
    hydrated = load_integration_config(stored.config, stored.encrypted_config)
    assert hydrated["api_key"] == "api-secret-value"
    assert hydrated["signing_secret"] == "signing-secret-value"
    assert hydrated["headers"]["Authorization"] == "Bearer header-secret-value"
    assert hydrated["events"] == ["call.completed", "call.analytics_updated"]

    replaced = await client.patch(
        f"/api/v1/integrations/{created['id']}",
        headers=auth_headers,
        json={"config": {"api_key": "replacement-secret-value"}},
    )
    assert replaced.status_code == 200
    assert "replacement-secret-value" not in replaced.text

    cleared = await client.patch(
        f"/api/v1/integrations/{created['id']}",
        headers=auth_headers,
        json={"clear_secrets": ["api_key", "headers.Authorization"]},
    )
    assert cleared.status_code == 200
    assert cleared.json()["secret_fields"] == [
        "secretKey",
        "signing_secret",
        "url",
    ]

    await db.refresh(stored)
    hydrated = load_integration_config(stored.config, stored.encrypted_config)
    assert "api_key" not in hydrated
    assert "Authorization" not in hydrated["headers"]
    assert hydrated["headers"]["X-Trace-Id"] == "visible-value"
    assert "/path-secret-value/" in hydrated["url"]
    assert "replacement-secret-value" not in json.dumps(stored.config)
    assert "replacement-secret-value" not in stored.encrypted_config


@pytest.mark.asyncio
async def test_corrupt_envelope_fails_closed_without_leaking(
    client: AsyncClient,
    auth_headers,
    db: AsyncSession,
):
    created = await _create_webhook(client, auth_headers)
    stored = await db.get(Integration, UUID(created["id"]))
    assert stored is not None
    stored.encrypted_config = "fernet:v1:corrupt-secret-looking-ciphertext"
    await db.commit()

    listed = await client.get("/api/v1/integrations", headers=auth_headers)
    updated = await client.patch(
        f"/api/v1/integrations/{created['id']}",
        headers=auth_headers,
        json={"name": "Must not bypass authentication"},
    )

    assert listed.status_code == 500
    assert updated.status_code == 500
    assert listed.json() == {"detail": "Integration configuration is unavailable"}
    assert updated.json() == {"detail": "Integration configuration is unavailable"}
    assert "signing-secret-value" not in listed.text + updated.text
    await db.refresh(stored)
    assert stored.name == "Production webhook"


@pytest.mark.asyncio
async def test_legacy_plaintext_row_is_sanitized_and_encrypted_on_first_mutation(
    client: AsyncClient,
    auth_headers,
    db: AsyncSession,
    tenant: Tenant,
):
    legacy = Integration(
        tenant_id=tenant.id,
        name="Legacy webhook",
        integration_type="webhook",
        config={
            "url": "https://hooks.vendor.com/legacy?token=legacy-url-secret",
            "events": ["call.completed"],
            "signing_secret": "legacy-signing-secret",
            "api_key": "legacy-api-secret",
        },
        encrypted_config=None,
    )
    db.add(legacy)
    await db.commit()

    response = await client.patch(
        f"/api/v1/integrations/{legacy.id}",
        headers=auth_headers,
        json={"name": "Migrated webhook"},
    )

    assert response.status_code == 200
    assert response.json()["secret_fields"] == [
        "api_key",
        "signing_secret",
        "url",
    ]
    await db.refresh(legacy)
    assert legacy.name == "Migrated webhook"
    assert legacy.config == {
        "url": PUBLIC_URL_REDACTION_PLACEHOLDER,
        "events": ["call.completed"],
    }
    assert legacy.encrypted_config is not None
    persisted = json.dumps(legacy.config) + legacy.encrypted_config
    assert "legacy-api-secret" not in persisted
    assert "legacy-signing-secret" not in persisted
    assert "legacy-url-secret" not in persisted
    hydrated = load_integration_config(legacy.config, legacy.encrypted_config)
    assert hydrated["api_key"] == "legacy-api-secret"


@pytest.mark.asyncio
async def test_tenant_backfill_is_idempotent_and_leaves_other_tenants_untouched(
    client: AsyncClient,
    auth_headers,
    db: AsyncSession,
    tenant: Tenant,
    monkeypatch,
):
    other_tenant = Tenant(name="Backfill Other", slug="backfill-other")
    db.add(other_tenant)
    await db.flush()
    legacy = Integration(
        tenant_id=tenant.id,
        name="Legacy CRM",
        integration_type="crm",
        config={
            "provider": "example",
            "api_key": "tenant-legacy-secret",
            "base_url": "https://crm.vendor.com/api",
        },
        encrypted_config=None,
    )
    versionless_full_config = {
        "provider": "versionless",
        "api_key": "versionless-encrypted-secret",
        "base_url": "https://versionless.vendor.com/api",
    }
    monkeypatch.setattr(settings, "integration_encryption_key", "")
    fallback_ciphertext = encrypt_integration_config(versionless_full_config)
    versionless_encrypted = Integration(
        tenant_id=tenant.id,
        name="Versionless encrypted CRM",
        integration_type="crm",
        config={"provider": "previous-public-projection"},
        encrypted_config=fallback_ciphertext,
        config_encryption_version=None,
    )
    foreign_legacy = Integration(
        tenant_id=other_tenant.id,
        name="Foreign legacy CRM",
        integration_type="crm",
        config={"api_key": "foreign-legacy-secret"},
        encrypted_config=None,
    )
    db.add_all([legacy, versionless_encrypted, foreign_legacy])
    await db.commit()

    # Setting the new dedicated key must still decrypt fallback envelopes and
    # rewrap them without collapsing to the sanitized public projection.
    monkeypatch.setattr(settings, "integration_encryption_key", "backfill-dedicated-key")

    first = await client.post(
        "/api/v1/integrations/encryption/backfill",
        headers=auth_headers,
    )
    second = await client.post(
        "/api/v1/integrations/encryption/backfill",
        headers=auth_headers,
    )

    assert first.status_code == 200
    assert first.json() == {"migrated": 2, "remaining": 0}
    assert second.status_code == 200
    assert second.json() == {"migrated": 0, "remaining": 0}
    await db.refresh(legacy)
    await db.refresh(versionless_encrypted)
    await db.refresh(foreign_legacy)
    assert legacy.config == {}
    assert legacy.encrypted_config is not None
    assert legacy.config_encryption_version == INTEGRATION_CONFIG_STORAGE_VERSION
    assert "tenant-legacy-secret" not in json.dumps(legacy.config) + legacy.encrypted_config
    assert versionless_encrypted.config == {}
    assert versionless_encrypted.encrypted_config != fallback_ciphertext
    assert versionless_encrypted.config_encryption_version == INTEGRATION_CONFIG_STORAGE_VERSION
    assert (
        load_integration_config(
            versionless_encrypted.config,
            versionless_encrypted.encrypted_config,
        )
        == versionless_full_config
    )
    assert foreign_legacy.config["api_key"] == "foreign-legacy-secret"
    assert foreign_legacy.encrypted_config is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsafe_url",
    [
        "http://hooks.vendor.com/events",
        "file:///etc/passwd",
        "https://localhost/events",
        "https://127.0.0.1/events",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/events",
        "https://metadata.google.internal/events",
        "https://127.0.0.1.nip.io/events",
        "https://2130706433/events",
        "https://0x7f000001/events",
        "https://user:password@hooks.vendor.com/events",
    ],
)
async def test_rejects_ssrf_risk_integration_urls(
    client: AsyncClient,
    auth_headers,
    unsafe_url: str,
):
    response = await client.post(
        "/api/v1/integrations",
        headers=auth_headers,
        json={
            "name": "Unsafe webhook",
            "integration_type": "webhook",
            "config": {"url": unsafe_url, "api_key": "must-not-be-echoed"},
        },
    )

    assert response.status_code == 422
    assert "must-not-be-echoed" not in response.text


@pytest.mark.asyncio
async def test_validates_nested_base_urls(client: AsyncClient, auth_headers):
    response = await client.post(
        "/api/v1/integrations",
        headers=auth_headers,
        json={
            "name": "CRM",
            "integration_type": "crm",
            "config": {"provider": {"baseUrl": "http://10.0.0.4/api"}},
        },
    )

    assert response.status_code == 422
    assert "config.provider.baseUrl" in response.json()["detail"]


@pytest.mark.asyncio
async def test_webhook_requires_write_only_signing_secret(client: AsyncClient, auth_headers):
    response = await client.post(
        "/api/v1/integrations",
        headers=auth_headers,
        json={
            "name": "Unsigned webhook",
            "integration_type": "webhook",
            "config": {
                "url": "https://hooks.vendor.com/events",
                "events": ["call.completed"],
            },
        },
    )

    assert response.status_code == 422
    assert "signing secret" in response.json()["detail"].lower()


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["name", "is_active"])
async def test_integration_patch_rejects_explicit_null(
    client: AsyncClient,
    auth_headers,
    field: str,
):
    integration = await _create_webhook(client, auth_headers)
    response = await client.patch(
        f"/api/v1/integrations/{integration['id']}",
        headers=auth_headers,
        json={field: None},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_null_config_explicitly_preserves_encrypted_configuration(
    client: AsyncClient,
    auth_headers,
    db: AsyncSession,
):
    integration = await _create_webhook(client, auth_headers)
    stored = await db.get(Integration, UUID(integration["id"]))
    assert stored is not None
    ciphertext = stored.encrypted_config

    response = await client.patch(
        f"/api/v1/integrations/{integration['id']}",
        headers=auth_headers,
        json={"name": "Renamed webhook", "config": None},
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed webhook"
    assert response.json()["secret_fields"] == integration["secret_fields"]
    await db.refresh(stored)
    assert stored.encrypted_config == ciphertext
    hydrated = load_integration_config(stored.config, stored.encrypted_config)
    assert hydrated["api_key"] == "api-secret-value"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "unsupported_event",
    ["call.started", "call.failed", "opaque-event-secret"],
)
async def test_webhook_rejects_uncontrolled_event_metadata_without_echoing_it(
    client: AsyncClient,
    auth_headers,
    unsupported_event: str,
):
    response = await client.post(
        "/api/v1/integrations",
        headers=auth_headers,
        json={
            "name": "Unsafe event metadata",
            "integration_type": "webhook",
            "config": {
                "url": "https://hooks.vendor.com/events",
                "events": ["call.completed", unsupported_event],
                "signing_secret": "valid-signing-secret",
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Webhook integrations contain an unsupported event type"
    assert unsupported_event not in response.text


@pytest.mark.asyncio
async def test_integration_mutations_are_tenant_scoped(
    client: AsyncClient,
    auth_headers,
    db: AsyncSession,
):
    other_tenant = Tenant(name="Other Corp", slug="integration-other-corp")
    db.add(other_tenant)
    await db.flush()
    other_user = User(
        tenant_id=other_tenant.id,
        email="integration-owner@other.example",
        hashed_password=hash_password("otherpassword"),
        full_name="Other Owner",
        role="owner",
    )
    db.add(other_user)
    await db.flush()
    foreign = Integration(
        tenant_id=other_tenant.id,
        name="Foreign webhook",
        integration_type="webhook",
        config={"url": "https://hooks.vendor.com/foreign", "api_key": "foreign-secret"},
    )
    db.add(foreign)
    await db.commit()

    patch_response = await client.patch(
        f"/api/v1/integrations/{foreign.id}",
        headers=auth_headers,
        json={"name": "Compromised"},
    )
    delete_response = await client.delete(
        f"/api/v1/integrations/{foreign.id}", headers=auth_headers
    )
    assert patch_response.status_code == 404
    assert delete_response.status_code == 404

    other_headers = {
        "Authorization": (
            f"Bearer {create_access_token(other_user.id, other_tenant.id, other_user.role)}"
        )
    }
    visible = await client.get("/api/v1/integrations", headers=other_headers)
    assert visible.status_code == 200
    assert [item["id"] for item in visible.json()] == [str(foreign.id)]
    assert "foreign-secret" not in visible.text


def test_integration_mutation_lookup_compiles_to_postgresql_row_lock():
    statement = _locked_tenant_integration_statement(UUID(int=1), UUID(int=2))

    compiled = str(statement.compile(dialect=postgresql.dialect()))

    assert "FOR UPDATE" in compiled
    assert "integrations.id" in compiled
    assert "integrations.tenant_id" in compiled
    assert statement.get_execution_options()["populate_existing"] is True


@pytest.mark.asyncio
async def test_concurrent_stale_patch_cannot_restore_rotated_signing_secret(
    client: AsyncClient,
    auth_headers,
    db: AsyncSession,
    tenant: Tenant,
    user: User,
):
    """Exercise the real row lock when the test database is PostgreSQL.

    SQLite does not implement ``SELECT ... FOR UPDATE`` and the shared in-memory
    fixture cannot model independent row locks, so the compile-level assertion
    above remains the local regression check.
    """
    if db.bind is None or db.bind.dialect.name != "postgresql":
        pytest.skip("PostgreSQL is required to exercise integration row locking")

    created = await _create_webhook(client, auth_headers)
    integration_id = UUID(created["id"])
    session_factory = async_sessionmaker(db.bind, expire_on_commit=False)
    current_user = CurrentUser(
        id=user.id,
        tenant_id=tenant.id,
        email=user.email,
        role=user.role,
        full_name=user.full_name,
    )
    rotation_flushed = asyncio.Event()
    release_rotation = asyncio.Event()

    async def rotate_secret_while_holding_lock() -> None:
        async with session_factory() as session, session.begin():
            await update_integration(
                integration_id,
                IntegrationUpdate(config={"signing_secret": "rotated-signing-secret"}),
                current_user,
                session,
            )
            rotation_flushed.set()
            await release_rotation.wait()

    async def apply_stale_masked_patch() -> None:
        await rotation_flushed.wait()
        async with session_factory() as session, session.begin():
            await update_integration(
                integration_id,
                IntegrationUpdate(
                    config={
                        "events": ["call.completed", "call.analytics_updated"],
                        "signing_secret": "********",
                    }
                ),
                current_user,
                session,
            )

    rotation_task = asyncio.create_task(rotate_secret_while_holding_lock())
    await rotation_flushed.wait()
    stale_patch_task = asyncio.create_task(apply_stale_masked_patch())
    await asyncio.sleep(0.1)
    release_rotation.set()
    await asyncio.gather(rotation_task, stale_patch_task)

    async with session_factory() as session:
        stored = await session.get(Integration, integration_id)
        assert stored is not None
        hydrated = load_integration_config(stored.config, stored.encrypted_config)
    assert hydrated["signing_secret"] == "rotated-signing-secret"
    assert hydrated["events"] == ["call.completed", "call.analytics_updated"]
