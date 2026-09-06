"""Pure pacing and eligibility rules shared by simulation and real dispatch."""

import math
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

MODES = {
    "preview": "Each queued customer requires operator approval before dialing.",
    "progressive": "One call at a time; advance when the previous call ends.",
    "parallel": "Independent AI calls up to reserved channel capacity.",
    "predictive": "Answer-rate pacing within reserved AI/channel capacity; no overbooking.",
    "scheduled": "Only dial callbacks after their specified time.",
    "event": "Only dial jobs explicitly submitted by an authenticated event integration.",
}
ACTIVE = {"reserved", "dispatching", "calling", "unknown"}
TERMINAL = {"completed", "failed", "busy", "no_answer", "canceled", "cancelled"}
UNCERTAIN = {"dispatch_unknown", "terminal_unknown"}


def utc(value):
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def within_window(config, customer, purpose, now):
    """Require both campaign and customer local windows; add UAE marketing bounds."""
    start, end = config["calling_hours_start"], config["calling_hours_end"]
    for zone in (config["timezone"], customer.timezone):
        clock = now.astimezone(ZoneInfo(zone)).strftime("%H:%M")
        if not start <= clock < end:
            return False
    if customer.phone_number.startswith("+971") and purpose in {"reactivation", "qualification"}:
        return "09:00" <= now.astimezone(ZoneInfo("Asia/Dubai")).strftime("%H:%M") < "18:00"
    return True


def eligibility(campaign, job, customer, now=None):
    now = now or datetime.now(UTC)
    if campaign.status != "running":
        return "campaign_not_running"
    if customer.company != campaign.company:
        return "company_mismatch"
    if customer.opted_out or not customer.contact_allowed or not customer.consent_reference:
        return "contact_permission_missing_or_revoked"
    if not campaign.config.get("compliance_approved"):
        return "compliance_review_required"
    if campaign.purpose == "collections":
        return "verified_customer_and_live_balance_connector_required"
    if utc(job.available_at) > now:
        return "not_due"
    if campaign.mode == "preview" and not job.approved:
        return "preview_approval_required"
    if job.attempts >= campaign.config["max_attempts_per_customer"] and job.state == "queued":
        return "attempt_limit"
    if not within_window(campaign.config, customer, campaign.purpose, now):
        return "outside_calling_hours"
    return None


def dial_capacity(mode, config, active_count, answered_active=0, history=()):
    """No mode may exceed the capacity reserved for fully served AI calls."""
    ceiling = 1 if mode == "progressive" else config["max_concurrent_calls"]
    free = max(0, ceiling - active_count)
    if mode != "predictive":
        return free
    # Warmup must use observed terminal outcomes, never an invented answer rate.
    if len(history) < 20:
        return min(free, 1)
    answer_rate = max(0.1, min(1.0, sum(history) / len(history)))
    expected_ringing_answers = max(0, active_count - answered_active) * answer_rate
    demand = max(0, config["target_live_calls"] - answered_active - expected_ringing_answers)
    return min(free, math.ceil(demand / answer_rate))
