import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import event, select

from app.api.v1.endpoints import agents as agents_endpoint
from app.api.v1.endpoints import calls as calls_endpoint
from app.models.agent import Agent
from app.providers.smallest import BrowserSession, SmallestAIError
from app.tasks.call_tasks import reconcile_call_dispatch, reconcile_direct_call_terminal
from tests.conftest import engine as test_engine
from tests.conftest import test_session_factory as session_factory


@dataclass
class FakeSmallestClient:
    is_configured: bool = True
    draft_calls: list[dict] = field(default_factory=list)
    create_calls: int = 0
    outbound_calls: int = 0
    outbound_payloads: list[dict] = field(default_factory=list)
    clone_catalog_calls: int = 0
    publish_calls: int = 0
    webhook_calls: int = 0
    publish_state: str = "committed"
    revision_status: str = "published"
    security_status: str = "passed"

    async def create_agent(self, **_kwargs):
        self.create_calls += 1
        return "smallest_agent_123"

    async def get_default_branch_id(self, _agent_id):
        return "main_branch_123"

    async def set_agent_webhook_subscriptions(self, **_kwargs):
        self.webhook_calls += 1
        return {"status": True}

    async def update_agent_draft(self, **kwargs):
        self.draft_calls.append(kwargs)
        return {"status": True}

    async def list_voices(self):
        return [
            {
                "voiceId": "jordan",
                "displayName": "Jordan",
                "tags": {
                    "language": ["English", "Hindi"],
                    "accent": "Indian",
                    "gender": "female",
                },
                "modelIds": ["lightning-v3.1"],
            }
        ]

    async def list_voice_clones(self):
        self.clone_catalog_calls += 1
        return [
            {
                "voiceId": "brand_voice",
                "displayName": "Brand Voice",
                "language": "English",
                "status": "completed",
                "modelIds": ["lightning-v3.1"],
            }
        ]

    async def publish_draft(self, **_kwargs):
        self.publish_calls += 1
        return {"state": self.publish_state}

    async def get_latest_branch_revision(self, **_kwargs):
        if self.publish_calls == 0:
            return {"_id": "revision_122"}
        return {"_id": f"revision_{122 + self.publish_calls}"}

    async def get_branch_revision(self, **kwargs):
        return {
            "_id": kwargs["revision_id"],
            "status": self.revision_status,
            "securityCheck": {"status": self.security_status},
        }

    async def create_browser_session(self, **_kwargs):
        return BrowserSession(access_token="wct_test", expires_in=30, sample_rate=24000)

    async def start_outbound_call(self, **kwargs):
        self.outbound_calls += 1
        self.outbound_payloads.append(kwargs)
        return "smallest_call_123"


@pytest.fixture(autouse=True)
def configured_smallest_webhook(monkeypatch):
    monkeypatch.setattr(agents_endpoint.settings, "smallest_webhook_id", "webhook_test_123")


@pytest.mark.asyncio
async def test_provision_sync_and_mint_browser_session(
    client: AsyncClient, auth_headers, monkeypatch
):
    fake = FakeSmallestClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    monkeypatch.setattr(calls_endpoint, "get_smallest_client", lambda: fake)
    monkeypatch.setattr(reconcile_call_dispatch, "apply_async", lambda **_kwargs: None)
    monkeypatch.setattr(reconcile_direct_call_terminal, "apply_async", lambda **_kwargs: None)

    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Al Zaabi Receptionist",
            "system_prompt": "Be concise and helpful.",
            "greeting_message": "Welcome to Al Zaabi Group.",
            "supported_languages": ["en", "hi"],
        },
    )
    agent_id = created.json()["id"]

    provisioned = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision", headers=auth_headers
    )
    assert provisioned.status_code == 200
    assert provisioned.json()["provider_agent_id"] == "smallest_agent_123"
    assert provisioned.json()["provider_revision_id"] == "revision_123"
    assert provisioned.json()["sync_status"] == "synced"
    assert fake.draft_calls[0]["supported_languages"] == ["en", "hi"]
    assert fake.webhook_calls == 1

    duplicate_provision = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision", headers=auth_headers
    )
    assert duplicate_provision.status_code == 409
    assert fake.create_calls == 1

    updated = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
        json={"system_prompt": "Updated prompt."},
    )
    assert updated.json()["sync_status"] == "dirty"

    synced = await client.post(f"/api/v1/agents/{agent_id}/smallest/sync", headers=auth_headers)
    assert synced.status_code == 200
    assert synced.json()["sync_status"] == "synced"
    assert synced.json()["provider_revision_id"] == "revision_124"
    assert fake.webhook_calls == 2

    session = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/session",
        headers=auth_headers,
        json={"variables": {"customer_name": "Vimal"}},
    )
    assert session.status_code == 200
    assert session.json() == {
        "access_token": "wct_test",
        "expires_in": 30,
        "sample_rate": 24000,
    }

    outbound = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "smallest-call-test-0001"},
        json={
            "agent_id": agent_id,
            "to_number": "+971501234567",
            "context": {"customer_name": "Aisha"},
        },
    )
    assert outbound.status_code == 201
    assert outbound.json()["provider"] == "smallest"
    assert outbound.json()["provider_call_sid"] == "smallest_call_123"
    assert outbound.json()["status"] == "ringing"
    assert fake.outbound_payloads[0]["variables"]["_vav_call_id"] == outbound.json()["id"]
    assert fake.outbound_payloads[0]["variables"]["customer_name"] == "Aisha"

    replay = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "smallest-call-test-0001"},
        json={
            "agent_id": agent_id,
            "to_number": "+971501234567",
            "context": {"customer_name": "Aisha"},
        },
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == outbound.json()["id"]
    assert fake.outbound_calls == 1
    assert fake.create_calls == 1

    conflicting_replay = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "smallest-call-test-0001"},
        json={"agent_id": agent_id, "to_number": "+971501234568"},
    )
    assert conflicting_replay.status_code == 409
    assert fake.outbound_calls == 1


@pytest.mark.asyncio
async def test_catalog_includes_public_voices_languages_and_templates_without_private_clones(
    client: AsyncClient, auth_headers, monkeypatch
):
    fake = FakeSmallestClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)

    response = await client.get("/api/v1/agents/provider/catalog", headers=auth_headers)

    assert response.status_code == 200
    payload = response.json()
    assert [voice["id"] for voice in payload["voices"]] == ["jordan"]
    assert payload["voices"][0]["synthesizer_model"] == "waves_lightning_v3_1"
    assert {language["code"] for language in payload["languages"]} == {"en", "hi"}
    assert len(payload["templates"]) == 5
    assert payload["templates"][0]["id"] == "receptionist"
    assert fake.clone_catalog_calls == 0


@pytest.mark.asyncio
async def test_private_clone_id_cannot_be_provisioned_without_tenant_entitlement(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    fake = FakeSmallestClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Unentitled clone",
            "system_prompt": "This agent must not use a private clone.",
            "voice_id": "brand_voice",
        },
    )

    response = await client.post(
        f"/api/v1/agents/{created.json()['id']}/smallest/provision",
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert "tenant-owned entitlement" in response.json()["detail"]
    assert fake.create_calls == 0


@pytest.mark.asyncio
async def test_scanning_publish_is_reconciled_without_republishing(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    fake = FakeSmallestClient(
        publish_state="scanning",
        revision_status="scanning",
        security_status="queued",
    )
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Scanning agent", "system_prompt": "Handle every call carefully."},
    )
    agent_id = created.json()["id"]

    provisioned = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision",
        headers=auth_headers,
    )
    assert provisioned.status_code == 200
    assert provisioned.json()["sync_status"] == "provider_scanning"
    assert fake.publish_calls == 1

    blocked_edit = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
        json={"name": "Unsafe edit"},
    )
    blocked_delete = await client.delete(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
    )
    assert blocked_edit.status_code == 409
    assert blocked_delete.status_code == 409

    fake.revision_status = "published"
    fake.security_status = "passed"
    reconciled = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/sync",
        headers=auth_headers,
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["sync_status"] == "synced"
    assert fake.publish_calls == 1

    provisioned_delete = await client.delete(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
    )
    assert provisioned_delete.status_code == 409
    assert "Provisioned agents cannot be deleted" in provisioned_delete.json()["detail"]


@pytest.mark.asyncio
async def test_ambiguous_publish_is_reconciled_without_a_second_publish(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    class AmbiguousPublishClient(FakeSmallestClient):
        async def publish_draft(self, **_kwargs):
            self.publish_calls += 1
            raise SmallestAIError(
                "Connection ended after publish",
                status_code=504,
                ambiguous=True,
            )

    fake = AmbiguousPublishClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Unknown publish", "system_prompt": "Handle uncertain delivery safely."},
    )
    agent_id = created.json()["id"]

    provisioned = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision",
        headers=auth_headers,
    )
    assert provisioned.status_code == 504
    persisted = await client.get(f"/api/v1/agents/{agent_id}", headers=auth_headers)
    assert persisted.json()["sync_status"] == "publish_unknown"
    blocked_call = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "blocked-before-publish-001"},
        json={"agent_id": agent_id, "to_number": "+971501234567"},
    )
    blocked_session = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/session",
        headers=auth_headers,
        json={"variables": {}},
    )
    assert blocked_call.status_code == 409
    assert blocked_session.status_code == 409

    reconciled = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/sync",
        headers=auth_headers,
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["sync_status"] == "synced"
    assert fake.publish_calls == 1


@pytest.mark.asyncio
async def test_ambiguous_create_fails_closed_without_duplicate_or_edit(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    class AmbiguousCreateClient(FakeSmallestClient):
        async def create_agent(self, **_kwargs):
            self.create_calls += 1
            raise SmallestAIError(
                "Connection ended after create",
                status_code=504,
                ambiguous=True,
            )

    fake = AmbiguousCreateClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Unknown create", "system_prompt": "Never create duplicate agents."},
    )
    agent_id = created.json()["id"]

    first = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision",
        headers=auth_headers,
    )
    second = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision",
        headers=auth_headers,
    )
    blocked_edit = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
        json={"name": "Changed"},
    )
    blocked_delete = await client.delete(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
    )

    assert first.status_code == 504
    assert second.status_code == 409
    assert blocked_edit.status_code == 409
    assert blocked_delete.status_code == 409
    assert fake.create_calls == 1

    unsafe_attach = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/resolve",
        headers=auth_headers,
        json={
            "action": "attach_created_agent",
            "provider_agent_id": "smallest_agent_123",
            "confirmation": "ATTACH CONFIRMED REMOTE AGENT",
        },
    )
    assert unsafe_attach.status_code == 422

    resolved = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/resolve",
        headers=auth_headers,
        json={
            "action": "confirm_create_absent",
            "confirmation": "I CONFIRM NO REMOTE AGENT EXISTS",
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["provider_agent_id"] is None
    assert resolved.json()["sync_status"] == "local_only"

    audit = await client.get(
        "/api/v1/audit-events?action=agent.provider_create_confirmed_absent",
        headers=auth_headers,
    )
    assert audit.status_code == 200
    assert audit.json()[0]["resource_id"] == agent_id


@pytest.mark.asyncio
async def test_owner_can_confirm_an_ambiguous_publish_created_no_revision(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    class NoRevisionAmbiguousPublishClient(FakeSmallestClient):
        async def publish_draft(self, **_kwargs):
            self.publish_calls += 1
            raise SmallestAIError("Publish response was lost", status_code=504, ambiguous=True)

        async def get_latest_branch_revision(self, **_kwargs):
            return {"_id": "revision_122"}

    fake = NoRevisionAmbiguousPublishClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Absent publish", "system_prompt": "Recover ambiguous publish outcomes."},
    )
    agent_id = created.json()["id"]
    provisioned = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision",
        headers=auth_headers,
    )
    assert provisioned.status_code == 504

    resolved = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/resolve",
        headers=auth_headers,
        json={
            "action": "confirm_publish_absent",
            "confirmation": "I CONFIRM NO NEW REVISION EXISTS",
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["sync_status"] == "dirty"
    assert fake.publish_calls == 1

    editable = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
        json={"system_prompt": "The confirmed absent publish is editable again."},
    )
    assert editable.status_code == 200


@pytest.mark.asyncio
async def test_provision_requires_webhook_id_before_remote_create(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    fake = FakeSmallestClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    monkeypatch.setattr(agents_endpoint.settings, "smallest_webhook_id", "")
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "No webhook", "system_prompt": "Callbacks are required for safety."},
    )

    response = await client.post(
        f"/api/v1/agents/{created.json()['id']}/smallest/provision",
        headers=auth_headers,
    )
    assert response.status_code == 503
    assert "SMALLEST_WEBHOOK_ID" in response.json()["detail"]
    assert fake.create_calls == 0


@pytest.mark.asyncio
async def test_direct_call_distinguishes_definitive_and_ambiguous_provider_failures(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    fake = FakeSmallestClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    monkeypatch.setattr(calls_endpoint, "get_smallest_client", lambda: fake)
    monkeypatch.setattr(reconcile_call_dispatch, "apply_async", lambda **_kwargs: None)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Dispatch errors", "system_prompt": "Classify provider failures safely."},
    )
    agent_id = created.json()["id"]
    provisioned = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision",
        headers=auth_headers,
    )
    assert provisioned.json()["sync_status"] == "synced"

    fabricated_caller = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "fabricated-smallest-caller-001"},
        json={
            "agent_id": agent_id,
            "to_number": "+971501234567",
            "from_number": "+971501111111",
        },
    )
    assert fabricated_caller.status_code == 422

    fake.start_outbound_call = AsyncMock(
        side_effect=SmallestAIError("Invalid recipient", status_code=422)
    )
    definitive = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "definitive-provider-error-001"},
        json={"agent_id": agent_id, "to_number": "+971501234567"},
    )
    assert definitive.status_code == 201
    assert definitive.json()["status"] == "failed"

    fake.start_outbound_call = AsyncMock(
        side_effect=SmallestAIError(
            "Provider timed out",
            status_code=504,
            ambiguous=True,
        )
    )
    ambiguous = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "ambiguous-provider-error-001"},
        json={"agent_id": agent_id, "to_number": "+971501234567"},
    )
    assert ambiguous.status_code == 201
    assert ambiguous.json()["status"] == "dispatch_unknown"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"context": {"_vav_call_id": "attacker-value"}},
        {"context": {"_voice_ai_call_id": "attacker-value"}},
        {"from_product_id": "shared-org-resource"},
        {"version_id": "unowned-provider-revision"},
    ],
)
async def test_direct_call_rejects_reserved_context_and_provider_resource_ids(
    client: AsyncClient,
    auth_headers,
    payload,
):
    request = {
        "agent_id": "00000000-0000-0000-0000-000000000001",
        "to_number": "+971501234567",
        **payload,
    }
    response = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "reserved-call-input-test-001"},
        json=request,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_browser_session_rejects_reserved_provider_variables(
    client: AsyncClient,
    auth_headers,
):
    response = await client.post(
        "/api/v1/agents/00000000-0000-0000-0000-000000000001/smallest/session",
        headers=auth_headers,
        json={"variables": {"_voice_ai_campaign_id": "attacker-value"}},
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_browser_session_and_direct_call_require_active_published_agent(
    client: AsyncClient,
    auth_headers,
    db,
    monkeypatch,
):
    fake = FakeSmallestClient()
    fake.create_browser_session = AsyncMock(wraps=fake.create_browser_session)
    fake.start_outbound_call = AsyncMock(wraps=fake.start_outbound_call)
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    monkeypatch.setattr(calls_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Readiness guard", "system_prompt": "Serve only when safely published."},
    )
    agent_id = created.json()["id"]
    agent = await db.scalar(select(Agent).where(Agent.id == UUID(agent_id)))
    agent.provider_agent_id = "smallest-readiness-agent"
    agent.last_synced_at = datetime.now(UTC)
    agent.provider_revision_id = None
    await db.commit()

    unpublished_session = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/session",
        headers=auth_headers,
        json={"variables": {}},
    )
    unpublished_call = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "unpublished-readiness-001"},
        json={"agent_id": agent_id, "to_number": "+971501234567"},
    )
    assert unpublished_session.status_code == 409
    assert unpublished_call.status_code == 409

    agent.provider_revision_id = "smallest-readiness-revision"
    agent.is_active = False
    await db.commit()
    inactive_session = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/session",
        headers=auth_headers,
        json={"variables": {}},
    )
    inactive_call = await client.post(
        "/api/v1/calls",
        headers={**auth_headers, "Idempotency-Key": "inactive-readiness-001"},
        json={"agent_id": agent_id, "to_number": "+971501234567"},
    )
    assert inactive_session.status_code == 409
    assert inactive_session.json()["detail"] == "Agent is inactive"
    assert inactive_call.status_code == 409
    fake.create_browser_session.assert_not_awaited()
    fake.start_outbound_call.assert_not_awaited()


@pytest.mark.asyncio
async def test_postgres_browser_session_waits_for_committed_deactivation(
    client: AsyncClient,
    auth_headers,
    db,
    monkeypatch,
):
    if test_engine.dialect.name != "postgresql":
        pytest.skip("Requires PostgreSQL row-lock semantics")

    fake = FakeSmallestClient()
    session_mint = AsyncMock(wraps=fake.create_browser_session)
    fake.create_browser_session = session_mint
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)

    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Browser session safety boundary",
            "system_prompt": "Never mint a session after committed deactivation.",
        },
    )
    assert created.status_code == 201
    agent_id = UUID(created.json()["id"])
    agent = await db.scalar(select(Agent).where(Agent.id == agent_id))
    agent.provider_agent_id = "smallest-browser-session-lock"
    agent.provider_revision_id = "smallest-browser-session-revision"
    agent.last_synced_at = datetime.now(UTC)
    await db.commit()

    blocker = session_factory()
    request_task = None
    session_lock_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    listener_installed = False
    try:
        locked_agent = await blocker.scalar(
            select(Agent).where(Agent.id == agent_id).with_for_update()
        )
        locked_agent.is_active = False
        await blocker.flush()

        def observe_session_lock(_conn, _cursor, statement, _parameters, _context, _many):
            normalized = statement.upper()
            if "FROM AGENTS" in normalized and "FOR UPDATE" in normalized:
                loop.call_soon_threadsafe(session_lock_requested.set)

        event.listen(test_engine.sync_engine, "before_cursor_execute", observe_session_lock)
        listener_installed = True
        request_task = asyncio.create_task(
            client.post(
                f"/api/v1/agents/{agent_id}/smallest/session",
                headers=auth_headers,
                json={"variables": {}},
            )
        )
        await asyncio.wait_for(session_lock_requested.wait(), timeout=5)
        session_mint.assert_not_awaited()

        await blocker.commit()
        response = await asyncio.wait_for(request_task, timeout=5)
    finally:
        if listener_installed:
            event.remove(test_engine.sync_engine, "before_cursor_execute", observe_session_lock)
        if request_task is not None and not request_task.done():
            request_task.cancel()
        await blocker.close()

    assert response.status_code == 409
    assert response.json()["detail"] == "Agent is inactive"
    session_mint.assert_not_awaited()


@pytest.mark.asyncio
async def test_expired_provisioning_lease_becomes_unknown_without_duplicate_create(
    client: AsyncClient,
    auth_headers,
    db,
    monkeypatch,
):
    fake = FakeSmallestClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Interrupted create",
            "system_prompt": "Never create a duplicate provider agent.",
        },
    )
    agent_id = created.json()["id"]
    agent = await db.scalar(select(Agent).where(Agent.id == UUID(agent_id)))
    agent.sync_status = "provisioning"
    agent.provider_config = {
        "provision": {
            "id": "interrupted-create-operation",
            "phase": "create_request",
            "lease_expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        }
    }
    await db.commit()

    reconciled = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision",
        headers=auth_headers,
    )
    retry = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision",
        headers=auth_headers,
    )
    resolved = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/resolve",
        headers=auth_headers,
        json={
            "action": "confirm_create_absent",
            "confirmation": "I CONFIRM NO REMOTE AGENT EXISTS",
        },
    )

    assert reconciled.status_code == 200
    assert reconciled.json()["sync_status"] == "provision_unknown"
    assert retry.status_code == 409
    assert resolved.status_code == 200
    assert resolved.json()["sync_status"] == "local_only"
    assert fake.create_calls == 0


@pytest.mark.asyncio
async def test_expired_pre_publish_lease_becomes_dirty_without_publishing(
    client: AsyncClient,
    auth_headers,
    db,
    monkeypatch,
):
    fake = FakeSmallestClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Interrupted draft", "system_prompt": "Resume without duplicate publishing."},
    )
    agent_id = created.json()["id"]
    agent = await db.scalar(select(Agent).where(Agent.id == UUID(agent_id)))
    agent.provider_agent_id = "mapped_agent_123"
    agent.sync_status = "publishing"
    agent.provider_config = {
        "publish": {
            "id": "interrupted-operation",
            "phase": "branch_lookup",
            "lease_expires_at": (datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
        }
    }
    await db.commit()

    reconciled = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/sync",
        headers=auth_headers,
    )
    assert reconciled.status_code == 200
    assert reconciled.json()["sync_status"] == "dirty"
    assert fake.publish_calls == 0


@pytest.mark.asyncio
async def test_agents_can_be_edited_with_multiple_languages(client: AsyncClient, auth_headers):
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Support Agent",
            "system_prompt": "Help the customer solve their issue.",
            "language": "en",
            "supported_languages": ["en", "hi"],
        },
    )
    assert created.status_code == 201

    updated = await client.patch(
        f"/api/v1/agents/{created.json()['id']}",
        headers=auth_headers,
        json={
            "name": "Multilingual Support",
            "language": "hi",
            "supported_languages": ["hi", "en", "ml"],
            "voice_id": "jordan",
        },
    )

    assert updated.status_code == 200
    assert updated.json()["name"] == "Multilingual Support"
    assert updated.json()["language"] == "hi"
    assert updated.json()["supported_languages"] == ["hi", "en", "ml"]
    assert updated.json()["voice_id"] == "jordan"


@pytest.mark.asyncio
async def test_agent_language_configuration_is_validated_on_create_and_edit(
    client: AsyncClient, auth_headers
):
    invalid_create = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Invalid Agent",
            "system_prompt": "This prompt is long enough.",
            "language": "en",
            "supported_languages": ["hi"],
        },
    )
    assert invalid_create.status_code == 422

    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Tamil Agent",
            "system_prompt": "This prompt is long enough.",
            "language": "ta",
            "supported_languages": ["ta"],
        },
    )
    invalid_edit = await client.patch(
        f"/api/v1/agents/{created.json()['id']}",
        headers=auth_headers,
        json={"supported_languages": ["ta", "en"]},
    )
    assert invalid_edit.status_code == 422
    assert (
        invalid_edit.json()["detail"] == "Tamil cannot be combined with other supported languages"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"name": None},
        {"system_prompt": None},
        {"temperature": 2.1},
        {"speech_rate": 0.1},
        {"max_call_duration_seconds": 5},
        {"timezone": "Not/A_Timezone"},
        {"model_provider": "unsupported"},
    ],
)
async def test_agent_edits_reject_nulls_and_out_of_range_values(
    client: AsyncClient,
    auth_headers,
    payload,
):
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Validated agent", "system_prompt": "Use only validated settings."},
    )

    response = await client.patch(
        f"/api/v1/agents/{created.json()['id']}",
        headers=auth_headers,
        json=payload,
    )
    assert response.status_code == 422


@pytest.mark.asyncio
async def test_voice_language_capabilities_are_enforced_before_provision(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    fake = FakeSmallestClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Unsupported voice language",
            "system_prompt": "Use the selected languages and voice safely.",
            "voice_id": "jordan",
            "language": "en",
            "supported_languages": ["en", "ml"],
        },
    )

    response = await client.post(
        f"/api/v1/agents/{created.json()['id']}/smallest/provision",
        headers=auth_headers,
    )
    assert response.status_code == 422
    assert response.json()["detail"].endswith("ml")
    assert fake.create_calls == 0


@pytest.mark.asyncio
async def test_pro_voice_is_visible_but_cannot_use_an_undocumented_atoms_model(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    fake = FakeSmallestClient()

    async def pro_voices():
        return [
            {
                "voiceId": "rhea",
                "displayName": "Rhea",
                "tags": {"language": ["English", "Hindi"]},
            }
        ]

    fake.list_voices = pro_voices
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Pro voice agent",
            "system_prompt": "Use the verified Pro voice model pairing.",
            "voice_id": "rhea",
            "language": "en",
            "supported_languages": ["en", "hi"],
        },
    )
    catalog = await client.get("/api/v1/agents/provider/catalog", headers=auth_headers)
    response = await client.post(
        f"/api/v1/agents/{created.json()['id']}/smallest/provision",
        headers=auth_headers,
    )

    assert catalog.status_code == 200
    assert catalog.json()["voices"][0]["synthesizer_model"] is None
    assert "not yet documented for Atoms" in catalog.json()["voices"][0]["unavailability_reason"]
    assert response.status_code == 422
    assert "catalog-visible" in response.json()["detail"]
    assert fake.create_calls == 0
