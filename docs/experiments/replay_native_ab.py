"""Matched synthetic QA via normal browser admission; no PSTN or booking tools."""

import asyncio
import hashlib
import json
import re
import sys
import time
import uuid
from array import array
from pathlib import Path
from types import SimpleNamespace

import aiohttp
from app.api.v1.endpoints.agents import (
    _delete_browser_room_despite_cancellation,
    _tenant_inworld_client,
    create_livekit_browser_session,
)
from app.core.database import async_session_factory
from app.models.agent import Agent, AgentRuntimeProfile
from app.models.call import Call
from app.schemas.agent import LiveKitSessionRequest
from app.services.audio_replay_canary import _AgentResponseQuiescence
from livekit import rtc
from livekit.plugins.inworld import TTS
from sqlalchemy import select
from starlette.requests import Request

IDS = {
    "A": "ae2a1477-709d-41f7-a150-f952a657d1e5",
    "B": "5730e252-002b-42be-ad13-554358c788e4",
}
CASES = [
    ("simple", ("Hello. Can you hear me?",), 0),
    ("chairman", ("Who is the chairman of Al Zaabi Group?",), 0),
    ("established", ("When was Al Zaabi Group established?",), 0),
    ("leadership", ("List the leadership team of Al Zaabi Group.",), 0),
    ("interrupt", ("Stop. Just give me the phone number.",), 0),
    ("paused_name", ("Is Saeed Al Zaabi", "in Al Zaabi Trading?"), 700),
    ("follow_up", ("No, I mean Al Zaabi Group. What is his role?",), 0),
    (
        "switch_company",
        ("Give me the phone number for Adam and Eve Cosmetic Medical Center.",),
        0,
    ),
    (
        "correct_company",
        ("Not the cosmetic centre. I mean Adam and Eve Specialized Medical Center.",),
        0,
    ),
    (
        "unsupported",
        (
            "What is the annual revenue? I was told it is one billion dirhams. Is that correct?",
        ),
        0,
    ),
    (
        "date_correction",
        (
            "Do not make a booking. I need an appointment on Monday,",
            "no, Tuesday afternoon. Which day did I ask for?",
        ),
        1200,
    ),
    ("closing", ("Thank you and goodbye.",), 0),
]
lane = sys.argv[1]
round_id = sys.argv[2]
assert lane in IDS
cases = CASES[:2] if "--smoke" in sys.argv else CASES
if "--knowledge-smoke" in sys.argv:
    cases = CASES[1:2]
if "--focused-v2" in sys.argv:
    cases = [
        case
        for case in CASES
        if case[0] not in {"paused_name", "follow_up", "unsupported"}
    ]


def voiced(data):
    values = array("h", data)
    return bool(values) and sum(v * v for v in values) / len(values) > 200**2


async def main():
    qa = uuid.UUID(IDS[lane])
    async with async_session_factory() as db:
        agent = await db.get(Agent, qa)
        assert agent.agent_metadata.get("qa_ab_run") == "native-ab-20260906"
        p = await db.scalar(
            select(AgentRuntimeProfile).where(AgentRuntimeProfile.agent_id == qa)
        )
        assert p.runtime_config["inworld_single_pass"] == (lane == "A")
        assert not p.assigned_numbers and not agent.transfer_number
        assert (
            await db.scalar(
                select(Call.id)
                .where(
                    Call.agent_id == qa,
                    Call.status.in_(["initiated", "ringing", "in_progress"]),
                )
                .limit(1)
            )
            is None
        )
        tenant_id = agent.tenant_id
        client, _, _ = await _tenant_inworld_client(db, tenant_id)
    clips = []
    async with aiohttp.ClientSession() as http:
        tts = TTS(
            api_key=client.api_key,
            voice="Ashley",
            model="inworld-tts-1.5-max",
            encoding="PCM",
            sample_rate=24000,
            http_session=http,
        )
        try:
            directory = Path("/tmp/vav-native-ab-20260906")
            directory.mkdir(mode=0o700, exist_ok=True)
            for label, segments, gap in cases:
                key = hashlib.sha256(
                    json.dumps(
                        [segments, gap, "Ashley", "inworld-tts-1.5-max", 24000]
                    ).encode()
                ).hexdigest()
                path = directory / (key + ".pcm")
                if path.exists():
                    pcm = path.read_bytes()
                else:
                    pcm = b""
                    for i, segment in enumerate(segments):
                        if i:
                            pcm += b"\0" * (48 * gap)
                        async with asyncio.timeout(20):
                            async with tts.synthesize(segment) as stream:
                                async for chunk in stream:
                                    pcm += bytes(chunk.frame.data)
                    assert 0 < len(pcm) < 48000 * 30
                    path.write_bytes(pcm)
                clips.append(pcm)
        finally:
            await tts.aclose()
    async with async_session_factory() as db:
        issued = await create_livekit_browser_session(
            agent_id=qa,
            data=LiveKitSessionRequest(variables={}),
            request=Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/internal-qa",
                    "headers": [],
                    "client": ("127.0.0.1", 0),
                }
            ),
            idempotency_key="native-ab-"
            + lane
            + "-"
            + round_id
            + "-"
            + uuid.uuid4().hex,
            current_user=SimpleNamespace(tenant_id=tenant_id, id=None),
            db=db,
        )
    print(
        "AB_CALL:"
        + json.dumps({"lane": lane, "round": round_id, "call_id": str(issued.call_id)}),
        flush=True,
    )
    room = rtc.Room()
    state = _AgentResponseQuiescence()
    ready = asyncio.Event()
    readers = []
    samples = []
    transcripts = []
    started = time.monotonic()
    source = None
    error = None

    @room.on("participant_connected")
    def connected(participant):
        if state.observe_participant(participant):
            ready.set()

    @room.on("participant_attributes_changed")
    def attributes(changed, participant):
        state.observe_attributes(changed, participant)

    @room.on("transcription_received")
    def transcript(segments, participant, publication):
        state.observe_transcription(segments, participant)
        for segment in segments:
            if segment.final:
                item = {
                    "at_ms": round((time.monotonic() - started) * 1000),
                    "agent": state._is_agent(participant),
                    "text": segment.text,
                }
                transcripts.append(item)
                print(
                    "AB_TEXT:" + json.dumps(dict(item, lane=lane, round=round_id)),
                    flush=True,
                )

    async def drain(track):
        stream = rtc.AudioStream(track)
        try:
            async for event in stream:
                if voiced(bytes(event.frame.data)):
                    samples.append(time.monotonic())
        finally:
            await stream.aclose()

    @room.on("track_subscribed")
    def subscribed(track, publication, participant):
        if track.kind == rtc.TrackKind.KIND_AUDIO and state._is_agent(participant):
            readers.append(asyncio.create_task(drain(track)))

    try:
        await room.connect(issued.url, issued.access_token)
        assert room.name == issued.room_name
        for participant in room.remote_participants.values():
            connected(participant)
        await asyncio.wait_for(ready.wait(), 30)
        source = rtc.AudioSource(24000, 1, queue_size_ms=100)
        track = rtc.LocalAudioTrack.create_audio_track("synthetic-ab-caller", source)
        await room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        )
        await asyncio.sleep(2)
        for index, ((label, segments, gap), pcm) in enumerate(zip(cases, clips)):
            if label != "interrupt":
                await state.wait_until_listening(35)
                await asyncio.sleep(0.7)
            tail = " ".join(re.findall(r"\w+", segments[-1].casefold())[-3:])
            state.arm(
                list(room.remote_participants.values()), expected_caller_tail=tail
            )
            text_index = len(transcripts)
            turn_start = time.monotonic()
            last_voiced = 0
            for frame_index, offset in enumerate(range(0, len(pcm), 960)):
                data = pcm[offset : offset + 960].ljust(960, b"\0")
                if voiced(data):
                    last_voiced = (frame_index + 1) * 0.02
                await source.capture_frame(
                    rtc.AudioFrame(
                        data=data,
                        sample_rate=24000,
                        num_channels=1,
                        samples_per_channel=480,
                    )
                )
                await asyncio.sleep(
                    max(0, turn_start + (frame_index + 1) * 0.02 - time.monotonic())
                )
            await source.wait_for_playout()
            last_speech_at = turn_start + last_voiced
            observation_error = None
            if label == "leadership":
                async with asyncio.timeout(25):
                    while not state.response_started or state.agent_state != "speaking":
                        await asyncio.sleep(0.05)
                await asyncio.sleep(1)
            else:
                await asyncio.sleep(2)
                try:
                    await state.wait(20)
                    await state.wait_until_listening(35)
                    # Avoid advancing on a filler followed by a tool-driven answer.
                    async with asyncio.timeout(35):
                        while state.agent_state != "listening" or (
                            samples and time.monotonic() - samples[-1] < 1.5
                        ):
                            await asyncio.sleep(0.1)
                except Exception as exc:
                    observation_error = type(exc).__name__
                    # Record a silent turn as a failed scenario, not a fast answer.
                    # Only continue when the room is actually ready for new speech.
                    if state.agent_state != "listening":
                        raise
            eligible = [s for s in samples if s >= last_speech_at]
            result = {
                "lane": lane,
                "round": round_id,
                "case": label,
                "call_id": str(issued.call_id),
                "fixture_sha256": hashlib.sha256(pcm).hexdigest(),
                "client_first_voiced_after_fixture_speech_ms": round(
                    (eligible[0] - last_speech_at) * 1000
                )
                if eligible
                else None,
                "overlap_case": label == "interrupt",
                "measurement_version": "caller-tail-v2",
                "complete_caller_boundary_observed": state.caller_final_observed,
                "useful_answer_audio_ms": None,
                "transcripts": transcripts[text_index:],
            }
            result["observation_error"] = observation_error
            print("AB_TURN:" + json.dumps(result), flush=True)
        print(
            "AB_COMPLETE:"
            + json.dumps(
                {"lane": lane, "round": round_id, "call_id": str(issued.call_id)}
            ),
            flush=True,
        )
    except Exception as exc:  # noqa: BLE001 - redact provider errors before runner output
        error = type(exc).__name__
        print(
            "AB_ERROR:"
            + json.dumps(
                {
                    "lane": lane,
                    "round": round_id,
                    "call_id": str(issued.call_id),
                    "error": error,
                    "state": state.agent_state,
                }
            ),
            flush=True,
        )
    finally:
        for task in readers:
            task.cancel()
        await asyncio.gather(*readers, return_exceptions=True)
        try:
            await room.disconnect()
        finally:
            if source:
                await source.aclose()
            await _delete_browser_room_despite_cancellation(room_name=issued.room_name)
    if error:
        raise RuntimeError(error)


async def bounded():
    try:
        await asyncio.wait_for(main(), 540)
    except Exception as exc:  # noqa: BLE001 - redact provider errors before runner output
        detail = {"error": type(exc).__name__}
        if hasattr(exc, "status_code"):
            detail.update(status=exc.status_code, detail=exc.detail)
        print("AB_FAILURE:" + json.dumps(detail), flush=True)
        raise SystemExit(1)


asyncio.run(bounded())
