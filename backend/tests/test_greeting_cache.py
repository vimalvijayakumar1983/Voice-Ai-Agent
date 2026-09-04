from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from livekit import rtc
from redis.exceptions import RedisError

from app.livekit_runtime import greeting_cache as greeting_cache_module
from app.livekit_runtime.greeting_cache import (
    CachedGreetingAudio,
    FrozenAudioFrame,
    GreetingAudioCache,
    _deserialize_cached_audio,
    _serialize_cached_audio,
    greeting_cache_key,
    greeting_is_static,
    load_shared_greeting_audio,
    prepare_greeting_audio,
    store_shared_greeting_audio,
)


def _frame(value: int = 1) -> rtc.AudioFrame:
    return rtc.AudioFrame(
        data=(value.to_bytes(2, "little", signed=True) * 240),
        sample_rate=24_000,
        num_channels=1,
        samples_per_channel=240,
    )


class _Stream:
    def __init__(
        self,
        frames: list[rtc.AudioFrame],
        gate: asyncio.Event | None = None,
        *,
        error: BaseException | None = None,
        yielded: list[int] | None = None,
    ):
        self.frames = frames
        self.gate = gate
        self.error = error
        self.yielded = yielded

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return None

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        if self.gate is not None:
            await self.gate.wait()
        for frame in self.frames:
            if self.yielded is not None:
                self.yielded[0] += 1
            yield SimpleNamespace(frame=frame)
        if self.error is not None:
            raise self.error


class _TTS:
    def __init__(
        self,
        frames: list[rtc.AudioFrame],
        gate: asyncio.Event | None = None,
        *,
        error: BaseException | None = None,
        yielded: list[int] | None = None,
    ):
        self.frames = frames
        self.gate = gate
        self.error = error
        self.yielded = yielded
        self.calls = 0

    def synthesize(self, _text: str):
        self.calls += 1
        return _Stream(
            self.frames,
            self.gate,
            error=self.error,
            yielded=self.yielded,
        )


class _PushStream(_Stream):
    def __init__(self, frames: list[rtc.AudioFrame], pushed: list[str], ended: list[int]):
        super().__init__(frames)
        self.pushed = pushed
        self.ended = ended

    def push_text(self, text: str) -> None:
        self.pushed.append(text)

    def end_input(self) -> None:
        self.ended[0] += 1


class _StreamingTTS:
    capabilities = SimpleNamespace(streaming=True)

    def __init__(self, frames: list[rtc.AudioFrame]):
        self.frames = frames
        self.stream_calls = 0
        self.synthesize_calls = 0
        self.pushed: list[str] = []
        self.ended = [0]

    def stream(self):
        self.stream_calls += 1
        return _PushStream(self.frames, self.pushed, self.ended)

    def synthesize(self, _text: str):
        self.synthesize_calls += 1
        raise AssertionError("streaming-capable greeting TTS must not use chunked synthesis")


class _Redis:
    def __init__(
        self,
        value: bytes | None = None,
        *,
        error: BaseException | bool = False,
        store_result: int = 1,
    ):
        self.value = value
        self.error = error
        self.store_result = store_result
        self.get_calls: list[tuple[str, int]] = []
        self.set_calls: list[tuple[str, bytes, int]] = []
        self.set_limits: list[tuple[int, int]] = []
        self.close_calls = 0

    async def eval(self, script: str, _key_count: int, *args):
        if self.error:
            if isinstance(self.error, BaseException):
                raise self.error
            raise RedisError("unavailable")
        key = args[0]
        if "return value" in script:
            assert len(args) == 5
            self.get_calls.append((key, int(args[4])))
            return self.value
        assert len(args) == 9
        payload = args[3]
        ttl = int(args[4])
        self.set_calls.append((key, payload, ttl))
        self.set_limits.append((int(args[7]), int(args[8])))
        self.value = payload
        return self.store_result

    async def aclose(self):
        self.close_calls += 1


class _StalledPushStream(_PushStream):
    async def _iterate(self):
        await asyncio.Event().wait()
        if False:  # pragma: no cover - makes this an async generator
            yield None


class _StalledStreamingTTS(_StreamingTTS):
    def __init__(self):
        super().__init__([])

    def stream(self):
        self.stream_calls += 1
        return _StalledPushStream([], self.pushed, self.ended)


class _StalledStream(_Stream):
    async def _iterate(self):
        await asyncio.Event().wait()
        if False:  # pragma: no cover - makes this an async generator
            yield None


class _StalledTTS(_TTS):
    def __init__(self):
        self.calls = 0

    def synthesize(self, _text: str):
        self.calls += 1
        return _StalledStream([])


class _CancellationSwallowingExitStream(_StalledStream):
    def __init__(self, entered_exit: asyncio.Event, release_exit: asyncio.Event):
        super().__init__([])
        self.entered_exit = entered_exit
        self.release_exit = release_exit

    async def __aexit__(self, *_args):
        self.entered_exit.set()
        while not self.release_exit.is_set():
            try:
                await self.release_exit.wait()
            except asyncio.CancelledError:
                # Simulate a broken SDK transport that consumes cancellation
                # while waiting for its network cleanup to finish.
                continue


class _CancellationSwallowingExitTTS:
    def __init__(self, entered_exit: asyncio.Event, release_exit: asyncio.Event):
        self.entered_exit = entered_exit
        self.release_exit = release_exit
        self.calls = 0

    def synthesize(self, _text: str):
        self.calls += 1
        return _CancellationSwallowingExitStream(self.entered_exit, self.release_exit)


class _StaleCompletingExitStream(_Stream):
    def __init__(
        self,
        frames: list[rtc.AudioFrame],
        entered_exit: asyncio.Event,
        release_exit: asyncio.Event,
    ) -> None:
        super().__init__(frames)
        self.entered_exit = entered_exit
        self.release_exit = release_exit

    async def __aexit__(self, *_args):
        self.entered_exit.set()
        while not self.release_exit.is_set():
            try:
                await self.release_exit.wait()
            except asyncio.CancelledError:
                continue


class _StaleCompletingExitTTS:
    def __init__(self, entered_exit: asyncio.Event, release_exit: asyncio.Event):
        self.entered_exit = entered_exit
        self.release_exit = release_exit
        self.calls = 0

    def synthesize(self, _text: str):
        self.calls += 1
        return _StaleCompletingExitStream(
            [_frame(1)],
            self.entered_exit,
            self.release_exit,
        )


class _PauseAfterTwoFramesStream(_Stream):
    def __init__(
        self,
        frames: list[rtc.AudioFrame],
        paused: asyncio.Event,
        release: asyncio.Event,
    ) -> None:
        super().__init__(frames)
        self.paused = paused
        self.release = release

    async def _iterate(self):
        for index, frame in enumerate(self.frames):
            if index == 2:
                self.paused.set()
                await self.release.wait()
            yield SimpleNamespace(frame=frame)


class _PauseAfterTwoFramesTTS:
    def __init__(self, paused: asyncio.Event, release: asyncio.Event):
        self.paused = paused
        self.release = release
        self.calls = 0

    def synthesize(self, _text: str):
        self.calls += 1
        return _PauseAfterTwoFramesStream(
            [_frame(1), _frame(2), _frame(3)],
            self.paused,
            self.release,
        )


class _ThreadGateStream(_Stream):
    def __init__(
        self,
        frames: list[rtc.AudioFrame],
        gate: threading.Event,
        yielded: list[int],
    ) -> None:
        super().__init__(frames, yielded=yielded)
        self.thread_gate = gate

    async def _iterate(self):
        await asyncio.to_thread(self.thread_gate.wait)
        async for item in super()._iterate():
            yield item


class _ThreadGateTTS(_TTS):
    def __init__(self, frames: list[rtc.AudioFrame], gate: threading.Event):
        self.frames = frames
        self.thread_gate = gate
        self.calls = 0
        self.yielded = [0]

    def synthesize(self, _text: str):
        self.calls += 1
        return _ThreadGateStream(self.frames, self.thread_gate, self.yielded)


def test_only_static_greetings_are_cacheable():
    assert greeting_is_static("Welcome to Al Zaabi Group.") is True
    assert greeting_is_static("Hello {{ customer_name }}") is False
    assert greeting_is_static("Hello ${customer_name}") is False
    assert greeting_is_static("") is False


def test_cache_key_changes_for_audio_affecting_configuration():
    base = {
        "tenant_id": "tenant-1",
        "agent_id": "agent-1",
        "greeting": "Welcome",
        "voice_id": "Ashley",
        "model_id": "inworld-tts-2",
        "language": "en-GB",
        "speech_rate": 1.0,
        "delivery_mode": "BALANCED",
    }
    first = greeting_cache_key(**base)
    assert first == greeting_cache_key(**base)
    assert first != greeting_cache_key(**{**base, "speech_rate": 1.1})
    assert first != greeting_cache_key(**{**base, "tenant_id": "tenant-2"})
    assert "Welcome" not in first


def test_cache_enforces_entry_and_byte_bounds():
    cache = GreetingAudioCache(max_entries=2, max_bytes=1_000)
    frame = FrozenAudioFrame.from_frame(_frame())
    item = CachedGreetingAudio((frame,), len(frame.data), frame.duration)
    assert cache.put("a", item)
    assert cache.put("b", item)
    assert cache.get("a") is not None
    assert cache.put("c", item)
    assert cache.get("b") is None
    assert cache.entry_count == 2
    assert cache.byte_count <= 1_000


def test_shared_cache_binary_round_trip_is_strict_and_content_agnostic():
    frames = tuple(FrozenAudioFrame.from_frame(_frame(value)) for value in (1, 2))
    item = CachedGreetingAudio(
        frames=frames,
        byte_count=sum(len(frame.data) for frame in frames),
        duration_seconds=sum(frame.duration for frame in frames),
    )

    cache_key = "d" * 64
    payload = _serialize_cached_audio(item, cache_key=cache_key)
    restored = _deserialize_cached_audio(payload, cache_key=cache_key)

    assert restored == item
    assert all(frame.thaw().sample_rate == 24_000 for frame in restored.frames)
    with pytest.raises(ValueError, match="integrity"):
        _deserialize_cached_audio(payload + b"unexpected", cache_key=cache_key)
    with pytest.raises(ValueError, match="integrity"):
        _deserialize_cached_audio(payload, cache_key="e" * 64)
    tampered = bytearray(payload)
    tampered[0] ^= 1
    with pytest.raises(ValueError, match="integrity"):
        _deserialize_cached_audio(bytes(tampered), cache_key=cache_key)


def test_shared_cache_serializer_rejects_invalid_pcm_format_and_duration():
    cache_key = "f" * 64
    malformed = FrozenAudioFrame(
        data=b"\x00\x00",
        sample_rate=24_000,
        num_channels=1,
        samples_per_channel=240,
    )
    with pytest.raises(ValueError, match="metadata"):
        _serialize_cached_audio(
            CachedGreetingAudio((malformed,), 2, malformed.duration),
            cache_key=cache_key,
        )

    first = FrozenAudioFrame.from_frame(_frame(1))
    changed_format = FrozenAudioFrame(
        data=b"\x00\x00" * 80,
        sample_rate=8_000,
        num_channels=1,
        samples_per_channel=80,
    )
    with pytest.raises(ValueError, match="metadata"):
        _serialize_cached_audio(
            CachedGreetingAudio(
                (first, changed_format),
                len(first.data) + len(changed_format.data),
                first.duration + changed_format.duration,
            ),
            cache_key=cache_key,
        )

    one_second = FrozenAudioFrame(
        data=b"\x00\x00" * 8_000,
        sample_rate=8_000,
        num_channels=1,
        samples_per_channel=8_000,
    )
    too_long = CachedGreetingAudio(
        frames=(one_second,) * 13,
        byte_count=len(one_second.data) * 13,
        duration_seconds=13.0,
    )
    with pytest.raises(ValueError, match="duration"):
        _serialize_cached_audio(too_long, cache_key=cache_key)


def test_shared_cache_decoder_enforces_pcm_length_even_with_a_valid_mac():
    cache_key = "9" * 64
    frame = FrozenAudioFrame.from_frame(_frame(1))
    item = CachedGreetingAudio((frame,), len(frame.data), frame.duration)
    payload = _serialize_cached_audio(item, cache_key=cache_key)
    body = bytearray(payload[: -greeting_cache_module._SHARED_CACHE_MAC_BYTES])
    samples_offset = greeting_cache_module._SHARED_CACHE_HEADER.size + 6
    body[samples_offset : samples_offset + 4] = (241).to_bytes(4, "big")
    forged = bytes(body) + greeting_cache_module._shared_cache_mac(cache_key, bytes(body))

    with pytest.raises(ValueError, match="frame"):
        _deserialize_cached_audio(forged, cache_key=cache_key)


@pytest.mark.asyncio
async def test_shared_cache_hit_uses_fixed_ttl_and_avoids_provider_request():
    frames = (FrozenAudioFrame.from_frame(_frame(7)),)
    item = CachedGreetingAudio(
        frames=frames,
        byte_count=len(frames[0].data),
        duration_seconds=frames[0].duration,
    )
    key = "a" * 64
    redis = _Redis(_serialize_cached_audio(item, cache_key=key))

    lookup = await load_shared_greeting_audio(
        key,
        enabled=True,
        client=redis,
        ttl_seconds=60,
    )
    engine = _TTS([_frame(9)])
    prepared = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key=key,
        preloaded_audio=lookup.item,
        cache=GreetingAudioCache(max_entries=2, max_bytes=10_000),
    )

    assert lookup.status == "hit"
    assert redis.get_calls == [("voiceai:greeting-audio:v1:" + key, 120)]
    assert "EXPIRE', KEYS[1]" not in greeting_cache_module._SHARED_CACHE_GET_LUA
    assert len(await _collect_frames(prepared)) == 1
    assert prepared.cache_status == "hit"
    assert prepared.provider_request_count == 0
    assert engine.calls == 0


@pytest.mark.asyncio
async def test_shared_cache_miss_is_stored_with_bounded_ttl():
    redis = _Redis()
    key = "b" * 64
    lookup = await load_shared_greeting_audio(key, enabled=True, client=redis, ttl_seconds=60)
    frames = (FrozenAudioFrame.from_frame(_frame(3)),)
    item = CachedGreetingAudio(
        frames=frames,
        byte_count=len(frames[0].data),
        duration_seconds=frames[0].duration,
    )

    stored = await store_shared_greeting_audio(
        key,
        item,
        enabled=True,
        client=redis,
        ttl_seconds=60,
    )

    assert lookup.status == "miss"
    assert stored.status == "stored"
    assert len(redis.set_calls) == 1
    redis_key, payload, ttl = redis.set_calls[0]
    assert redis_key == "voiceai:greeting-audio:v1:" + key
    assert ttl == 60
    assert redis.set_limits == [(128, 32 * 1024 * 1024)]
    assert _deserialize_cached_audio(payload, cache_key=key) == item


@pytest.mark.asyncio
async def test_shared_cache_owned_clients_are_closed_per_operation(monkeypatch):
    key = "8" * 64
    frame = FrozenAudioFrame.from_frame(_frame(2))
    item = CachedGreetingAudio((frame,), len(frame.data), frame.duration)
    redis = _Redis(_serialize_cached_audio(item, cache_key=key))
    monkeypatch.setattr(greeting_cache_module, "_shared_redis", lambda: redis)

    lookup = await load_shared_greeting_audio(key, enabled=True)
    stored = await store_shared_greeting_audio(key, item, enabled=True)

    assert lookup.status == "hit"
    assert stored.status == "stored"
    assert redis.close_calls == 2


@pytest.mark.asyncio
async def test_shared_cache_reports_when_quota_evicts_the_written_item():
    key = "6" * 64
    frame = FrozenAudioFrame.from_frame(_frame(2))
    item = CachedGreetingAudio((frame,), len(frame.data), frame.duration)

    stored = await store_shared_greeting_audio(
        key,
        item,
        enabled=True,
        client=_Redis(store_result=0),
    )

    assert stored.status == "quota_rejected"


@pytest.mark.asyncio
async def test_shared_cache_runtime_errors_fail_open_but_cancellation_propagates():
    key = "7" * 64

    unavailable = await load_shared_greeting_audio(
        key,
        enabled=True,
        client=_Redis(error=RuntimeError("different event loop")),
    )
    assert unavailable.status == "unavailable"

    with pytest.raises(asyncio.CancelledError):
        await load_shared_greeting_audio(
            key,
            enabled=True,
            client=_Redis(error=asyncio.CancelledError()),
        )


@pytest.mark.asyncio
async def test_shared_cache_corruption_and_outage_fail_open_without_content_in_status():
    key = "c" * 64
    corrupted = await load_shared_greeting_audio(
        key,
        enabled=True,
        client=_Redis(b"not-a-valid-frame"),
    )
    unavailable = await load_shared_greeting_audio(
        key,
        enabled=True,
        client=_Redis(error=True),
    )

    assert corrupted.item is None
    assert corrupted.status == "invalid"
    assert unavailable.item is None
    assert unavailable.status == "unavailable"

    engine = _TTS([_frame(4)])
    prepared = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key=key,
        preloaded_audio=unavailable.item,
        cache=GreetingAudioCache(max_entries=2, max_bytes=10_000),
    )
    assert len(await _collect_frames(prepared)) == 1
    assert prepared.provider_request_count == 1


@pytest.mark.asyncio
async def test_preparation_starts_before_consumer_and_caches_static_audio():
    gate = asyncio.Event()
    engine = _TTS([_frame(1), _frame(2)], gate)
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    prepared = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="static-key",
        cache=cache,
    )
    await asyncio.sleep(0)
    assert engine.calls == 1
    gate.set()
    frames = [frame async for frame in prepared.frames()]
    assert len(frames) == 2
    assert prepared.cache_status == "miss_cached"
    assert prepared.provider_request_count == 1
    assert prepared.started_at_monotonic <= prepared.first_frame_at_monotonic
    assert prepared.first_frame_at_monotonic <= prepared.completed_at_monotonic
    assert prepared.retained_byte_count == 0
    assert cache.get("static-key") is not None

    cached = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="static-key",
        cache=cache,
    )
    assert cached.cache_status == "hit"
    assert cached.provider_request_count == 0
    cached_frames = [frame async for frame in cached.frames()]
    assert len(cached_frames) == 2
    assert cached_frames[0] is not frames[0]
    assert engine.calls == 1


@pytest.mark.asyncio
async def test_streaming_capable_tts_prepares_greeting_on_reusable_stream_transport():
    engine = _StreamingTTS([_frame(1), _frame(2)])
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    prepared = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="streaming-key",
        cache=cache,
    )

    frames = [frame async for frame in prepared.frames()]

    assert len(frames) == 2
    assert engine.stream_calls == 1
    assert engine.synthesize_calls == 0
    assert engine.pushed == ["Welcome"]
    assert engine.ended == [1]
    assert prepared.provider_request_count == 1
    assert prepared.cache_status == "miss_cached"
    assert cache.get("streaming-key") is not None


@pytest.mark.asyncio
async def test_stalled_streaming_greeting_evicts_singleflight_without_chunked_fallback():
    engine = _StalledStreamingTTS()
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    prepared = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="stalled-streaming-key",
        cache=cache,
        synthesis_total_timeout_seconds=0.1,
        synthesis_idle_timeout_seconds=0.02,
    )

    with pytest.raises(TimeoutError, match="stalled"):
        _ = [frame async for frame in prepared.frames()]

    assert engine.stream_calls == 1
    assert engine.synthesize_calls == 0
    assert engine.pushed == ["Welcome"]
    assert engine.ended == [1]
    assert prepared.provider_request_count == 1
    assert prepared.cache_status == "failed"
    assert cache.inflight_count == 0
    assert cache.get("stalled-streaming-key") is None


@pytest.mark.asyncio
async def test_personalized_greeting_streams_without_entering_cache():
    engine = _TTS([_frame()])
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    prepared = prepare_greeting_audio(
        tts_engine=engine,
        text="Hello Vimal",
        cache_key=None,
        cache=cache,
    )
    assert prepared.cache_status == "bypassed_personalized"
    assert len([frame async for frame in prepared.frames()]) == 1
    assert prepared.cache_status == "bypassed_personalized"
    assert prepared.retained_byte_count == 0
    assert cache.entry_count == 0


@pytest.mark.asyncio
async def test_same_key_concurrent_misses_share_one_streaming_synthesis():
    gate = asyncio.Event()
    engine = _TTS([_frame(1), _frame(2)], gate)
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)

    first = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="same-key",
        cache=cache,
    )
    second = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="same-key",
        cache=cache,
    )
    await asyncio.sleep(0)
    assert engine.calls == 1
    assert cache.inflight_count == 1

    async def collect(prepared):
        return [frame async for frame in prepared.frames()]

    first_task = asyncio.create_task(collect(first))
    second_task = asyncio.create_task(collect(second))
    gate.set()
    first_frames, second_frames = await asyncio.gather(first_task, second_task)

    assert len(first_frames) == len(second_frames) == 2
    assert first_frames[0] is not second_frames[0]
    assert first.cache_status == second.cache_status == "miss_cached"
    assert engine.calls == 1
    assert cache.inflight_count == 0
    assert cache.entry_count == 1


@pytest.mark.asyncio
async def test_leader_close_before_first_yield_preserves_shared_request_attribution():
    engine = _TTS([_frame(1)])
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    leader = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="shared-attribution-key",
        cache=cache,
    )
    follower = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="shared-attribution-key",
        cache=cache,
    )

    # The leader exits before the scheduled producer's first ordinary event-loop
    # turn. Its close must still observe the provider request retained for the
    # follower, or aggregate call accounting would report zero requests.
    await leader.aclose()
    assert engine.calls == 1
    assert leader.provider_request_count == 1
    assert follower.provider_request_count == 0
    assert len(await asyncio.wait_for(_collect_frames(follower), timeout=1)) == 1


@pytest.mark.asyncio
async def test_non_consuming_same_key_follower_cannot_stall_healthy_call():
    gate = asyncio.Event()
    engine = _TTS([_frame(1), _frame(2), _frame(3)], gate)
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    healthy = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="laggard-key",
        cache=cache,
        queue_max_frames=1,
    )
    laggard = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="laggard-key",
        cache=cache,
        queue_max_frames=1,
    )
    healthy_task = asyncio.create_task(_collect_frames(healthy))
    gate.set()

    healthy_frames = await asyncio.wait_for(healthy_task, timeout=1)

    assert len(healthy_frames) == 3
    assert cache.inflight_count == 0
    assert cache.entry_count == 1
    assert engine.calls == 1
    # The detached follower can still play the complete cached greeting later.
    assert len(await asyncio.wait_for(_collect_frames(laggard), timeout=1)) == 3


@pytest.mark.asyncio
async def test_lagged_consumer_lease_survives_another_calls_close():
    paused = asyncio.Event()
    release = asyncio.Event()
    engine = _PauseAfterTwoFramesTTS(paused, release)
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    first = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="lagged-consumer-lease",
        cache=cache,
        queue_max_frames=1,
    )
    second = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="lagged-consumer-lease",
        cache=cache,
        queue_max_frames=1,
    )

    # Both live consumers have detached from broadcast backpressure and now
    # await the retained completion, while provider synthesis is still active.
    await asyncio.wait_for(paused.wait(), timeout=1)
    await first.aclose()
    assert cache.inflight_count == 1

    second_frames = asyncio.create_task(_collect_frames(second))
    release.set()
    assert len(await asyncio.wait_for(second_frames, timeout=1)) == 3
    assert engine.calls == 1


async def _collect_frames(prepared):
    return [frame async for frame in prepared.frames()]


@pytest.mark.asyncio
async def test_bounded_queue_applies_backpressure_to_slow_personalized_consumer():
    yielded = [0]
    engine = _TTS([_frame(index) for index in range(1, 11)], yielded=yielded)
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    prepared = prepare_greeting_audio(
        tts_engine=engine,
        text="Hello Vimal",
        cache_key=None,
        cache=cache,
        queue_max_frames=2,
    )

    # The producer can obtain one frame currently blocked on queue.put, but it
    # cannot consume the complete provider stream while playout is paused.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert yielded[0] <= 3
    assert prepared.retained_byte_count == 0

    frames = [frame async for frame in prepared.frames()]
    assert len(frames) == 10
    assert yielded[0] == 10
    assert cache.entry_count == 0


@pytest.mark.asyncio
async def test_oversize_static_audio_streams_but_is_never_retained():
    engine = _TTS([_frame(1), _frame(2), _frame(3)])
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    prepared = prepare_greeting_audio(
        tts_engine=engine,
        text="An unexpectedly long greeting",
        cache_key="oversize-key",
        cache=cache,
        max_synthesis_bytes=len(bytes(_frame().data)) + 1,
    )

    assert len([frame async for frame in prepared.frames()]) == 3
    assert prepared.cache_status == "miss_oversize"
    assert prepared.retained_byte_count == 0
    assert prepared.completed_at_monotonic is not None
    assert cache.entry_count == 0
    assert cache.inflight_count == 0

    again = prepare_greeting_audio(
        tts_engine=engine,
        text="An unexpectedly long greeting",
        cache_key="oversize-key",
        cache=cache,
        max_synthesis_bytes=len(bytes(_frame().data)) + 1,
    )
    assert len([frame async for frame in again.frames()]) == 3
    assert engine.calls == 2


@pytest.mark.asyncio
async def test_duration_cap_also_prevents_retention():
    engine = _TTS([_frame(1), _frame(2)])
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    prepared = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="duration-key",
        cache=cache,
        max_synthesis_duration_seconds=0.015,
    )

    assert len([frame async for frame in prepared.frames()]) == 2
    assert prepared.cache_status == "miss_oversize"
    assert cache.get("duration-key") is None


@pytest.mark.asyncio
async def test_provider_failure_is_truthful_and_does_not_poison_cache():
    engine = _TTS([], error=RuntimeError("provider unavailable"))
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    prepared = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="failed-key",
        cache=cache,
    )

    with pytest.raises(RuntimeError, match="provider unavailable"):
        _ = [frame async for frame in prepared.frames()]

    assert prepared.cache_status == "failed"
    assert prepared.failed_before_playout is True
    assert prepared.first_frame_at_monotonic is None
    assert prepared.completed_at_monotonic is not None
    assert prepared.retained_byte_count == 0
    assert cache.get("failed-key") is None
    assert cache.inflight_count == 0


@pytest.mark.asyncio
async def test_stalled_provider_times_out_evicts_singleflight_and_allows_retry():
    engine = _StalledTTS()
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    first = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="stalled-key",
        cache=cache,
        synthesis_total_timeout_seconds=0.1,
        synthesis_idle_timeout_seconds=0.02,
    )

    with pytest.raises(TimeoutError, match="stalled"):
        _ = [frame async for frame in first.frames()]

    assert first.cache_status == "failed"
    assert first.provider_request_count == 1
    assert cache.inflight_count == 0
    second = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="stalled-key",
        cache=cache,
        synthesis_total_timeout_seconds=0.1,
        synthesis_idle_timeout_seconds=0.02,
    )
    assert second.provider_request_count == 0
    await asyncio.sleep(0)
    assert second.provider_request_count == 1
    assert engine.calls == 2
    await second.aclose()
    await asyncio.sleep(0.03)
    assert cache.inflight_count == 0


@pytest.mark.asyncio
async def test_immediate_close_before_producer_starts_evicts_and_does_not_count_request():
    engine = _TTS([_frame(1)])
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    first = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="cancelled-before-start",
        cache=cache,
    )

    # Do not yield between construction and close: this is the worker race that
    # used to leave an uncompleted singleflight entry behind forever.
    await first.aclose()

    assert engine.calls == 0
    assert first.provider_request_count == 0
    assert first.cache_status == "failed"
    assert cache.inflight_count == 0

    retry = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="cancelled-before-start",
        cache=cache,
    )
    assert len(await asyncio.wait_for(_collect_frames(retry), timeout=1)) == 1
    assert engine.calls == 1
    assert retry.provider_request_count == 1


@pytest.mark.asyncio
async def test_cancellation_swallowing_provider_is_abandoned_within_cleanup_bound():
    entered_exit = asyncio.Event()
    release_exit = asyncio.Event()
    engine = _CancellationSwallowingExitTTS(entered_exit, release_exit)
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    prepared = prepare_greeting_audio(
        tts_engine=engine,
        text="Welcome",
        cache_key="stuck-provider-cleanup",
        cache=cache,
        synthesis_cancel_timeout_seconds=0.02,
    )
    await asyncio.sleep(0)
    assert engine.calls == 1

    close_task = asyncio.create_task(prepared.aclose())
    await asyncio.wait_for(entered_exit.wait(), timeout=1)
    await asyncio.wait_for(close_task, timeout=0.2)

    assert prepared.provider_request_count == 1
    assert prepared.cache_status == "failed"
    assert cache.inflight_count == 0

    # A healthy same-key request must not join the abandoned provider task.
    healthy = _TTS([_frame(2)])
    retry = prepare_greeting_audio(
        tts_engine=healthy,
        text="Welcome",
        cache_key="stuck-provider-cleanup",
        cache=cache,
    )
    assert len(await asyncio.wait_for(_collect_frames(retry), timeout=1)) == 1
    assert healthy.calls == 1

    # Let the simulated broken transport unwind so the test leaves no task.
    release_exit.set()
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_abandoned_old_producer_cannot_overwrite_replacement_cache_entry():
    entered_exit = asyncio.Event()
    release_exit = asyncio.Event()
    stale_engine = _StaleCompletingExitTTS(entered_exit, release_exit)
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    stale = prepare_greeting_audio(
        tts_engine=stale_engine,
        text="Welcome",
        cache_key="stale-provider-key",
        cache=cache,
        synthesis_cancel_timeout_seconds=0.02,
    )
    await asyncio.wait_for(entered_exit.wait(), timeout=1)
    stale_producer = stale._flight.producer
    assert stale_producer is not None
    await asyncio.wait_for(stale.aclose(), timeout=0.2)
    assert cache.inflight_count == 0

    healthy_engine = _TTS([_frame(9)])
    replacement = prepare_greeting_audio(
        tts_engine=healthy_engine,
        text="Welcome",
        cache_key="stale-provider-key",
        cache=cache,
    )
    assert len(await asyncio.wait_for(_collect_frames(replacement), timeout=1)) == 1
    expected_data = bytes(_frame(9).data)
    assert cache.get("stale-provider-key").frames[0].data == expected_data

    # The abandoned task eventually exits normally with different audio. Its
    # old flight identity must not be allowed to mutate the replacement cache.
    release_exit.set()
    await asyncio.wait_for(asyncio.shield(stale_producer), timeout=1)
    assert cache.get("stale-provider-key").frames[0].data == expected_data


def test_singleflight_can_be_joined_safely_across_event_loops():
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    gate = threading.Event()
    ready = threading.Barrier(3)
    engine = _ThreadGateTTS([_frame(1), _frame(2)], gate)

    def run_consumer() -> tuple[int, str]:
        async def consume() -> tuple[int, str]:
            prepared = prepare_greeting_audio(
                tts_engine=engine,
                text="Welcome",
                cache_key="cross-loop-key",
                cache=cache,
            )
            ready.wait(timeout=5)
            frames = [frame async for frame in prepared.frames()]
            return len(frames), prepared.cache_status

        return asyncio.run(consume())

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(run_consumer)
        second = executor.submit(run_consumer)
        ready.wait(timeout=5)
        gate.set()
        results = [first.result(timeout=5), second.result(timeout=5)]

    assert results == [(2, "miss_cached"), (2, "miss_cached")]
    assert engine.calls == 1
    assert cache.entry_count == 1


def test_cross_loop_waiter_lease_survives_leader_close():
    cache = GreetingAudioCache(max_entries=2, max_bytes=10_000)
    provider_gate = threading.Event()
    leader_ready = threading.Event()
    waiter_ready = threading.Event()
    leader_closed = threading.Event()
    engine = _ThreadGateTTS([_frame(1), _frame(2)], provider_gate)

    def close_leader() -> tuple[int, int]:
        async def run() -> tuple[int, int]:
            prepared = prepare_greeting_audio(
                tts_engine=engine,
                text="Welcome",
                cache_key="cross-loop-close-key",
                cache=cache,
            )
            await asyncio.sleep(0)
            leader_ready.set()
            await asyncio.to_thread(waiter_ready.wait, 5)
            await prepared.aclose()
            leader_closed.set()
            await asyncio.shield(asyncio.wrap_future(prepared._flight.completion_future))
            return engine.calls, prepared.provider_request_count

        return asyncio.run(run())

    def consume_waiter() -> tuple[int, str]:
        async def run() -> tuple[int, str]:
            await asyncio.to_thread(leader_ready.wait, 5)
            prepared = prepare_greeting_audio(
                tts_engine=engine,
                text="Welcome",
                cache_key="cross-loop-close-key",
                cache=cache,
            )
            waiter_ready.set()
            await asyncio.to_thread(leader_closed.wait, 5)
            frames = await _collect_frames(prepared)
            return len(frames), prepared.cache_status

        return asyncio.run(run())

    with ThreadPoolExecutor(max_workers=2) as executor:
        leader = executor.submit(close_leader)
        waiter = executor.submit(consume_waiter)
        assert leader_closed.wait(timeout=5)
        provider_gate.set()
        assert leader.result(timeout=5) == (1, 1)
        assert waiter.result(timeout=5) == (2, "miss_cached")

    assert engine.calls == 1
    assert cache.entry_count == 1
