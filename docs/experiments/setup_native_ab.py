"""Create two authorized browser-only QA copies; never change the source agent."""

import asyncio
import copy
import hashlib
import json
import sys
import uuid
from types import SimpleNamespace

from app.api.v1.endpoints.agents import create_agent
from app.core.database import async_session_factory
from app.models.agent import (
    Agent,
    AgentKnowledgeBinding,
    AgentRuntimeProfile,
    KnowledgeBase,
)
from app.schemas.agent import AgentCreate
from app.services.audit import record_audit_event
from sqlalchemy import inspect, select

SOURCE = uuid.UUID("0d7747ef-6d2a-448a-b7d0-4f9335ea178f")
RUN = "native-ab-20260906"


async def main():
    async with async_session_factory() as db:
        src = await db.get(Agent, SOURCE)
        assert src.name == "VAV Production Voice Quality QA"
        profile = await db.scalar(
            select(AgentRuntimeProfile).where(AgentRuntimeProfile.agent_id == SOURCE)
        )
        binding = await db.scalar(
            select(AgentKnowledgeBinding).where(
                AgentKnowledgeBinding.agent_id == SOURCE
            )
        )
        kb = await db.get(KnowledgeBase, binding.knowledge_base_id)
        assert kb.tenant_id == src.tenant_id and kb.approval_status == "approved"
        assert profile.runtime_config.get("inworld_single_pass") is True
        print(
            "AB:"
            + json.dumps(
                {
                    "event": "source",
                    "source": str(SOURCE),
                    "knowledge": str(kb.id),
                    "llm": profile.llm_model,
                    "stt_language": profile.stt_language,
                    "voice": src.voice_id,
                    "speech_rate": src.speech_rate,
                    "runtime_config_keys": sorted(profile.runtime_config),
                    "prompt_sha256": hashlib.sha256(
                        src.system_prompt.encode()
                    ).hexdigest(),
                }
            ),
            flush=True,
        )
        for lane, single in [("A", True), ("B", False)]:
            name = f"VAV Latency QA {lane} - " + (
                "Current" if single else "Native Inworld"
            )
            existing = await db.scalar(
                select(Agent).where(
                    Agent.tenant_id == src.tenant_id, Agent.name == name
                )
            )
            if existing:
                assert (existing.agent_metadata or {}).get("qa_ab_run") == RUN
                print(
                    "AB:"
                    + json.dumps(
                        {
                            "event": "existing",
                            "lane": lane,
                            "id": str(existing.id),
                            "name": name,
                        }
                    ),
                    flush=True,
                )
                continue
            if "--create" not in sys.argv:
                print(
                    "AB:"
                    + json.dumps({"event": "planned", "lane": lane, "name": name}),
                    flush=True,
                )
                continue
            fields = {
                k: copy.deepcopy(getattr(src, k))
                for k in AgentCreate.model_fields
                if hasattr(src, k)
            }
            fields.update(
                name=name,
                description=f"Browser-only controlled {RUN} comparison. No phone assignment.",
                transfer_number=None,
            )
            created = await create_agent(
                data=AgentCreate(**fields),
                current_user=SimpleNamespace(tenant_id=src.tenant_id, id=None),
                db=db,
            )
            dst = await db.get(Agent, created.id)
            dst.agent_metadata = {
                **copy.deepcopy(src.agent_metadata or {}),
                "qa_ab_run": RUN,
                "qa_ab_lane": lane,
                "qa_source_agent_id": str(SOURCE),
            }
            skip = {"id", "tenant_id", "agent_id", "created_at", "updated_at"}
            values = {
                a.key: copy.deepcopy(getattr(profile, a.key))
                for a in inspect(AgentRuntimeProfile).column_attrs
                if a.key not in skip
            }
            values["assigned_numbers"] = []
            values["runtime_config"] = {
                **values["runtime_config"],
                "inworld_single_pass": single,
            }
            db.add(
                AgentRuntimeProfile(tenant_id=src.tenant_id, agent_id=dst.id, **values)
            )
            db.add(
                AgentKnowledgeBinding(
                    tenant_id=src.tenant_id,
                    agent_id=dst.id,
                    knowledge_base_id=kb.id,
                    provider=binding.provider,
                    sync_status=binding.sync_status,
                )
            )
            await record_audit_event(
                db,
                tenant_id=src.tenant_id,
                actor_user_id=None,
                action="agent.qa_ab_created",
                resource_type="agent",
                resource_id=str(dst.id),
                details={
                    "experiment": RUN,
                    "lane": lane,
                    "source_agent_id": str(SOURCE),
                    "knowledge_base_id": str(kb.id),
                    "phone_numbers_assigned": False,
                },
            )
            await db.commit()
            print(
                "AB:"
                + json.dumps(
                    {"event": "created", "lane": lane, "id": str(dst.id), "name": name}
                ),
                flush=True,
            )


asyncio.run(main())
