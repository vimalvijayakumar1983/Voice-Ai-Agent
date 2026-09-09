"""Whole-request evidence, safe analysis, authorization and delivery regression tests."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from livekit.agents import llm

from app.livekit_runtime import mcp_tools
from app.livekit_runtime.mcp_answer_flow import (
    ANSWER_FLOW_FLAG,
    InworldAnswerVerifier,
    MCPAnswerFlow,
    enabled,
    evidence_packet,
    gate_native_audio,
    numbers_supported,
    spoken_draft,
)
from tests.test_mcp_delivery import context, result
from tests.test_report_presentation import report


def setup_flow(verifier=None):
    turns, entries, metrics = ["August revenue?"], [], {}
    verify = verifier or AsyncMock(return_value=True)
    flow = MCPAnswerFlow(metrics, entries.append, lambda: turns[-1], lambda: len(turns), verify)
    return flow, turns, entries, verify


async def collect(flow, source=None, *, scope="trading", authorize=None, **kwargs):
    payload = json.loads(source or report())
    return json.loads(
        await flow.collect(
            result(json.dumps(payload)),
            tool="sales_summary",
            arguments={k: payload[k] for k in ("start_date", "end_date", "group_by")},
            company="Fixture Trading",
            scope=scope,
            authorize=authorize or AsyncMock(),
            turn=flow.turn(),
            **kwargs,
        )
    )["source_id"]


def monthly(month, shops, gypsum):
    return report(
        start_date=f"2099-{month:02}-01",
        end_date=f"2099-{month:02}-31",
        rows=[
            {"group_value": "Shops", "revenue_ex_vat": shops},
            {"group_value": "Gypsum", "revenue_ex_vat": gypsum},
        ],
    )


def test_flag_requires_explicit_boolean_and_is_per_agent():
    for value in (None, False, "true", 1):
        assert not enabled(SimpleNamespace(runtime_config={ANSWER_FLOW_FLAG: value}))
    assert enabled(SimpleNamespace(runtime_config={ANSWER_FLOW_FLAG: True}))
    assert not enabled(SimpleNamespace())


async def test_read_does_not_speak_or_double_count_duplicate_structured_data():
    flow, _, entries, _ = setup_flow()
    ctx = context()
    source_id = await collect(flow, context=ctx)
    assert not ctx.session.spoken
    assert entries[0]["delivery_state"] == "evidence_only"
    packet = flow.records[source_id].packet
    assert packet["data"]["total_across_returned_groups_ex_vat"] == "15728298.80"
    assert "quantity" not in json.dumps(packet) and "document_count" not in json.dumps(packet)


async def test_call_regression_accept_comparison_then_ask_decliners_not_leaders():
    flow, turns, entries, verifier = setup_flow()
    aug = await collect(flow, monthly(8, "800", "300"))
    first = context()
    with pytest.raises(llm.StopResponse):
        await flow.answer(first, "August sales were 1,100 dirhams, excluding VAT.", [aug])
    assert first.session.spoken[0][1]["add_to_chat_ctx"] is True
    turns.append("Yes, do that comparison please")
    july = await collect(flow, monthly(7, "600", "900"))
    compared = json.loads(await flow.compare(july, aug))
    comparison_id = compared["source_id"]
    data = compared["evidence"]
    assert data["total"] == {
        "earlier": "1500",
        "later": "1100",
        "delta": "-400",
        "percent_change": "-26.67",
    }
    assert data["groups_by_change_ascending"][0]["group"] == "Gypsum"
    assert data["groups_by_change_ascending"][0]["delta"] == "-600"
    reply = context()
    with pytest.raises(llm.StopResponse):
        await flow.answer(
            reply,
            "August sales declined by 400 dirhams versus July, excluding VAT.",
            [comparison_id],
        )
    assert len(reply.session.spoken) == 1
    turns.append("Which channels underperformed?")
    followup = context()
    with pytest.raises(llm.StopResponse):
        await flow.answer(
            followup, "Gypsum declined by 600 dirhams; Shops grew by 200 dirhams.", [comparison_id]
        )
    assert len([e for e in entries if e.get("source_tool") == "sales_summary"]) == 2
    request = verifier.await_args.args[1]
    assert request["latest_question"] == "Which channels underperformed?"
    assert request["previous_answer"].startswith("August sales declined")
    assert flow.last_answer.startswith("Gypsum declined")


@pytest.mark.parametrize("fault", ["scope", "currency", "basis", "filters", "reversed"])
async def test_comparisons_fail_closed_on_incomparable_evidence(fault):
    flow, _, _, _ = setup_flow()
    old = await collect(flow, monthly(7, "100", "200"))
    source = json.loads(monthly(8, "90", "250"))
    if fault == "currency":
        source["currency"] = "USD"
    if fault == "basis":
        source["basis"] = "Different exclusions"
    new = await collect(flow, json.dumps(source), scope="other" if fault == "scope" else "trading")
    if fault == "filters":
        flow.records[new].brief.comparison_key += "different customer"
    with pytest.raises(ValueError):
        await flow.compare(new, old) if fault == "reversed" else await flow.compare(old, new)


async def test_rejected_draft_is_not_spoken_and_can_be_corrected_once():
    flow, _, _, verifier = setup_flow(AsyncMock(side_effect=[False, True]))
    source_id = await collect(flow, monthly(8, "100", "200"))
    ctx = context()
    rejected = json.loads(await flow.answer(ctx, "Shops sales were 200 dirhams.", [source_id]))
    assert rejected["status"] == "rejected"  # Numeric value exists but wrong entity.
    assert not ctx.session.spoken
    with pytest.raises(llm.StopResponse):
        await flow.answer(ctx, "Shops sales were 100 dirhams.", [source_id])
    assert len(ctx.session.spoken) == 1 and verifier.await_count == 2
    with pytest.raises(llm.StopResponse):
        await flow.answer(ctx, "Shops sales were 100 dirhams.", [source_id])
    assert len(ctx.session.spoken) == 1


async def test_unknown_numbers_rejected_before_paid_verifier():
    flow, _, _, verifier = setup_flow()
    source_id = await collect(flow)
    ctx = context()
    response = json.loads(await flow.answer(ctx, "Sales were 999999999 dirhams.", [source_id]))
    assert response["status"] == "rejected" and not ctx.session.spoken
    verifier.assert_not_awaited()


async def test_internal_evidence_identifiers_are_not_spoken():
    flow, _, _, verifier = setup_flow()
    source_id = await collect(flow)
    ctx = context()
    response = json.loads(await flow.answer(ctx, f"Sources: {source_id}.", [source_id]))
    assert response["status"] == "rejected" and not ctx.session.spoken
    verifier.assert_not_awaited()


@pytest.mark.parametrize("draft", ["", "word " * 151, "a" * 1801])
async def test_empty_or_oversized_drafts_rejected_before_verifier_and_speech(draft):
    flow, _, entries, verifier = setup_flow()
    ctx = context()
    response = json.loads(await flow.answer(ctx, draft, []))
    assert response["status"] == "rejected"
    assert entries[-1]["validation"] == "rejected_length"
    assert not ctx.session.spoken
    verifier.assert_not_awaited()


async def test_direct_native_audio_cannot_bypass_checked_delivery():
    async def audio():
        yield object()
        yield object()

    metrics = {}
    assert [frame async for frame in gate_native_audio(audio(), metrics)] == []
    assert metrics["mcp_unchecked_audio_frames_blocked"] == 2


async def test_social_reply_uses_checked_path_without_fetching_private_reports():
    flow, turns, _, verify = setup_flow()
    turns.append("Thank you, goodbye")
    ctx = context()
    with pytest.raises(llm.StopResponse):
        await flow.answer(ctx, "You're welcome. Goodbye!", [])
    assert len(ctx.session.spoken) == 1 and not flow.records
    assert verify.await_args.args[2] == []


async def test_repeated_rejection_has_one_bounded_nonfactual_fallback():
    flow, _, _, verify = setup_flow(AsyncMock(return_value=False))
    source_id = await collect(flow, monthly(8, "100", "200"))
    ctx = context()
    for _ in range(2):
        assert (
            json.loads(await flow.answer(ctx, "Shops were 200 dirhams.", [source_id]))["status"]
            == "rejected"
        )
    with pytest.raises(llm.StopResponse):
        await flow.answer(ctx, "Shops were 200 dirhams.", [source_id])
    assert len(ctx.session.spoken) == 1 and "200" not in ctx.session.spoken[0][0]
    assert verify.await_count == 2


async def test_failed_audio_does_not_claim_full_draft_was_heard():
    flow, _, _, _ = setup_flow()
    source_id = await collect(flow, monthly(8, "100", "200"))
    ctx = context(error=RuntimeError("delivery failed"))
    with pytest.raises(llm.StopResponse):
        await flow.answer(ctx, "Shops were 100 dirhams.", [source_id])
    assert flow.last_answer == "Previous answer was interrupted or not delivered completely."


def test_exact_requests_do_not_accept_summary_rounding():
    packet = {"revenue": "8018898.00"}
    assert numbers_supported("8.02 million dirhams", [packet], "Revenue?")
    assert not numbers_supported("8.02 million dirhams", [packet], "Exact amount?")
    assert numbers_supported("8,018,898.00 dirhams", [packet], "Exact amount?")


def test_labelled_rounded_percentages_do_not_enable_rounding_of_exact_amounts():
    packet = {"percent_change": "-5.43", "amount_due": "450.25"}
    assert numbers_supported("About 5.4% down", [packet], "Summarize the decline")
    assert not numbers_supported("About 5.4% down", [packet], "Exact percentage?")
    assert not numbers_supported("About 450 dirhams", [packet], "Invoice amount?")
    assert not numbers_supported("5.4% down", [packet], "What percentage?")


def test_spoken_format_removes_list_ordinals_not_financial_values_or_names():
    draft = "Channels:\n1. **Gypsum**: -22.00%\n2. **B2B**: AED 1,529,089.40"
    assert spoken_draft(draft) == "Channels: Gypsum: -22.00% B2B: AED 1,529,089.40"
    assert spoken_draft("450.00 dollars") == "450.00 dollars"
    assert spoken_draft("2026. Sales declined") == "2026. Sales declined"
    assert spoken_draft("- 22 dirhams") == "- 22 dirhams"


@pytest.mark.parametrize("failure", ["revoked", "expired", "verifier", "unknown_source"])
async def test_answer_failure_never_releases_private_data(failure):
    flow, _, _, verifier = setup_flow()
    auth = AsyncMock()
    source_id = await collect(flow, monthly(8, "100", "200"), authorize=auth)
    if failure == "revoked":
        auth.side_effect = ValueError("Private secret")
    if failure == "expired":
        flow.records[source_id].expires = 0
    if failure == "verifier":
        verifier.side_effect = TimeoutError()
    if failure == "unknown_source":
        source_id = "missing"
    ctx = context()
    response = await flow.answer(ctx, "Sales were 300 dirhams.", [source_id])
    assert json.loads(response)["status"] == "unavailable"
    assert "Private secret" not in response and not ctx.session.spoken


async def test_new_turn_during_verification_cancels_old_answer():
    flow, turns, _, verifier = setup_flow()
    source_id = await collect(flow, monthly(8, "100", "200"))

    async def changed(*_):
        turns.append("No, a different report please")
        return True

    verifier.side_effect = changed
    ctx = context()
    with pytest.raises(llm.StopResponse):
        await flow.answer(ctx, "Sales were 300 dirhams.", [source_id])
    assert not ctx.session.spoken


async def test_derived_evidence_expires_and_rechecks_original_grants():
    flow, _, _, _ = setup_flow()
    auth = AsyncMock()
    old = await collect(flow, monthly(7, "100", "200"), authorize=auth)
    new = await collect(flow, monthly(8, "90", "210"))
    derived = json.loads(await flow.compare(old, new))["source_id"]
    auth.side_effect = ValueError("revoked")
    with pytest.raises(ValueError):
        await flow.check(flow.records[derived])


def test_financial_contradiction_and_oversized_sources_not_passed_as_generic_text():
    with pytest.raises(ValueError):
        evidence_packet(report(total_revenue_ex_vat="1"), {}, "Trading", "x")
    with pytest.raises(ValueError):
        evidence_packet("a" * 25000, {}, "Trading", "x")


async def test_private_flag_uses_actual_tool_authorization_without_legacy_speech(
    db, tenant, user, monkeypatch
):
    from app.services.integration_security import prepare_integration_config_storage
    from tests.test_private_mcp import session_factory, setup_private

    agent, profile, integration, call, cfg = await setup_private(db, tenant, user)
    profile.runtime_config = {**profile.runtime_config, ANSWER_FLOW_FLAG: True}
    await db.commit()
    monkeypatch.setattr(mcp_tools, "async_session_factory", session_factory)
    monkeypatch.setattr(
        mcp_tools, "call_read_tool", AsyncMock(return_value=result("Approved trading information"))
    )
    turns = [{"role": "user", "content": "Company information"}]
    metrics = {}
    tools = await mcp_tools.load_mcp_tools(
        agent,
        profile,
        metrics,
        call_id=call.id,
        source_turns=turns,
        answer_verifier=AsyncMock(return_value=True),
    )
    assert len(tools) == 3 and metrics["mcp_delivery_mode"] == "mcp_answer_v2"
    ctx = context()
    source = json.loads(await tools[0]({}, ctx))
    assert source["status"] == "ok" and not ctx.session.spoken
    integration.config, integration.encrypted_config = prepare_integration_config_storage(
        {**cfg, "allowed_tools": []}, "mcp"
    )
    await db.commit()
    rejected = json.loads(
        await tools[-1](ctx, "Approved trading information", [source["source_id"]])
    )
    assert rejected["status"] == "unavailable" and not ctx.session.spoken


@pytest.mark.parametrize("effort", [None, "none"])
async def test_verifier_is_bounded_uses_configured_model_and_accounts_usage(effort):
    def respond(request):
        body = json.loads(request.content)
        assert body["model"] == "openai/gpt-4o-mini"
        if effort is None:
            assert "reasoning_effort" not in body
        else:
            assert body["reasoning_effort"] == "none"
        assert "wrong" in body["messages"][0]["content"]
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"reason":"supported","violations":[],"supported":true}'
                        },
                    }
                ],
                "usage": {"prompt_tokens": 100, "completion_tokens": 12},
            },
        )

    metrics = {}
    verifier = InworldAnswerVerifier(
        api_key="test",
        model="openai/gpt-4o-mini",
        base_url="https://example.test",
        metrics=metrics,
        transport=httpx.MockTransport(respond),
        reasoning_effort=effort,
    )
    assert await verifier("Approved info", "What info?", [{"source_text": "Approved info"}]) is True
    assert metrics["mcp_presentation_input_tokens"] == 100
    assert metrics["mcp_presentation_output_tokens"] == 12


async def test_different_salespeople_can_be_compared_without_inventing_zero_rows():
    flow, _, _, _ = setup_flow()
    old = await collect(
        flow,
        report(
            start_date="2099-07-01",
            end_date="2099-07-31",
            group_by="salesman",
            rows=[
                {"group_value": "Returning", "revenue_ex_vat": "8000"},
                {"group_value": "July only", "revenue_ex_vat": "4000"},
            ],
        ),
    )
    new = await collect(
        flow,
        report(
            group_by="salesman",
            rows=[
                {"group_value": "Returning", "revenue_ex_vat": "6000"},
                {"group_value": "New person", "revenue_ex_vat": "1000"},
                {"group_value": "Another new person", "revenue_ex_vat": "500"},
            ],
        ),
    )
    evidence = json.loads(await flow.compare(old, new))["evidence"]
    assert evidence["group_coverage"] == "matched_only"
    assert evidence["groups_by_change_ascending"] == [
        {
            "group": "Returning",
            "earlier": "8000",
            "later": "6000",
            "delta": "-2000",
            "percent_change": "-25.00",
        }
    ]
    assert len(evidence["unmatched_groups"]) == 3
    assert all(row["delta"] is None for row in evidence["unmatched_groups"])
    assert all(row["percent_change"] is None for row in evidence["unmatched_groups"])
    assert evidence["total"]["delta"] == "-4500"
    assert "not proof" in evidence["total_basis"]


@pytest.mark.parametrize("before", ["0", "-100"])
async def test_nonpositive_baseline_does_not_invent_percentage(before):
    flow, _, _, _ = setup_flow()
    old = await collect(flow, monthly(7, before, "100"))
    new = await collect(flow, monthly(8, "200", "100"))
    evidence = json.loads(await flow.compare(old, new))["evidence"]
    row = next(r for r in evidence["groups_by_change_ascending"] if r["group"] == "Shops")
    assert row["percent_change"] is None


async def test_verifier_receives_clarification_history_not_unspoken_candidates():
    from app.livekit_runtime.mcp_answer_flow import conversation_context

    flow, turns, entries, verify = setup_flow()
    dialogue = [
        {"role": "user", "content": "Compare August with the previous month."},
        {"role": "user", "content": "Means in July."},
        {"role": "source", "content": "private source should not leak as dialogue"},
        {"role": "analysis_candidate", "content": "Wrong rejected statement"},
    ]
    flow.conversation_provider = lambda: dialogue
    turns.append("Means in July.")
    old = await collect(flow, monthly(7, "800", "300"))
    new = await collect(flow, monthly(8, "600", "300"))
    compared = json.loads(await flow.compare(old, new))["source_id"]
    with pytest.raises(llm.StopResponse):
        await flow.answer(context(), "August declined by 200 dirhams versus July.", [compared])
    request = verify.await_args.args[1]
    assert request["latest_question"] == "Means in July."
    assert request["conversation_history"] == dialogue[:2]
    assert conversation_context(dialogue) == dialogue[:2]
    assert entries[-3]["request_context"] == request


async def test_final_spoken_summary_is_formatted_before_verification_source_stays_exact():
    flow, _, entries, verify = setup_flow()
    source_id = await collect(flow, monthly(8, "8236000", "426000"))
    original = json.dumps(flow.records[source_id].packet)
    ctx = context()
    with pytest.raises(llm.StopResponse):
        await flow.answer(ctx, "Shops were AED 8,236,000 and Gypsum AED 426,000.", [source_id])
    expected = "Shops were 8.24 million dirhams and Gypsum 426 thousand dirhams."
    assert ctx.session.spoken[0][0] == expected
    assert verify.await_args.args[0] == expected
    assert flow.last_answer == expected
    assert json.dumps(flow.records[source_id].packet) == original
    candidate = next(e for e in entries if e["role"] == "analysis_candidate")
    assert candidate["content"] != candidate["spoken_text"]
    assert len(candidate["financial_speech_bindings"]) == 2


async def test_expired_and_mismatched_reports_return_distinct_safe_repair_codes():
    flow, _, entries, _ = setup_flow()
    old = await collect(flow, monthly(7, "100", "200"))
    new = await collect(flow, monthly(8, "90", "210"))
    tool = flow.tools()[0]
    reversed_error = json.loads(await tool(new, old))
    assert reversed_error["code"] == "incompatible_reports"
    flow.records[old].expires = 0
    expired = json.loads(await tool(old, new))
    assert expired["code"] == "evidence_expired" and "fresh" in expired["reason"]
    missing = json.loads(await tool("missing", new))
    assert missing["code"] == "evidence_missing"
    assert entries[-1]["event"] == "mcp_answer_error"


def test_context_is_bounded_and_only_actual_dialogue_is_used():
    from app.livekit_runtime.mcp_answer_flow import conversation_context

    history = conversation_context([{"role": "user", "content": "a" * 2000} for _ in range(100)])
    assert len(history) <= 12
    assert sum(len(t["content"]) for t in history) <= 6000


@pytest.mark.parametrize(
    "verdict,expected",
    [
        ({"reason": "Wrong entity", "violations": ["Wrong entity"], "supported": True}, False),
        ({"reason": "Wrong entity", "violations": ["Wrong entity"], "supported": False}, False),
        ({"reason": "OK", "supported": True}, False),
        ({"reason": "OK", "violations": [], "supported": "true"}, False),
        ({"reason": "OK", "violations": [], "supported": True}, True),
    ],
)
async def test_verifier_rejects_inconsistent_or_incomplete_verdict(verdict, expected):
    def respond(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"content": json.dumps(verdict)},
                    }
                ]
            },
        )

    verifier = InworldAnswerVerifier(
        api_key="test",
        model="openai/gpt-5.6-luna",
        base_url="https://example.test",
        metrics={},
        transport=httpx.MockTransport(respond),
        reasoning_effort="none",
    )
    assert await verifier("Test", {}, []) is expected


def test_numeric_check_understands_explicit_units_on_generic_money_reports():
    packets = [
        {
            "source_text": json.dumps(
                {
                    "currency": "USD",
                    "unit": "thousands",
                    "purchase_amount": "426",
                }
            )
        }
    ]
    assert numbers_supported("USD 426000", packets, "Purchases?")
    assert not numbers_supported("USD 426000000", packets, "Purchases?")
