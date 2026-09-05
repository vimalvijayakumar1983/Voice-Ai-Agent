"""Meaning-preserving, metered, optional search repair regressions."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.livekit_runtime import worker
from app.services import knowledge_query_interpreter as interpreter
from app.services.call_metadata import public_call_metadata
from app.services.knowledge_retrieval import (
    _query_aware_excerpt,
    _structured_core,
    _structured_retrieval_content,
    rank_knowledge,
)
from tests.test_conversation_scope import setup_runtime


def client_reply(content, usage=True):
    create = AsyncMock(
        return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=300, completion_tokens=20) if usage else None,
        )
    )
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


async def test_interpreter_only_plans_and_reports_actual_usage():
    client = client_reply('{"action":"search","query":"What is the telephone number?"}')
    result = await interpreter.interpret_knowledge_question(
        api_key="test", question="How can I ring your office?", company="Northstar", client=client
    )
    assert result.plan.query == "What is the telephone number?"
    assert (result.input_tokens, result.output_tokens, result.attempted) == (300, 20, True)
    args = client.chat.completions.create.call_args.kwargs
    assert args["response_format"]["json_schema"]["strict"] is True
    assert json.loads(args["messages"][1]["content"])["company"] == "Northstar"
    assert client.chat.completions.create.await_count == 1


@pytest.mark.parametrize(
    "question,rewrite",
    [
        ("What is the price for Botox?", "What Botox services do you offer?"),
        ("Is a doctor available tomorrow?", "Who are your doctors?"),
        ("Is the chairman Saeed?", "Who is the president?"),
        ("Can I cancel the appointment?", "Can I book an appointment?"),
        ("Was it founded in 2003?", "When was it founded?"),
        ("Who sits at the top of the organisation?", "Who is the chairman?"),
    ],
)
async def test_repair_cannot_drop_critical_question_slots(question, rewrite):
    client = client_reply(json.dumps({"action": "search", "query": rewrite}))
    result = await interpreter.interpret_knowledge_question(
        api_key="test", question=question, company="Northstar", client=client
    )
    assert result.plan.action == "clarify"


@pytest.mark.parametrize(
    "content", ["not JSON", '{"action":"search","query":"","answer":"invented"}']
)
async def test_invalid_plan_keeps_usage_but_never_becomes_evidence(content):
    result = await interpreter.interpret_knowledge_question(
        api_key="test", question="Question", company="Northstar", client=client_reply(content)
    )
    assert result.status == "error" and result.plan is None
    assert result.input_tokens == 300


async def test_timeout_and_cancellation_do_not_retry_or_report_zero_usage(monkeypatch):
    monkeypatch.setattr(interpreter, "QUERY_TIMEOUT_SECONDS", 0.01)
    client = client_reply("{}")

    async def slow(**kwargs):
        await asyncio.sleep(10)

    client.chat.completions.create.side_effect = slow
    result = await interpreter.interpret_knowledge_question(
        api_key="test", question="Question", company="Northstar", client=client
    )
    assert result.status == "timeout" and result.attempted
    assert result.input_tokens is None
    client.chat.completions.create.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await interpreter.interpret_knowledge_question(
            api_key="test", question="Question", company="Northstar", client=client
        )


async def test_no_key_or_oversize_question_makes_no_request():
    client = client_reply("{}")
    for key, question in [("", "Question"), ("test", "x" * 801)]:
        result = await interpreter.interpret_knowledge_question(
            api_key=key, question=question, company="Northstar", client=client
        )
        assert not result.attempted and result.input_tokens is None
    client.chat.completions.create.assert_not_awaited()


def test_shared_quote_cannot_rank_an_unrelated_fact_or_detached_evidence():
    facts = []
    for value in ["Transport", "Healthcare", "Trading"]:
        facts.append(
            {
                "subject": "Northstar Group",
                "predicate": "business segment",
                "value": value,
                "search_phrases": [f"Tell me about {value}"],
                "evidence": "Northstar has Healthcare, Trading and Transport businesses.",
            }
        )
    content = _structured_retrieval_content({"facts": facts})
    matches = rank_knowledge(
        "Tell me briefly about Northstar Group healthcare division", [("Group", content)]
    )
    assert len(matches) == 1
    assert "business segment: Healthcare" in _structured_core(matches[0].text)
    assert not rank_knowledge("Northstar Group healthcare pricing", [("Group", content)])
    excerpt = _query_aware_excerpt(content, {"healthcare"}, limit=350)
    assert "business segment: Healthcare" in excerpt
    assert excerpt.startswith("VERIFIED STRUCTURED FACTS")


async def enabled_runtime(db, tenant, monkeypatch):
    runtime, _ = await setup_runtime(
        db, tenant, monkeypatch, "Northstar Group", "Northstar Trading"
    )
    runtime._company_scope.semantic_retrieval_enabled = True
    runtime._telemetry = worker._LiveKitRuntimeTelemetry({}, [], 0)
    monkeypatch.setattr(worker, "load_provider_config", AsyncMock(return_value={"api_key": "test"}))
    return runtime


def repaired(query="What is the phone number?"):
    return interpreter.SearchRepairResult(
        interpreter.SearchRepair(action="search", query=query), "completed", 120, 300, 20, True
    )


async def test_real_scoped_retrieval_repairs_once_caches_and_keeps_step1(db, tenant, monkeypatch):
    runtime = await enabled_runtime(db, tenant, monkeypatch)
    planner = AsyncMock(return_value=repaired())
    monkeypatch.setattr(worker, "interpret_knowledge_question", planner)
    original = "How can I ring your office?"
    result = await runtime.retrieve_single_pass_evidence(original)
    assert "665 9998" in result and "551 3831" not in result
    assert planner.await_count == 1
    assert "primary telephone" in planner.call_args.kwargs["search_vocabulary"]
    assert not any("Trading" in term for term in planner.call_args.kwargs["search_vocabulary"])
    await runtime.retrieve_single_pass_evidence(original)
    assert planner.await_count == 1
    result = await runtime.retrieve_single_pass_evidence("What is the phone number?")
    runtime.prepare_spoken_response("What is the phone number?", result)("+971 2 665 9998")
    assert "6, 6, 5" in await runtime.retrieve_single_pass_evidence("Repeat slowly")
    assert planner.await_count == 1
    assert runtime._telemetry.current_turn_trace["knowledge_result"] == "verified"
    metrics = runtime._telemetry.runtime_metrics
    assert metrics["knowledge_interpretation_requests"] == 1
    assert metrics["knowledge_interpretation_input_tokens"] == 300


async def test_default_off_does_not_call_extra_model(db, tenant, monkeypatch):
    runtime, _ = await setup_runtime(
        db, tenant, monkeypatch, "Northstar Group", "Northstar Trading"
    )
    planner = AsyncMock()
    monkeypatch.setattr(worker, "interpret_knowledge_question", planner)
    await runtime.retrieve_single_pass_evidence("How can I ring your office?")
    planner.assert_not_awaited()


async def test_repaired_no_match_cannot_loop(db, tenant, monkeypatch):
    runtime = await enabled_runtime(db, tenant, monkeypatch)
    planner = AsyncMock(return_value=repaired("What is the annual revenue?"))
    monkeypatch.setattr(worker, "interpret_knowledge_question", planner)
    assert (
        await runtime.retrieve_single_pass_evidence("How rich is the organisation?")
        == "NO_VERIFIED_KNOWLEDGE_MATCH"
    )
    assert planner.await_count == 1


async def test_model_cannot_switch_company(db, tenant, monkeypatch):
    runtime = await enabled_runtime(db, tenant, monkeypatch)
    monkeypatch.setattr(
        worker,
        "interpret_knowledge_question",
        AsyncMock(return_value=repaired("Northstar Trading phone number?")),
    )
    result = await runtime.retrieve_single_pass_evidence("How can I ring your office?")
    assert "Which company" in result and "551" not in result
    assert runtime._single_pass_active_subject == "Northstar Group"


async def test_stale_company_epoch_rejects_repair_even_if_company_switches_back(
    db, tenant, monkeypatch
):
    runtime = await enabled_runtime(db, tenant, monkeypatch)

    async def switch(**kwargs):
        runtime._switch_company("Northstar Trading")
        runtime._switch_company("Northstar Group")
        return repaired()

    monkeypatch.setattr(worker, "interpret_knowledge_question", switch)
    result = await runtime.retrieve_single_pass_evidence("How can I ring your office?")
    assert "Which company" in result and "665" not in result


def test_public_metadata_preserves_repair_usage_unknown_and_time():
    metadata = public_call_metadata(
        {
            "agent_configuration": {},
            "runtime": {
                "knowledge_interpretation_model": "gpt-4o-mini",
                "knowledge_interpretation_requests": 1,
                "knowledge_interpretation_usage_incomplete": True,
                "turn_diagnostics": [
                    {
                        "knowledge_interpretation_ms": 123,
                        "knowledge_interpretation_status": "timeout",
                    }
                ],
            },
        }
    )
    assert metadata["runtime"]["knowledge_interpretation_usage_incomplete"] is True
    assert metadata["runtime"]["turn_diagnostics"][0]["knowledge_interpretation_ms"] == 123


@pytest.mark.parametrize("known", [True, False])
def test_cost_report_separates_search_repair_and_unknown_usage(known):
    from app.models.call import Call
    from app.services.cost_reporting import _call_components

    runtime = {
        "knowledge_interpretation_model": "gpt-4o-mini",
        "knowledge_interpretation_requests": 1,
        "knowledge_interpretation_usage_incomplete": not known,
    }
    if known:
        runtime.update(
            knowledge_interpretation_input_tokens=1000, knowledge_interpretation_output_tokens=100
        )
    call = Call(
        provider="livekit",
        status="completed",
        duration_seconds=60,
        call_metadata={"runtime": runtime},
    )
    components, missing = _call_components(call, None, None)
    repairs = [part for part in components if "Knowledge question" in part["service"]]
    if known:
        assert len(repairs) == 2
        assert sum(part["cost_usd"] for part in repairs) == pytest.approx(0.00021)
        assert not any("Knowledge question" in part for part in missing)
    else:
        assert not repairs
        assert any("incomplete provider usage" in part for part in missing)
