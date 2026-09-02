"""Server-side LiveKit WebRTC room, dispatch, and token issuance."""

from __future__ import annotations

import asyncio
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
    dispatch_id: str
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
        livekit = api.LiveKitAPI(
            url=self.url,
            api_key=self.api_key,
            api_secret=self.api_secret,
        )
        room_created = False
        try:
            await livekit.room.create_room(
                api.CreateRoomRequest(
                    name=room_name,
                    # An unused token cannot retain a room indefinitely.
                    empty_timeout=BROWSER_TOKEN_TTL_SECONDS + 60,
                    departure_timeout=30,
                    # One standard browser participant plus the VAV agent.
                    max_participants=2,
                )
            )
            room_created = True
            metadata = create_browser_dispatch_metadata(
                tenant_id=tenant_id,
                agent_id=agent_id,
                call_id=call_id,
                room_name=room_name,
                participant_identity=participant_identity,
                ttl_seconds=BROWSER_TOKEN_TTL_SECONDS + 60,
            )
            dispatch = await livekit.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=agent_name,
                    room=room_name,
                    metadata=metadata,
                )
            )
            dispatch_id = str(dispatch.id or "").strip()
            if not dispatch_id:
                raise RuntimeError("LiveKit returned no browser dispatch identifier")
            token = self.mint_access_token(
                room_name=room_name,
                participant_identity=participant_identity,
                expires_in=BROWSER_TOKEN_TTL_SECONDS,
            )
        except asyncio.CancelledError:
            if room_created:
                # Cleanup must complete even though the request task is already
                # cancelled. Shielding preserves CancelledError for the caller
                # while preventing an orphaned dispatch/room from retaining
                # capacity or provider usage until its timeout.
                cleanup = asyncio.create_task(self._delete_room(livekit, room_name))
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
            raise
        except Exception as exc:
            cleanup_succeeded = not room_created or await self._delete_room(livekit, room_name)
            raise LiveKitBrowserSessionError(
                "LiveKit could not create the browser voice session",
                ambiguous=not cleanup_succeeded,
            ) from exc
        finally:
            try:
                await livekit.aclose()
            except Exception:
                # Transport cleanup must never replace the authoritative
                # room/dispatch result with an unrelated client-close error.
                logger.warning("livekit_browser_api_client_close_failed", exc_info=True)
        return LiveKitBrowserSession(
            access_token=token,
            room_name=room_name,
            participant_identity=participant_identity,
            dispatch_id=dispatch_id,
            expires_in=BROWSER_TOKEN_TTL_SECONDS,
        )

    def mint_access_token(
        self,
        *,
        room_name: str,
        participant_identity: str,
        expires_in: int,
    ) -> str:
        """Mint another credential for an existing room without extending its deadline."""
        ttl_seconds = int(expires_in)
        if (
            not room_name
            or not participant_identity
            or not 1 <= ttl_seconds <= BROWSER_TOKEN_TTL_SECONDS
        ):
            raise LiveKitBrowserSessionError("LiveKit browser token lifetime is invalid")
        return (
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
            .to_jwt()
        )

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
