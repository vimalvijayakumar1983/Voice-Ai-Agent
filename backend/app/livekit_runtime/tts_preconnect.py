"""Open Inworld's pooled transport without synthesizing throwaway speech.

LiveKit Inworld 1.6.10 prewarm() only allocates a pool. A public stream with no
text opens and closes a context, retaining the pooled socket for the real reply.
Start only after durable call admission; never wait for this on the reply path.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from livekit.agents.types import APIConnectOptions


async def preconnect_tts_transport(
    engine: Any,
    metrics: dict[str, Any],
    *,
    timeout_seconds: float = 3.0,
) -> None:
    started = time.monotonic()
    metrics["tts_transport_preconnect_status"] = "started"
    metrics["tts_transport_preconnect_text_characters"] = 0
    try:
        async with asyncio.timeout(timeout_seconds):
            async with engine.stream(
                conn_options=APIConnectOptions(timeout=timeout_seconds, max_retry=0)
            ) as stream:
                # No push_text, dummy synthesis, caller audio or cache mutation.
                stream.end_input()
                async for _ in stream:
                    # A provider protocol change must not play unexpected audio.
                    metrics["tts_transport_preconnect_status"] = "unexpected_audio"
                    return
        metrics["tts_transport_preconnect_status"] = "completed"
    except TimeoutError:
        metrics["tts_transport_preconnect_status"] = "timeout"
    except asyncio.CancelledError:
        metrics["tts_transport_preconnect_status"] = "cancelled"
        raise
    except Exception:
        # The ordinary reply keeps the SDK's own retry/connection behavior.
        # Never expose provider exceptions (which can contain credentials).
        metrics["tts_transport_preconnect_status"] = "failed"
    finally:
        metrics["tts_transport_preconnect_ms"] = round((time.monotonic() - started) * 1000)
