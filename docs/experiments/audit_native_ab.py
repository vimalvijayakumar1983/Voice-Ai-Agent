"""Read only: scoped synthetic QA call evidence, no credentials."""

import asyncio
import json
import sys
import uuid

from app.core.database import async_session_factory
from app.models.agent import Agent, AgentRuntimeProfile
from app.models.call import Call
from sqlalchemy import select


async def main():
    ids = [
        uuid.UUID(x)
        for x in [
            "ae2a1477-709d-41f7-a150-f952a657d1e5",
            "5730e252-002b-42be-ad13-554358c788e4",
        ]
    ]
    async with async_session_factory() as db:
        query = (
            select(Call)
            .where(Call.agent_id.in_(ids))
            .order_by(Call.created_at.desc())
            .limit(20)
        )
        if "--call" in sys.argv:
            query = select(Call).where(
                Call.agent_id.in_(ids),
                Call.id == uuid.UUID(sys.argv[sys.argv.index("--call") + 1]),
            )
        calls = (await db.scalars(query)).all()
        for c in calls:
            r = (c.call_metadata or {}).get("runtime", {})
            keys = [
                k
                for k in r
                if any(
                    s in k
                    for s in [
                        "provider_native_turns_qa",
                        "native_qa_tool_requests",
                        "native_request_ledger_available",
                        "serialized_model",
                        "serialized_language",
                        "knowledge_turn_mode",
                        "turn_latency",
                        "knowledge_tool",
                        "knowledge_serving",
                        "stt_model",
                        "stt_language",
                        "tts_model",
                        "llm_model",
                        "usage",
                        "token",
                        "grounding",
                        "knowledge_match",
                        "conversation_requests",
                        "error",
                        "recording",
                    ]
                )
                and not any(
                    s in k
                    for s in ["secret", "access_token", "credential", "api_key", "url"]
                )
            ]
            traces = r.get("turn_diagnostics", [])
            if "--compact" in sys.argv:
                fields = [
                    "turn",
                    "outcome",
                    "barge_in",
                    "knowledge_result",
                    "knowledge_tool_ms",
                    "speech_end_to_first_audio_ms",
                    "transcript_after_speech_ms",
                    "llm_first_token_ms",
                    "tts_first_byte_ms",
                    "single_pass_retrieval_ms",
                    "grounding_outcome",
                    "exact_fact_evidence_ids",
                ]
                traces = [{k: t[k] for k in fields if k in t} for t in traces]
            print(
                "AB_AUDIT:"
                + json.dumps(
                    {
                        "call_id": str(c.id),
                        "agent_id": str(c.agent_id),
                        "status": c.status,
                        "duration": c.duration_seconds,
                        "metrics": {k: r[k] for k in keys},
                        "trace": traces,
                        "transcript": c.transcript.turns if c.transcript else None,
                    },
                    default=str,
                ),
                flush=True,
            )
        for aid in ids:
            a = await db.get(Agent, aid)
            p = await db.scalar(
                select(AgentRuntimeProfile).where(AgentRuntimeProfile.agent_id == aid)
            )
            print(
                "AB_CONFIG:"
                + json.dumps(
                    {
                        "id": str(aid),
                        "name": a.name,
                        "config": p.runtime_config,
                        "metadata_keys": sorted(a.agent_metadata or {}),
                    }
                ),
                flush=True,
            )


asyncio.run(main())
