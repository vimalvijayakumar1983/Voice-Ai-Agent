"""Privacy-safe, manifest-driven audio replay canaries for QA voice agents.

The module deliberately cannot dial a telephone number.  It can either ask VAV
for a governed browser session or join an already-created, allowlisted SIP test
room.  Callers must opt in to live execution in the CLI; this module additionally
enforces test-agent and test-fixture allowlists before any network operation.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
import time
import unicodedata
import wave
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse
from uuid import UUID, uuid4

import httpx
from livekit import rtc
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

TERMINAL_CALL_STATUSES = {
    "completed",
    "failed",
    "busy",
    "no-answer",
    "canceled",
    "terminal_unknown",
}
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{2,127}$")
_SAFE_CASE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{2,127}$")
_SAFE_FIXTURE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,127}$")


class AudioReplayCanaryError(RuntimeError):
    """Controlled canary error whose text is safe to print without secrets."""


class ReplayManifestError(AudioReplayCanaryError):
    """The replay manifest or its environment references are invalid."""


class ReplayExecutionError(AudioReplayCanaryError):
    """The live replay could not be completed or observed."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReplayTargetSpec(_StrictModel):
    mode: Literal["browser_session", "sip_fixture"]
    api_base_url_env: str
    api_token_env: str
    agent_id_env: str
    livekit_url_env: str | None = None
    livekit_access_token_env: str | None = None
    room_name_env: str | None = None
    call_id_env: str | None = None
    sip_fixture_id_env: str | None = None
    variables: dict[str, str | int | float | bool] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_mode_requirements(self):
        env_references = {
            key: value
            for key, value in self.model_dump().items()
            if key.endswith("_env") and value is not None
        }
        for key, value in env_references.items():
            if not _ENV_NAME.fullmatch(value):
                raise ValueError(f"{key} must name an uppercase environment variable")
        sip_fields = (
            self.livekit_url_env,
            self.livekit_access_token_env,
            self.room_name_env,
            self.call_id_env,
            self.sip_fixture_id_env,
        )
        if self.mode == "sip_fixture" and not all(sip_fields):
            raise ValueError("SIP replay requires explicit room, token, call and fixture env refs")
        if self.mode == "browser_session" and any(sip_fields):
            raise ValueError("Browser replay obtains room credentials from the governed VAV API")
        return self


class ReplayAudioSpec(_StrictModel):
    path_env: str
    format: Literal["wav", "pcm_s16le"] = "wav"
    sample_rate_hz: int | None = Field(None, ge=8_000, le=48_000)
    channels: Literal[1, 2] | None = None
    frame_ms: Literal[10, 20, 40] = 20

    @field_validator("path_env")
    @classmethod
    def validate_path_env(cls, value: str) -> str:
        if not _ENV_NAME.fullmatch(value):
            raise ValueError("path_env must name an uppercase environment variable")
        return value

    @model_validator(mode="after")
    def validate_raw_audio_metadata(self):
        if self.format == "pcm_s16le" and (self.sample_rate_hz is None or self.channels is None):
            raise ValueError("Raw PCM replay requires sample_rate_hz and channels")
        return self


class ExpectedUtterance(_StrictModel):
    reference_text: str = Field(min_length=1, max_length=1_000)
    max_word_error_rate: float = Field(0.15, ge=0, le=1)
    required_phrases: tuple[str, ...] = ()
    required_entities: tuple[str, ...] = ()

    @field_validator("required_phrases", "required_entities")
    @classmethod
    def validate_nonempty_terms(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(value.strip() for value in values if value.strip())
        if len(cleaned) != len(values):
            raise ValueError("Expected transcript terms cannot be blank")
        if any(len(value) > 200 for value in cleaned):
            raise ValueError("Expected transcript terms must be 200 characters or fewer")
        return cleaned


class ExpectedTranscript(_StrictModel):
    utterances: tuple[ExpectedUtterance, ...] = Field(min_length=1, max_length=20)
    max_extra_user_turns: int = Field(0, ge=0, le=20)


class ExpectedLatency(_StrictModel):
    max_participant_active_to_first_server_speaking_ms: int | None = Field(None, gt=0, le=60_000)
    max_call_open_to_greeting_ms: int | None = Field(None, gt=0, le=60_000)
    max_turn_latency_p50_ms: int = Field(gt=0, le=60_000)
    max_turn_latency_p90_ms: int | None = Field(None, gt=0, le=60_000)
    max_turn_latency_p95_ms: int = Field(gt=0, le=60_000)
    max_last_speech_end_to_first_audio_ms: int | None = Field(None, gt=0, le=60_000)


class ExpectedGrounding(_StrictModel):
    require_turn_diagnostics: bool = True
    min_responses_after_verified_retrieval: int = Field(0, ge=0, le=50)
    max_unverified_answers: int = Field(0, ge=0, le=50)
    max_knowledge_errors: int = Field(0, ge=0, le=50)
    max_unexpected_script_turns: int = Field(0, ge=0, le=50)
    max_transcription_clarifications: int = Field(0, ge=0, le=50)


class ExpectedInteraction(_StrictModel):
    min_barge_in_count: int | None = Field(None, ge=0, le=50)
    max_barge_in_count: int | None = Field(None, ge=0, le=50)
    min_suppressed_fragment_count: int | None = Field(None, ge=0, le=50)
    max_suppressed_fragment_count: int | None = Field(None, ge=0, le=50)
    max_last_interruption_detection_ms: int | None = Field(None, gt=0, le=60_000)

    @model_validator(mode="after")
    def validate_ranges(self):
        if (
            self.min_barge_in_count is not None
            and self.max_barge_in_count is not None
            and self.min_barge_in_count > self.max_barge_in_count
        ):
            raise ValueError("Minimum barge-in count cannot exceed its maximum")
        if (
            self.min_suppressed_fragment_count is not None
            and self.max_suppressed_fragment_count is not None
            and self.min_suppressed_fragment_count > self.max_suppressed_fragment_count
        ):
            raise ValueError("Minimum suppressed-fragment count cannot exceed its maximum")
        return self


class ReplayExpectations(_StrictModel):
    terminal_status: Literal["completed"] = "completed"
    transcript: ExpectedTranscript
    latency: ExpectedLatency
    grounding: ExpectedGrounding = Field(default_factory=ExpectedGrounding)
    interaction: ExpectedInteraction = Field(default_factory=ExpectedInteraction)


class ReplayTiming(_StrictModel):
    wait_for_remote_seconds: float = Field(30, ge=1, le=120)
    pre_roll_seconds: float = Field(1.5, ge=0, le=30)
    post_roll_seconds: float = Field(8, ge=0, le=60)
    finalization_timeout_seconds: float = Field(60, ge=5, le=300)
    poll_interval_seconds: float = Field(1, ge=0.1, le=10)


class ReplaySafety(_StrictModel):
    test_only: Literal[True]
    allowlisted_agent_ids_env: str
    allowlisted_sip_fixture_ids_env: str | None = None
    required_agent_name_markers: tuple[str, ...] = ("qa", "test", "canary")
    max_fixture_duration_seconds: float = Field(120, gt=0, le=300)
    max_fixture_bytes: int = Field(20_000_000, ge=1_024, le=50_000_000)

    @model_validator(mode="after")
    def validate_safety_references(self):
        for field_name in ("allowlisted_agent_ids_env", "allowlisted_sip_fixture_ids_env"):
            value = getattr(self, field_name)
            if value is not None and not _ENV_NAME.fullmatch(value):
                raise ValueError(f"{field_name} must name an uppercase environment variable")
        markers = tuple(marker.strip().casefold() for marker in self.required_agent_name_markers)
        if not markers or any(not re.fullmatch(r"[a-z0-9]{2,32}", marker) for marker in markers):
            raise ValueError("At least one short QA agent-name marker is required")
        object.__setattr__(self, "required_agent_name_markers", markers)
        return self


class AudioReplayManifest(_StrictModel):
    schema_version: Literal["vav-audio-replay-canary-v1"]
    case_id: str
    description: str = Field(min_length=1, max_length=500)
    target: ReplayTargetSpec
    audio: ReplayAudioSpec
    expectations: ReplayExpectations
    timing: ReplayTiming = Field(default_factory=ReplayTiming)
    safety: ReplaySafety

    @field_validator("case_id")
    @classmethod
    def validate_case_id(cls, value: str) -> str:
        if not _SAFE_CASE_ID.fullmatch(value):
            raise ValueError("case_id must be a safe lowercase identifier")
        return value

    @model_validator(mode="after")
    def validate_sip_safety(self):
        if self.target.mode == "sip_fixture" and not self.safety.allowlisted_sip_fixture_ids_env:
            raise ValueError("SIP replay requires an environment-supplied fixture allowlist")
        return self


@dataclass(frozen=True)
class ResolvedReplayTarget:
    mode: Literal["browser_session", "sip_fixture"]
    api_base_url: str
    api_token: str = field(repr=False)
    agent_id: UUID
    livekit_url: str | None = None
    livekit_access_token: str | None = field(default=None, repr=False)
    room_name: str | None = None
    call_id: UUID | None = None
    sip_fixture_id: str | None = None


@dataclass(frozen=True)
class AudioFixture:
    pcm_s16le: bytes = field(repr=False)
    sample_rate_hz: int
    channels: int
    frame_ms: int
    duration_seconds: float
    sha256: str


@dataclass(frozen=True)
class LiveKitPublishReceipt:
    room_name: str
    frame_count: int
    published_audio_seconds: float
    final_room_transcript_segments: int


@dataclass(frozen=True)
class CanaryCheck:
    name: str
    passed: bool
    observed: int | float | str | bool | None
    expected: int | float | str | bool | None

    def public_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "observed": self.observed,
            "expected": self.expected,
        }


@dataclass(frozen=True)
class AudioReplayReport:
    schema_version: str
    case_id: str
    passed: bool
    call_id: str
    fixture_sha256: str
    fixture_duration_seconds: float
    user_transcript_sha256: str
    user_turn_count: int
    user_word_count: int
    runtime_metrics: dict[str, int | float | str | bool | None]
    grounding_counts: dict[str, int]
    turn_diagnostics: tuple[dict[str, Any], ...]
    checks: tuple[CanaryCheck, ...]

    def public_dict(self) -> dict[str, Any]:
        """Return a report with no transcript, audio path, token, or phone number."""
        return {
            "schema_version": self.schema_version,
            "case_id": self.case_id,
            "passed": self.passed,
            "call_id": self.call_id,
            "fixture_sha256": self.fixture_sha256,
            "fixture_duration_seconds": self.fixture_duration_seconds,
            "user_transcript_sha256": self.user_transcript_sha256,
            "user_turn_count": self.user_turn_count,
            "user_word_count": self.user_word_count,
            "runtime_metrics": self.runtime_metrics,
            "grounding_counts": self.grounding_counts,
            "turn_diagnostics": list(self.turn_diagnostics),
            "checks": [check.public_dict() for check in self.checks],
        }


def load_audio_replay_manifest(path: Path) -> AudioReplayManifest:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ReplayManifestError("Replay manifest must be a readable UTF-8 JSON file") from exc
    try:
        return AudioReplayManifest.model_validate(payload)
    except Exception as exc:
        raise ReplayManifestError("Replay manifest failed strict schema validation") from exc


def manifest_environment_names(manifest: AudioReplayManifest) -> tuple[str, ...]:
    names = {
        manifest.target.api_base_url_env,
        manifest.target.api_token_env,
        manifest.target.agent_id_env,
        manifest.audio.path_env,
        manifest.safety.allowlisted_agent_ids_env,
    }
    for value in (
        manifest.target.livekit_url_env,
        manifest.target.livekit_access_token_env,
        manifest.target.room_name_env,
        manifest.target.call_id_env,
        manifest.target.sip_fixture_id_env,
        manifest.safety.allowlisted_sip_fixture_ids_env,
    ):
        if value:
            names.add(value)
    return tuple(sorted(names))


def _required_environment_value(environ: Mapping[str, str], name: str | None) -> str:
    if not name:
        raise ReplayManifestError("A required environment reference is missing")
    value = str(environ.get(name) or "").strip()
    if not value:
        raise ReplayManifestError(f"Required environment variable {name} is unavailable")
    return value


def _uuid_environment_value(environ: Mapping[str, str], name: str | None, label: str) -> UUID:
    raw = _required_environment_value(environ, name)
    try:
        return UUID(raw)
    except ValueError as exc:
        raise ReplayManifestError(f"{label} environment value must be a UUID") from exc


def _split_allowlist(raw: str) -> set[str]:
    return {value.casefold() for value in re.split(r"[\s,]+", raw) if value.strip()}


def _validated_api_base_url(raw: str) -> str:
    parsed = urlparse(raw)
    local_host = (parsed.hostname or "").casefold() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme not in ({"http", "https"} if local_host else {"https"}):
        raise ReplayManifestError("VAV API URL must use HTTPS except on loopback")
    if not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ReplayManifestError("VAV API URL is invalid")
    base = raw.rstrip("/")
    if not base.endswith("/api/v1"):
        base = f"{base}/api/v1"
    return base


def _validated_livekit_url(raw: str) -> str:
    parsed = urlparse(raw)
    local_host = (parsed.hostname or "").casefold() in {"localhost", "127.0.0.1", "::1"}
    if parsed.scheme not in ({"ws", "wss"} if local_host else {"wss"}) or not parsed.netloc:
        raise ReplayManifestError("LiveKit URL must use WSS except on loopback")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ReplayManifestError("LiveKit URL is invalid")
    return raw.rstrip("/")


def resolve_replay_target(
    manifest: AudioReplayManifest,
    *,
    environ: Mapping[str, str] | None = None,
) -> ResolvedReplayTarget:
    """Resolve credentials only after the CLI's explicit live-confirmation gate."""
    values = os.environ if environ is None else environ
    agent_id = _uuid_environment_value(values, manifest.target.agent_id_env, "Agent ID")
    allowed_agents = _split_allowlist(
        _required_environment_value(values, manifest.safety.allowlisted_agent_ids_env)
    )
    if str(agent_id).casefold() not in allowed_agents:
        raise ReplayManifestError("Target agent is not in the explicit QA replay allowlist")

    common = {
        "mode": manifest.target.mode,
        "api_base_url": _validated_api_base_url(
            _required_environment_value(values, manifest.target.api_base_url_env)
        ),
        "api_token": _required_environment_value(values, manifest.target.api_token_env),
        "agent_id": agent_id,
    }
    if manifest.target.mode == "browser_session":
        return ResolvedReplayTarget(**common)

    fixture_id = _required_environment_value(values, manifest.target.sip_fixture_id_env)
    if not _SAFE_FIXTURE_ID.fullmatch(fixture_id):
        raise ReplayManifestError("SIP fixture ID is invalid")
    allowed_fixtures = _split_allowlist(
        _required_environment_value(
            values,
            manifest.safety.allowlisted_sip_fixture_ids_env,
        )
    )
    if fixture_id.casefold() not in allowed_fixtures:
        raise ReplayManifestError("SIP fixture is not in the explicit QA replay allowlist")
    return ResolvedReplayTarget(
        **common,
        livekit_url=_validated_livekit_url(
            _required_environment_value(values, manifest.target.livekit_url_env)
        ),
        livekit_access_token=_required_environment_value(
            values, manifest.target.livekit_access_token_env
        ),
        room_name=_required_environment_value(values, manifest.target.room_name_env),
        call_id=_uuid_environment_value(values, manifest.target.call_id_env, "Call ID"),
        sip_fixture_id=fixture_id,
    )


def load_audio_fixture(
    manifest: AudioReplayManifest,
    *,
    environ: Mapping[str, str] | None = None,
) -> AudioFixture:
    values = os.environ if environ is None else environ
    audio_path = Path(_required_environment_value(values, manifest.audio.path_env)).expanduser()
    try:
        resolved_path = audio_path.resolve(strict=True)
        size = resolved_path.stat().st_size
    except OSError as exc:
        raise ReplayManifestError("Audio fixture must be an existing local file") from exc
    if not resolved_path.is_file() or size > manifest.safety.max_fixture_bytes:
        raise ReplayManifestError("Audio fixture is not a bounded regular file")

    if manifest.audio.format == "wav":
        try:
            with wave.open(str(resolved_path), "rb") as wav_file:
                if wav_file.getcomptype() != "NONE" or wav_file.getsampwidth() != 2:
                    raise ReplayManifestError("WAV fixture must be uncompressed signed 16-bit PCM")
                sample_rate = wav_file.getframerate()
                channels = wav_file.getnchannels()
                pcm = wav_file.readframes(wav_file.getnframes())
        except (OSError, EOFError, wave.Error) as exc:
            raise ReplayManifestError("WAV fixture could not be decoded") from exc
        if not 8_000 <= sample_rate <= 48_000 or channels not in {1, 2}:
            raise ReplayManifestError("WAV fixture must be 8-48 kHz mono or stereo")
    else:
        sample_rate = int(manifest.audio.sample_rate_hz or 0)
        channels = int(manifest.audio.channels or 0)
        try:
            pcm = resolved_path.read_bytes()
        except OSError as exc:
            raise ReplayManifestError("PCM fixture could not be read") from exc

    sample_width = 2
    frame_width = sample_width * channels
    if not pcm or len(pcm) % frame_width:
        raise ReplayManifestError("Audio fixture contains an incomplete PCM sample")
    duration = len(pcm) / (sample_rate * frame_width)
    if duration > manifest.safety.max_fixture_duration_seconds:
        raise ReplayManifestError("Audio fixture exceeds the live-replay duration limit")
    return AudioFixture(
        pcm_s16le=pcm,
        sample_rate_hz=sample_rate,
        channels=channels,
        frame_ms=manifest.audio.frame_ms,
        duration_seconds=duration,
        sha256=hashlib.sha256(pcm).hexdigest(),
    )


class VAVReplayAPI:
    """Minimal API client that never includes provider response bodies in errors."""

    def __init__(self, *, base_url: str, token: str, timeout_seconds: float = 20) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
            follow_redirects=False,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _json(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        allow_not_found: bool = False,
    ) -> dict[str, Any] | None:
        try:
            response = await self._client.request(
                method,
                f"{self._base_url}/{path.lstrip('/')}",
                json=json_body,
                headers=headers,
            )
        except httpx.HTTPError as exc:
            raise ReplayExecutionError(f"VAV API request failed for {path}") from exc
        if allow_not_found and response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            raise ReplayExecutionError(f"VAV API returned HTTP {response.status_code} for {path}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ReplayExecutionError(f"VAV API returned invalid JSON for {path}") from exc
        if not isinstance(payload, dict):
            raise ReplayExecutionError(f"VAV API returned an invalid object for {path}")
        return payload

    async def get_agent(self, agent_id: UUID) -> dict[str, Any]:
        payload = await self._json("GET", f"agents/{agent_id}")
        assert payload is not None
        return payload

    async def issue_browser_session(
        self,
        *,
        agent_id: UUID,
        variables: Mapping[str, str | int | float | bool],
    ) -> dict[str, Any]:
        payload = await self._json(
            "POST",
            f"agents/{agent_id}/livekit/session",
            json_body={"variables": dict(variables)},
            headers={"Idempotency-Key": f"qa-replay-{uuid4().hex}"},
        )
        assert payload is not None
        return payload

    async def get_call(self, call_id: UUID) -> dict[str, Any]:
        payload = await self._json("GET", f"calls/{call_id}")
        assert payload is not None
        return payload

    async def get_transcript(self, call_id: UUID) -> dict[str, Any] | None:
        return await self._json(
            "GET",
            f"calls/{call_id}/transcript",
            allow_not_found=True,
        )


class LiveKitAudioPublisher:
    """Publish bounded PCM in real time as a microphone track."""

    async def publish(
        self,
        *,
        url: str,
        access_token: str,
        room_name: str,
        audio: AudioFixture,
        timing: ReplayTiming,
    ) -> LiveKitPublishReceipt:
        room = rtc.Room()
        remote_ready = asyncio.Event()
        final_segments = 0

        @room.on("participant_connected")
        def _participant_connected(participant: Any) -> None:
            if getattr(participant, "kind", None) == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT:
                remote_ready.set()

        @room.on("transcription_received")
        def _transcription_received(
            segments: Sequence[Any],
            _participant: Any,
            _publication: Any,
        ) -> None:
            nonlocal final_segments
            final_segments += sum(bool(getattr(segment, "final", False)) for segment in segments)

        source: rtc.AudioSource | None = None
        try:
            await room.connect(url, access_token)
            if str(room.name or "") != room_name:
                raise ReplayExecutionError("LiveKit token joined an unexpected room")
            if any(
                participant.kind == rtc.ParticipantKind.PARTICIPANT_KIND_AGENT
                for participant in room.remote_participants.values()
            ):
                remote_ready.set()
            try:
                await asyncio.wait_for(
                    remote_ready.wait(),
                    timeout=timing.wait_for_remote_seconds,
                )
            except TimeoutError as exc:
                raise ReplayExecutionError("LiveKit QA agent did not join the test room") from exc

            source = rtc.AudioSource(
                audio.sample_rate_hz,
                audio.channels,
                queue_size_ms=max(100, audio.frame_ms * 5),
            )
            track = rtc.LocalAudioTrack.create_audio_track("qa-replay-microphone", source)
            options = rtc.TrackPublishOptions()
            options.source = rtc.TrackSource.SOURCE_MICROPHONE
            await room.local_participant.publish_track(track, options)
            await asyncio.sleep(timing.pre_roll_seconds)

            samples_per_channel = audio.sample_rate_hz * audio.frame_ms // 1_000
            frame_bytes = samples_per_channel * audio.channels * 2
            frame_count = math.ceil(len(audio.pcm_s16le) / frame_bytes)
            started = time.monotonic()
            for index in range(frame_count):
                frame_data = audio.pcm_s16le[index * frame_bytes : (index + 1) * frame_bytes]
                if len(frame_data) < frame_bytes:
                    frame_data += b"\x00" * (frame_bytes - len(frame_data))
                await source.capture_frame(
                    rtc.AudioFrame(
                        data=frame_data,
                        sample_rate=audio.sample_rate_hz,
                        num_channels=audio.channels,
                        samples_per_channel=samples_per_channel,
                    )
                )
                target_elapsed = ((index + 1) * audio.frame_ms) / 1_000
                remaining = target_elapsed - (time.monotonic() - started)
                if remaining > 0:
                    await asyncio.sleep(remaining)
            await source.wait_for_playout()
            await asyncio.sleep(timing.post_roll_seconds)
            return LiveKitPublishReceipt(
                room_name=room_name,
                frame_count=frame_count,
                published_audio_seconds=audio.duration_seconds,
                final_room_transcript_segments=final_segments,
            )
        except ReplayExecutionError:
            raise
        except Exception as exc:
            raise ReplayExecutionError("LiveKit audio replay failed") from exc
        finally:
            if source is not None:
                try:
                    await source.aclose()
                except Exception:
                    pass
            try:
                await room.disconnect()
            except Exception:
                pass


def _normalize_transcript(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(
        "".join(character if character.isalnum() else " " for character in normalized).split()
    )


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Compute case/punctuation-insensitive word error rate."""
    reference_words = _normalize_transcript(reference).split()
    hypothesis_words = _normalize_transcript(hypothesis).split()
    if not reference_words:
        return 0.0 if not hypothesis_words else 1.0
    previous = list(range(len(hypothesis_words) + 1))
    for reference_index, reference_word in enumerate(reference_words, start=1):
        current = [reference_index]
        for hypothesis_index, hypothesis_word in enumerate(hypothesis_words, start=1):
            substitution = previous[hypothesis_index - 1] + (reference_word != hypothesis_word)
            current.append(
                min(
                    previous[hypothesis_index] + 1,
                    current[hypothesis_index - 1] + 1,
                    substitution,
                )
            )
        previous = current
    return previous[-1] / len(reference_words)


def _runtime_metadata(call: Mapping[str, Any]) -> dict[str, Any]:
    metadata = call.get("call_metadata")
    if not isinstance(metadata, Mapping):
        return {}
    runtime = metadata.get("runtime")
    return dict(runtime) if isinstance(runtime, Mapping) else {}


def _user_turns(transcript: Mapping[str, Any]) -> list[str]:
    turns = transcript.get("turns")
    if not isinstance(turns, Sequence) or isinstance(turns, (str, bytes)):
        return []
    result: list[str] = []
    for turn in turns:
        if not isinstance(turn, Mapping) or str(turn.get("role") or "").casefold() not in {
            "user",
            "caller",
        }:
            continue
        content = turn.get("content")
        if isinstance(content, str) and content.strip():
            result.append(content.strip())
    return result


def _safe_turn_diagnostics(runtime: Mapping[str, Any]) -> tuple[dict[str, Any], ...]:
    turns = runtime.get("turn_diagnostics")
    if not isinstance(turns, Sequence) or isinstance(turns, (str, bytes)):
        return ()
    numeric_fields = {
        "turn",
        "transcript_words",
        "transcript_after_speech_ms",
        "transcript_to_first_audio_ms",
        "speech_end_to_first_audio_ms",
        "llm_first_token_ms",
        "tts_first_byte_ms",
        "knowledge_tool_ms",
        "interruption_detection_ms",
    }
    boolean_fields = {
        "barge_in",
        "unexpected_script",
    }
    enum_fields = {
        "knowledge_result": {"verified", "no_match", "error"},
        "grounding_outcome": {
            "verified_answer",
            "response_after_verified_retrieval",
            "no_match_correctly_refused",
            "no_match_clarification",
            "no_match_unverified_response",
            "knowledge_error_response",
        },
        "response_action": {
            "answered_from_verified_evidence",
            "responded_after_verified_retrieval",
            "refused_despite_verified_evidence",
            "asked_clarification_despite_verified_evidence",
            "refused_unverified",
            "asked_clarification",
            "answered_without_verified_evidence",
            "knowledge_error_response",
            "asked_transcription_clarification",
        },
        "outcome": {"answered", "fragment_suppressed", "superseded_by_caller"},
    }
    result: list[dict[str, Any]] = []
    for raw in turns:
        if not isinstance(raw, Mapping):
            continue
        safe: dict[str, Any] = {}
        for key in numeric_fields:
            value = raw.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                safe[key] = value
        for key in boolean_fields:
            value = raw.get(key)
            if isinstance(value, bool):
                safe[key] = value
        for key, allowed_values in enum_fields.items():
            value = raw.get(key)
            if isinstance(value, str) and value in allowed_values:
                safe[key] = value
        if safe:
            result.append(safe)
    return tuple(result)


def evaluate_audio_replay(
    manifest: AudioReplayManifest,
    *,
    call: Mapping[str, Any],
    transcript: Mapping[str, Any],
    fixture: AudioFixture,
) -> AudioReplayReport:
    """Evaluate a completed call without exposing transcript content in the report."""
    checks: list[CanaryCheck] = []

    def check(
        name: str,
        passed: bool,
        observed: int | float | str | bool | None,
        expected: int | float | str | bool | None,
    ) -> None:
        checks.append(CanaryCheck(name, passed, observed, expected))

    status = str(call.get("status") or "")
    check(
        "call.terminal_status",
        status == manifest.expectations.terminal_status,
        status,
        manifest.expectations.terminal_status,
    )
    turns = _user_turns(transcript)
    expected_turns = manifest.expectations.transcript.utterances
    min_turns = len(expected_turns)
    max_turns = min_turns + manifest.expectations.transcript.max_extra_user_turns
    check("transcript.minimum_user_turns", len(turns) >= min_turns, len(turns), min_turns)
    check("transcript.maximum_user_turns", len(turns) <= max_turns, len(turns), max_turns)
    for index, expected in enumerate(expected_turns):
        if index >= len(turns):
            check(
                f"transcript.turn[{index}].word_error_rate",
                False,
                None,
                expected.max_word_error_rate,
            )
            for phrase_index, _phrase in enumerate(expected.required_phrases):
                check(
                    f"transcript.turn[{index}].required_phrase[{phrase_index}]", False, False, True
                )
            for entity_index, _entity in enumerate(expected.required_entities):
                check(
                    f"transcript.turn[{index}].required_entity[{entity_index}]", False, False, True
                )
            continue
        normalized_actual = _normalize_transcript(turns[index])
        error_rate = word_error_rate(expected.reference_text, turns[index])
        check(
            f"transcript.turn[{index}].word_error_rate",
            error_rate <= expected.max_word_error_rate,
            round(error_rate, 4),
            expected.max_word_error_rate,
        )
        for phrase_index, phrase in enumerate(expected.required_phrases):
            found = _normalize_transcript(phrase) in normalized_actual
            check(f"transcript.turn[{index}].required_phrase[{phrase_index}]", found, found, True)
        for entity_index, entity in enumerate(expected.required_entities):
            found = _normalize_transcript(entity) in normalized_actual
            check(f"transcript.turn[{index}].required_entity[{entity_index}]", found, found, True)

    runtime = _runtime_metadata(call)
    latency_fields = {
        "participant_active_to_first_server_speaking_ms": (
            manifest.expectations.latency.max_participant_active_to_first_server_speaking_ms
        ),
        "call_open_to_greeting_ms": manifest.expectations.latency.max_call_open_to_greeting_ms,
        "turn_latency_p50_ms": manifest.expectations.latency.max_turn_latency_p50_ms,
        "turn_latency_p90_ms": manifest.expectations.latency.max_turn_latency_p90_ms,
        "turn_latency_p95_ms": manifest.expectations.latency.max_turn_latency_p95_ms,
        "last_speech_end_to_first_audio_ms": (
            manifest.expectations.latency.max_last_speech_end_to_first_audio_ms
        ),
    }
    report_metrics: dict[str, int | float | str | bool | None] = {}
    for field_name, maximum in latency_fields.items():
        if maximum is None:
            continue
        observed = runtime.get(field_name)
        numeric = (
            float(observed)
            if isinstance(observed, (int, float)) and not isinstance(observed, bool)
            else None
        )
        report_metrics[field_name] = observed if numeric is not None else None
        check(
            f"latency.{field_name}",
            numeric is not None and numeric <= maximum,
            numeric,
            maximum,
        )

    diagnostics = _safe_turn_diagnostics(runtime)
    grounding_counts = Counter(
        str(turn["grounding_outcome"])
        for turn in diagnostics
        if isinstance(turn.get("grounding_outcome"), str)
    )
    clarification_count = sum(
        turn.get("response_action") == "asked_transcription_clarification" for turn in diagnostics
    )
    unexpected_script_count = sum(bool(turn.get("unexpected_script")) for turn in diagnostics)
    grounding = manifest.expectations.grounding
    if grounding.require_turn_diagnostics:
        check("grounding.turn_diagnostics_present", bool(diagnostics), bool(diagnostics), True)
    responses_after_verified_retrieval = (
        grounding_counts["response_after_verified_retrieval"] + grounding_counts["verified_answer"]
    )
    unverified = grounding_counts["no_match_unverified_response"]
    errors = grounding_counts["knowledge_error_response"]
    check(
        "grounding.responses_after_verified_retrieval",
        responses_after_verified_retrieval >= grounding.min_responses_after_verified_retrieval,
        responses_after_verified_retrieval,
        grounding.min_responses_after_verified_retrieval,
    )
    check(
        "grounding.unverified_answers",
        unverified <= grounding.max_unverified_answers,
        unverified,
        grounding.max_unverified_answers,
    )
    check(
        "grounding.knowledge_errors",
        errors <= grounding.max_knowledge_errors,
        errors,
        grounding.max_knowledge_errors,
    )
    check(
        "recognition.unexpected_script_turns",
        unexpected_script_count <= grounding.max_unexpected_script_turns,
        unexpected_script_count,
        grounding.max_unexpected_script_turns,
    )
    check(
        "recognition.transcription_clarifications",
        clarification_count <= grounding.max_transcription_clarifications,
        clarification_count,
        grounding.max_transcription_clarifications,
    )

    interaction = manifest.expectations.interaction
    interaction_values = {
        "barge_in_count": runtime.get("barge_in_count"),
        "suppressed_fragment_count": runtime.get("suppressed_fragment_count"),
        "last_interruption_detection_ms": runtime.get("last_interruption_detection_ms"),
    }
    for key, value in interaction_values.items():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            report_metrics[key] = value
    count_constraints = (
        (
            "barge_in_count",
            interaction.min_barge_in_count,
            interaction.max_barge_in_count,
        ),
        (
            "suppressed_fragment_count",
            interaction.min_suppressed_fragment_count,
            interaction.max_suppressed_fragment_count,
        ),
    )
    for field_name, minimum, maximum in count_constraints:
        observed = interaction_values[field_name]
        numeric = (
            float(observed)
            if isinstance(observed, (int, float)) and not isinstance(observed, bool)
            else None
        )
        if minimum is not None:
            check(
                f"interaction.{field_name}.minimum",
                numeric is not None and numeric >= minimum,
                numeric,
                minimum,
            )
        if maximum is not None:
            check(
                f"interaction.{field_name}.maximum",
                numeric is not None and numeric <= maximum,
                numeric,
                maximum,
            )
    if interaction.max_last_interruption_detection_ms is not None:
        observed = interaction_values["last_interruption_detection_ms"]
        numeric = (
            float(observed)
            if isinstance(observed, (int, float)) and not isinstance(observed, bool)
            else None
        )
        check(
            "interaction.last_interruption_detection_ms",
            numeric is not None and numeric <= interaction.max_last_interruption_detection_ms,
            numeric,
            interaction.max_last_interruption_detection_ms,
        )

    user_text = "\n".join(turns)
    raw_call_id = str(call.get("id") or "")
    try:
        safe_call_id = str(UUID(raw_call_id))
    except ValueError:
        safe_call_id = "unavailable"
    return AudioReplayReport(
        schema_version="vav-audio-replay-report-v1",
        case_id=manifest.case_id,
        passed=all(item.passed for item in checks),
        call_id=safe_call_id,
        fixture_sha256=fixture.sha256,
        fixture_duration_seconds=round(fixture.duration_seconds, 3),
        user_transcript_sha256=hashlib.sha256(user_text.encode("utf-8")).hexdigest(),
        user_turn_count=len(turns),
        user_word_count=sum(len(_normalize_transcript(turn).split()) for turn in turns),
        runtime_metrics=report_metrics,
        grounding_counts=dict(sorted(grounding_counts.items())),
        turn_diagnostics=diagnostics,
        checks=tuple(checks),
    )


def _validate_agent_for_replay(
    manifest: AudioReplayManifest,
    target: ResolvedReplayTarget,
    agent: Mapping[str, Any],
) -> None:
    try:
        observed_id = UUID(str(agent.get("id") or ""))
    except ValueError as exc:
        raise ReplayExecutionError("VAV returned an invalid QA agent identity") from exc
    if observed_id != target.agent_id or not bool(agent.get("is_active")):
        raise ReplayExecutionError("QA agent identity or active state did not match the manifest")
    name_tokens = set(re.findall(r"[a-z0-9]+", str(agent.get("name") or "").casefold()))
    if not any(marker in name_tokens for marker in manifest.safety.required_agent_name_markers):
        raise ReplayExecutionError("Allowlisted agent name is missing its QA/test/canary marker")


def _validate_call_target(
    target: ResolvedReplayTarget,
    call: Mapping[str, Any],
    *,
    expected_provider: str,
    room_name: str,
) -> None:
    try:
        call_agent_id = UUID(str(call.get("agent_id") or ""))
    except ValueError as exc:
        raise ReplayExecutionError("VAV call returned an invalid agent identity") from exc
    if call_agent_id != target.agent_id:
        raise ReplayExecutionError("VAV call does not belong to the allowlisted QA agent")
    if str(call.get("provider") or "") != expected_provider:
        raise ReplayExecutionError("VAV call transport does not match the replay manifest")
    call_metadata = call.get("call_metadata")
    if not isinstance(call_metadata, Mapping):
        raise ReplayExecutionError("VAV call is missing its canonical LiveKit room metadata")
    provider_room = str(call_metadata.get("livekit_room") or "").strip()
    if not provider_room:
        raise ReplayExecutionError("VAV call is missing its canonical LiveKit room metadata")
    if provider_room != room_name:
        raise ReplayExecutionError("VAV call room does not match the explicit test room")
    if str(call.get("status") or "") in TERMINAL_CALL_STATUSES:
        raise ReplayExecutionError("VAV test call is already terminal")


async def _wait_for_final_call(
    api: Any,
    *,
    call_id: UUID,
    timing: ReplayTiming,
) -> tuple[dict[str, Any], dict[str, Any]]:
    deadline = time.monotonic() + timing.finalization_timeout_seconds
    last_call: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_call = await api.get_call(call_id)
        status = str(last_call.get("status") or "")
        if status in TERMINAL_CALL_STATUSES:
            transcript = await api.get_transcript(call_id)
            if transcript is not None:
                return last_call, transcript
        await asyncio.sleep(timing.poll_interval_seconds)
    if last_call is not None and str(last_call.get("status") or "") in TERMINAL_CALL_STATUSES:
        raise ReplayExecutionError("VAV call finalized without an available transcript")
    raise ReplayExecutionError("VAV call did not finalize within the canary timeout")


async def run_live_audio_replay(
    manifest: AudioReplayManifest,
    *,
    environ: Mapping[str, str] | None = None,
    api: Any | None = None,
    publisher: Any | None = None,
) -> AudioReplayReport:
    """Run one explicitly confirmed live QA replay and return a redacted report.

    Confirmation is intentionally a CLI concern so library callers (including
    tests and CI wrappers) must make their own explicit policy decision before
    calling this function.  Tenant ownership is independently enforced by the
    VAV bearer credential and the agent/call identity checks here.
    """
    target = resolve_replay_target(manifest, environ=environ)
    fixture = load_audio_fixture(manifest, environ=environ)
    owned_api = api is None
    replay_api = api or VAVReplayAPI(base_url=target.api_base_url, token=target.api_token)
    audio_publisher = publisher or LiveKitAudioPublisher()
    try:
        agent = await replay_api.get_agent(target.agent_id)
        _validate_agent_for_replay(manifest, target, agent)
        if target.mode == "browser_session":
            session = await replay_api.issue_browser_session(
                agent_id=target.agent_id,
                variables=manifest.target.variables,
            )
            try:
                call_id = UUID(str(session.get("call_id") or ""))
            except ValueError as exc:
                raise ReplayExecutionError(
                    "VAV browser session returned an invalid call ID"
                ) from exc
            url = _validated_livekit_url(str(session.get("url") or ""))
            access_token = str(session.get("access_token") or "")
            room_name = str(session.get("room_name") or "")
            if not access_token or not room_name:
                raise ReplayExecutionError("VAV browser session credentials are incomplete")
            initial_call = await replay_api.get_call(call_id)
            _validate_call_target(
                target,
                initial_call,
                expected_provider="livekit_webrtc",
                room_name=room_name,
            )
        else:
            assert target.call_id is not None
            assert target.livekit_url is not None
            assert target.livekit_access_token is not None
            assert target.room_name is not None
            call_id = target.call_id
            url = target.livekit_url
            access_token = target.livekit_access_token
            room_name = target.room_name
            initial_call = await replay_api.get_call(call_id)
            _validate_call_target(
                target,
                initial_call,
                expected_provider="livekit_sip",
                room_name=room_name,
            )

        await audio_publisher.publish(
            url=url,
            access_token=access_token,
            room_name=room_name,
            audio=fixture,
            timing=manifest.timing,
        )
        final_call, transcript = await _wait_for_final_call(
            replay_api,
            call_id=call_id,
            timing=manifest.timing,
        )
        try:
            final_agent_id = UUID(str(final_call.get("agent_id") or ""))
        except ValueError as exc:
            raise ReplayExecutionError("Final VAV call returned an invalid agent identity") from exc
        if final_agent_id != target.agent_id:
            raise ReplayExecutionError("Final VAV call no longer matches the QA agent")
        return evaluate_audio_replay(
            manifest,
            call=final_call,
            transcript=transcript,
            fixture=fixture,
        )
    finally:
        if owned_api:
            await replay_api.aclose()
