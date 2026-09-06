"""Optional call-scoped HTTP preparation; never generates speculative model output."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

import httpx
from openai import AsyncOpenAI

REPAIR_TRANSPORT_FLAG = "knowledge_repair_transport_enabled"


def _client(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=api_key,
        timeout=2.0,
        max_retries=0,
        http_client=httpx.AsyncClient(
            limits=httpx.Limits(
                max_connections=2, max_keepalive_connections=2, keepalive_expiry=120
            )
        ),
    )


class RepairTransport:
    """One tenant/call, one preparation, no cross-call credentials or answer cache.

    Foreground requests never await preparation. A changed credential or failed
    preparation falls back to the interpreter's existing owned-client path.
    The models-list read only prepares HTTP transport; it generates zero tokens.
    Reuse does not prove that the provider kept the underlying socket alive.
    """

    def __init__(self, metrics: dict[str, Any], *, client_factory=_client):
        self._metrics = metrics
        self._factory = client_factory
        self._task: asyncio.Task | None = None
        self._client: Any = None
        self._key: str | None = None
        self._closed = False
        self._ready = False

    def start(self, load_key: Callable[[], Awaitable[str]]) -> None:
        if not self._closed and self._task is None:
            self._task = asyncio.create_task(self._prepare(load_key), name="vav_repair_transport")

    async def _prepare(self, load_key: Callable[[], Awaitable[str]]) -> None:
        started = perf_counter()
        self._metrics["knowledge_repair_transport_status"] = "preparing"
        try:
            async with asyncio.timeout(2.0):
                key = await load_key()
                if not key:
                    self._metrics["knowledge_repair_transport_status"] = "unavailable"
                    return
                self._client = self._factory(key)
                self._key = key
                await self._client.models.list()
                self._ready = True
                self._metrics["knowledge_repair_transport_status"] = "prepared"
        except asyncio.CancelledError:
            self._metrics["knowledge_repair_transport_status"] = "cancelled"
            raise
        except TimeoutError:
            self._metrics["knowledge_repair_transport_status"] = "timeout"
        except Exception:
            self._metrics["knowledge_repair_transport_status"] = "unavailable"
        finally:
            self._metrics["knowledge_repair_transport_prepare_ms"] = round(
                (perf_counter() - started) * 1000
            )

    def client_for(self, api_key: str):
        if self._closed or not self._ready or not api_key or api_key != self._key:
            return None
        field = "knowledge_repair_transport_client_uses"
        self._metrics[field] = int(self._metrics.get(field, 0)) + 1
        return self._client

    async def aclose(self) -> None:
        self._closed = True
        self._ready = False
        if self._task is not None:
            if not self._task.done():
                self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        client, self._client = self._client, None
        self._key = None
        if client is not None:
            try:
                async with asyncio.timeout(2.0):
                    await client.close()
            except Exception:
                self._metrics["knowledge_repair_transport_close_failed"] = True
