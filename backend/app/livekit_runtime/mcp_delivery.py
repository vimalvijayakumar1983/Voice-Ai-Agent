"""Preserve MCP sources and deliver separately validated spoken presentations.

Presentation calculations belong to the typed report compiler, never a freeform LLM.
Only authorised results enter this boundary. Private source text is retained in the
call transcript, not metrics, logs, the LLM tool output or the LLM chat history.
"""

import asyncio
import hashlib
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from livekit.agents import RunContext, llm

from app.livekit_runtime.mcp_request_context import forecast_requested
from app.livekit_runtime.report_presentation import ReportPresenter


def source_text(result: dict) -> str:
    """Prefer the server's text response; don't double-read its structured duplicate."""
    if result.get("status") != "ok" or not isinstance(result.get("data"), dict):
        raise ValueError("Successful source response required")
    data = result["data"]
    blocks = data.get("text", [])
    if not isinstance(blocks, list) or any(not isinstance(text, str) for text in blocks):
        raise ValueError("Invalid source text")
    if any(text.strip() for text in blocks):
        return "\n\n".join(blocks)
    if data.get("structured") is not None:
        # JSON labels/values remain intact; never infer prose or totals from rows.
        return json.dumps(data["structured"], ensure_ascii=False, indent=2, allow_nan=False)
    raise ValueError("Empty source response")


class SourceDelivery:
    def __init__(self, metrics: dict, append_source: Callable[[dict], None], *, presenter=None):
        self.metrics = metrics
        self.append_source = append_source
        self._lock = asyncio.Lock()
        self.presenter = presenter or ReportPresenter(metrics)

    async def deliver(
        self,
        context: RunContext | None,
        result: dict,
        *,
        tool: str,
        authorize: Callable[[], Awaitable[None]] | None = None,
        question: str = "",
        arguments: dict | None = None,
        company: str = "",
        scope: str = "",
        forecast_loader=None,
        timezone="UTC",
    ) -> None:
        """Stop automatic tool continuation even if source playback cannot start."""
        entry = None
        try:
            text = source_text(result)
            if context is None or context.session.tts is None:
                # Never fall back to a generative realtime say() implementation.
                self.metrics["mcp_source_delivery_state"] = "tts_unavailable"
                return
            async with self._lock:
                if context.speech_handle.interrupted:
                    self.metrics["mcp_source_delivery_state"] = "superseded"
                    return
                if authorize is not None:
                    await authorize()  # Recheck after waiting behind another tool's speech.
                # Permission checks yield; the caller may have moved to a new turn.
                if context.speech_handle.interrupted:
                    self.metrics["mcp_source_delivery_state"] = "superseded"
                    return
                entry = {
                    "role": "source",
                    "content": text,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "source_tool": tool,
                    "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "delivery_state": "preparing",
                }
                self.append_source(entry)
                forecast = None
                if forecast_loader is not None and forecast_requested(question):
                    forecast = await forecast_loader()
                    if forecast:
                        baseline_text = source_text(forecast["result"])
                        entry["forecast_source"] = {
                            "content": baseline_text,
                            "arguments": forecast["arguments"],
                            "source_sha256": hashlib.sha256(baseline_text.encode()).hexdigest(),
                        }
                        forecast = {"source": baseline_text, "arguments": forecast["arguments"]}
                presentation = await self.presenter.present(
                    text,
                    question=question,
                    arguments=arguments,
                    company=company,
                    scope=scope,
                    forecast=forecast,
                    timezone=timezone,
                )
                # The model only selects server-validated sentences. Recheck
                # permissions and interruption after the bounded presentation call.
                if authorize is not None:
                    await authorize()
                if context.speech_handle.interrupted:
                    entry["delivery_state"] = "superseded"
                    self.metrics["mcp_source_delivery_state"] = "superseded"
                    return
                entry.update(
                    {
                        "presentation_text": presentation["text"],
                        "presentation_version": presentation["version"],
                        "presentation_kind": presentation["kind"],
                        "presentation_state": presentation["state"],
                        "presentation_sentence_ids": presentation["sentence_ids"],
                        "presentation_fact_sentences": presentation["fact_sentences"],
                        "delivery_state": "scheduled",
                    }
                )
                speech = presentation["text"]
                audio = _SourceAudio(context.session, speech)
                frames = audio.frames()
                handle = context.session.say(
                    speech, audio=frames, allow_interruptions=True, add_to_chat_ctx=False
                )
                try:
                    await handle
                    failure = handle.exception()
                    if failure is not None:
                        raise failure
                    # LiveKit 1.6 can log say() task failures without exposing
                    # them on SpeechHandle.exception(). Observe our own stream
                    # completion, not private SDK task internals.
                    if not handle.interrupted and (not audio.completed or not audio.frame_count):
                        raise RuntimeError("Source audio did not complete")
                except asyncio.CancelledError:
                    entry["delivery_state"] = "interrupted"
                    raise
                except Exception:
                    entry["delivery_state"] = "failed"
                    raise
                else:
                    entry["delivery_state"] = "interrupted" if handle.interrupted else "finished"
                finally:
                    if not handle.done():
                        handle.interrupt()
                        # LiveKit owns and closes the iterator while interrupting.
                    else:
                        await frames.aclose()
                self.metrics["mcp_source_delivery_state"] = entry["delivery_state"]
                self.metrics["mcp_source_delivery_count"] = (
                    self.metrics.get("mcp_source_delivery_count", 0) + 1
                )
        except asyncio.CancelledError:
            if entry is not None:
                entry["delivery_state"] = "interrupted"
            raise
        except Exception:
            # Source data must not escape through exception text or model fallback.
            self.metrics["mcp_source_delivery_state"] = "failed"
            if entry is not None:
                entry["delivery_state"] = "failed"
            if context is not None and context.session.tts is not None:
                if not context.speech_handle.interrupted:
                    await context.session.say(
                        "I couldn't deliver the original report. Please try the request again.",
                        allow_interruptions=True,
                        add_to_chat_ctx=False,
                    )
        finally:
            # StopResponse is LiveKit's supported no-model-continuation tool result.
            # Cancellation must still propagate so stale turns are not resurrected.
            if not asyncio.current_task().cancelling():
                raise llm.StopResponse()


class _SourceAudio:
    """Track literal TTS generation through public SDK APIs, including silent failures."""

    def __init__(self, session, text: str):
        self.session = session
        self.text = text
        self.completed = False
        self.frame_count = 0

    async def frames(self):
        engine = self.session.tts
        options = self.session.conn_options.tts_conn_options
        if engine.capabilities.streaming:
            async with engine.stream(conn_options=options) as stream:
                stream.push_text(self.text)
                stream.end_input()
                async for event in stream:
                    self.frame_count += 1
                    yield event.frame
        else:
            async with engine.synthesize(self.text, conn_options=options) as stream:
                async for event in stream:
                    self.frame_count += 1
                    yield event.frame
        self.completed = True
