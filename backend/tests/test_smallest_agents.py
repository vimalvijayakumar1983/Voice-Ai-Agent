import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from httpx import AsyncClient, MockTransport, Request, Response
from sqlalchemy import event, select

from app.api.v1.endpoints import agents as agents_endpoint
from app.api.v1.endpoints import calls as calls_endpoint
from app.models.agent import Agent, AgentKnowledgeBinding, KnowledgeBase
from app.providers.smallest import BrowserSession, SmallestAIClient, SmallestAIError
from app.services.agent_catalog_cache import public_agent_catalog_cache
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
    publish_labels: list[str] = field(default_factory=list)
    webhook_calls: int = 0
    publish_state: str = "committed"
    revision_status: str = "published"
    security_status: str = "passed"
    preview_calls: list[dict] = field(default_factory=list)
    voice_catalog_calls: int = 0
    active_knowledge_base_id: str | None = None
    knowledge_binding_lookup_calls: int = 0

    async def create_agent(self, **_kwargs):
        self.create_calls += 1
        return "smallest_agent_123"

    async def get_default_branch_id(self, _agent_id):
        return "main_branch_123"

    async def get_agent_knowledge_base_id(self, _agent_id):
        self.knowledge_binding_lookup_calls += 1
        return self.active_knowledge_base_id

    async def set_agent_webhook_subscriptions(self, **_kwargs):
        self.webhook_calls += 1
        return {"status": True}

    async def update_agent_draft(self, **kwargs):
        self.draft_calls.append(kwargs)
        return {"status": True}

    async def list_voices(self):
        self.voice_catalog_calls += 1
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

    async def synthesize_voice_preview(self, **kwargs):
        self.preview_calls.append(kwargs)
        return b"RIFF\x04\x00\x00\x00WAVE"

    async def publish_draft(self, **kwargs):
        self.publish_calls += 1
        self.publish_labels.append(kwargs["label"])
        return {"state": self.publish_state}

    async def get_latest_branch_revision(self, **_kwargs):
        if self.publish_calls == 0:
            return {"_id": "revision_122"}
        return {
            "_id": f"revision_{122 + self.publish_calls}",
            "label": self.publish_labels[-1],
        }

    async def get_open_branch_draft(self, **_kwargs):
        return None

    async def get_branch_revision(self, **kwargs):
        return {
            "_id": kwargs["revision_id"],
            "label": self.publish_labels[-1],
            "status": self.revision_status,
            "securityCheck": {"status": self.security_status},
        }

    async def get_agent(self, _agent_id, **kwargs):
        draft = self.draft_calls[-1]
        synthesizer = {
            "voiceConfig": {
                "voiceId": draft["voice_id"],
                "model": draft["synthesizer_model"],
            },
            "speed": draft["speech_rate"],
        }
        return {
            "_configSource": "version" if kwargs.get("version_id") else "active",
            "timezone": draft["timezone"],
            "language": {
                "default": draft["language"],
                "supported": draft["supported_languages"],
                "switching": {"isEnabled": draft["language_switching_enabled"]},
            },
            "synthesizer": synthesizer,
            "sessionTimeoutConfig": {
                "timeoutTimeInSecs": draft["max_call_duration_seconds"],
            },
            "_resolvedConfig": {
                "globalPrompt": draft["global_prompt"],
                "firstMessage": draft["first_message"] or "",
                "modelName": draft["slm_model"],
                "timezone": draft["timezone"],
                "defaultLanguage": draft["language"],
                "supportedLanguages": draft["supported_languages"],
                "languageSwitching": {"isEnabled": draft["language_switching_enabled"]},
                "synthesizer": synthesizer,
                "sessionTimeoutConfig": {
                    "timeoutTimeInSecs": draft["max_call_duration_seconds"],
                },
            },
        }

    async def create_browser_session(self, **_kwargs):
        return BrowserSession(access_token="wct_test", expires_in=30, sample_rate=24000)

    async def start_outbound_call(self, **kwargs):
        self.outbound_calls += 1
        self.outbound_payloads.append(kwargs)
        return "smallest_call_123"


def _full_agent_editor_payload(agent: dict) -> dict:
    fields = {
        "name",
        "description",
        "system_prompt",
        "model_provider",
        "model_name",
        "temperature",
        "max_tokens",
        "voice_provider",
        "voice_id",
        "language",
        "supported_languages",
        "language_switching_enabled",
        "language_switching_mode",
        "speech_rate",
        "greeting_message",
        "fallback_message",
        "max_call_duration_seconds",
        "transfer_number",
        "is_active",
        "timezone",
    }
    return {field: agent[field] for field in fields}


def test_provider_publish_label_is_bounded_and_provider_safe():
    operation_id = "12345678-1234-1234-1234-123456789abc"

    label = agents_endpoint._provider_publish_label(
        "VAV Voice AI initial release [Dubai] / production",
        operation_id,
    )

    assert len(label) <= 40
    assert label.endswith("-12345678")
    assert all(
        (character.isascii() and character.isalnum()) or character in {" ", ".", ",", "-", "(", ")"}
        for character in label
    )


async def _provision_editor_regression_agent(
    client: AsyncClient,
    auth_headers: dict,
) -> dict:
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Editor diff agent",
            "description": "Original local metadata",
            "system_prompt": "Keep provider configuration stable across local edits.",
            "voice_id": "jordan",
            "language": "en",
            "supported_languages": ["en", "hi"],
            "greeting_message": "Hello and welcome.",
        },
    )
    assert created.status_code == 201
    provisioned = await client.post(
        f"/api/v1/agents/{created.json()['id']}/smallest/provision",
        headers=auth_headers,
    )
    assert provisioned.status_code == 200
    assert provisioned.json()["sync_status"] == "synced"
    return provisioned.json()


@pytest.fixture(autouse=True)
def configured_smallest_webhook(monkeypatch):
    public_agent_catalog_cache.clear()
    monkeypatch.setattr(agents_endpoint.settings, "smallest_webhook_id", "webhook_test_123")
    yield
    public_agent_catalog_cache.clear()


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
    assert "global_knowledge_base_id" not in fake.draft_calls[0]
    assert fake.knowledge_binding_lookup_calls == 1
    assert fake.webhook_calls == 1
    first_operation = provisioned.json()["provider_config"]["publish"]
    assert first_operation["id"][:8] in first_operation["label"]
    assert fake.publish_labels == [first_operation["label"]]

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

    stale_session = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/session",
        headers=auth_headers,
        json={"variables": {"customer_name": "Vimal"}},
    )
    assert stale_session.status_code == 409
    assert "current provider revision" in stale_session.json()["detail"]

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
    assert fake.outbound_payloads[0]["version_id"] == "revision_124"

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
async def test_publish_clears_only_an_existing_provider_knowledge_binding(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    fake = FakeSmallestClient(active_knowledge_base_id="provider_kb_123")
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Detached knowledge", "system_prompt": "Publish without stale knowledge."},
    )

    response = await client.post(
        f"/api/v1/agents/{created.json()['id']}/smallest/provision",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert fake.draft_calls[0]["global_knowledge_base_id"] is None
    assert response.json()["provider_config"]["publish"]["knowledge_binding_action"] == "clear"


@pytest.mark.asyncio
async def test_draft_rejection_reports_publish_phase(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    class DraftRejectingClient(FakeSmallestClient):
        async def update_agent_draft(self, **_kwargs):
            raise SmallestAIError("Voice ID is not compatible", status_code=400)

    fake = DraftRejectingClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Rejected draft", "system_prompt": "Report the failed provider phase."},
    )

    response = await client.post(
        f"/api/v1/agents/{created.json()['id']}/smallest/provision",
        headers=auth_headers,
    )

    assert response.status_code == 400
    assert response.json()["detail"] == (
        "Could not update the draft configuration on Smallest.ai: Voice ID is not compatible"
    )
    persisted = await client.get(f"/api/v1/agents/{created.json()['id']}", headers=auth_headers)
    assert persisted.json()["provider_config"]["publish"]["phase"] == "draft_update_failed"


@pytest.mark.asyncio
async def test_identical_full_editor_patch_preserves_synced_without_voice_catalog_call(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    fake = FakeSmallestClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    provisioned = await _provision_editor_regression_agent(client, auth_headers)
    public_agent_catalog_cache.clear()
    catalog_calls_before = fake.voice_catalog_calls

    response = await client.patch(
        f"/api/v1/agents/{provisioned['id']}",
        headers=auth_headers,
        json=_full_agent_editor_payload(provisioned),
    )

    assert response.status_code == 200
    assert response.json()["sync_status"] == "synced"
    assert response.json()["updated_at"].removesuffix("Z") == provisioned[
        "updated_at"
    ].removesuffix("Z")
    assert fake.voice_catalog_calls == catalog_calls_before


@pytest.mark.asyncio
async def test_full_editor_local_metadata_patch_preserves_synced_without_voice_catalog_call(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    fake = FakeSmallestClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    provisioned = await _provision_editor_regression_agent(client, auth_headers)
    public_agent_catalog_cache.clear()
    catalog_calls_before = fake.voice_catalog_calls
    payload = _full_agent_editor_payload(provisioned)
    payload.update(
        {
            "name": "Renamed locally",
            "description": "Updated local metadata only",
        }
    )

    response = await client.patch(
        f"/api/v1/agents/{provisioned['id']}",
        headers=auth_headers,
        json=payload,
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed locally"
    assert response.json()["description"] == "Updated local metadata only"
    assert response.json()["sync_status"] == "synced"
    assert response.json()["provider_revision_id"] == provisioned["provider_revision_id"]
    assert fake.voice_catalog_calls == catalog_calls_before


@pytest.mark.asyncio
async def test_voice_preflight_does_not_overwrite_concurrent_provider_operation(
    client: AsyncClient,
    auth_headers,
    db,
    monkeypatch,
):
    fake = FakeSmallestClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    provisioned = await _provision_editor_regression_agent(client, auth_headers)
    agent_id = UUID(provisioned["id"])
    original_list_voices = fake.list_voices

    async def list_voices_during_concurrent_sync():
        async with session_factory() as concurrent_db:
            concurrent_agent = await concurrent_db.scalar(select(Agent).where(Agent.id == agent_id))
            config = dict(concurrent_agent.provider_config or {})
            config["publish"] = {
                "id": "concurrent-publish-operation",
                "phase": "publish_request",
            }
            concurrent_agent.provider_config = config
            concurrent_agent.sync_status = "publishing"
            await concurrent_db.commit()
        return await original_list_voices()

    fake.list_voices = list_voices_during_concurrent_sync
    public_agent_catalog_cache.clear()

    response = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
        json={"supported_languages": ["en"]},
    )

    await db.rollback()
    persisted = await db.scalar(select(Agent).where(Agent.id == agent_id))
    assert response.status_code == 409
    assert response.json()["detail"] == (
        "Agent cannot be edited while its provider operation is unresolved"
    )
    assert persisted.sync_status == "publishing"
    assert persisted.provider_config["publish"]["id"] == "concurrent-publish-operation"
    assert persisted.supported_languages == ["en", "hi"]
    assert persisted.language_switching_enabled is True


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
    assert payload["voices"][0]["voice_pool"] == "standard"
    assert {language["code"] for language in payload["languages"]} == {"en", "hi"}
    assert len(payload["templates"]) == 5
    assert payload["templates"][0]["id"] == "receptionist"
    assert payload["field_capabilities"]["system_prompt"] == {
        "status": "synced",
        "provider_field": "singlePromptConfig.prompt",
        "reason": None,
    }
    assert payload["field_capabilities"]["temperature"]["status"] == "local_only"
    assert fake.clone_catalog_calls == 0


@pytest.mark.asyncio
async def test_voice_preview_is_catalog_validated_and_api_key_stays_server_side(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    fake = FakeSmallestClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)

    response = await client.post(
        "/api/v1/agents/provider/voice-preview",
        headers=auth_headers,
        json={"voice_id": "jordan"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.headers["cache-control"] == "private, no-store"
    assert response.content == b"RIFF\x04\x00\x00\x00WAVE"
    assert fake.preview_calls == [
        {"voice_id": "jordan", "model": "lightning_v3.1", "language": "en"}
    ]
    assert b"sk_" not in response.content


@pytest.mark.asyncio
async def test_voice_preview_rejects_ids_outside_public_catalog(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    fake = FakeSmallestClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)

    response = await client.post(
        "/api/v1/agents/provider/voice-preview",
        headers=auth_headers,
        json={"voice_id": "brand_voice"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "Voice is not in the public catalog"
    assert fake.preview_calls == []


@pytest.mark.asyncio
async def test_voice_preview_rejects_language_outside_voice_capabilities(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    fake = FakeSmallestClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)

    response = await client.post(
        "/api/v1/agents/provider/voice-preview",
        headers=auth_headers,
        json={"voice_id": "jordan", "language": "ml"},
    )

    assert response.status_code == 422
    assert response.json()["detail"].endswith("ml")
    assert fake.preview_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("provider_status", [401, 403])
async def test_catalog_does_not_expose_provider_auth_as_application_auth(
    provider_status: int,
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    def handler(_request: Request) -> Response:
        return Response(provider_status, json={"message": "raw provider auth failure"})

    provider = SmallestAIClient(
        api_key="sk_invalid",
        transport=MockTransport(handler),
    )
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: provider)

    response = await client.get("/api/v1/agents/provider/catalog", headers=auth_headers)

    assert response.status_code == 502
    assert response.json()["detail"] == (
        "Smallest.ai rejected the configured server credentials or permissions."
    )
    assert "raw provider auth failure" not in response.text


@pytest.mark.asyncio
async def test_private_clone_id_cannot_be_saved_without_tenant_entitlement(
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

    assert created.status_code == 422
    assert "tenant-owned entitlement" in created.json()["detail"]
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
async def test_open_draft_publish_state_transitions_from_scanning_to_actionable_error(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    class PendingDraftClient(FakeSmallestClient):
        pending_state = "active"

        async def get_latest_branch_revision(self, **_kwargs):
            return {"_id": "revision_122"}

        async def get_open_branch_draft(self, **_kwargs):
            return {"pendingPublish": {"state": self.pending_state}}

    fake = PendingDraftClient(publish_state="scanning")
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Draft scan", "system_prompt": "Track the provider scan safely."},
    )
    agent_id = created.json()["id"]

    scanning = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision",
        headers=auth_headers,
    )

    assert scanning.status_code == 200
    assert scanning.json()["sync_status"] == "provider_scanning"
    assert scanning.json()["provider_config"]["publish"]["provider_state"] == "active"
    assert fake.publish_calls == 1

    fake.pending_state = "errored"
    failed = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/sync",
        headers=auth_headers,
    )

    assert failed.status_code == 200
    assert failed.json()["sync_status"] == "error"
    operation = failed.json()["provider_config"]["publish"]
    assert operation["phase"] == "publish_failed"
    assert operation["provider_state"] == "errored"
    assert "correct the issue" in operation["last_error"]
    assert fake.publish_calls == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("revision_state", "security_state", "expected_phase"),
    [
        ("error", "passed", "publish_failed"),
        ("errored", "passed", "publish_failed"),
        ("published", "error", "security_failed"),
        ("published", "errored", "security_failed"),
    ],
)
async def test_committed_revision_error_states_are_normalized_as_publish_failures(
    revision_state: str,
    security_state: str,
    expected_phase: str,
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    fake = FakeSmallestClient(
        revision_status=revision_state,
        security_status=security_state,
    )
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Failed revision", "system_prompt": "Surface provider failures safely."},
    )

    provisioned = await client.post(
        f"/api/v1/agents/{created.json()['id']}/smallest/provision",
        headers=auth_headers,
    )

    assert provisioned.status_code == 200
    assert provisioned.json()["sync_status"] == "error"
    operation = provisioned.json()["provider_config"]["publish"]
    assert operation["phase"] == expected_phase
    assert f"revision state: {revision_state}" in operation["last_error"]
    assert f"security state: {security_state}" in operation["last_error"]
    assert "correct the issue" in operation["last_error"]
    assert fake.publish_calls == 1


@pytest.mark.asyncio
async def test_reconciliation_does_not_accept_newer_revision_with_conflicting_label(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    class OutOfBandRevisionClient(FakeSmallestClient):
        async def get_latest_branch_revision(self, **_kwargs):
            if self.publish_calls == 0:
                return {"_id": "revision_122", "label": "Previous release"}
            return {"_id": "revision_999", "label": "Manual console release"}

        async def get_branch_revision(self, **kwargs):
            return {
                "_id": kwargs["revision_id"],
                "label": "Manual console release",
                "status": "published",
                "securityCheck": {"status": "passed"},
            }

    fake = OutOfBandRevisionClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Label guard", "system_prompt": "Reject unrelated provider revisions."},
    )
    agent_id = created.json()["id"]

    provisioned = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision",
        headers=auth_headers,
    )

    assert provisioned.status_code == 200
    assert provisioned.json()["sync_status"] == "publish_unknown"
    assert provisioned.json()["provider_revision_id"] is None
    operation = provisioned.json()["provider_config"]["publish"]
    assert operation["phase"] == "revision_label_mismatch"
    assert operation["observed_revision_id"] == "revision_999"
    assert operation["observed_revision_label"] == "Manual console release"
    assert fake.publish_calls == 1

    checked_again = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/sync",
        headers=auth_headers,
    )
    assert checked_again.status_code == 200
    assert checked_again.json()["sync_status"] == "publish_unknown"
    assert fake.publish_calls == 1


@pytest.mark.asyncio
async def test_ambiguous_publish_is_reconciled_without_a_second_publish(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    class AmbiguousPublishClient(FakeSmallestClient):
        async def publish_draft(self, **kwargs):
            self.publish_calls += 1
            self.publish_labels.append(kwargs["label"])
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
        async def publish_draft(self, **kwargs):
            self.publish_calls += 1
            self.publish_labels.append(kwargs["label"])
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
async def test_confirm_publish_absent_refuses_every_reported_pending_publish(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    class PendingAmbiguousPublishClient(FakeSmallestClient):
        pending_publish: dict | None = {"state": "active"}

        async def publish_draft(self, **kwargs):
            self.publish_calls += 1
            self.publish_labels.append(kwargs["label"])
            raise SmallestAIError("Publish response was lost", status_code=504, ambiguous=True)

        async def get_latest_branch_revision(self, **_kwargs):
            return {"_id": "revision_122"}

        async def get_open_branch_draft(self, **_kwargs):
            return {"pendingPublish": self.pending_publish}

    fake = PendingAmbiguousPublishClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={"name": "Pending publish", "system_prompt": "Never abandon pending work."},
    )
    agent_id = created.json()["id"]
    provisioned = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision",
        headers=auth_headers,
    )
    assert provisioned.status_code == 504

    resolution = {
        "action": "confirm_publish_absent",
        "confirmation": "I CONFIRM NO NEW REVISION EXISTS",
    }
    for pending_publish in ({"state": "active"}, {"state": "failed"}, {}):
        fake.pending_publish = pending_publish
        refused = await client.post(
            f"/api/v1/agents/{agent_id}/smallest/resolve",
            headers=auth_headers,
            json=resolution,
        )
        assert refused.status_code == 409
        assert "pending publish" in refused.json()["detail"]

    persisted = await client.get(f"/api/v1/agents/{agent_id}", headers=auth_headers)
    assert persisted.json()["sync_status"] == "publish_unknown"

    fake.pending_publish = None
    resolved = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/resolve",
        headers=auth_headers,
        json=resolution,
    )
    assert resolved.status_code == 200
    assert resolved.json()["sync_status"] == "dirty"
    assert fake.publish_calls == 1


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
        },
    )

    assert updated.status_code == 200
    assert updated.json()["name"] == "Multilingual Support"
    assert updated.json()["language"] == "hi"
    assert updated.json()["supported_languages"] == ["hi", "en", "ml"]
    assert updated.json()["language_switching_enabled"] is True
    assert updated.json()["language_switching_mode"] == "automatic"


@pytest.mark.asyncio
async def test_multilingual_tamil_agents_require_one_voice_covering_every_language(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    fake = FakeSmallestClient()

    async def multilingual_voices():
        return [
            {
                "voiceId": "jordan",
                "displayName": "Jordan",
                "tags": {
                    "language": ["English", "Tamil"],
                    "accent": "Indian",
                    "gender": "female",
                },
                "modelIds": ["lightning-v3.1"],
            }
        ]

    fake.list_voices = multilingual_voices
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)

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

    english_primary = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "English Tamil Agent",
            "system_prompt": "This prompt is long enough.",
            "voice_id": "jordan",
            "language": "en",
            "supported_languages": ["en", "ta"],
            "language_switching_enabled": True,
            "language_switching_mode": "automatic",
        },
    )
    assert english_primary.status_code == 201
    assert english_primary.json()["supported_languages"] == ["en", "ta"]
    assert english_primary.json()["language_switching_enabled"] is True

    tamil_primary = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Tamil English Agent",
            "system_prompt": "This prompt is long enough.",
            "voice_id": "jordan",
            "language": "ta",
            "supported_languages": ["ta", "en"],
            "language_switching_enabled": True,
            "language_switching_mode": "automatic",
        },
    )
    assert tamil_primary.status_code == 201
    assert tamil_primary.json()["language"] == "ta"
    assert tamil_primary.json()["supported_languages"] == ["ta", "en"]
    assert tamil_primary.json()["language_switching_enabled"] is True

    unsupported_language = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Unsupported Tamil Agent",
            "system_prompt": "This prompt is long enough.",
            "voice_id": "jordan",
            "language": "ta",
            "supported_languages": ["ta", "en", "hi"],
        },
    )
    assert unsupported_language.status_code == 422
    assert unsupported_language.json()["detail"].endswith("hi")


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
async def test_voice_language_capabilities_are_enforced_before_save(
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

    assert created.status_code == 422
    assert created.json()["detail"].endswith("ml")
    assert fake.create_calls == 0


@pytest.mark.asyncio
async def test_voice_language_preflight_accepts_base_and_matching_locale_tags(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    fake = FakeSmallestClient()

    async def regional_voices():
        return [
            {
                "voiceId": "regional",
                "displayName": "Regional",
                "tags": {"language": ["English (United States)"]},
                "modelIds": ["lightning-v3.1"],
            }
        ]

    fake.list_voices = regional_voices
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Locale compatible voice",
            "system_prompt": "Accept a provider locale for its matching base language.",
            "voice_id": "regional",
            "language": "en",
            "supported_languages": ["en"],
        },
    )
    assert created.status_code == 201

    updated = await client.patch(
        f"/api/v1/agents/{created.json()['id']}",
        headers=auth_headers,
        json={"language": "en-US", "supported_languages": ["en-US"]},
    )
    assert updated.status_code == 200
    assert updated.json()["language"] == "en-us"
    assert updated.json()["supported_languages"] == ["en-us"]


@pytest.mark.asyncio
async def test_pro_voice_uses_provider_routed_atoms_synthesizer_model(
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
    assert catalog.json()["voices"][0]["synthesizer_model"] == "waves_lightning_v3_1"
    assert catalog.json()["voices"][0]["voice_pool"] == "pro"
    assert catalog.json()["voices"][0]["unavailability_reason"] is None
    assert response.status_code == 200
    assert response.json()["sync_status"] == "synced"
    assert fake.draft_calls[0]["voice_id"] == "rhea"
    assert fake.draft_calls[0]["synthesizer_model"] == "waves_lightning_v3_1"
    assert fake.create_calls == 1


@pytest.mark.asyncio
async def test_clearing_voice_resolves_an_explicit_platform_default_on_sync(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    class MultipleVoiceClient(FakeSmallestClient):
        async def list_voices(self):
            return [
                {
                    "voiceId": "jordan",
                    "displayName": "Jordan",
                    "tags": {"language": ["English", "Hindi"]},
                    "modelIds": ["lightning-v3.1"],
                },
                {
                    "voiceId": "nyah",
                    "displayName": "Nyah",
                    "tags": {"language": ["English", "Hindi"]},
                    "modelIds": ["lightning-v3.1"],
                },
            ]

    fake = MultipleVoiceClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Default voice reset",
            "system_prompt": "Resolve every provider voice selection explicitly.",
            "voice_id": "jordan",
            "supported_languages": ["en", "hi"],
        },
    )
    agent_id = created.json()["id"]
    provisioned = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision",
        headers=auth_headers,
    )
    assert provisioned.status_code == 200
    assert fake.draft_calls[-1]["voice_id"] == "jordan"

    cleared = await client.patch(
        f"/api/v1/agents/{agent_id}",
        headers=auth_headers,
        json={"voice_id": ""},
    )
    assert cleared.status_code == 200
    assert cleared.json()["voice_id"] == ""
    assert cleared.json()["sync_status"] == "dirty"

    synced = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/sync",
        headers=auth_headers,
    )
    assert synced.status_code == 200
    assert synced.json()["sync_status"] == "synced"
    assert fake.draft_calls[-1]["voice_id"] == "nyah"
    assert fake.draft_calls[-1]["synthesizer_model"] == "waves_lightning_v3_1"
    assert synced.json()["provider_config"]["requested_voice_id"] == ""
    assert synced.json()["provider_config"]["resolved_voice_id"] == "nyah"
    assert synced.json()["provider_config"]["voice_resolution_source"] == "platform_default"


@pytest.mark.asyncio
async def test_provider_round_trip_mismatch_blocks_synced_status(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    class MismatchedProviderClient(FakeSmallestClient):
        async def get_agent(self, agent_id, **kwargs):
            provider_agent = await super().get_agent(agent_id, **kwargs)
            provider_agent["_resolvedConfig"]["supportedLanguages"] = ["en"]
            return provider_agent

    fake = MismatchedProviderClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Round trip guard",
            "system_prompt": "Reject provider configuration drift before taking calls.",
            "supported_languages": ["en", "hi"],
        },
    )

    response = await client.post(
        f"/api/v1/agents/{created.json()['id']}/smallest/provision",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["sync_status"] == "error"
    operation = response.json()["provider_config"]["publish"]
    assert operation["phase"] == "provider_config_mismatch"
    assert operation["configuration_mismatches"] == ["supported_languages"]
    assert response.json()["last_synced_at"] is None


@pytest.mark.asyncio
async def test_published_revision_must_also_match_active_runtime_configuration(
    client: AsyncClient,
    auth_headers,
    monkeypatch,
):
    class StaleActiveProviderClient(FakeSmallestClient):
        active_caught_up = False

        async def get_agent(self, agent_id, **kwargs):
            provider_agent = await super().get_agent(agent_id, **kwargs)
            if not kwargs.get("version_id") and not self.active_caught_up:
                provider_agent["_resolvedConfig"]["firstMessage"] = "Stale active greeting"
            return provider_agent

    fake = StaleActiveProviderClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Active runtime guard",
            "system_prompt": "Verify the published revision is also serving production calls.",
            "greeting_message": "Current greeting",
        },
    )

    response = await client.post(
        f"/api/v1/agents/{created.json()['id']}/smallest/provision",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["sync_status"] == "error"
    operation = response.json()["provider_config"]["publish"]
    assert operation["published_configuration_mismatches"] == []
    assert operation["active_configuration_mismatches"] == ["greeting_message"]
    assert operation["configuration_mismatches"] == ["greeting_message"]

    mutation_counts = (
        fake.webhook_calls,
        len(fake.draft_calls),
        fake.publish_calls,
        fake.voice_catalog_calls,
    )
    fake.active_caught_up = True
    recovered = await client.post(
        f"/api/v1/agents/{created.json()['id']}/smallest/sync",
        headers=auth_headers,
    )

    assert recovered.status_code == 200
    assert recovered.json()["sync_status"] == "synced"
    assert recovered.json()["provider_revision_id"] == response.json()["provider_revision_id"]
    assert recovered.json()["provider_config"]["publish"]["phase"] == "complete"
    assert (
        fake.webhook_calls,
        len(fake.draft_calls),
        fake.publish_calls,
        fake.voice_catalog_calls,
    ) == mutation_counts


def test_prompt_verification_respects_provider_workflow_type():
    resolved = {
        "prompt": "Canonical single prompt",
        "globalPrompt": "Workflow graph prompt",
    }

    assert (
        agents_endpoint._resolved_system_prompt(
            {"workflowType": "single_prompt"},
            resolved,
        )
        == "Canonical single prompt"
    )
    assert (
        agents_endpoint._resolved_system_prompt(
            {"workflowType": "workflow_graph"},
            resolved,
        )
        == "Workflow graph prompt"
    )
    assert agents_endpoint._resolved_system_prompt({}, resolved) is agents_endpoint._MISSING


@pytest.mark.asyncio
async def test_provider_config_mismatch_accepts_manual_kb_fix_without_republishing(
    client: AsyncClient,
    auth_headers,
    db,
    monkeypatch,
):
    class ManualKnowledgeFixClient(FakeSmallestClient):
        manual_revision = False
        manual_fixed = False

        async def set_agent_webhook_subscriptions(self, **kwargs):
            if self.manual_revision:
                raise AssertionError("configuration recovery must not update webhooks")
            return await super().set_agent_webhook_subscriptions(**kwargs)

        async def update_agent_draft(self, **kwargs):
            if self.manual_revision:
                raise AssertionError("configuration recovery must not update a draft")
            return await super().update_agent_draft(**kwargs)

        async def publish_draft(self, **kwargs):
            if self.manual_revision:
                raise AssertionError("configuration recovery must not publish")
            return await super().publish_draft(**kwargs)

        async def get_latest_branch_revision(self, **kwargs):
            if self.manual_revision:
                return {"_id": "revision_124", "label": "Manual knowledge correction"}
            return await super().get_latest_branch_revision(**kwargs)

        async def get_branch_revision(self, **kwargs):
            if self.manual_revision:
                return {
                    "_id": kwargs["revision_id"],
                    "label": "Manual knowledge correction",
                    "status": "published",
                    "securityCheck": {"status": "passed"},
                }
            return await super().get_branch_revision(**kwargs)

        async def get_agent(self, agent_id, **kwargs):
            provider_agent = await super().get_agent(agent_id, **kwargs)
            draft = self.draft_calls[-1]
            provider_agent["workflowType"] = "single_prompt"
            provider_agent["_resolvedConfig"]["globalPrompt"] = "Obsolete legacy prompt"
            provider_agent["_resolvedConfig"]["prompt"] = draft["global_prompt"]
            if self.manual_fixed:
                provider_agent["_resolvedConfig"]["tools"] = [
                    {
                        "type": "knowledge_base_search",
                        "enabled": True,
                        "knowledgeBaseId": "provider_kb_123",
                    }
                ]
            return provider_agent

    fake = ManualKnowledgeFixClient()
    monkeypatch.setattr(agents_endpoint, "get_smallest_client", lambda: fake)
    created = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Manual KB recovery",
            "system_prompt": "Use the approved knowledge base for every answer.",
        },
    )
    agent_id = UUID(created.json()["id"])
    agent = await db.scalar(select(Agent).where(Agent.id == agent_id))
    knowledge_base = KnowledgeBase(
        tenant_id=agent.tenant_id,
        name="Approved provider knowledge",
        provider_knowledge_base_id="provider_kb_123",
        sync_status="ready",
        approval_status="approved",
    )
    db.add(knowledge_base)
    await db.flush()
    binding = AgentKnowledgeBinding(
        tenant_id=agent.tenant_id,
        agent_id=agent.id,
        knowledge_base_id=knowledge_base.id,
        sync_status="pending",
    )
    db.add(binding)
    await db.commit()

    first_publish = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/provision",
        headers=auth_headers,
    )

    assert first_publish.status_code == 200
    assert first_publish.json()["sync_status"] == "error"
    first_operation = first_publish.json()["provider_config"]["publish"]
    assert first_operation["phase"] == "provider_config_mismatch"
    assert first_operation["configuration_mismatches"] == ["global_knowledge_base_id"]
    operation_id = first_operation["id"]
    mutation_counts = (
        fake.webhook_calls,
        len(fake.draft_calls),
        fake.publish_calls,
        fake.voice_catalog_calls,
    )

    fake.manual_revision = True
    still_stale = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/sync",
        headers=auth_headers,
    )

    assert still_stale.status_code == 200
    assert still_stale.json()["sync_status"] == "error"
    assert still_stale.json()["provider_revision_id"] == "revision_123"
    stale_operation = still_stale.json()["provider_config"]["publish"]
    assert stale_operation["phase"] == "provider_config_mismatch"
    assert stale_operation["active_configuration_mismatches"] == ["global_knowledge_base_id"]
    assert (
        fake.webhook_calls,
        len(fake.draft_calls),
        fake.publish_calls,
        fake.voice_catalog_calls,
    ) == mutation_counts

    fake.manual_fixed = True
    recovered = await client.post(
        f"/api/v1/agents/{agent_id}/smallest/sync",
        headers=auth_headers,
    )

    assert recovered.status_code == 200
    assert recovered.json()["sync_status"] == "synced"
    assert recovered.json()["provider_revision_id"] == "revision_124"
    assert recovered.json()["last_synced_at"] is not None
    recovered_operation = recovered.json()["provider_config"]["publish"]
    assert recovered_operation["id"] == operation_id
    assert recovered_operation["phase"] == "complete"
    assert recovered_operation["reconciled_revision_id"] == "revision_124"
    assert recovered_operation["reconciled_revision_label"] == "Manual knowledge correction"
    assert recovered_operation["configuration_mismatches"] == []
    assert recovered_operation["published_configuration_mismatches"] == []
    assert recovered_operation["active_configuration_mismatches"] == []
    persisted_binding = await db.scalar(
        select(AgentKnowledgeBinding)
        .where(AgentKnowledgeBinding.agent_id == agent_id)
        .execution_options(populate_existing=True)
    )
    assert persisted_binding.sync_status == "synced"
    assert persisted_binding.last_synced_at is not None
    assert (
        fake.webhook_calls,
        len(fake.draft_calls),
        fake.publish_calls,
        fake.voice_catalog_calls,
    ) == mutation_counts


@pytest.mark.asyncio
async def test_language_switching_rejects_inconsistent_and_single_language_tuples(
    client: AsyncClient,
    auth_headers,
):
    inconsistent = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Inconsistent switching",
            "system_prompt": "Reject inconsistent language switching policies.",
            "supported_languages": ["en", "hi"],
            "language_switching_enabled": False,
            "language_switching_mode": "automatic",
        },
    )
    single_language = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Single switching",
            "system_prompt": "Reject automatic switching for one language.",
            "supported_languages": ["en"],
            "language_switching_enabled": True,
            "language_switching_mode": "automatic",
        },
    )

    assert inconsistent.status_code == 422
    assert single_language.status_code == 422


@pytest.mark.asyncio
async def test_cross_region_language_set_is_not_rejected_without_voice_evidence(
    client: AsyncClient,
    auth_headers,
):
    response = await client.post(
        "/api/v1/agents",
        headers=auth_headers,
        json={
            "name": "Provider governed language set",
            "system_prompt": "Let the selected provider voice define its language coverage.",
            "language": "en",
            "supported_languages": ["en", "ml", "fr"],
            "language_switching_enabled": True,
            "language_switching_mode": "automatic",
        },
    )

    assert response.status_code == 201
    assert response.json()["supported_languages"] == ["en", "ml", "fr"]
    assert response.json()["language_switching_enabled"] is True
