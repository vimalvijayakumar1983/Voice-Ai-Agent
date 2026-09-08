"""Professional narration must not let the model write financial facts."""

import asyncio
import json
from decimal import Decimal
from unittest.mock import AsyncMock

import httpx
import pytest
from livekit.agents import llm

from app.livekit_runtime.mcp_delivery import SourceDelivery
from app.livekit_runtime.report_presentation import (
    INVALID,
    UNSUPPORTED,
    InworldNarrativePlanner,
    ReportPresenter,
    amount,
    compile_brief,
    include_presentation_usage,
    render_plan,
)
from tests.test_mcp_delivery import context, result


def report(**updates):
    payload = {
        "currency": "AED",
        "start_date": "2099-08-01",
        "end_date": "2099-08-31",
        "group_by": "channel",
        "basis": "internal-company and tyres exclusions applied",
        "rows": [
            {
                "group_value": "Shops",
                "revenue_ex_vat": "8018898.00",
                "quantity": 333,
                "document_count": 25,
            },
            {"group_value": "Contractors", "revenue_ex_vat": "4299154.24"},
            {"group_value": "Bulk Cement", "revenue_ex_vat": "3410246.56"},
        ],
    }
    payload.update(updates)
    return json.dumps(payload)


async def test_structured_report_becomes_headline_highlights_not_json():
    source = report()
    presentation = await ReportPresenter({}).present(source)
    text = presentation["text"]
    assert "15.73 million dirhams" in text
    assert "Shops at 8.02 million" in text
    assert "Contractors at 4.30 million" in text
    assert "excluding VAT" in text and "tyres are excluded" in text
    assert "returned channels" in text  # No unsupported claim of complete company revenue.
    for technical in ("revenue_ex_vat", "document_count", "333", "{", "corrected channel logic"):
        assert technical not in text
    assert len(text.split()) < 100
    assert "down" not in text and "July" not in text


@pytest.mark.parametrize(
    "value,exact,expected",
    [
        ("4299154.24", False, "4.30 million"),
        ("450", False, "450"),
        ("450000", False, "450,000"),
        ("4299154.24", True, "4,299,154.24"),
        ("-0.01", True, "-0.01"),
        ("8018898.00", False, "8.02 million"),
        ("0", False, "0"),
    ],
)
def test_decimal_summary_rounding_and_exact_amounts(value, exact, expected):
    assert amount(Decimal(value), exact=exact) == expected


async def test_invoice_currency_scale_is_explicit_and_never_inferred():
    for unit, expected in (("units", "450"), ("thousands", "450,000")):
        presentation = await ReportPresenter({}).present(
            json.dumps(
                {
                    "invoice_id": "INV-42",
                    "currency": "USD",
                    "unit": unit,
                    "amount_due": 450,
                }
            )
        )
        assert presentation["text"] == f"Invoice INV-42 has an amount due of {expected} US dollars."


@pytest.mark.parametrize(
    "updates",
    [
        {"currency": None},
        {"units": "unknown"},
        {"total_revenue_ex_vat": "30000000"},
        {"rows": [{"group_value": "Shops", "revenue_ex_vat": None}]},
        {"rows": [{"group_value": "Shops", "revenue_ex_vat": "NaN"}]},
        {"rows": [{"group_value": "Shops", "revenue_ex_vat": True}]},
        {"rows": [{"group_value": "Shops", "revenue_ex_vat": "450", "currency": "USD"}]},
        {"rows": [{"group_value": "Shops", "revenue_ex_vat": "450", "units": "thousands"}]},
        {
            "rows": [
                {"group_value": "Shops", "revenue_ex_vat": 1},
                {"group_value": "shops", "revenue_ex_vat": 1},
            ]
        },
        {"end_date": "2099-07-01"},
    ],
)
def test_bad_or_contradictory_data_is_not_narrated(updates):
    assert compile_brief(report(**updates)).sentences == {"lead": INVALID}


def test_wrong_returned_period_is_rejected():
    assert (
        compile_brief(report(), arguments={"start_date": "2099-07-01"}).sentences["lead"] == INVALID
    )


async def test_unknown_schema_is_not_read_out():
    presentation = await ReportPresenter({}).present(
        '{"currency":"AED","rows":[{"secret_key":450}]}'
    )
    assert presentation["text"] == UNSUPPORTED


async def test_just_total_and_requested_breakdown_are_respected():
    short = await ReportPresenter({}).present(report(), question="Just the total please")
    assert "Shops" not in short["text"] and "Would you" not in short["text"]
    detailed = await ReportPresenter({}).present(
        report(), question="Give me the channel-wise breakdown"
    )
    assert (
        "By channel: Shops, 8.02 million; Contractors, 4.30 million; Bulk Cement, 3.41 million."
        in detailed["text"]
    )


@pytest.mark.parametrize(
    "plan",
    [
        {"sentences": ["lead", "invented"], "follow_up": "none"},
        {"sentences": ["lead"], "follow_up": "none"},  # Missing accounting scope.
        {"sentences": ["lead", "scope"], "follow_up": "Invent 30 million"},
        {"sentences": ["lead", "scope", "lead"], "follow_up": "none"},
        {"sentences": ["lead", "scope"], "follow_up": "none", "text": "30 million"},
    ],
)
async def test_unverified_model_output_falls_back_without_changing_a_fact(plan):
    planner = AsyncMock(
        return_value=(json.dumps(plan), {"prompt_tokens": 30, "completion_tokens": 15})
    )
    metrics = {}
    rendered = await ReportPresenter(metrics, planner).present(report())
    expected = await ReportPresenter({}).present(report())
    assert rendered["text"] == expected["text"]
    assert rendered["state"] == "deterministic_fallback"
    assert metrics["mcp_presentation_input_tokens"] == 30  # Failed validation still costs tokens.


async def test_valid_plan_uses_only_preverified_sentences():
    planner = AsyncMock(
        return_value=('{"sentences":["lead","leaders","scope"],"follow_up":"none"}', None)
    )
    rendered = await ReportPresenter({}, planner).present(report())
    assert rendered["state"] == "llm_selected_validated"
    assert rendered["text"] == " ".join(
        rendered["fact_sentences"][i] for i in rendered["sentence_ids"]
    )


async def test_verbatim_approved_options_can_resolve_to_ids_but_edited_amounts_cannot():
    brief = compile_brief(report())
    plan = {"sentences": [brief.sentences[i] for i in brief.required], "follow_up": ""}
    planner = AsyncMock(return_value=(json.dumps(plan), None))
    valid = await ReportPresenter({}, planner).present(report())
    assert valid["state"] == "llm_selected_validated"
    assert valid["sentence_ids"] == brief.required
    plan["sentences"][0] = plan["sentences"][0].replace("15.73", "30.00")
    planner.return_value = (json.dumps(plan), None)
    invalid = await ReportPresenter({}, planner).present(report())
    assert invalid["state"] == "deterministic_fallback"
    assert "30.00" not in invalid["text"]


async def test_comparison_is_deterministic_scoped_and_required():
    presenter = ReportPresenter({})
    july = report(start_date="2099-07-01", end_date="2099-07-31")
    await presenter.present(july, company="Fixture Trading", scope="integration-1")
    august = json.loads(report())
    august["rows"][0]["revenue_ex_vat"] = "7018898.00"
    compared = await presenter.present(
        json.dumps(august),
        company="Fixture Trading",
        scope="integration-1",
        question="Compare with July",
    )
    assert "down by 1.00 million dirhams, or 6.36 percent" in compared["text"]
    assert "comparison" in compared["sentence_ids"]
    other = await presenter.present(
        july, company="Other Company", scope="integration-1", question="Compare with August"
    )
    assert "comparison" not in other["sentence_ids"]


async def test_planner_timeout_or_cancellation_is_safe():
    planner = AsyncMock(side_effect=TimeoutError())
    assert (await ReportPresenter({}, planner).present(report()))[
        "state"
    ] == "deterministic_fallback"
    planner.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await ReportPresenter({}, planner).present(report())


async def test_source_is_preserved_and_only_presentation_reaches_audio():
    ctx, entries, metrics = context(), [], {}
    raw = report()
    with pytest.raises(llm.StopResponse):
        await SourceDelivery(metrics, entries.append).deliver(ctx, result(raw), tool="sales")
    assert entries[0]["content"] == raw
    assert ctx.session.tts.inputs == [entries[0]["presentation_text"]]
    assert "15.73 million" in ctx.session.tts.inputs[0]
    assert "revenue_ex_vat" not in ctx.session.tts.inputs[0]
    assert raw not in json.dumps(metrics)


@pytest.mark.parametrize("revoke", [True, False])
async def test_permission_or_interruption_during_presentation_prevents_playback(revoke):
    ctx, entries = context(), []
    auth = AsyncMock(side_effect=[None, ValueError("revoked")]) if revoke else AsyncMock()

    async def planner(brief, question):
        if not revoke:
            ctx.speech_handle.interrupted = True
        return '{"sentences":["lead","scope"],"follow_up":"none"}', None

    with pytest.raises(llm.StopResponse):
        await SourceDelivery({}, entries.append, presenter=ReportPresenter({}, planner)).deliver(
            ctx, result(report()), tool="sales", authorize=auth
        )
    assert all("15.73" not in text for text, _, _ in ctx.session.spoken)
    assert entries[0]["delivery_state"] in {"failed", "superseded"}


def test_usage_fresh_snapshots_are_idempotent_and_unknown_is_not_free():
    metrics = {
        "mcp_presentation_requests": 1,
        "mcp_presentation_input_tokens": 30,
        "mcp_presentation_output_tokens": 10,
    }
    snapshot = {"llm_tokens": 100, "llm_input_tokens": 80, "llm_output_tokens": 20}
    first = include_presentation_usage(snapshot, metrics)
    assert first == include_presentation_usage(snapshot, metrics)
    assert first["llm_tokens"] == 140 and snapshot["llm_tokens"] == 100
    assert first["runtime_usage_components_complete"] is False
    from app.livekit_runtime.worker import _reconcile_external_tts_usage

    first.update(metrics, usage_components_expected=["llm"], usage_components_reported=["llm"])
    _reconcile_external_tts_usage(first)
    assert first["runtime_usage_components_complete"] is False


async def test_provider_request_and_bounded_response():
    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/chat/completions"
        assert body["stream"] is False
        assert "document_count" not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "content": '{"sentences":["lead","leaders","scope"],"follow_up":"none"}'
                        },
                    }
                ],
                "usage": {"prompt_tokens": 20, "completion_tokens": 10},
            },
        )

    planner = InworldNarrativePlanner(
        api_key="fixture",
        model="fixture",
        base_url="https://example.test",
        transport=httpx.MockTransport(handler),
    )
    text, usage = await planner(compile_brief(report()), "Last month's sales?")
    assert render_plan(compile_brief(report()), json.loads(text))
    assert usage["prompt_tokens"] == 20
