"""Data mapping performed by migration 20260910_027 (knowledge served locally)."""

import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.models.agent import (
    Agent,
    AgentKnowledgeBinding,
    KnowledgeBase,
    KnowledgeProviderCleanup,
    KnowledgeSource,
)

_MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "migrations"
    / "versions"
    / "20260910_027_knowledge_local_only.py"
)


def _load_migration():
    spec = importlib.util.spec_from_file_location("knowledge_local_only_migration", _MIGRATION)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_migration_maps_legacy_provider_state_to_local_serving(tenant, db):
    native_agent = Agent(
        tenant_id=tenant.id, name="Inworld", system_prompt="x", voice_provider="inworld"
    )
    legacy_agent = Agent(
        tenant_id=tenant.id, name="Smallest", system_prompt="x", voice_provider="smallest"
    )
    provisioning_ready = KnowledgeBase(
        tenant_id=tenant.id,
        name="Was provisioning, sources indexed",
        provider="smallest",
        provider_knowledge_base_id="remote-kb-1",
        sync_status="provisioning",
    )
    provisioning_ready.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="text",
            name="FAQ",
            content="Approved answers.",
            status="indexed",
            provider_item_id="remote-item-1",
        )
    )
    provisioning_failed = KnowledgeBase(
        tenant_id=tenant.id,
        name="Was provisioning, one failure",
        provider="smallest",
        sync_status="provisioning",
    )
    provisioning_failed.sources.extend(
        [
            KnowledgeSource(
                tenant_id=tenant.id, source_type="text", name="A", content="a", status="indexed"
            ),
            KnowledgeSource(tenant_id=tenant.id, source_type="url", name="B", status="failed"),
        ]
    )
    provisioning_empty = KnowledgeBase(
        tenant_id=tenant.id, name="Was provisioning, empty", sync_status="provisioning"
    )
    db.add_all(
        [native_agent, legacy_agent, provisioning_ready, provisioning_failed, provisioning_empty]
    )
    await db.flush()
    db.add_all(
        [
            AgentKnowledgeBinding(
                tenant_id=tenant.id,
                agent_id=native_agent.id,
                knowledge_base_id=provisioning_ready.id,
                provider="inworld",
                sync_status="synced",
                last_synced_at=datetime.now(UTC),
            ),
            AgentKnowledgeBinding(
                tenant_id=tenant.id,
                agent_id=legacy_agent.id,
                knowledge_base_id=provisioning_ready.id,
                provider="smallest",
                sync_status="synced",
                last_synced_at=datetime.now(UTC),
            ),
            KnowledgeProviderCleanup(
                tenant_id=tenant.id,
                knowledge_base_id=provisioning_ready.id,
                provider="smallest",
                provider_knowledge_base_id="remote-kb-1",
                provider_item_id="remote-item-old",
                status="pending",
                available_at=datetime.now(UTC),
            ),
            KnowledgeProviderCleanup(
                tenant_id=tenant.id,
                knowledge_base_id=provisioning_ready.id,
                provider="smallest",
                provider_knowledge_base_id="remote-kb-1",
                provider_item_id="remote-item-done",
                status="completed",
                available_at=datetime.now(UTC),
            ),
        ]
    )
    await db.commit()

    migration = _load_migration()
    for statement in migration.upgrade_statements():
        await db.execute(statement)
    await db.commit()
    db.expire_all()

    bases = {kb.name: kb for kb in (await db.scalars(select(KnowledgeBase))).all()}
    assert {kb.provider for kb in bases.values()} == {"vav"}
    assert bases["Was provisioning, sources indexed"].sync_status == "ready"
    assert bases["Was provisioning, one failure"].sync_status == "error"
    assert bases["Was provisioning, empty"].sync_status == "local_only"
    # Remote handles survive so the collections can be decommissioned explicitly.
    assert bases["Was provisioning, sources indexed"].provider_knowledge_base_id == "remote-kb-1"
    source = await db.scalar(select(KnowledgeSource).where(KnowledgeSource.name == "FAQ"))
    assert source.provider_item_id == "remote-item-1"

    bindings = {
        binding.provider: binding
        for binding in (await db.scalars(select(AgentKnowledgeBinding))).all()
    }
    assert bindings["inworld"].sync_status == "synced"
    assert bindings["smallest"].sync_status == "pending"
    assert bindings["smallest"].last_synced_at is None

    cleanups = {
        row.provider_item_id: row
        for row in (await db.scalars(select(KnowledgeProviderCleanup))).all()
    }
    assert cleanups["remote-item-old"].status == "cancelled"
    assert "20260910_027" in cleanups["remote-item-old"].last_error
    assert cleanups["remote-item-done"].status == "completed"
