"""The voice tool must retain the caller question, not just an LLM rewrite."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from livekit.agents import llm

from app.livekit_runtime import worker
from app.livekit_runtime.worker import VAVInworldRealtimeAgent
from app.models.agent import KnowledgeBase, KnowledgeSource
from app.services.knowledge_serving import publish_serving_revision
from app.services.speech_lexicon import publish_speech_lexicon


def agent_with_history(*messages):
    model = SimpleNamespace(id=uuid4(), tenant_id=uuid4(), system_prompt="Use approved knowledge.")
    agent = VAVInworldRealtimeAgent(model=model)
    agent._chat_ctx = llm.ChatContext()
    for role, content in messages:
        agent._chat_ctx.add_message(role=role, content=content)
    agent._retrieve_approved_knowledge = AsyncMock(return_value="approved result")
    return agent


@pytest.mark.asyncio
async def test_model_rewording_keeps_original_caller_query():
    caller = "What's the name of your dental doctor?"
    agent = agent_with_history(("user", caller), ("assistant", "I will check."))
    result = await agent.search_approved_knowledge(
        query="Who is the dentist at the center?",
        semantic_query="Which clinician provides dentistry?",
    )
    assert result == "approved result"
    agent._retrieve_approved_knowledge.assert_awaited_once_with(
        query="Who is the dentist at the center?",
        query_variants=(caller, "Which clinician provides dentistry?"),
    )


@pytest.mark.asyncio
async def test_only_latest_caller_question_is_retained():
    agent = agent_with_history(
        ("user", "Who is your dental doctor?"),
        ("assistant", "Dr Example."),
        ("user", "What departments are available?"),
    )
    await agent.search_approved_knowledge(query="List the center's specialties.")
    assert agent._retrieve_approved_knowledge.call_args.kwargs["query_variants"] == (
        "What departments are available?",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("control", ["Thank you. Goodbye.", "stop", "wait"])
async def test_control_turn_does_not_reuse_old_business_question(control):
    agent = agent_with_history(("user", "Who is your dental doctor?"), ("user", control))
    await agent.search_approved_knowledge(query="Unfinished model search")
    assert agent._retrieve_approved_knowledge.call_args.kwargs["query_variants"] == ()


@pytest.mark.asyncio
async def test_tool_remains_usable_without_chat_history():
    agent = agent_with_history()
    await agent.search_approved_knowledge(query="What services are offered?")
    agent._retrieve_approved_knowledge.assert_awaited_once_with(
        query="What services are offered?", query_variants=()
    )


@pytest.mark.asyncio
async def test_real_native_tool_rewrite_retrieves_caller_fact(db, tenant, monkeypatch):
    company = "Royal Medical Center One Day Surgery"
    kb = KnowledgeBase(
        tenant_id=tenant.id,
        name=company,
        owner_company=company,
        sync_status="ready",
        approval_status="draft",
        source_count=1,
        indexed_source_count=1,
    )
    kb.sources.append(
        KnowledgeSource(
            tenant_id=tenant.id,
            source_type="text",
            name="Dental Doctor",
            status="indexed",
            content="Dr Kevin is the dental doctor",
            structured_content={
                "facts": [
                    {
                        "subject": "Dr Kevin",
                        "predicate": "role",
                        "value": "dental doctor",
                        "evidence": "Dr Kevin is the dental doctor",
                        "search_phrases": ["Who is the dental doctor?"],
                    }
                ]
            },
        )
    )
    db.add(kb)
    await db.flush()
    lexicon = await publish_speech_lexicon(
        db,
        tenant_id=tenant.id,
        knowledge_base=kb,
        allow_draft_for_approval=True,
    )
    revision = await publish_serving_revision(
        db,
        tenant_id=tenant.id,
        knowledge_base=kb,
        speech_lexicon=lexicon,
        allow_draft_for_approval=True,
    )

    @asynccontextmanager
    async def session():
        yield db

    monkeypatch.setattr(worker, "async_session_factory", session)
    agent = VAVInworldRealtimeAgent(
        model=SimpleNamespace(
            id=uuid4(), tenant_id=tenant.id, name=company, system_prompt="Use approved knowledge."
        ),
        knowledge_serving_revision_id=revision.id,
        knowledge_base_id=kb.id,
    )
    agent._chat_ctx = llm.ChatContext()
    agent._chat_ctx.add_message(role="user", content="What's the name of your dental doctor?")
    # Observed native request: location/name qualifiers are model-added, not
    # present in the caller's question or the short source text.
    query = "Royal Medical Center One Day Surgery Abu Dhabi dental doctor name"
    semantic = (
        "Which dentist or dental doctors are listed for "
        "Royal Medical Center One Day Surgery in Abu Dhabi?"
    )
    assert (
        await agent._retrieve_approved_knowledge(
            query=query,
            query_variants=(semantic,),
        )
        == "NO_VERIFIED_KNOWLEDGE_MATCH"
    )
    result = await agent.search_approved_knowledge(query=query, semantic_query=semantic)
    assert "Dr Kevin" in result
    assert "dental doctor" in result

    agent._chat_ctx.add_message(
        role="user",
        content="Who is the dental doctor at Different Medical Center?",
    )
    result = await agent.search_approved_knowledge(
        query="Who is the dental doctor at Different Medical Center?",
    )
    assert result == "NO_VERIFIED_KNOWLEDGE_MATCH"
