"""QA pair only: align TTS models and select provider-native control on B."""

import asyncio
import json
import uuid

from app.core.database import async_session_factory
from app.models.agent import Agent, AgentRuntimeProfile
from app.models.call import Call
from app.services.audit import record_audit_event
from sqlalchemy import select


async def main():
    async with async_session_factory() as db:
        for lane, aid in [
            ("A", "ae2a1477-709d-41f7-a150-f952a657d1e5"),
            ("B", "5730e252-002b-42be-ad13-554358c788e4"),
        ]:
            a = await db.get(Agent, uuid.UUID(aid))
            assert (
                a.agent_metadata.get("qa_ab_run") == "native-ab-20260906"
                and a.agent_metadata.get("qa_ab_lane") == lane
            )
            assert (
                await db.scalar(
                    select(Call.id)
                    .where(
                        Call.agent_id == a.id,
                        Call.status.in_(["initiated", "ringing", "in_progress"]),
                    )
                    .limit(1)
                )
                is None
            )
            p = await db.scalar(
                select(AgentRuntimeProfile)
                .where(AgentRuntimeProfile.agent_id == a.id)
                .with_for_update()
            )
            config = dict(p.runtime_config)
            assert (
                config["inworld_single_pass"] == (lane == "A")
                and not p.assigned_numbers
            )
            # A's deterministic speech already uses TTS-2; match B's realtime output.
            config["inworld_realtime_tts_model"] = "inworld-tts-2"
            config["provider_native_turns_qa"] = lane == "B"
            p.runtime_config = config
            await record_audit_event(
                db,
                tenant_id=a.tenant_id,
                actor_user_id=None,
                action="agent.qa_ab_configured",
                resource_type="agent",
                resource_id=aid,
                details={
                    "experiment": "native-ab-20260906",
                    "lane": lane,
                    "tts_model": "inworld-tts-2",
                    "provider_native_turns": lane == "B",
                },
            )
            print(
                "AB_CONFIGURED:"
                + json.dumps(
                    {
                        "lane": lane,
                        "id": aid,
                        "native_turns": lane == "B",
                        "tts_model": "inworld-tts-2",
                    }
                ),
                flush=True,
            )
        await db.commit()


asyncio.run(main())
