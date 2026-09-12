"""Request-local Soniox diagnostics and an opt-in knowledge-history experiment.

Never mutates session history, changes retrieval, or estimates billed tokens.
"""

import asyncio
import time

from livekit.agents import llm

CONTEXT_FLAG = "soniox_knowledge_context_window_v1"
MAX_REQUEST_RECORDS = 128
KNOWLEDGE_TOOL = "search_approved_knowledge"
OMITTED_EVIDENCE = (
    "Earlier knowledge search output omitted from this request. "
    "Search approved knowledge again if those facts are needed."
)


def context_window_enabled(metadata, *, has_mcp_tools: bool, tools_only: bool) -> bool:
    return (
        isinstance(metadata, dict)
        and metadata.get(CONTEXT_FLAG) is True
        and not has_mcp_tools
        and not tools_only
    )


def context_counts(chat_ctx: llm.ChatContext) -> dict[str, int]:
    """Character counts, not tokenizer estimates; do not record private content."""
    counts = {"instruction_chars": 0, "message_chars": 0, "tool_output_chars": 0}
    for item in chat_ctx.items:
        if isinstance(item, llm.ChatMessage):
            key = "instruction_chars" if item.role in {"system", "developer"} else "message_chars"
            counts[key] += len(item.text_content or "")
        elif isinstance(item, llm.FunctionCallOutput):
            counts["tool_output_chars"] += len(item.output)
    return counts


def request_context(chat_ctx: llm.ChatContext, *, enabled: bool):
    """Keep current and preceding user-turn evidence, all dialogue and tool pairs.

    Only successful, paired KB result bodies older than those turns are replaced.
    Errors, unknown tools, pending calls, MCP data and current evidence stay exact.
    No model rewrite or summarizer is introduced. A new search remains mandatory.
    """
    if not enabled:
        return chat_ctx, 0
    users = [
        i
        for i, item in enumerate(chat_ctx.items)
        if isinstance(item, llm.ChatMessage) and item.role == "user"
    ]
    if len(users) < 3:
        return chat_ctx, 0
    cutoff = users[-2]
    calls = {
        item.call_id: (i, item.name)
        for i, item in enumerate(chat_ctx.items)
        if isinstance(item, llm.FunctionCall)
    }
    copied = chat_ctx.copy()
    removed = 0
    for i, item in enumerate(copied.items):
        if not isinstance(item, llm.FunctionCallOutput) or item.is_error or i >= cutoff:
            continue
        call = calls.get(item.call_id)
        if not call or call[0] >= cutoff or call[1] != KNOWLEDGE_TOOL:
            continue
        if item.name not in {"", KNOWLEDGE_TOOL} or len(item.output) <= len(OMITTED_EVIDENCE):
            continue
        copied.items[i] = item.model_copy(update={"output": OMITTED_EVIDENCE})
        removed += len(item.output) - len(OMITTED_EVIDENCE)
    return copied, removed


async def observe_requests(
    chunks, *, metrics: dict, chat_ctx, removed_chars: int, turn_number: int | None = None
):
    """Observe the existing stream without buffering speech or adding a model pass."""
    started = time.perf_counter()
    sequence = int(metrics.get("pipeline_llm_request_count", 0)) + 1
    metrics["pipeline_llm_request_count"] = sequence
    user_items = [
        item for item in chat_ctx.items if isinstance(item, llm.ChatMessage) and item.role == "user"
    ]
    record = {
        "sequence": sequence,
        "turn": turn_number,
        "user_item_id": user_items[-1].id if user_items else None,
        "status": "running",
        **context_counts(chat_ctx),
        "omitted_history_chars": removed_chars,
        "first_text_ms": None,
        "first_tool_call_ms": None,
        "duration_ms": None,
        "prompt_tokens": None,
        "cached_prompt_tokens": None,
        "completion_tokens": None,
    }
    records = metrics.setdefault("pipeline_llm_requests", [])
    records.append(record)
    if len(records) > MAX_REQUEST_RECORDS:
        del records[:-MAX_REQUEST_RECORDS]
        metrics["pipeline_llm_requests_truncated"] = True
    try:
        async for chunk in chunks:
            elapsed = round((time.perf_counter() - started) * 1000, 1)
            delta = chunk.delta if isinstance(chunk, llm.ChatChunk) else None
            if (isinstance(chunk, str) and chunk) or (delta and delta.content):
                if record["first_text_ms"] is None:
                    record["first_text_ms"] = elapsed
            if delta and delta.tool_calls and record["first_tool_call_ms"] is None:
                record["first_tool_call_ms"] = elapsed
            if isinstance(chunk, llm.ChatChunk) and chunk.usage is not None:
                # Provider usage is a request snapshot, not an incremental count.
                record["prompt_tokens"] = chunk.usage.prompt_tokens
                record["cached_prompt_tokens"] = chunk.usage.prompt_cached_tokens
                record["completion_tokens"] = chunk.usage.completion_tokens
            yield chunk
        record["status"] = "completed"
    except (asyncio.CancelledError, GeneratorExit):
        record["status"] = "cancelled"
        raise
    except Exception:
        record["status"] = "failed"
        raise
    finally:
        record["duration_ms"] = round((time.perf_counter() - started) * 1000, 1)
        # Cancelled consumers must not leave an upstream paid generation running.
        close = getattr(chunks, "aclose", None)
        if close is not None:
            await close()
