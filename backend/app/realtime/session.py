"""Bidirectional Twilio media session orchestrated by VAV."""

from __future__ import annotations

import asyncio
import json
import math
import re
import time
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

import structlog
from fastapi import WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.orm import noload

from app.ai.conversation import ConversationEngine
from app.core.config import settings
from app.core.database import async_session_factory
from app.models.agent import Agent, AgentRuntimeProfile
from app.models.call import Call, CallTranscript
from app.providers.sarvam import sarvam_language_code
from app.realtime.elevenlabs_stream import ElevenLabsTTSStream
from app.realtime.sarvam_stream import (
    SarvamSTTStream,
    SarvamTTSStream,
    is_speech_end,
    is_speech_start,
    parse_transcript_event,
)
from app.services.knowledge_retrieval import (
    load_agent_knowledge_terminology,
    retrieve_knowledge_context,
)
from app.services.knowledge_serving import (
    INBOUND_KNOWLEDGE_ADMISSION_STATE,
    KNOWLEDGE_ADMISSION_STATE,
    KnowledgeServingError,
    knowledge_admission_is_durable,
    load_durably_admitted_serving_revision,
    serving_knowledge_base_id_from_call_metadata,
    serving_revision_id_from_call_metadata,
    serving_revocation_generation_from_call_metadata,
    validate_call_speech_lexicon_reservation,
)
from app.services.provider_credentials import load_provider_config
from app.services.realtime_speech_config import sarvam_stt_wire_language
from app.services.runtime_capacity import TERMINAL_CALL_STATUSES

logger = structlog.get_logger()
TTS_MAX_FRAGMENT_CHARS = 120
MAX_VOICE_RESPONSE_TOKENS = 160
VOICE_CALL_GUIDANCE = """

Voice-call delivery requirements:
- Keep the first answer to one or two short sentences unless the caller asks for more detail.
- Ask no more than one question at a time.
- Expand an unfamiliar medical abbreviation on first use, then pronounce its letters separately.
- Prefer clear, speakable wording over symbols, dense lists, or long paragraphs.
""".rstrip()
_TTS_SENTENCE_BOUNDARY = re.compile(r"(?<=[.!?।])(?:\s+|$)")
_SQLITE_MEDIA_CLAIM_LOCK = asyncio.Lock()


class RuntimeMediaSessionAlreadyClaimedError(RuntimeError):
    """A different WebSocket already owns this call's paid media session."""


class _NullAsyncContext:
    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


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
    speech_provider: str
    sarvam_api_key: str
    tts_api_key: str
    openai_api_key: str
    knowledge_serving_revision_id: UUID
    knowledge_serving_knowledge_base_id: UUID
    knowledge_serving_revocation_generation: int
    knowledge_terminology: tuple[str, ...]
    media_session_claim_id: UUID | None = None
    media_stream_sid: str | None = None


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


async def audio_with_fallback(
    primary: ElevenLabsTTSStream | SarvamTTSStream,
    fallback: SarvamTTSStream | None,
    fragments: AsyncIterator[str],
    *,
    language_code: str,
    on_primary_failure: Callable[[], None] | None = None,
    on_fallback: Callable[[], None] | None = None,
) -> AsyncIterator[str]:
    """Replay all consumed text through a fallback when primary TTS fails before audio."""
    captured: list[str] = []
    audio_started = False

    async def capture_fragments() -> AsyncIterator[str]:
        async for fragment in fragments:
            captured.append(fragment)
            yield fragment

    try:
        async for audio in primary.audio_for(capture_fragments(), language_code=language_code):
            audio_started = True
            yield audio
        return
    except asyncio.CancelledError:
        raise
    except Exception:
        if on_primary_failure is not None:
            on_primary_failure()
        if fallback is None or audio_started:
            raise
        logger.exception("realtime_primary_tts_failed_before_audio")

    if on_fallback is not None:
        on_fallback()

    async def replay_fragments() -> AsyncIterator[str]:
        for fragment in captured:
            yield fragment
        async for fragment in fragments:
            yield fragment

    async for audio in fallback.audio_for(replay_fragments(), language_code=language_code):
        yield audio


def latency_percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * percentile) - 1)
    return ordered[min(index, len(ordered) - 1)]


def voice_response_token_limit(configured_limit: int) -> int:
    """Bound live-call responses without changing stored agent configuration."""

    return max(32, min(int(configured_limit), MAX_VOICE_RESPONSE_TOKENS))


async def claim_runtime_media_session(
    call_id: UUID,
    *,
    stream_sid: str,
    provider_call_sid: str,
) -> UUID | None:
    """Atomically grant one WebSocket ownership of a paid native call.

    The media capability is a bearer credential and Twilio can retry delivery.
    Persisting the winning claim before any provider is opened prevents a
    replay from creating a second STT/TTS/LLM session.  PostgreSQL serializes
    contenders with the Call row lock; SQLite uses a process lock so the same
    invariant is exercised by local deployments and tests.
    """

    stream_sid = str(stream_sid or "").strip()
    provider_call_sid = str(provider_call_sid or "").strip()
    if not stream_sid or not provider_call_sid:
        return None
    async with async_session_factory() as db:
        use_local_lock = db.get_bind().dialect.name == "sqlite"
        if use_local_lock:
            await _SQLITE_MEDIA_CLAIM_LOCK.acquire()
        try:
            call = await db.scalar(
                select(Call)
                .where(Call.id == call_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                call is None
                or call.provider != "twilio"
                or call.direction not in {"inbound", "outbound"}
                or call.status in TERMINAL_CALL_STATUSES
                or call.provider_call_sid != provider_call_sid
            ):
                return None
            metadata = call.call_metadata if isinstance(call.call_metadata, dict) else {}
            runtime = metadata.get("runtime")
            if (
                not isinstance(runtime, dict)
                or runtime.get("transport") != "twilio_media_streams"
                or not knowledge_admission_is_durable(metadata)
            ):
                return None
            existing_claim = runtime.get("media_session_claim")
            if isinstance(existing_claim, dict) and existing_claim.get("id"):
                raise RuntimeMediaSessionAlreadyClaimedError(
                    "A media session already owns this call"
                )
            claim_id = uuid4()
            claimed_at = datetime.now(UTC).isoformat()
            call.call_metadata = {
                **metadata,
                "runtime": {
                    **runtime,
                    "media_session_claim": {
                        "id": str(claim_id),
                        "stream_sid": stream_sid,
                        "provider_call_sid": provider_call_sid,
                        "claimed_at": claimed_at,
                    },
                    "media_stream_started": True,
                    "media_stream_started_at": claimed_at,
                    "cost_state": "pending_provider_billing_sync",
                },
            }
            await db.commit()
            return claim_id
        finally:
            if use_local_lock:
                _SQLITE_MEDIA_CLAIM_LOCK.release()


async def load_runtime_session(
    call_id: UUID,
    *,
    media_session_claim_id: UUID | None = None,
) -> RuntimeSessionConfig | None:
    async with async_session_factory() as db:
        row = (
            await db.execute(
                select(Call, Agent, AgentRuntimeProfile)
                .join(Agent, Agent.id == Call.agent_id)
                .join(AgentRuntimeProfile, AgentRuntimeProfile.agent_id == Agent.id)
                .where(Call.id == call_id, Call.tenant_id == Agent.tenant_id)
                .options(
                    noload(Agent.knowledge_bases),
                    noload(Agent.knowledge_binding),
                    noload(Agent.runtime_profile),
                )
            )
        ).one_or_none()
        if row is None:
            return None
        call, agent, profile = row
        if (
            call.provider != "twilio"
            or call.direction not in {"inbound", "outbound"}
            or call.status in TERMINAL_CALL_STATUSES
            or not agent.is_active
            or agent.voice_provider not in {"sarvam", "elevenlabs"}
            or not profile.enabled
            or profile.status != "active"
            or profile.telephony_provider != "twilio"
            or profile.primary_speech_provider != agent.voice_provider
        ):
            return None
        try:
            if not knowledge_admission_is_durable(call.call_metadata):
                return None
            serving_revision_id = serving_revision_id_from_call_metadata(call.call_metadata)
            knowledge_base_id = serving_knowledge_base_id_from_call_metadata(call.call_metadata)
            revocation_generation = serving_revocation_generation_from_call_metadata(
                call.call_metadata
            )
        except KnowledgeServingError:
            return None
        if (
            serving_revision_id is None
            or knowledge_base_id is None
            or revocation_generation is None
        ):
            return None
        serving_revision = await load_durably_admitted_serving_revision(
            db,
            tenant_id=call.tenant_id,
            knowledge_base_id=knowledge_base_id,
            serving_revision_id=serving_revision_id,
            include_sources=False,
        )
        if serving_revision is None:
            return None
        metadata = call.call_metadata if isinstance(call.call_metadata, dict) else {}
        try:
            await validate_call_speech_lexicon_reservation(
                db,
                tenant_id=call.tenant_id,
                knowledge_base_id=knowledge_base_id,
                revision=serving_revision,
                metadata=metadata,
            )
        except KnowledgeServingError:
            return None
        runtime = metadata.get("runtime")
        if not isinstance(runtime, dict):
            return None
        stored_claim = runtime.get("media_session_claim")
        stored_claim_id: UUID | None = None
        stored_stream_sid: str | None = None
        if isinstance(stored_claim, dict):
            try:
                stored_claim_id = UUID(str(stored_claim.get("id") or ""))
            except ValueError:
                stored_claim_id = None
            stored_stream_sid = str(stored_claim.get("stream_sid") or "").strip() or None
        if media_session_claim_id is not None and stored_claim_id != media_session_claim_id:
            return None
        expected_admission_state = (
            INBOUND_KNOWLEDGE_ADMISSION_STATE
            if call.direction == "inbound"
            else KNOWLEDGE_ADMISSION_STATE
        )
        if (
            runtime.get("transport") != "twilio_media_streams"
            or runtime.get("speech_provider") != agent.voice_provider
            or runtime.get("knowledge_admission_state") != expected_admission_state
            or runtime.get("knowledge_serving_content_sha256") != serving_revision.content_sha256
            or runtime.get("knowledge_source_revision_sha256")
            != serving_revision.source_revision_sha256
        ):
            return None
        knowledge_terminology = await load_agent_knowledge_terminology(
            db,
            tenant_id=call.tenant_id,
            agent_id=agent.id,
            hints=(agent.name,),
            serving_revision_id=serving_revision_id,
            knowledge_base_id=knowledge_base_id,
        )
        sarvam_config = await load_provider_config(db, agent.tenant_id, "sarvam")
        sarvam_api_key = str(
            (sarvam_config or {}).get("api_key") or settings.sarvam_api_key
        ).strip()
        if not sarvam_api_key:
            return None
        if agent.voice_provider == "elevenlabs":
            elevenlabs_config = await load_provider_config(db, agent.tenant_id, "elevenlabs")
            tts_api_key = str(
                (elevenlabs_config or {}).get("api_key") or settings.elevenlabs_api_key
            ).strip()
        else:
            tts_api_key = sarvam_api_key
        if not tts_api_key:
            return None
        openai_config = await load_provider_config(db, agent.tenant_id, "openai")
        openai_api_key = str(
            (openai_config or {}).get("api_key") or settings.openai_api_key
        ).strip()
        if not openai_api_key:
            return None
        voice_prefix = f"{agent.voice_provider}:"
        if not agent.voice_id.startswith(voice_prefix):
            return None
        speaker = agent.voice_id.removeprefix(voice_prefix)
        if not speaker:
            return None
        primary_language = sarvam_language_code(agent.language)
        stt_language = sarvam_stt_wire_language(model=agent, profile=profile)
        return RuntimeSessionConfig(
            call_id=call.id,
            tenant_id=call.tenant_id,
            agent_id=agent.id,
            system_prompt=f"{agent.system_prompt.rstrip()}\n\n{VOICE_CALL_GUIDANCE}",
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
            speech_provider=agent.voice_provider,
            sarvam_api_key=sarvam_api_key,
            tts_api_key=tts_api_key,
            openai_api_key=openai_api_key,
            knowledge_serving_revision_id=serving_revision_id,
            knowledge_serving_knowledge_base_id=knowledge_base_id,
            knowledge_serving_revocation_generation=revocation_generation,
            knowledge_terminology=knowledge_terminology,
            media_session_claim_id=stored_claim_id,
            media_stream_sid=stored_stream_sid,
        )


async def _retrieve_session_knowledge(
    config: RuntimeSessionConfig,
    query: str,
) -> str | None:
    """Retrieve from exactly the release admitted for this media session."""

    async with async_session_factory() as db:
        return await retrieve_knowledge_context(
            db,
            tenant_id=config.tenant_id,
            agent_id=config.agent_id,
            query=query,
            terminology=config.knowledge_terminology,
            serving_revision_id=config.knowledge_serving_revision_id,
            knowledge_base_id=config.knowledge_serving_knowledge_base_id,
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


def _media_claim_matches(config: RuntimeSessionConfig, runtime: dict) -> bool:
    """Keep stale or replayed sessions from writing another owner's state."""

    if config.media_session_claim_id is None:
        return True
    claim = runtime.get("media_session_claim")
    return bool(
        isinstance(claim, dict)
        and claim.get("id") == str(config.media_session_claim_id)
        and claim.get("stream_sid") == config.media_stream_sid
    )


async def _store_metrics(config: RuntimeSessionConfig, metrics: dict) -> None:
    async with async_session_factory() as db:
        call = await db.scalar(
            select(Call)
            .where(Call.id == config.call_id, Call.tenant_id == config.tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if call is None:
            return
        metadata = dict(call.call_metadata or {})
        existing_runtime = metadata.get("runtime")
        preserved_runtime = existing_runtime if isinstance(existing_runtime, dict) else {}
        if not _media_claim_matches(config, preserved_runtime):
            return
        metadata["runtime"] = {
            **preserved_runtime,
            "speech_provider": config.speech_provider,
            "llm_provider": "openai",
            "llm_model": config.llm_model,
            "stt_language": config.stt_language,
            "turn_count": metrics.get("turn_count", 0),
            "llm_tokens": metrics.get("llm_tokens", 0),
            "llm_input_tokens": metrics.get("llm_input_tokens", 0),
            "llm_output_tokens": metrics.get("llm_output_tokens", 0),
            "tts_characters": metrics.get("tts_characters", 0),
            "inbound_audio_bytes": metrics.get("inbound_audio_bytes", 0),
            "outbound_audio_bytes": metrics.get("outbound_audio_bytes", 0),
            "barge_in_count": metrics.get("barge_in_count", 0),
            "tts_failure_count": metrics.get("tts_failure_count", 0),
            "tts_fallback_count": metrics.get("tts_fallback_count", 0),
            "last_llm_latency_ms": metrics.get("last_llm_latency_ms"),
            "last_llm_first_token_ms": metrics.get("last_llm_first_token_ms"),
            "last_tts_first_byte_ms": metrics.get("last_tts_first_byte_ms"),
            "last_transcript_to_first_audio_ms": metrics.get("last_transcript_to_first_audio_ms"),
            "last_speech_end_to_first_audio_ms": metrics.get("last_speech_end_to_first_audio_ms"),
            "turn_latency_p50_ms": metrics.get("turn_latency_p50_ms"),
            "turn_latency_p95_ms": metrics.get("turn_latency_p95_ms"),
            "knowledge_terminology_count": len(config.knowledge_terminology),
            "media_stream_started": True,
            "cost_state": "pending_provider_billing_sync",
        }
        call.call_metadata = metadata
        await db.commit()


async def fail_inbound_runtime_start(
    call_id: UUID,
    *,
    reason: str,
    media_session_claim_id: UUID | None = None,
) -> bool:
    """Terminalize an authenticated native inbound reservation that cannot start.

    The webhook has already committed the capacity reservation before Twilio
    connects its media stream. A configuration failure must therefore release
    that reservation durably. The row lock prevents this cleanup from
    overwriting a terminal provider callback, and the transport/direction
    checks ensure outbound calls remain callback/watchdog owned.
    """

    now = datetime.now(UTC)
    async with async_session_factory() as db:
        call = await db.scalar(
            select(Call)
            .where(
                Call.id == call_id,
                Call.provider == "twilio",
                Call.direction == "inbound",
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if call is None or call.status in TERMINAL_CALL_STATUSES:
            return False
        metadata = call.call_metadata if isinstance(call.call_metadata, dict) else {}
        runtime = metadata.get("runtime")
        if not isinstance(runtime, dict) or runtime.get("transport") != "twilio_media_streams":
            return False
        if media_session_claim_id is not None:
            claim = runtime.get("media_session_claim")
            if not isinstance(claim, dict) or claim.get("id") != str(media_session_claim_id):
                return False
        call.status = "failed"
        call.ended_at = now
        # An authenticated media stream proves Twilio answered the call. Keep
        # a conservative billable proxy until the provider callback supplies
        # its authoritative duration, without replacing a larger measurement.
        call.answered_at = call.answered_at or now
        answered_at = call.answered_at
        if answered_at.tzinfo is None:
            answered_at = answered_at.replace(tzinfo=UTC)
        call.duration_seconds = max(
            call.duration_seconds or 0,
            round((now - answered_at).total_seconds()),
            1,
        )
        call.call_metadata = {
            **metadata,
            "lifecycle_error": reason,
            "runtime": {
                **runtime,
                "runtime_start_failure": reason,
                "runtime_start_failed_at": now.isoformat(),
                "media_stream_started": True,
                "cost_state": "pending_provider_billing_sync",
                "duration_source": runtime.get("duration_source")
                or "minimum_answered_runtime_start_failure",
            },
        }
        await db.commit()
        return True


async def _finalize_inbound_call(config: RuntimeSessionConfig) -> None:
    async with async_session_factory() as db:
        call = await db.scalar(
            select(Call)
            .where(Call.id == config.call_id, Call.tenant_id == config.tenant_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if call is None or call.direction != "inbound" or call.status in TERMINAL_CALL_STATUSES:
            return
        metadata = call.call_metadata if isinstance(call.call_metadata, dict) else {}
        runtime = metadata.get("runtime")
        if not _media_claim_matches(
            config,
            runtime if isinstance(runtime, dict) else {},
        ):
            return
        now = datetime.now(UTC)
        call.status = "completed"
        call.ended_at = now
        if call.answered_at:
            answered_at = call.answered_at
            if answered_at.tzinfo is None:
                answered_at = answered_at.replace(tzinfo=UTC)
            call.duration_seconds = max(round((now - answered_at).total_seconds()), 0)
        await db.commit()


async def run_twilio_media_session(
    websocket: WebSocket,
    config: RuntimeSessionConfig,
    *,
    initial_messages: list[str] | None = None,
) -> None:
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
    conversation_engine = ConversationEngine(api_key=config.openai_api_key)
    if config.speech_provider == "elevenlabs":
        tts = ElevenLabsTTSStream(
            api_key=config.tts_api_key,
            base_url=settings.elevenlabs_base_url,
            voice_id=config.speaker,
            speed=config.speech_rate,
        )
    else:
        tts = SarvamTTSStream(
            api_key=config.tts_api_key,
            base_url=settings.sarvam_base_url,
            speaker=config.speaker,
            pace=config.speech_rate,
        )
    emergency_tts = (
        SarvamTTSStream(
            api_key=config.sarvam_api_key,
            base_url=settings.sarvam_base_url,
            speaker="ishita",
            pace=max(0.5, min(config.speech_rate, 2.0)),
        )
        if config.speech_provider == "elevenlabs"
        else None
    )
    metrics: dict[str, int | float | None] = {
        "turn_count": 0,
        "llm_tokens": 0,
        "llm_input_tokens": 0,
        "llm_output_tokens": 0,
        "tts_characters": 0,
        "inbound_audio_bytes": 0,
        "outbound_audio_bytes": 0,
        "barge_in_count": 0,
        "tts_failure_count": 0,
        "tts_fallback_count": 0,
        "last_llm_latency_ms": None,
        "last_llm_first_token_ms": None,
        "last_tts_first_byte_ms": None,
        "last_transcript_to_first_audio_ms": None,
        "last_speech_end_to_first_audio_ms": None,
        "turn_latency_p50_ms": None,
        "turn_latency_p95_ms": None,
    }
    pending_twilio_messages = deque(initial_messages or [])
    fatal_error: BaseException | None = None

    def record_fatal_error(error: BaseException, *, worker: str) -> None:
        nonlocal fatal_error
        if fatal_error is None:
            fatal_error = error
            logger.error(
                "realtime_media_worker_failed",
                call_id=str(config.call_id),
                worker=worker,
                error_type=type(error).__name__,
            )
        stop_event.set()

    async def supervise_worker(worker: str, operation: Awaitable[None]) -> None:
        """Turn an unexpected media-worker exit into a call-level failure."""

        try:
            await operation
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            record_fatal_error(exc, worker=worker)
        else:
            if not stop_event.is_set():
                record_fatal_error(
                    RuntimeError(f"Realtime {worker} stopped unexpectedly"),
                    worker=worker,
                )

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

        async def metered_fragments() -> AsyncIterator[str]:
            async for fragment in fragments:
                metrics["tts_characters"] = int(metrics["tts_characters"] or 0) + len(fragment)
                yield fragment

        def record_tts_failure() -> None:
            metrics["tts_failure_count"] = int(metrics["tts_failure_count"] or 0) + 1

        def record_tts_fallback() -> None:
            metrics["tts_fallback_count"] = int(metrics["tts_fallback_count"] or 0) + 1

        audio_stream = (
            audio_with_fallback(
                tts,
                emergency_tts,
                metered_fragments(),
                language_code=language_code,
                on_primary_failure=record_tts_failure,
                on_fallback=record_tts_fallback,
            )
            if emergency_tts is not None
            else tts.audio_for(metered_fragments(), language_code=language_code)
        )
        async for audio in audio_stream:
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

    async def play_text(
        text: str,
        language_code: str,
    ) -> None:
        input_at = time.perf_counter()

        async def fragments() -> AsyncIterator[str]:
            yield text

        await play_fragments(
            fragments(),
            language_code,
            first_input_at=lambda: input_at,
        )

    async def play_greeting() -> None:
        try:
            await play_text(config.greeting_message, config.language_code)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            record_fatal_error(exc, worker="greeting_playback")
            raise

    async def handle_turn(
        text: str,
        detected_language: str | None,
        transcript_at: float,
        speech_end_at: float | None,
    ) -> None:
        await _append_turn(config, "user", text)
        history.append({"role": "user", "content": text})
        knowledge = await _retrieve_session_knowledge(config, text)
        llm_started = time.perf_counter()
        first_tts_input_at: float | None = None
        response_parts: list[str] = []
        tokens = 0
        input_tokens = 0
        output_tokens = 0
        persisted = False
        audio_started = False
        language = (
            detected_language
            if detected_language and detected_language != "auto"
            else config.language_code
        )

        async def response_fragments() -> AsyncIterator[str]:
            nonlocal first_tts_input_at, tokens, input_tokens, output_tokens
            buffer = ""
            first_token = True
            async for event in conversation_engine.stream_response(
                config.system_prompt,
                history[-12:],
                model=config.llm_model,
                temperature=config.temperature,
                max_tokens=voice_response_token_limit(config.max_tokens),
                knowledge_context=knowledge,
            ):
                if event.is_final:
                    tokens = event.tokens_used
                    input_tokens = event.input_tokens
                    output_tokens = event.output_tokens
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
        metrics["llm_input_tokens"] = int(metrics["llm_input_tokens"] or 0) + int(input_tokens)
        metrics["llm_output_tokens"] = int(metrics["llm_output_tokens"] or 0) + int(output_tokens)
        metrics["turn_count"] = int(metrics["turn_count"] or 0) + 1
        await persist_response()
        await _store_metrics(config, metrics)

    async def receive_twilio(stt: SarvamSTTStream) -> None:
        nonlocal stream_sid, current_response
        try:
            while not stop_event.is_set():
                message = (
                    pending_twilio_messages.popleft()
                    if pending_twilio_messages
                    else await websocket.receive_text()
                )
                payload = json.loads(message)
                event = payload.get("event")
                if event == "start":
                    start = payload.get("start") or {}
                    stream_sid = (
                        str(start.get("streamSid") or payload.get("streamSid") or "") or None
                    )
                    if stream_sid and current_response is None:
                        current_response = asyncio.create_task(play_greeting())
                elif event == "media":
                    audio = (payload.get("media") or {}).get("payload")
                    if isinstance(audio, str) and audio:
                        metrics["inbound_audio_bytes"] = int(
                            metrics["inbound_audio_bytes"] or 0
                        ) + (len(audio) * 3 // 4)
                        await stt.send_audio(audio)
                elif event == "stop":
                    stop_event.set()
        except WebSocketDisconnect:
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
            except Exception as exc:
                logger.exception("realtime_turn_failed", call_id=str(config.call_id))
                record_fatal_error(exc, worker="response_worker")
                return

    @asynccontextmanager
    async def finalize_session():
        """Terminalize even when a provider context fails while it is entering.

        The finalizer is deliberately the first context in the stack. Python
        then invokes it when a later Sarvam or TTS ``__aenter__`` raises, which
        prevents answered inbound calls from remaining ``in_progress`` after
        a provider handshake failure.
        """
        session_failed = False
        try:
            yield
        except BaseException:
            session_failed = True
            raise
        finally:
            try:
                await _store_metrics(config, metrics)
            finally:
                if session_failed:
                    await fail_inbound_runtime_start(
                        config.call_id,
                        reason="runtime_provider_session_failed",
                        media_session_claim_id=config.media_session_claim_id,
                    )
                else:
                    await _finalize_inbound_call(config)

    async with (
        finalize_session(),
        SarvamSTTStream(
            api_key=config.sarvam_api_key,
            base_url=settings.sarvam_base_url,
            language_code=config.stt_language,
        ) as stt,
        tts,
        emergency_tts if emergency_tts is not None else _NullAsyncContext(),
    ):
        tasks = [
            asyncio.create_task(supervise_worker("twilio_receiver", receive_twilio(stt))),
            asyncio.create_task(supervise_worker("speech_receiver", receive_sarvam(stt))),
            asyncio.create_task(supervise_worker("response_worker", response_worker())),
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
        if fatal_error is not None:
            raise fatal_error
