"""Small, process-local last-known-good cache for normalized voice catalogs."""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Callable
from copy import deepcopy
from threading import Lock
from time import monotonic
from typing import Any


class AgentCatalogSnapshotCache:
    """Keep bounded, short-lived catalog snapshots without external infrastructure.

    The caller decides which provider failures are safe to mask with a snapshot.
    Authentication and permission failures should still surface to operators. A
    snapshot is accepted only when it contains at least one selectable voice, so
    a transient empty/unknown response cannot replace the last known good data.

    This cache is intentionally process-local. It is a resilience aid for a
    single API worker, not a source of truth shared across a deployment.
    """

    def __init__(
        self,
        *,
        max_entries: int = 4,
        ttl_seconds: float = 300,
        clock: Callable[[], float] = monotonic,
    ) -> None:
        if max_entries < 1:
            raise ValueError("max_entries must be at least 1")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._entries: OrderedDict[str, tuple[float, list[dict[str, Any]]]] = OrderedDict()
        self._lock = Lock()

    def remember(self, key: str, voices: list[dict[str, Any]]) -> bool:
        """Store a defensive copy when the catalog has a usable voice."""

        if not key or not any(voice.get("synthesizer_model") for voice in voices):
            return False

        snapshot = deepcopy(voices)
        with self._lock:
            self._entries[key] = (self._clock(), snapshot)
            self._entries.move_to_end(key)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)
        return True

    def get(self, key: str) -> list[dict[str, Any]] | None:
        """Return a defensive copy of a fresh snapshot, or ``None``."""

        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            stored_at, voices = entry
            if self._clock() - stored_at >= self._ttl_seconds:
                del self._entries[key]
                return None
            self._entries.move_to_end(key)
            return deepcopy(voices)

    def clear(self) -> None:
        """Drop all snapshots; useful for explicit invalidation and tests."""

        with self._lock:
            self._entries.clear()


PUBLIC_CATALOG_CACHE_KEY = "smallest:public-voice-catalog"
public_agent_catalog_cache = AgentCatalogSnapshotCache()
