import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.livekit_runtime import mcp_tools
from app.livekit_runtime.mcp_request_context import (
    completed_month_arguments,
    forecast_requested,
    report_arguments,
    report_date_instruction,
)
from app.livekit_runtime.report_presentation import ReportPresenter
from app.services.mcp_connections import MCPError
from tests.test_mcp_retrieval import SCHEMA, runtime_fixture
from tests.test_report_presentation import report

NOW = datetime(2026, 9, 8, 9, tzinfo=UTC)
QUESTION = "Current month sales and expected turnover by month end?"
ARGS = {"start_date": "2026-09-01", "end_date": "2026-09-08", "group_by": "channel"}


@pytest.mark.parametrize(
    "question",
    ["No forecast please", "Current month actuals without an estimate", "Don't forecast"],
)
def test_no_unsolicited_forecast(question):
    assert not forecast_requested(question)


def test_open_range_not_collapsed_to_mtd():
    args = {"start_date": "2026-09-01", "end_date": "2026-10-31"}
    assert report_arguments(args, "This month to next month", SCHEMA, now=NOW) == args


@pytest.mark.parametrize(
    "question", ["This month's sales", "Current month sales", "Month to date sales", "MTD sales"]
)
def test_current_month_overrides_stale_previous_month_dates(question):
    original = {"start_date": "2026-08-01", "end_date": "2026-08-31", "group_by": "channel"}
    assert report_arguments(original, question, SCHEMA, timezone="Asia/Dubai", now=NOW) == ARGS
    assert original["end_date"] == "2026-08-31"


@pytest.mark.parametrize(
    "question",
    ["Compare current month with last month", "Not this month, July", "This month of 2025"],
)
def test_explicit_or_compared_period_not_overwritten(question):
    assert report_arguments(ARGS, question, SCHEMA, now=NOW) == ARGS


def test_first_day_no_baseline_and_timezone_rollover():
    now = datetime(2026, 8, 31, 21, tzinfo=UTC)
    args = report_arguments({}, QUESTION, SCHEMA, timezone="Asia/Dubai", now=now)
    assert args == {"start_date": "2026-09-01", "end_date": "2026-09-01"}
    assert completed_month_arguments(args, QUESTION, timezone="Asia/Dubai", now=now) is None
    assert "2026-09-01 through 2026-09-08" in report_date_instruction("Asia/Dubai", now=NOW)


def sources():
    actual = report(
        start_date="2026-09-01",
        end_date="2026-09-08",
        rows=[{"group_value": "Shops", "revenue_ex_vat": "750000"}],
    )
    baseline_args = dict(ARGS, end_date="2026-09-07")
    baseline = report(
        start_date="2026-09-01",
        end_date="2026-09-07",
        rows=[{"group_value": "Shops", "revenue_ex_vat": "700000"}],
    )
    return actual, {"source": baseline, "arguments": baseline_args}


async def test_forecast_excludes_partial_today_and_preserves_actuals():
    actual, baseline = sources()
    result = await ReportPresenter({}).present(
        actual, question=QUESTION, arguments=ARGS, forecast=baseline, timezone="Asia/Dubai", now=NOW
    )
    text = result["text"]
    assert "750,000 dirhams" in text and "3.00 million dirhams" in text
    assert "7 calendar days" in text and "excluding today's partial day" in text
    assert "estimate, not booked revenue" in text and "same daily pace" in text
    assert "2.81 million" not in text  # Incorrectly dividing today's partial actuals by eight.
    assert len(text.split()) < 150
    assert json.loads(actual)["rows"][0]["revenue_ex_vat"] == "750000"


@pytest.mark.parametrize(
    "fault", ["missing", "wrong_period", "wrong_currency", "different_groups", "negative"]
)
async def test_unverified_baseline_keeps_actuals_without_a_forecast(fault):
    actual, baseline = sources()
    if fault == "missing":
        baseline = None
    else:
        payload = json.loads(baseline["source"])
        if fault == "wrong_period":
            payload["end_date"] = "2026-09-06"
        elif fault == "wrong_currency":
            payload["currency"] = "USD"
        elif fault == "different_groups":
            payload["rows"][0]["group_value"] = "Contractors"
        else:
            payload["rows"][0]["revenue_ex_vat"] = "-1"
        baseline["source"] = json.dumps(payload)
    result = await ReportPresenter({}).present(
        actual, question=QUESTION, arguments=ARGS, forecast=baseline, now=NOW
    )
    assert "750,000 dirhams" in result["text"]
    assert "baseline is unavailable" in result["text"]
    assert "3.00 million" not in result["text"]


async def test_actuals_only_does_not_add_unsolicited_prediction():
    actual, baseline = sources()
    result = await ReportPresenter({}).present(
        actual, question="Current month sales?", arguments=ARGS, forecast=baseline, now=NOW
    )
    assert "estimate" not in result["text"] and "run-rate" not in result["text"]


@pytest.mark.parametrize("failure", [None, "network", "revoked", "interrupted"])
async def test_forecast_baseline_is_authorized_and_optional(monkeypatch, failure):
    _, metrics, auth, execute = runtime_fixture(monkeypatch, [{"status": "ok"}, {"status": "ok"}])
    cfg = auth.return_value
    descriptor = cfg["tools"][0]
    descriptor["input_schema"] = SCHEMA
    baseline = dict(ARGS, end_date="2026-09-07")
    monkeypatch.setattr(mcp_tools, "completed_month_arguments", lambda *a, **k: baseline)
    monkeypatch.setattr(mcp_tools, "report_arguments", lambda args, *a, **k: args)
    captured = {}
    context = SimpleNamespace(speech_handle=SimpleNamespace(interrupted=False))

    async def deliver(ctx, response, **kwargs):
        captured["actual"] = response
        if failure == "network":
            execute.side_effect = MCPError("safe", code="network_error")
        elif failure == "revoked":
            auth.side_effect = MCPError("revoked")
        elif failure == "interrupted":
            context.speech_handle.interrupted = True
        captured["baseline"] = await kwargs["forecast_loader"]()

    tool = mcp_tools._make_tool(
        uuid4(),
        uuid4(),
        uuid4(),
        "Fixture",
        descriptor,
        metrics,
        delivery=SimpleNamespace(deliver=deliver),
        question_provider=lambda: "Where will we finish at this pace?",
    )
    from livekit.agents import llm

    if failure == "interrupted":
        with pytest.raises(llm.StopResponse):
            await tool(dict(ARGS, vav_report_analysis="month_end_run_rate"), context)
    else:
        await tool(dict(ARGS, vav_report_analysis="month_end_run_rate"), context)
    assert execute.call_args_list[0].args[2] == ARGS  # VAV intent never leaks into MCP arguments.
    assert "vav_report_analysis" not in descriptor["input_schema"]["properties"]
    if failure in ("revoked", "interrupted"):
        assert execute.await_count == 1
    elif failure is None:
        assert captured["baseline"]["arguments"] == baseline and execute.await_count == 2
    else:
        assert captured["baseline"] is None and captured["actual"]["status"] == "ok"
