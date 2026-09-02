"""Server-side LiveKit WebRTC room, dispatch, and token issuance."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

from livekit import api

from app.livekit_runtime.constants import BROWSER_TOKEN_TTL_SECONDS
from app.livekit_runtime.dispatch_auth import create_browser_dispatch_metadata

logger = logging.getLogger(__name__)


class LiveKitBrowserSessionError(RuntimeError):
    def __init__(self, message: str, *, ambiguous: bool = False):
        super().__init__(message)
        self.ambiguous = ambiguous


@dataclass(frozen=True)
class LiveKitBrowserSession:
    access_token: str
    room_name: str
    participant_identity: str
    dispatch_id: str | None
    expires_in: int


class LiveKitBrowserSessionProvider:
    def __init__(self, *, url: str, api_key: str, api_secret: str):
        self.url = str(url or "").strip()
        self.api_key = str(api_key or "").strip()
        self.api_secret = str(api_secret or "").strip()
        if not self.url or not self.api_key or not self.api_secret:
            raise LiveKitBrowserSessionError("LiveKit server credentials are unavailable")

    async def create_session(
        self,
        *,
        tenant_id: UUID,
        agent_id: UUID,
        call_id: UUID,
        agent_name: str,
        max_call_duration_seconds: int,
    ) -> LiveKitBrowserSession:
        duration_limit = int(max_call_duration_seconds)
        if not 30 <= duration_limit <= 7200:
            raise LiveKitBrowserSessionError("VAV maximum browser call duration is invalid")
        room_name = f"vav-browser-{call_id}"
        participant_identity = f"browser-{call_id}"
        metadata = create_browser_dispatch_metadata(
            tenant_id=tenant_id,
            agent_id=agent_id,
            call_id=call_id,
            room_name=room_name,
            participant_identity=participant_identity,
            ttl_seconds=BROWSER_TOKEN_TTL_SECONDS + 60,
        )
        try:
            token = self.mint_access_token(
                room_name=room_name,
                participant_identity=participant_identity,
                expires_in=BROWSER_TOKEN_TTL_SECONDS,
                agent_name=agent_name,
                dispatch_metadata=metadata,
            )
        except Exception as exc:
            raise LiveKitBrowserSessionError(
                "LiveKit could not issue the browser voice session",
                ambiguous=False,
            ) from exc
        return LiveKitBrowserSession(
            access_token=token,
            room_name=room_name,
            participant_identity=participant_identity,
            # LiveKit creates the dispatch atomically when the participant
            # presents this token and creates its unique room.
            dispatch_id=None,
            expires_in=BROWSER_TOKEN_TTL_SECONDS,
        )

    def mint_access_token(
        self,
        *,
        room_name: str,
        participant_identity: str,
        expires_in: int,
        agent_name: str | None = None,
        dispatch_metadata: str | None = None,
    ) -> str:
        """Mint another credential for an existing room without extending its deadline."""
        ttl_seconds = int(expires_in)
        if (
            not room_name
            or not participant_identity
            or not 1 <= ttl_seconds <= BROWSER_TOKEN_TTL_SECONDS
        ):
            raise LiveKitBrowserSessionError("LiveKit browser token lifetime is invalid")
        token = (
            api.AccessToken(self.api_key, self.api_secret)
            .with_identity(participant_identity)
            .with_name("VAV browser tester")
            .with_ttl(timedelta(seconds=ttl_seconds))
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=room_name,
                    can_publish=True,
                    can_subscribe=True,
                    can_publish_data=False,
                    can_publish_sources=["microphone"],
                    can_update_own_metadata=False,
                    room_admin=False,
                    room_create=False,
                    room_list=False,
                    room_record=False,
                )
            )
        )
        if agent_name is not None or dispatch_metadata is not None:
            if not agent_name or not dispatch_metadata:
                raise LiveKitBrowserSessionError(
                    "LiveKit browser dispatch configuration is incomplete"
                )
            token = token.with_room_config(
                api.RoomConfiguration(
                    name=room_name,
                    empty_timeout=BROWSER_TOKEN_TTL_SECONDS + 60,
                    departure_timeout=30,
                    max_participants=2,
                    agents=[
                        api.RoomAgentDispatch(
                            agent_name=agent_name,
                            metadata=dispatch_metadata,
                        )
                    ],
                )
            )
        return token.to_jwt()

    @staticmethod
    async def _delete_room(livekit: api.LiveKitAPI, room_name: str) -> bool:
        try:
            await livekit.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:
            return False
        return True


async def delete_browser_room(*, url: str, api_key: str, api_secret: str, room_name: str) -> bool:
    """Best-effort room termination used by the worker's duration guard."""
    if not url or not api_key or not api_secret or not room_name:
        return False
    livekit = api.LiveKitAPI(url=url, api_key=api_key, api_secret=api_secret)
    try:
        removed = await LiveKitBrowserSessionProvider._delete_room(livekit, room_name)
    finally:
        try:
            await livekit.aclose()
        except Exception:
            logger.warning("livekit_browser_api_client_close_failed", exc_info=True)
    return removed
