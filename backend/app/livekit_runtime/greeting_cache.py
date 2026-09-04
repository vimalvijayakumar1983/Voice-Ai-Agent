"""Bounded, privacy-safe audio preparation for deterministic call greetings.

The first spoken message is authored content and does not need an LLM turn. This
module starts TTS while the realtime session is connecting and retains only
static (non-personalised) audio in a small process-local cache. Concurrent cache
misses for the same greeting share one synthesis inside the worker process.

Only the bounded playout queue temporarily holds personalised or oversized PCM;
neither is retained as a cache candidate after synthesis.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import threading
import time
from collections import OrderedDict
from collections.abc import AsyncIterator, Callable
from concurrent.futures import Future as ConcurrentFuture
from dataclasses import dataclass
from typing import Any, Literal

from livekit import rtc

_TEMPLATE_MARKER = re.compile(r"{{|}}|\$\{|{%|%}")
_END = object()

DEFAULT_QUEUE_MAX_FRAMES = 128
DEFAULT_MAX_SYNTHESIS_BYTES = 2 * 1024 * 1024
DEFAULT_MAX_SYNTHESIS_DURATION_SECONDS = 30.0
DEFAULT_SYNTHESIS_TOTAL_TIMEOUT_SECONDS = 45.0
DEFAULT_SYNTHESIS_IDLE_TIMEOUT_SECONDS = 8.0
DEFAULT_SYNTHESIS_CANCEL_TIMEOUT_SECONDS = 1.0

GreetingCacheResultState = Literal[
    "hit",
    "miss_cached",
    "miss_oversize",
    "failed",
    "bypassed_personalized",
]


@dataclass(frozen=True, slots=True)
class FrozenAudioFrame:
    """An immutable, loop-independent representation of a LiveKit audio frame."""

    data: bytes
    sample_rate: int
    num_channels: int
    samples_per_channel: int

    @classmethod
    def from_frame(cls, frame: rtc.AudioFrame) -> FrozenAudioFrame:
        return cls(
            data=bytes(frame.data),
            sample_rate=int(frame.sample_rate),
            num_channels=int(frame.num_channels),
            samples_per_channel=int(frame.samples_per_channel),
        )

    def thaw(self) -> rtc.AudioFrame:
        # Give every playout a fresh frame because LiveKit attaches timing
        # metadata to frames as they move through the output pipeline.
        return rtc.AudioFrame(
            data=self.data,
            sample_rate=self.sample_rate,
            num_channels=self.num_channels,
            samples_per_channel=self.samples_per_channel,
        )

    @property
    def duration(self) -> float:
        if self.sample_rate <= 0:
            return 0.0
        return self.samples_per_channel / self.sample_rate


@dataclass(frozen=True, slots=True)
class CachedGreetingAudio:
    frames: tuple[FrozenAudioFrame, ...]
    byte_count: int
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class _FlightCompletion:
    state: GreetingCacheResultState
    item: CachedGreetingAudio | None
    error: BaseException | None
    started_at_monotonic: float
    first_frame_at_monotonic: float | None
    completed_at_monotonic: float


class GreetingAudioCache:
    """A bounded, thread-safe LRU and process-local singleflight registry.

    The worker can serve many tenants, so both entry count and total PCM bytes
    are capped. Keys contain only hashes and provider configuration, never the
    greeting text or caller variables. A regular threading lock deliberately
    protects the registry: unlike an ``asyncio.Lock``, it is not bound to one
    event loop and remains safe if a process hosts more than one loop/thread.
    """

    def __init__(self, *, max_entries: int = 32, max_bytes: int = 24 * 1024 * 1024):
        if max_entries < 1 or max_bytes < 1:
            raise ValueError("Greeting cache bounds must be positive")
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._bytes = 0
        self._items: OrderedDict[str, CachedGreetingAudio] = OrderedDict()
        self._flights: dict[str, _GreetingFlight] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> CachedGreetingAudio | None:
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            self._items.move_to_end(key)
            return item

    def put(self, key: str, item: CachedGreetingAudio) -> bool:
        with self._lock:
            return self._put_locked(key, item)

    def _put_locked(self, key: str, item: CachedGreetingAudio) -> bool:
        if not item.frames or item.byte_count <= 0 or item.byte_count > self._max_bytes:
            return False
        previous = self._items.pop(key, None)
        if previous is not None:
            self._bytes -= previous.byte_count
        self._items[key] = item
        self._bytes += item.byte_count
        while len(self._items) > self._max_entries or self._bytes > self._max_bytes:
            _evicted_key, evicted = self._items.popitem(last=False)
            self._bytes -= evicted.byte_count
        return key in self._items

    def _resolve(
        self,
        *,
        key: str,
        loop: asyncio.AbstractEventLoop,
        factory: Callable[[], _GreetingFlight],
    ) -> tuple[str, CachedGreetingAudio | _GreetingFlight]:
        """Atomically resolve a hit, joinable flight, cross-loop wait, or leader."""

        with self._lock:
            cached = self._items.get(key)
            if cached is not None:
                self._items.move_to_end(key)
                return "hit", cached

            flight = self._flights.get(key)
            if flight is not None:
                # Acquire while the registry lock still proves this is the
                # current flight. A foreign event loop could otherwise cancel
                # its last lease in the gap before the waiter registers.
                flight.acquire_consumer()
                if flight.can_subscribe(loop):
                    return "flight", flight
                return "wait", flight

            flight = factory()
            flight.acquire_consumer()
            self._flights[key] = flight
            return "leader", flight

    def _complete_flight(
        self,
        *,
        key: str,
        flight: _GreetingFlight,
        item: CachedGreetingAudio | None,
    ) -> bool:
        with self._lock:
            # A cancellation-swallowing provider may finish after its flight was
            # abandoned and a replacement began. Never let that stale producer
            # populate or evict the replacement's cache entry.
            if self._flights.get(key) is not flight:
                return False
            cached = self._put_locked(key, item) if item is not None else False
            self._flights.pop(key, None)
            return cached

    def clear(self) -> None:
        # Clearing cached audio intentionally does not cancel active provider
        # requests. They still have live consumers and will repopulate safely.
        with self._lock:
            self._items.clear()
            self._bytes = 0

    @property
    def entry_count(self) -> int:
        with self._lock:
            return len(self._items)

    @property
    def byte_count(self) -> int:
        with self._lock:
            return self._bytes

    @property
    def inflight_count(self) -> int:
        with self._lock:
            return len(self._flights)


GREETING_AUDIO_CACHE = GreetingAudioCache()


def greeting_is_static(template: str | None) -> bool:
    """Return whether audio may be reused without retaining caller data."""

    value = str(template or "").strip()
    return bool(value) and _TEMPLATE_MARKER.search(value) is None


def greeting_cache_key(
    *,
    agent_id: object,
    greeting: str,
    voice_id: str,
    model_id: str,
    language: str,
    speech_rate: float,
    delivery_mode: str,
) -> str:
    payload = "\x1f".join(
        (
            str(agent_id),
            hashlib.sha256(greeting.encode("utf-8")).hexdigest(),
            voice_id,
            model_id,
            language,
            f"{speech_rate:.4f}",
            delivery_mode,
            "greeting-audio-v2",
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(eq=False, slots=True)
class _Subscription:
    queue: asyncio.Queue[FrozenAudioFrame | object]
    snapshot: tuple[FrozenAudioFrame, ...]
    active: bool = True
    lagged: bool = False

    def close(self) -> None:
        self.active = False
        # Unblock a producer applying backpressure to an abandoned consumer.
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break


class _GreetingFlight:
    """One provider synthesis shared by subscribers on its owning event loop."""

    def __init__(
        self,
        *,
        tts_engine: Any,
        text: str,
        cache: GreetingAudioCache,
        cache_key: str | None,
        loop: asyncio.AbstractEventLoop,
        queue_max_frames: int,
        max_synthesis_bytes: int,
        max_synthesis_duration_seconds: float,
        synthesis_total_timeout_seconds: float,
        synthesis_idle_timeout_seconds: float,
        on_provider_request: Callable[[], None],
    ) -> None:
        self._tts = tts_engine
        self._text = text
        self._cache = cache
        self._cache_key = cache_key
        self._loop = loop
        self._queue_max_frames = queue_max_frames
        self._max_synthesis_bytes = max_synthesis_bytes
        self._max_synthesis_duration_seconds = max_synthesis_duration_seconds
        self._synthesis_total_timeout_seconds = synthesis_total_timeout_seconds
        self._synthesis_idle_timeout_seconds = synthesis_idle_timeout_seconds
        self._on_provider_request = on_provider_request
        self._subscribers: set[_Subscription] = set()
        # A lagged same-loop subscriber and a cross-loop waiter stop receiving
        # live queue broadcasts, but both still depend on this provider request.
        # Track that consumer lease separately so another call's close cannot
        # cancel synthesis underneath them.
        self._consumer_count = 0
        self._consumer_lock = threading.Lock()
        self._candidate_frames: list[FrozenAudioFrame] = []
        self._candidate_bytes = 0
        self._candidate_duration_seconds = 0.0
        self._oversize = False
        self._state: GreetingCacheResultState = (
            "miss_cached" if cache_key is not None else "bypassed_personalized"
        )
        self._error: BaseException | None = None
        self._started_at_monotonic = time.monotonic()
        self._first_frame_at_monotonic: float | None = None
        self._completed_at_monotonic: float | None = None
        self._completion_future: ConcurrentFuture[_FlightCompletion] = ConcurrentFuture()
        self._producer: asyncio.Task[None] | None = None

    def start(self) -> None:
        if self._producer is not None:
            return
        self._producer = self._loop.create_task(
            self._produce(),
            name="vav_prepare_greeting_audio",
        )
        # A task cancelled before its first event-loop step never enters
        # ``_produce`` and therefore cannot run that coroutine's cleanup. Keep
        # the singleflight registry self-healing from outside the task too.
        self._producer.add_done_callback(self._on_producer_done)

    def _on_producer_done(self, producer: asyncio.Task[None]) -> None:
        if self._completion_future.done():
            # Retrieve any terminal exception so an abandoned provider task
            # cannot emit "Task exception was never retrieved" later.
            if not producer.cancelled():
                producer.exception()
            return
        if producer.cancelled():
            error: BaseException = asyncio.CancelledError(
                "Greeting synthesis was cancelled before it completed"
            )
        else:
            error = producer.exception() or RuntimeError(
                "Greeting synthesis ended without publishing a result"
            )
        self.abandon(error)

    def abandon(self, error: BaseException) -> None:
        """Publish one terminal failure even when provider cleanup is stuck."""

        if self._completion_future.done():
            return
        self._state = "failed"
        self._error = error
        self._candidate_frames.clear()
        self._candidate_bytes = 0
        self._candidate_duration_seconds = 0.0
        if self._cache_key is not None:
            self._cache._complete_flight(key=self._cache_key, flight=self, item=None)
        self._completed_at_monotonic = time.monotonic()
        self._completion_future.set_result(
            _FlightCompletion(
                state=self._state,
                item=None,
                error=self._error,
                started_at_monotonic=self._started_at_monotonic,
                first_frame_at_monotonic=self._first_frame_at_monotonic,
                completed_at_monotonic=self._completed_at_monotonic,
            )
        )
        # Abandonment is only used once no live subscriber should depend on the
        # provider task. Still release any waiter defensively and discard the
        # partial prefix rather than allowing truncated speech to play later.
        for subscription in tuple(self._subscribers):
            subscription.close()
            subscription.queue.put_nowait(_END)
        self._subscribers.clear()

    def can_subscribe(self, loop: asyncio.AbstractEventLoop) -> bool:
        # A late subscriber can replay the bounded candidate prefix. Once that
        # prefix is discarded for oversize audio, joining would lose speech.
        return loop is self._loop and not self._oversize and self._completed_at_monotonic is None

    def subscribe(self) -> _Subscription:
        if asyncio.get_running_loop() is not self._loop:
            raise RuntimeError("Greeting flight subscriptions must use the owning event loop")
        subscription = _Subscription(
            queue=asyncio.Queue(maxsize=self._queue_max_frames),
            snapshot=tuple(self._candidate_frames),
        )
        self._subscribers.add(subscription)
        return subscription

    def acquire_consumer(self) -> None:
        with self._consumer_lock:
            self._consumer_count += 1

    def release_consumer(self) -> bool:
        """Release one lease and report whether another consumer is still live."""

        with self._consumer_lock:
            if self._consumer_count > 0:
                self._consumer_count -= 1
            return self._consumer_count > 0

    def unsubscribe(self, subscription: _Subscription) -> bool:
        subscription.close()
        self._subscribers.discard(subscription)
        return self.release_consumer()

    async def _broadcast(self, item: FrozenAudioFrame | object) -> None:
        # The subscriber set is snapshotted before the first await. A late
        # subscriber receives the current frame in its retained prefix instead,
        # preventing either a gap or a duplicate.
        subscribers = tuple(self._subscribers)
        for subscription in subscribers:
            if not subscription.active:
                continue
            if self._cache_key is None:
                await subscription.queue.put(item)
                continue
            try:
                subscription.queue.put_nowait(item)
            except asyncio.QueueFull:
                # A static same-key call that has not begun playout must never
                # head-of-line block synthesis or every other call. Detach it;
                # its consumer resumes from the completed bounded cache by
                # frame ordinal (or privately re-synthesizes oversize audio).
                subscription.lagged = True
                subscription.close()
                subscription.queue.put_nowait(_END)
                self._subscribers.discard(subscription)

    def _consider_for_cache(self, frame: FrozenAudioFrame) -> None:
        if self._cache_key is None or self._oversize:
            return
        next_bytes = self._candidate_bytes + len(frame.data)
        next_duration = self._candidate_duration_seconds + frame.duration
        if (
            next_bytes > self._max_synthesis_bytes
            or next_duration > self._max_synthesis_duration_seconds
        ):
            self._oversize = True
            self._state = "miss_oversize"
            self._candidate_frames.clear()
            self._candidate_bytes = 0
            self._candidate_duration_seconds = 0.0
            return
        self._candidate_frames.append(frame)
        self._candidate_bytes = next_bytes
        self._candidate_duration_seconds = next_duration

    async def _consume_provider_stream(self, stream: Any) -> None:
        iterator = stream.__aiter__()
        deadline = self._loop.time() + self._synthesis_total_timeout_seconds
        while True:
            remaining = deadline - self._loop.time()
            if remaining <= 0:
                raise TimeoutError("Greeting TTS exceeded its total timeout")
            try:
                audio = await asyncio.wait_for(
                    anext(iterator),
                    timeout=min(self._synthesis_idle_timeout_seconds, remaining),
                )
            except StopAsyncIteration:
                break
            except TimeoutError as exc:
                raise TimeoutError("Greeting TTS stream stalled") from exc
            frozen = FrozenAudioFrame.from_frame(audio.frame)
            if self._first_frame_at_monotonic is None:
                self._first_frame_at_monotonic = time.monotonic()
            self._consider_for_cache(frozen)
            await self._broadcast(frozen)

    async def _produce(self) -> None:
        error: BaseException | None = None
        try:
            # Count a provider unit at the exact boundary where VAV invokes the
            # provider, not when a task is merely scheduled. A pre-start
            # cancellation is therefore correctly reported as zero requests.
            self._on_provider_request()
            capabilities = getattr(self._tts, "capabilities", None)
            stream_factory = getattr(self._tts, "stream", None)
            if getattr(capabilities, "streaming", False) is True and callable(stream_factory):
                # Inworld's streaming API uses its pooled WebSocket transport.
                # Preparing the already-required greeting on that path opens
                # the connection while the realtime session is starting, so a
                # later deterministic ``session.say`` can reuse it instead of
                # paying another cold connection setup. This does not add a
                # provider request; it changes only the transport used by the
                # existing greeting synthesis.
                async with stream_factory() as stream:
                    stream.push_text(self._text)
                    stream.end_input()
                    await self._consume_provider_stream(stream)
            else:
                # Retain compatibility with non-streaming TTS implementations.
                async with self._tts.synthesize(self._text) as stream:
                    await self._consume_provider_stream(stream)
        except BaseException as exc:
            error = exc

        item: CachedGreetingAudio | None = None
        if error is None and self._first_frame_at_monotonic is None:
            error = RuntimeError("Greeting TTS completed without returning audio")
        if error is not None:
            self._state = "failed"
            self._error = error
            self._candidate_frames.clear()
            self._candidate_bytes = 0
            self._candidate_duration_seconds = 0.0
        elif self._cache_key is not None and not self._oversize:
            item = CachedGreetingAudio(
                frames=tuple(self._candidate_frames),
                byte_count=self._candidate_bytes,
                duration_seconds=self._candidate_duration_seconds,
            )

        if self._cache_key is not None:
            cached = self._cache._complete_flight(key=self._cache_key, flight=self, item=item)
            if error is None and not self._oversize:
                self._state = "miss_cached" if cached else "miss_oversize"
        self._completed_at_monotonic = time.monotonic()
        completion = _FlightCompletion(
            state=self._state,
            item=item if self._state == "miss_cached" else None,
            error=self._error,
            started_at_monotonic=self._started_at_monotonic,
            first_frame_at_monotonic=self._first_frame_at_monotonic,
            completed_at_monotonic=self._completed_at_monotonic,
        )
        if not self._completion_future.done():
            self._completion_future.set_result(completion)

        # Cached audio now owns the immutable frames. The flight itself should
        # not retain an additional candidate collection after admission.
        self._candidate_frames.clear()
        self._candidate_bytes = 0
        self._candidate_duration_seconds = 0.0

        try:
            await self._broadcast(_END)
        except asyncio.CancelledError:
            for subscription in tuple(self._subscribers):
                subscription.close()
            raise

    @property
    def completion_future(self) -> ConcurrentFuture[_FlightCompletion]:
        return self._completion_future

    @property
    def state(self) -> GreetingCacheResultState:
        return self._state

    @property
    def error(self) -> BaseException | None:
        return self._error

    @property
    def started_at_monotonic(self) -> float:
        return self._started_at_monotonic

    @property
    def first_frame_at_monotonic(self) -> float | None:
        return self._first_frame_at_monotonic

    @property
    def completed_at_monotonic(self) -> float | None:
        return self._completed_at_monotonic

    @property
    def retained_byte_count(self) -> int:
        return self._candidate_bytes

    @property
    def producer(self) -> asyncio.Task[None] | None:
        return self._producer


class PreparedGreetingAudio:
    """One bounded greeting preparation, potentially sharing a static flight."""

    def __init__(
        self,
        *,
        tts_engine: Any,
        text: str,
        cache: GreetingAudioCache,
        cache_key: str | None,
        queue_max_frames: int,
        max_synthesis_bytes: int,
        max_synthesis_duration_seconds: float,
        synthesis_total_timeout_seconds: float,
        synthesis_idle_timeout_seconds: float,
        synthesis_cancel_timeout_seconds: float,
    ) -> None:
        if queue_max_frames < 1:
            raise ValueError("Greeting queue bound must be positive")
        if (
            max_synthesis_bytes < 1
            or max_synthesis_duration_seconds <= 0
            or synthesis_total_timeout_seconds <= 0
            or synthesis_idle_timeout_seconds <= 0
            or synthesis_cancel_timeout_seconds <= 0
        ):
            raise ValueError("Greeting synthesis bounds must be positive")

        self._tts = tts_engine
        self._text = text
        self._cache = cache
        self._cache_key = cache_key
        self._queue_max_frames = queue_max_frames
        self._max_synthesis_bytes = min(max_synthesis_bytes, cache._max_bytes)
        self._max_synthesis_duration_seconds = max_synthesis_duration_seconds
        self._synthesis_total_timeout_seconds = synthesis_total_timeout_seconds
        self._synthesis_idle_timeout_seconds = synthesis_idle_timeout_seconds
        self._synthesis_cancel_timeout_seconds = synthesis_cancel_timeout_seconds
        self._loop = asyncio.get_running_loop()
        self._flight: _GreetingFlight | None = None
        self._subscription: _Subscription | None = None
        self._cached: CachedGreetingAudio | None = None
        self._wait_for: ConcurrentFuture[_FlightCompletion] | None = None
        self._has_consumer_lease = False
        self._cancel_flight_on_close = False
        self._frames_yielded = 0
        self._frames_started = False
        self._closed = False
        self._provider_request_count = 0
        self._state: GreetingCacheResultState = (
            "miss_cached" if cache_key is not None else "bypassed_personalized"
        )
        now = time.monotonic()
        self._started_at_monotonic = now
        self._first_frame_at_monotonic: float | None = None
        self._completed_at_monotonic: float | None = None
        self._error: BaseException | None = None

        if cache_key is None:
            flight = self._new_flight(cache_key=None)
            self._attach_flight(flight, cancel_on_close=True)
            self._start_flight(flight)
            return

        resolution, value = cache._resolve(
            key=cache_key,
            loop=self._loop,
            factory=lambda: self._new_flight(cache_key=cache_key),
        )
        if resolution == "hit":
            assert isinstance(value, CachedGreetingAudio)
            self._cached = value
            self._state = "hit"
            self._first_frame_at_monotonic = now
            self._completed_at_monotonic = now
            return
        if resolution == "wait":
            assert isinstance(value, _GreetingFlight)
            self._flight = value
            self._wait_for = value.completion_future
            self._has_consumer_lease = True
            self._started_at_monotonic = value.started_at_monotonic
            return

        assert isinstance(value, _GreetingFlight)
        self._attach_flight(value, lease_acquired=True)
        if resolution == "leader":
            self._start_flight(value)

    def _new_flight(self, *, cache_key: str | None) -> _GreetingFlight:
        return _GreetingFlight(
            tts_engine=self._tts,
            text=self._text,
            cache=self._cache,
            cache_key=cache_key,
            loop=self._loop,
            queue_max_frames=self._queue_max_frames,
            max_synthesis_bytes=self._max_synthesis_bytes,
            max_synthesis_duration_seconds=self._max_synthesis_duration_seconds,
            synthesis_total_timeout_seconds=self._synthesis_total_timeout_seconds,
            synthesis_idle_timeout_seconds=self._synthesis_idle_timeout_seconds,
            on_provider_request=self._record_provider_request,
        )

    def _record_provider_request(self) -> None:
        self._provider_request_count += 1

    def _start_flight(self, flight: _GreetingFlight) -> None:
        flight.start()

    def _attach_flight(
        self,
        flight: _GreetingFlight,
        *,
        cancel_on_close: bool = False,
        lease_acquired: bool = False,
    ) -> None:
        if not lease_acquired:
            flight.acquire_consumer()
        self._flight = flight
        self._subscription = flight.subscribe()
        self._has_consumer_lease = True
        self._cancel_flight_on_close = cancel_on_close
        self._started_at_monotonic = flight.started_at_monotonic

    async def _yield_flight(self) -> AsyncIterator[rtc.AudioFrame]:
        assert self._flight is not None
        assert self._subscription is not None
        for frame in self._subscription.snapshot:
            self._frames_yielded += 1
            yield frame.thaw()

        while True:
            item = await self._subscription.queue.get()
            if item is _END:
                self._sync_from_flight()
                if self._error is not None:
                    raise self._error
                if self._subscription.lagged:
                    async for frame in self._yield_lagged_flight():
                        yield frame
                return
            assert isinstance(item, FrozenAudioFrame)
            self._frames_yielded += 1
            yield item.thaw()

    async def _yield_lagged_flight(self) -> AsyncIterator[rtc.AudioFrame]:
        """Resume a detached static subscriber without repeating played frames."""

        assert self._flight is not None
        completion = await asyncio.shield(asyncio.wrap_future(self._flight.completion_future))
        if completion.error is not None:
            raise completion.error
        already_played = self._frames_yielded
        if completion.item is not None:
            for frame in completion.item.frames[already_played:]:
                self._frames_yielded += 1
                yield frame.thaw()
            return

        # Oversize static audio is intentionally absent from the cache. Use a
        # private bounded synthesis and skip the exact prefix already played.
        self._release_consumer_lease()
        retry = self._new_flight(cache_key=None)
        self._attach_flight(retry, cancel_on_close=True)
        self._start_flight(retry)
        ordinal = 0
        async for frame in self._yield_flight():
            if ordinal >= already_played:
                yield frame
            ordinal += 1
        if ordinal < already_played:
            raise RuntimeError("Greeting retry returned fewer frames than already played")
        if retry.state != "failed":
            self._state = "miss_oversize"

    async def _yield_cross_loop_waiter(self) -> AsyncIterator[rtc.AudioFrame]:
        assert self._wait_for is not None
        completion = await asyncio.shield(asyncio.wrap_future(self._wait_for))
        self._state = completion.state
        self._started_at_monotonic = completion.started_at_monotonic
        self._first_frame_at_monotonic = completion.first_frame_at_monotonic
        self._completed_at_monotonic = completion.completed_at_monotonic
        self._error = completion.error
        if completion.error is not None:
            raise completion.error
        if completion.item is not None:
            for frame in completion.item.frames:
                self._frames_yielded += 1
                yield frame.thaw()
            return

        # Oversized audio is intentionally not retained. A consumer on another
        # event loop cannot join the owning loop's streaming queues, so it starts
        # a private bounded stream after the original flight completes.
        self._release_consumer_lease()
        retry = self._new_flight(cache_key=None)
        self._attach_flight(retry, cancel_on_close=True)
        self._start_flight(retry)
        async for frame in self._yield_flight():
            yield frame
        if retry.state != "failed":
            self._state = "miss_oversize"

    async def frames(self) -> AsyncIterator[rtc.AudioFrame]:
        if self._frames_started:
            raise RuntimeError("Prepared greeting audio can only be consumed once")
        self._frames_started = True

        if self._cached is not None:
            for frame in self._cached.frames:
                self._frames_yielded += 1
                yield frame.thaw()
            return
        if self._wait_for is not None:
            async for frame in self._yield_cross_loop_waiter():
                yield frame
            return
        async for frame in self._yield_flight():
            yield frame

    def _sync_from_flight(self) -> None:
        if self._flight is None:
            return
        self._state = self._flight.state
        self._error = self._flight.error
        self._started_at_monotonic = self._flight.started_at_monotonic
        self._first_frame_at_monotonic = self._flight.first_frame_at_monotonic
        self._completed_at_monotonic = self._flight.completed_at_monotonic

    def _release_consumer_lease(self) -> bool:
        if self._flight is None or not self._has_consumer_lease:
            return False
        self._has_consumer_lease = False
        if self._subscription is not None:
            has_other_consumers = self._flight.unsubscribe(self._subscription)
            self._subscription = None
            return has_other_consumers
        return self._flight.release_consumer()

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._flight is not None and self._has_consumer_lease:
            has_other_consumers = self._release_consumer_lease()
            # Once every consumer has gone away, keeping a provider request
            # alive only burns quota and can strand the singleflight entry.
            # A shared flight remains active while any other caller needs it.
            # A cross-loop waiter cannot safely cancel or await a task owned by
            # another loop. The owning consumer will cancel after its last peer
            # releases; otherwise the synthesis's own strict timeout applies.
            owns_flight_loop = asyncio.get_running_loop() is self._flight._loop
            producer = self._flight.producer
            if (
                owns_flight_loop
                and has_other_consumers
                and producer is not None
                and not producer.done()
                and self._provider_request_count == 0
            ):
                # The leader can close in the same scheduling turn in which it
                # created the flight. Let the already-queued producer cross its
                # invocation boundary before this call's usage is finalized;
                # otherwise the one shared provider unit would be attributed to
                # neither the departing leader nor its zero-cost followers.
                await asyncio.sleep(0)
            if owns_flight_loop and (self._cancel_flight_on_close or not has_other_consumers):
                if producer is not None and not producer.done():
                    producer.cancel()
                if producer is not None:
                    try:
                        done, _pending = await asyncio.wait(
                            {producer},
                            timeout=self._synthesis_cancel_timeout_seconds,
                        )
                    except asyncio.CancelledError:
                        self._flight.abandon(
                            asyncio.CancelledError(
                                "Greeting cleanup was cancelled before the provider stopped"
                            )
                        )
                        raise
                    if not done:
                        # Some provider transports swallow task cancellation in
                        # their stream cleanup. Detach them after a strict bound
                        # so call finalization and future same-key calls proceed.
                        self._flight.abandon(
                            TimeoutError("Greeting provider cleanup exceeded its timeout")
                        )
                    else:
                        # The done callback normally publishes the result. Call
                        # it directly as an idempotent safeguard before returning.
                        self._flight._on_producer_done(producer)
        self._sync_from_flight()

    @property
    def cache_status(self) -> GreetingCacheResultState:
        """Current result; it becomes final once ``completed_at_monotonic`` is set."""

        self._sync_from_flight()
        return self._state

    @property
    def failed_before_playout(self) -> bool:
        self._sync_from_flight()
        return self._error is not None and self._frames_yielded == 0

    @property
    def started_at_monotonic(self) -> float:
        self._sync_from_flight()
        return self._started_at_monotonic

    @property
    def first_frame_at_monotonic(self) -> float | None:
        self._sync_from_flight()
        return self._first_frame_at_monotonic

    @property
    def completed_at_monotonic(self) -> float | None:
        self._sync_from_flight()
        return self._completed_at_monotonic

    @property
    def retained_byte_count(self) -> int:
        if self._flight is None:
            return 0
        return self._flight.retained_byte_count

    @property
    def provider_request_count(self) -> int:
        """Number of provider syntheses attributable to this call (cache hits are zero)."""

        return self._provider_request_count


def prepare_greeting_audio(
    *,
    tts_engine: Any,
    text: str,
    cache_key: str | None,
    cache: GreetingAudioCache = GREETING_AUDIO_CACHE,
    queue_max_frames: int = DEFAULT_QUEUE_MAX_FRAMES,
    max_synthesis_bytes: int = DEFAULT_MAX_SYNTHESIS_BYTES,
    max_synthesis_duration_seconds: float = DEFAULT_MAX_SYNTHESIS_DURATION_SECONDS,
    synthesis_total_timeout_seconds: float = DEFAULT_SYNTHESIS_TOTAL_TIMEOUT_SECONDS,
    synthesis_idle_timeout_seconds: float = DEFAULT_SYNTHESIS_IDLE_TIMEOUT_SECONDS,
    synthesis_cancel_timeout_seconds: float = DEFAULT_SYNTHESIS_CANCEL_TIMEOUT_SECONDS,
) -> PreparedGreetingAudio:
    """Start bounded synthesis immediately and return an async playout source."""

    return PreparedGreetingAudio(
        tts_engine=tts_engine,
        text=text,
        cache=cache,
        cache_key=cache_key,
        queue_max_frames=queue_max_frames,
        max_synthesis_bytes=max_synthesis_bytes,
        max_synthesis_duration_seconds=max_synthesis_duration_seconds,
        synthesis_total_timeout_seconds=synthesis_total_timeout_seconds,
        synthesis_idle_timeout_seconds=synthesis_idle_timeout_seconds,
        synthesis_cancel_timeout_seconds=synthesis_cancel_timeout_seconds,
    )
