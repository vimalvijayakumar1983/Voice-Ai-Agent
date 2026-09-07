"""Use LiveKit's native idle/interruption scheduler, with no extra LLM pass."""

from contextlib import asynccontextmanager
from weakref import WeakSet

from livekit.agents import RunContext


class LookupFiller:
    """One cue per speech turn, shared by all MCP tools in this session."""

    def __init__(self, metrics: dict, language: str = "en"):
        self.metrics = metrics
        self.language = language.lower().split("-")[0]
        self._announced = WeakSet()

    @asynccontextmanager
    async def pending(self, context: RunContext | None):
        phrases = {
            "en": "I'm checking the requested information.",
            "ar": "أتحقق من المعلومات المطلوبة.",
            "hi": "मैं मांगी गई जानकारी जाँच रही हूँ।",
            "ml": "ആവശ്യപ്പെട്ട വിവരങ്ങൾ പരിശോധിക്കുകയാണ്.",
        }
        phrase = phrases.get(self.language)
        if context is None or phrase is None or not callable(getattr(context, "with_filler", None)):
            self.metrics["mcp_filler_supported"] = False
            yield
            return
        self.metrics["mcp_filler_supported"] = True
        handle = None

        def clear(_handle):
            self.metrics["mcp_filler_active"] = False

        def speak(_step):
            nonlocal handle
            parent = context.speech_handle
            if parent.interrupted or parent in self._announced:
                return None
            self._announced.add(parent)
            # Set before say(): server speaking events must not count this as an answer.
            self.metrics["mcp_filler_active"] = True
            try:
                handle = context.session.say(
                    phrase, allow_interruptions=True, add_to_chat_ctx=False
                )
                handle.add_done_callback(clear)
                self.metrics["mcp_filler_scheduled_count"] = (
                    self.metrics.get("mcp_filler_scheduled_count", 0) + 1
                )
                return handle
            except Exception:
                clear(None)
                self.metrics["mcp_filler_error_count"] = (
                    self.metrics.get("mcp_filler_error_count", 0) + 1
                )
                return None  # Optional speech failure must not prevent the lookup.

        try:
            async with context.with_filler(speak, delay=1.5, max_steps=1):
                yield
        finally:
            # Native scope exit cancels the timer, but not speech already scheduled.
            # Explicitly cancel that speech so the real result cannot queue behind it.
            if handle is not None:
                if not handle.done():
                    handle.interrupt()
                    await handle
                clear(handle)
