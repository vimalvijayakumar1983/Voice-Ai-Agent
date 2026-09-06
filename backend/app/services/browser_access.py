"""Explicit staff-only browser policy. Phone enablement is a separate authority."""

from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select

from app.models.agent import Agent, AgentRuntimeProfile
from app.models.call import Call
from app.models.user import User

STAFF_ROLES = {"owner", "admin"}


def staff_browser(profile) -> bool:
    return bool(profile and (profile.runtime_config or {}).get("staff_browser_only") is True)


def tools_only(profile) -> bool:
    return (
        staff_browser(profile)
        and (profile.runtime_config or {}).get("knowledge_source_mode") == "tools_only"
    )


def check_browser_actor(profile, user) -> None:
    if staff_browser(profile) and user.role not in STAFF_ROLES:
        raise HTTPException(
            403, "This browser agent is restricted to workspace owners and administrators"
        )


async def validate_staff_call(db, *, tenant_id, agent_id, call_id):
    """Use the durable server-created identity, never caller variables or model claims."""
    call = await db.scalar(
        select(Call).where(
            Call.id == call_id,
            Call.tenant_id == tenant_id,
            Call.agent_id == agent_id,
            Call.provider == "livekit_webrtc",
            Call.status.in_(("initiated", "in_progress")),
        )
    )
    profile = await db.scalar(
        select(AgentRuntimeProfile)
        .where(
            AgentRuntimeProfile.agent_id == agent_id,
            AgentRuntimeProfile.tenant_id == tenant_id,
        )
        .execution_options(populate_existing=True)
    )
    agent = await db.scalar(select(Agent).where(Agent.id == agent_id, Agent.tenant_id == tenant_id))
    metadata = (call.call_metadata or {}) if call else {}
    if not (
        call
        and agent
        and agent.is_active
        and staff_browser(profile)
        and profile.status != "inactive"
        and metadata.get("staff_browser_only") is True
        and metadata.get("channel") == "browser"
    ):
        raise ValueError("Staff browser authorization unavailable")
    try:
        user_id = UUID(metadata["browser_user_id"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Staff browser identity unavailable") from exc
    user = await db.scalar(
        select(User)
        .where(
            User.id == user_id,
            User.tenant_id == tenant_id,
            User.is_active.is_(True),
            User.role.in_(STAFF_ROLES),
        )
        .execution_options(populate_existing=True)
    )
    if user is None:
        raise ValueError("Staff browser access revoked")
    return call, profile


async def require_call_access(db, user, call_id) -> None:
    if user.role in STAFF_ROLES:
        return
    call = await db.scalar(select(Call).where(Call.id == call_id, Call.tenant_id == user.tenant_id))
    if call and (call.call_metadata or {}).get("staff_browser_only") is True:
        raise HTTPException(404, "Call not found")
