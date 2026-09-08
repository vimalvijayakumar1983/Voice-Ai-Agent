"""Report request semantics and content-free failure instructions shared by MCP agents."""

import re
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.services.mcp_connections import calendar_date_fields


def local_today(timezone="UTC", now=None):
    try:
        zone = ZoneInfo(timezone or "UTC")
    except ZoneInfoNotFoundError:
        zone = ZoneInfo("UTC")
    return (now or datetime.now(UTC)).astimezone(zone).date()


def forecast_requested(question):
    return bool(
        re.search(
            r"\b(forecast|projected?|projection|expected turnover|"
            r"expected sales|month.end estimate|run.rate)\b",
            question,
            re.I,
        )
    )


def completed_month_arguments(arguments, question, *, timezone="UTC", now=None):
    """Separate baseline: never treat today's partial sales as a complete day."""
    today = local_today(timezone, now)
    if (
        not forecast_requested(question)
        or today.day == 1
        or arguments.get("start_date") != today.replace(day=1).isoformat()
        or arguments.get("end_date") != today.isoformat()
    ):
        return None
    return dict(arguments, end_date=(today - timedelta(days=1)).isoformat())


def report_arguments(arguments, question, schema, *, timezone="UTC", now=None):
    """Resolve only an unambiguous previous calendar month; preserve all other filters."""
    result = dict(arguments)
    if calendar_date_fields(schema) != {"start_date", "end_date"}:
        return result
    current = bool(re.search(r"\b((this|current) month|month.to.date|mtd)\b", question, re.I))
    previous = bool(re.search(r"\b(last|previous) month\b", question, re.I))
    if not current and not previous:
        return result
    # Never collapse comparisons, explicit years/ranges or negated/corrected periods.
    if re.search(
        r"\b(compare|comparison|versus|vs|not|except|before|after|since|until)\b|\d",
        question,
        re.I,
    ):
        return result
    if current and previous:
        return result
    if previous and re.search(r"\bto\b", question, re.I):
        return result
    today = local_today(timezone, now)
    if current:
        return dict(result, start_date=today.replace(day=1).isoformat(), end_date=today.isoformat())
    previous_end = today.replace(day=1) - timedelta(days=1)
    result.update(
        start_date=previous_end.replace(day=1).isoformat(), end_date=previous_end.isoformat()
    )
    return result


def report_date_instruction(timezone="UTC", *, now=None):
    today = local_today(timezone, now)
    values = report_arguments(
        {},
        "last month",
        {
            "properties": {
                key: {"type": "string", "format": "date"} for key in ("start_date", "end_date")
            }
        },
        timezone=timezone,
        now=now,
    )
    return (
        "\nFor report requests, 'last month' means the previous complete calendar month: "
        f"{values['start_date']} through {values['end_date']}. "
        f"Current-month/month-to-date actuals mean {today.replace(day=1)} through {today}. "
        "Always attempt the available authorised sales tool for current-month requests; "
        "never infer data availability from a previously retrieved month. For a requested "
        "month-end forecast or expected turnover, retrieve current-month sales. VAV can "
        "calculate a labelled same-pace run-rate using a separate completed-day baseline; "
        "do not refuse all estimates as predictions or invent a forecast yourself. "
        "Use these ISO dates without asking the caller to restate an unambiguous period. "
        "Keep company and filters unchanged unless the caller explicitly changes them. "
        "Follow the caller's latest correction. For an overall sales summary use the tool's "
        "default grouping; group by salesperson only when that is actually requested. "
        "A failed lookup does not establish missing permissions. Follow the tool's error code; "
        "never say access was revoked for a network, timeout, invalid-request or tool error.\n"
    )


def failure_instruction(code):
    descriptions = {
        "invalid_arguments": (
            "The report request was invalid. Correct its arguments from the caller's request "
            "and tool schema; ask only for genuinely missing information."
        ),
        "schema_changed": (
            "The approved tool definition has changed and needs administrator review. "
            "Do not bypass approval or claim the caller lacks permission."
        ),
        "access_denied": "Access checks denied this lookup. Do not release data or bypass access.",
        "authorization_failed": "VAV could not verify access. Do not bypass access or invent data.",
        "upstream_auth_failed": (
            "The MCP endpoint rejected VAV's authentication. Request a connection review; "
            "do not claim the caller lacks permission."
        ),
        "result_too_large": (
            "The report exceeded VAV's response limit. Explain and request a narrower report; "
            "do not silently change its meaning or scope."
        ),
        "tool_error": (
            "The MCP tool could not complete this report. This is not evidence of missing "
            "user access. Briefly explain the retrieval failure."
        ),
        "unsafe_result": "VAV blocked an unsafe result. Do not repeat it or bypass the block.",
    }
    return (
        descriptions.get(
            code,
            "The report request failed. Do not claim access is missing; offer to try again.",
        )
        + " Never invent figures or claim a successful lookup."
    )
