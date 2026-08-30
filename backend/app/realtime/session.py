"""Bidirectional Twilio media session orchestrated by VAV."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select

from app.ai.conversation import conversation_engine
from app.core.config import settings
from app.core.database import async_session_factory
from app.models.agent import Agent, AgentRuntimeProfile
from app.models.call import Call, CallTranscript
from app.realtime.sarvam_stream import (
    SarvamSTTStream,
    SarvamTTSStream,
    is_speech_end,
    is_speech_start,
    parse_transcript_event,
)
from app.services.knowledge_retrieval import retrieve_knowledge_context
from app.services.provider_credentials import load_provider_config

logger = structlog.get_logger()
TTS_MAX_FRAGMENT_CHARS = 120
_TTS_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?।])(?:\s+|$)")


@dataclass(frozen=True)
class RuntimeSessionConfig:
    call_id: UUID
    tenant_id: UUID
    agent_id: UUID
    system_prompt: str
    greeting_message: str
    fallback_message: str
    speaker: str
    language_code: str
    stt_language: str
    speech_rate: float
    temperature: float
    max_tokens: int
    llm_model: str
    max_duration_seconds: int
    sarvam_api_key: str


def _language_code(language: str) -> str:
    normalized = language.strip().replace("_", "-")
    if normalized.endswith("-IN"):
        return normalized
    aliases = {"or": "od"}
    base = aliases.get(normalized.split("-", 1)[0].lower(), normalized.split("-", 1)[0].lower())
    return f"{base}-IN"


def split_tts_buffer(value: str, *, final: bool = False) -> tuple[list[str], str]:
    """Extract sentence-aware low-latency fragments from streamed LLM text."""
    fragments: list[str] = []
    remaining = value
    while remaining:
        window = remaining[:TTS_MAX_FRAGMENT_CHARS]
        boundary = _TTS_SENTENCE_BOUNDARY.search(window)
        if boundary is not None:
            fragments.append(remaining[: boundary.end()])
            remaining = remaining[boundary.end() :]
            continue
        if len(remaining) >= TTS_MAX_FRAGMENT_CHARS:
            split_at = window.rfind(" ")
            if split_at < TTS_MAX_FRAGMENT_CHARS // 2:
                split_at = TTS_MAX_FRAGMENT_CHARS
                separator = ""
            else:
                separator = " "
            fragments.append(remaining[:split_at] + separator)
            remaining = remaining[split_at:].lstrip()
            continue
        if final:
            fragments.append(remaining)
            remaining = ""
        break
    return fragments, remaining


def latency_percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[min(index, len(ordered) - 1)]


async def load_runtime_session(call_id: UUID) -> RuntimeSessionConfig | None:
    async with async_session_factory() as db:
        row = (
            await db.execute(
                select(Call, Agent, AgentRuntimeProfile)
                .join(Agent, Agent.id == Call.agent_id)
                .join(AgentRuntimeProfile, AgentRuntimeProfile.agent_id == Agent.id)
                .where(Call.id == call_id, Call.tenant_id == Agent.tenant_id)
            )
        ).one_or_none()
        if row is None:
            return None
        call, agent, profile = row
        if (
            not agent.is_active
            or agent.voice_provider != "sarvam"
            or not profile.enabled
            or profile.status != "active"
            or profile.telephony_provider != "twilio"
        ):
            return None
        tenant_config = await load_provider_config(db, agent.tenant_id, "sarvam")
        api_key = str((tenant_config or {}).get("api_key") or settings.sarvam_api_key).strip()
        if not api_key:
            return None
        speaker = agent.voice_id.removeprefix("sarvam:") or "ishita"
        primary_language = _language_code(agent.language)
        stt_language = "auto" if agent.language_switching_enabled else profile.stt_language
        if stt_language != "auto":
            stt_language = _language_code(stt_language)
        return RuntimeSessionConfig(
            call_id=call.id,
            tenant_id=call.tenant_id,
            agent_id=agent.id,
            system_prompt=agent.system_prompt,
            greeting_message=agent.greeting_message or "Hello, how may I help you today?",
            fallback_message=(
                agent.fallback_message or "I am sorry, I could not complete that request."
            ),
            speaker=speaker,
            language_code=primary_language,
            stt_language=stt_language,
            speech_rate=agent.speech_rate,
            temperature=agent.temperature,
            max_tokens=agent.max_tokens,
            llm_model=profile.llm_model,
            max_duration_seconds=agent.max_call_duration_seconds,
            sarvam_api_key=api_key,
        )


async def _append_turn(config: RuntimeSessionConfig, role: str, content: str) -> None:
    async with async_session_factory() as db:
        transcript = await db.scalar(
            select(CallTranscript).where(
                CallTranscript.call_id == config.call_id,
                CallTranscript.tenant_id == config.tenant_id,
            )
        )
        turn = {
            "role": role,
            "content": content,
            "timestamp": datetime.now(UTC).isoformat(),
        }
        if transcript is None:
            transcript = CallTranscript(
                tenant_id=config.tenant_id,
                call_id=config.call_id,
                turns=[turn],
                full_text=f"{role.title()}: {content}",
            )
            db.add(transcript)
        else:
            turns = list(transcript.turns or [])
            turns.append(turn)
            transcript.turns = turns
            transcript.full_text = "\n".join(
                f"{str(item.get('role', 'unknown')).title()}: {item.get('content', '')}"
                for item in turns
            )
        await db.commit()


async def _store_metrics(config: RuntimeSessionConfig, metrics: dict) -> None:
    async with async_session_factory() as db:
        call = await db.scalar(
            select(Call).where(Call.id == config.call_id, Call.tenant_id == config.tenant_id)
        )
        if call is None:
            return
        metadata = dict(call.call_metadata or {})
        metadata["runtime"] = {
            "speech_provider": "sarvam",
            "llm_provider": "openai",
            "llm_model": config.llm_model,
            "stt_language": config.stt_language,
            "turn_count": metrics.get("turn_count", 0),
            "llm_tokens": metrics.get("llm_tokens", 0),
            "inbound_audio_bytes": metrics.get("inbound_audio_bytes", 0),
            "outbound_audio_bytes": metrics.get("outbound_audio_bytes", 0),
            "barge_in_count": metrics.get("barge_in_count", 0),
            "last_llm_latency_ms": metrics.get("last_llm_latency_ms"),
            "last_llm_first_token_ms": metrics.get("last_llm_first_token_ms"),
            "last_tts_first_byte_ms": metrics.get("last_tts_first_byte_ms"),
            "last_transcript_to_first_audio_ms": metrics.get("last_transcript_to_first_audio_ms"),
            "last_speech_end_to_first_audio_ms": metrics.get("last_speech_end_to_first_audio_ms"),
            "turn_latency_p50_ms": metrics.get("turn_latency_p50_ms"),
            "turn_latency_p95_ms": metrics.get("turn_latency_p95_ms"),
            "cost_state": "pending_provider_billing_sync",
        }
        call.call_metadata = metadata
        await db.commit()


async def _finalize_inbound_call(config: RuntimeSessionConfig) -> None:
    async with async_session_factory() as db:
        call = await db.scalar(
            select(Call).where(Call.id == config.call_id, Call.tenant_id == config.tenant_id)
        )
        if (
            call is None
            or call.direction != "inbound"
            or call.status
            in {
                "completed",
                "failed",
                "busy",
                "no_answer",
            }
        ):
            return
        now = datetime.now(UTC)
        call.status = "completed"
        call.ended_at = now
        if call.answered_at:
            call.duration_seconds = max(round((now - call.answered_at).total_seconds()), 0)
        await db.commit()


async def run_twilio_media_session(websocket: WebSocket, config: RuntimeSessionConfig) -> None:
    stream_sid: str | None = None
    stop_event = asyncio.Event()
    send_lock = asyncio.Lock()
    utterances: asyncio.Queue[tuple[str, str | None, float, float | None]] = asyncio.Queue(
        maxsize=8
    )
    history: list[dict[str, str]] = []
    current_response: asyncio.Task | None = None
    last_speech_end_at: float | None = None
    turn_latency_samples: list[int] = []
    tts = SarvamTTSStream(
        api_key=config.sarvam_api_key,
        base_url=settings.sarvam_base_url,
        speaker=config.speaker,
        pace=config.speech_rate,
    )
    metrics: dict[str, int | float | None] = {
        "turn_count": 0,
        "llm_tokens": 0,
        "inbound_audio_bytes": 0,
        "outbound_audio_bytes": 0,
        "barge_in_count": 0,
        "last_llm_latency_ms": None,
        "last_llm_first_token_ms": None,
        "last_tts_first_byte_ms": None,
        "last_transcript_to_first_audio_ms": None,
        "last_speech_end_to_first_audio_ms": None,
        "turn_latency_p50_ms": None,
        "turn_latency_p95_ms": None,
    }

    async def send_json(payload: dict) -> None:
        async with send_lock:
            await websocket.send_text(json.dumps(payload))

    async def clear_playback() -> None:
        if stream_sid:
            await send_json({"event": "clear", "streamSid": stream_sid})

    async def play_fragments(
        fragments: AsyncIterator[str],
        language_code: str,
        *,
        first_input_at: Callable[[], float | None],
        transcript_at: float | None = None,
        speech_end_at: float | None = None,
        on_first_audio: Callable[[float], None] | None = None,
    ) -> None:
        first = True
        async for audio in tts.audio_for(fragments, language_code=language_code):
            if stop_event.is_set() or not stream_sid:
                return
            if first:
                first_audio_at = time.perf_counter()
                if on_first_audio is not None:
                    on_first_audio(first_audio_at)
                input_at = first_input_at() or first_audio_at
                metrics["last_tts_first_byte_ms"] = round((first_audio_at - input_at) * 1000)
                if transcript_at is not None:
                    metrics["last_transcript_to_first_audio_ms"] = round(
                        (first_audio_at - transcript_at) * 1000
                    )
                if speech_end_at is not None:
                    speech_latency = round((first_audio_at - speech_end_at) * 1000)
                    metrics["last_speech_end_to_first_audio_ms"] = speech_latency
                    turn_latency_samples.append(speech_latency)
                    metrics["turn_latency_p50_ms"] = latency_percentile(turn_latency_samples, 0.5)
                    metrics["turn_latency_p95_ms"] = latency_percentile(turn_latency_samples, 0.95)
                first = False
            metrics["outbound_audio_bytes"] = int(metrics["outbound_audio_bytes"] or 0) + (
                len(audio) * 3 // 4
            )
            await send_json(
                {"event": "media", "streamSid": stream_sid, "media": {"payload": audio}}
            )
        if stream_sid:
            await send_json(
                {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": f"turn-{metrics['turn_count']}"},
                }
            )

    async def play_text(text: str, language_code: str) -> None:
        input_at = time.perf_counter()

        async def fragments() -> AsyncIterator[str]:
            yield text

        await play_fragments(fragments(), language_code, first_input_at=lambda: input_at)

    async def handle_turn(
        text: str,
        detected_language: str | None,
        transcript_at: float,
        speech_end_at: float | None,
    ) -> None:
        await _append_turn(config, "user", text)
        history.append({"role": "user", "content": text})
        async with async_session_factory() as db:
            knowledge = await retrieve_knowledge_context(
                db,
                tenant_id=config.tenant_id,
                agent_id=config.agent_id,
                query=text,
            )
        llm_started = time.perf_counter()
        first_tts_input_at: float | None = None
        response_parts: list[str] = []
        tokens = 0
        persisted = False
        audio_started = False
        language = (
            detected_language
            if detected_language and detected_language != "auto"
            else config.language_code
        )

        async def response_fragments() -> AsyncIterator[str]:
            nonlocal first_tts_input_at, tokens
            buffer = ""
            first_token = True
            async for event in conversation_engine.stream_response(
                config.system_prompt,
                history[-12:],
                model=config.llm_model,
                temperature=config.temperature,
                max_tokens=config.max_tokens,
                knowledge_context=knowledge,
            ):
                if event.is_final:
                    tokens = event.tokens_used
                    metrics["last_llm_latency_ms"] = round(
                        (time.perf_counter() - llm_started) * 1000
                    )
                    ready, buffer = split_tts_buffer(buffer, final=True)
                else:
                    if first_token:
                        metrics["last_llm_first_token_ms"] = round(
                            (time.perf_counter() - llm_started) * 1000
                        )
                        first_token = False
                    response_parts.append(event.text)
                    buffer += event.text
                    ready, buffer = split_tts_buffer(buffer)
                for fragment in ready:
                    if first_tts_input_at is None:
                        first_tts_input_at = time.perf_counter()
                    yield fragment
            if not response_parts:
                response_parts.append(config.fallback_message)
                if first_tts_input_at is None:
                    first_tts_input_at = time.perf_counter()
                yield config.fallback_message

        async def persist_response() -> None:
            nonlocal persisted
            response = "".join(response_parts).strip()
            if persisted or not response:
                return
            history.append({"role": "assistant", "content": response})
            await _append_turn(config, "assistant", response)
            persisted = True

        def mark_audio_started(_started_at: float) -> None:
            nonlocal audio_started
            audio_started = True

        try:
            await play_fragments(
                response_fragments(),
                language,
                first_input_at=lambda: first_tts_input_at,
                transcript_at=transcript_at,
                speech_end_at=speech_end_at,
                on_first_audio=mark_audio_started,
            )
        except asyncio.CancelledError:
            await persist_response()
            await _store_metrics(config, metrics)
            raise
        except Exception:
            logger.exception("realtime_streamed_response_failed", call_id=str(config.call_id))
            metrics["last_llm_latency_ms"] = round((time.perf_counter() - llm_started) * 1000)
            if not audio_started:
                response_parts.clear()
                response_parts.append(config.fallback_message)
                await play_text(config.fallback_message, language)
        metrics["llm_tokens"] = int(metrics["llm_tokens"] or 0) + int(tokens)
        metrics["turn_count"] = int(metrics["turn_count"] or 0) + 1
        await persist_response()
        await _store_metrics(config, metrics)

    async def receive_twilio(stt: SarvamSTTStream) -> None:
        nonlocal stream_sid, current_response
        try:
            while not stop_event.is_set():
                message = await websocket.receive_text()
                payload = json.loads(message)
                event = payload.get("event")
                if event == "start":
                    start = payload.get("start") or {}
                    stream_sid = (
                        str(start.get("streamSid") or payload.get("streamSid") or "") or None
                    )
                    if stream_sid and current_response is None:
                        current_response = asyncio.create_task(
                            play_text(config.greeting_message, config.language_code)
                        )
                elif event == "media":
                    audio = (payload.get("media") or {}).get("payload")
                    if isinstance(audio, str) and audio:
                        metrics["inbound_audio_bytes"] = int(
                            metrics["inbound_audio_bytes"] or 0
                        ) + (len(audio) * 3 // 4)
                        await stt.send_audio(audio)
                elif event == "stop":
                    stop_event.set()
        except (WebSocketDisconnect, json.JSONDecodeError):
            stop_event.set()

    async def receive_sarvam(stt: SarvamSTTStream) -> None:
        nonlocal current_response, last_speech_end_at
        async for payload in stt.events():
            if stop_event.is_set():
                return
            if is_speech_start(payload):
                last_speech_end_at = None
                if current_response is not None and not current_response.done():
                    current_response.cancel()
                    metrics["barge_in_count"] = int(metrics["barge_in_count"] or 0) + 1
                await clear_playback()
                continue
            if is_speech_end(payload):
                last_speech_end_at = time.perf_counter()
                continue
            transcript = parse_transcript_event(payload)
            if transcript and transcript.is_final:
                transcript_at = time.perf_counter()
                speech_end_at = last_speech_end_at
                last_speech_end_at = None
                try:
                    utterances.put_nowait(
                        (
                            transcript.text,
                            transcript.language_code,
                            transcript_at,
                            speech_end_at,
                        )
                    )
                except asyncio.QueueFull:
                    logger.warning("realtime_utterance_queue_full", call_id=str(config.call_id))

    async def response_worker() -> None:
        nonlocal current_response
        while not stop_event.is_set():
            text, language, transcript_at, speech_end_at = await utterances.get()
            if current_response is not None and not current_response.done():
                current_response.cancel()
                try:
                    await current_response
                except asyncio.CancelledError:
                    pass
            current_response = asyncio.create_task(
                handle_turn(text, language, transcript_at, speech_end_at)
            )
            try:
                await current_response
            except asyncio.CancelledError:
                continue
            except Exception:
                logger.exception("realtime_turn_failed", call_id=str(config.call_id))

    async with (
        SarvamSTTStream(
            api_key=config.sarvam_api_key,
            base_url=settings.sarvam_base_url,
            language_code=config.stt_language,
        ) as stt,
        tts,
    ):
        tasks = [
            asyncio.create_task(receive_twilio(stt)),
            asyncio.create_task(receive_sarvam(stt)),
            asyncio.create_task(response_worker()),
        ]
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=config.max_duration_seconds)
        except TimeoutError:
            stop_event.set()
            await clear_playback()
        finally:
            for task in tasks:
                task.cancel()
            if current_response is not None:
                current_response.cancel()
            pending = [*tasks]
            if current_response is not None:
                pending.append(current_response)
            await asyncio.gather(*pending, return_exceptions=True)
            await _store_metrics(config, metrics)
            await _finalize_inbound_call(config)
