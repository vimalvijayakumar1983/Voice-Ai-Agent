"""LiveKit Cloud outbound SIP adapter for the VAV Inworld worker."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from uuid import UUID

import httpx
from livekit import api


class LiveKitSIPError(RuntimeError):
    """A LiveKit dispatch may be ambiguous after a network interruption."""

    def __init__(
        self,
        message: str,
        *,
        ambiguous: bool = True,
        terminal_status: str | None = None,
    ):
        super().__init__(message)
        self.ambiguous = ambiguous
        self.terminal_status = terminal_status


@dataclass(frozen=True)
class LiveKitSIPResult:
    provider_call_sid: str
    room_name: str


class LiveKitSIPProvider:
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        api_secret: str,
        http_transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.url = url
        self.api_key = api_key
        self.api_secret = api_secret
        self._http_transport = http_transport

    async def verify_worker(self, *, health_url: str, agent_name: str) -> None:
        """Verify the private worker is healthy and registered under the expected name.

        LiveKit Agents exposes two complementary endpoints. ``/`` fails when
        the worker loses its LiveKit connection, while ``/worker`` reports the
        registration metadata. Checking both prevents a saved agent name from
        being mistaken for a running worker.
        """
        origin = str(health_url or "").strip().rstrip("/")
        try:
            parsed = httpx.URL(origin)
        except (TypeError, ValueError) as exc:
            raise LiveKitSIPError("LiveKit worker health URL is invalid") from exc
        if parsed.scheme not in {"http", "https"} or not parsed.host:
            raise LiveKitSIPError("LiveKit worker health URL must be an HTTP origin")

        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(5.0),
                follow_redirects=False,
                trust_env=False,
                transport=self._http_transport,
            ) as client:
                health = await client.get(f"{origin}/")
                if health.status_code != 200:
                    raise LiveKitSIPError("LiveKit worker is not connected and healthy")
                worker = await client.get(f"{origin}/worker")
                if worker.status_code != 200:
                    raise LiveKitSIPError("LiveKit worker registration endpoint is unavailable")
                payload = worker.json()
        except LiveKitSIPError:
            raise
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            raise LiveKitSIPError("LiveKit worker health verification failed") from exc

        if not isinstance(payload, dict):
            raise LiveKitSIPError("LiveKit worker returned invalid registration metadata")
        registered_name = str(payload.get("agent_name") or "").strip()
        if not registered_name or registered_name != agent_name:
            raise LiveKitSIPError("LiveKit worker is registered under the wrong agent name")
        if not str(payload.get("worker_type") or "").strip():
            raise LiveKitSIPError("LiveKit worker registration metadata is incomplete")

    async def make_call(
        self,
        *,
        call_id: UUID,
        agent_id: UUID,
        to_number: str,
        from_number: str,
        outbound_trunk_id: str,
        agent_name: str,
        max_call_duration_seconds: int,
    ) -> LiveKitSIPResult:
        room_name = f"vav-call-{call_id}"
        livekit = api.LiveKitAPI(
            url=self.url,
            api_key=self.api_key,
            api_secret=self.api_secret,
        )
        try:
            participant = await livekit.sip.create_sip_participant(
                api.CreateSIPParticipantRequest(
                    sip_trunk_id=outbound_trunk_id,
                    sip_call_to=to_number,
                    sip_number=from_number,
                    room_name=room_name,
                    participant_identity=f"sip-{call_id}",
                    participant_name=to_number,
                    # This provider-side cap remains effective even when the
                    # Railway worker never starts or crashes before its local
                    # asyncio duration guard can run.
                    max_call_duration=timedelta(seconds=max_call_duration_seconds),
                    # The call row must not be reported as answered until the
                    # carrier confirms an active SIP participant. LiveKit then
                    # raises a SipCallError for busy/no-answer/trunk failures.
                    wait_until_answered=True,
                )
            )
            provider_call_sid = str(participant.sip_call_id or participant.participant_id).strip()
            if not provider_call_sid:
                raise LiveKitSIPError("LiveKit returned no SIP call identifier")
            # Dispatch only after the carrier has answered. This prevents a
            # dialing participant from starting an orphaned paid agent job on
            # busy, no-answer, or trunk failure.
            await livekit.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=agent_name,
                    room=room_name,
                    metadata=json.dumps({"agent_id": str(agent_id), "call_id": str(call_id)}),
                )
            )
        except api.SipCallError as exc:
            await self._delete_room(livekit, room_name)
            sip_code = exc.sip_status_code
            if sip_code in {486, 600}:
                terminal_status = "busy"
            elif sip_code in {404, 408, 410, 480, 487, 603}:
                terminal_status = "no_answer"
            else:
                terminal_status = "failed"
            raise LiveKitSIPError(
                "LiveKit SIP call ended before answer",
                # LiveKit's explicit SIP response is provider-definitive even
                # when best-effort room deletion itself returns 404/fails.
                ambiguous=False,
                terminal_status=terminal_status,
            ) from exc
        except Exception as exc:
            # The room name is deterministic and known before dialing. Deleting
            # it hangs up an answered SIP participant and terminates any agent
            # job even when dispatch returned an error after accepting work.
            cleanup_succeeded = await self._delete_room(livekit, room_name)
            raise LiveKitSIPError(
                "LiveKit could not confirm outbound SIP dispatch",
                ambiguous=not cleanup_succeeded,
                terminal_status="failed" if cleanup_succeeded else None,
            ) from exc
        finally:
            await livekit.aclose()
        return LiveKitSIPResult(provider_call_sid=provider_call_sid, room_name=room_name)

    @staticmethod
    async def _delete_room(livekit: api.LiveKitAPI, room_name: str) -> bool:
        try:
            await livekit.room.delete_room(api.DeleteRoomRequest(room=room_name))
        except Exception:
            return False
        return True

    async def verify_route(
        self,
        *,
        inbound_trunk_id: str,
        dispatch_rule_id: str,
        outbound_trunk_id: str | None,
        sip_uri: str,
        agent_name: str,
        assigned_numbers: list[str],
    ) -> None:
        """Verify exact LiveKit resources and their inbound agent binding."""
        livekit = api.LiveKitAPI(
            url=self.url,
            api_key=self.api_key,
            api_secret=self.api_secret,
        )
        try:
            inbound = await livekit.sip.list_sip_inbound_trunk(
                api.ListSIPInboundTrunkRequest(trunk_ids=[inbound_trunk_id])
            )
            rules = await livekit.sip.list_sip_dispatch_rule(
                api.ListSIPDispatchRuleRequest(dispatch_rule_ids=[dispatch_rule_id])
            )
            outbound = None
            if outbound_trunk_id:
                outbound = await livekit.sip.list_sip_outbound_trunk(
                    api.ListSIPOutboundTrunkRequest(trunk_ids=[outbound_trunk_id])
                )
        except Exception as exc:
            raise LiveKitSIPError("LiveKit route verification failed") from exc
        finally:
            await livekit.aclose()
        inbound_route = next(
            (item for item in inbound.items if item.sip_trunk_id == inbound_trunk_id),
            None,
        )
        if inbound_route is None:
            raise LiveKitSIPError("LiveKit inbound trunk was not found")
        rule = next(
            (item for item in rules.items if item.sip_dispatch_rule_id == dispatch_rule_id),
            None,
        )
        if rule is None:
            raise LiveKitSIPError("LiveKit dispatch rule was not found")
        if rule.trunk_ids and inbound_trunk_id not in rule.trunk_ids:
            raise LiveKitSIPError("LiveKit dispatch rule is not bound to the inbound trunk")
        matching_agents = [
            agent for agent in rule.room_config.agents if agent.agent_name == agent_name
        ]
        if not matching_agents:
            raise LiveKitSIPError("LiveKit dispatch rule does not target the VAV worker")
        route_numbers = {
            str(number).strip()
            for number in (
                list(getattr(inbound_route, "numbers", []) or [])
                + list(getattr(rule, "inbound_numbers", []) or [])
                + list(getattr(rule, "numbers", []) or [])
            )
            if str(number).strip()
        }
        expected_numbers = {
            str(number).strip() for number in assigned_numbers if str(number).strip()
        }
        if not expected_numbers or not expected_numbers.issubset(route_numbers):
            raise LiveKitSIPError("LiveKit route does not explicitly include every assigned DID")
        if outbound_trunk_id:
            outbound_route = next(
                (
                    item
                    for item in (outbound.items if outbound else [])
                    if item.sip_trunk_id == outbound_trunk_id
                ),
                None,
            )
            if outbound_route is None:
                raise LiveKitSIPError("LiveKit outbound trunk was not found")
            outbound_numbers = {
                str(number).strip()
                for number in (getattr(outbound_route, "numbers", []) or [])
                if str(number).strip()
            }
            if not expected_numbers.issubset(outbound_numbers):
                raise LiveKitSIPError(
                    "LiveKit outbound trunk does not explicitly allow every assigned caller DID"
                )
            configured_host = _sip_route_host(sip_uri)
            outbound_hosts = {
                host
                for host in (
                    _sip_route_host(getattr(outbound_route, "address", "")),
                    _sip_route_host(getattr(outbound_route, "from_host", "")),
                )
                if host
            }
            if not configured_host or configured_host not in outbound_hosts:
                raise LiveKitSIPError(
                    "Configured e& SIP URI does not match the LiveKit outbound trunk address"
                )


def _sip_route_host(value: object) -> str:
    """Return a comparable host from a SIP URI or LiveKit trunk address."""
    route = str(value or "").strip().lower()
    if route.startswith("sip:"):
        route = route[4:]
    elif route.startswith("sips:"):
        route = route[5:]
    route = route.split("?", 1)[0].split(";", 1)[0]
    if "@" in route:
        route = route.rsplit("@", 1)[1]
    if route.startswith("[") and "]" in route:
        return route[1 : route.index("]")].rstrip(".")
    return route.split(":", 1)[0].rstrip(".")
