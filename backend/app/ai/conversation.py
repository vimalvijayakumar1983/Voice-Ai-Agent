"""AI conversation engine - abstraction over LLM providers."""

import inspect
from collections.abc import AsyncIterator
from dataclasses import dataclass

import structlog
from openai import AsyncOpenAI

from app.core.config import settings

logger = structlog.get_logger()


@dataclass(frozen=True)
class ResponseStreamEvent:
    """One text delta, or the terminal usage event, from an LLM stream."""

    text: str = ""
    tokens_used: int = 0
    is_final: bool = False


class ConversationEngine:
    """Manages AI-driven voice conversations."""

    def __init__(self, *, api_key: str | None = None):
        self._api_key = api_key
        self._openai: AsyncOpenAI | None = None

    @property
    def openai(self) -> AsyncOpenAI:
        if not self._openai:
            self._openai = AsyncOpenAI(api_key=self._api_key or settings.openai_api_key)
        return self._openai

    async def generate_response(
        self,
        system_prompt: str,
        conversation_history: list[dict],
        model: str = "gpt-4o",
        temperature: float = 0.7,
        max_tokens: int = 500,
        knowledge_context: str | None = None,
    ) -> tuple[str, int]:
        """Generate an AI response for a voice conversation.

        Returns (response_text, tokens_used).
        """
        messages = [{"role": "system", "content": system_prompt}]

        if knowledge_context:
            messages.append(
                {
                    "role": "system",
                    "content": f"Relevant knowledge base context:\n{knowledge_context}",
                }
            )

        messages.extend(conversation_history)

        response = await self.openai.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )

        content = response.choices[0].message.content or ""
        tokens_used = response.usage.total_tokens if response.usage else 0

        logger.info(
            "ai_response_generated",
            model=model,
            tokens=tokens_used,
            response_length=len(content),
        )

        return content, tokens_used

    async def stream_response(
        self,
        system_prompt: str,
        conversation_history: list[dict],
        model: str = "gpt-4o",
        temperature: float = 0.7,
        max_tokens: int = 500,
        knowledge_context: str | None = None,
    ) -> AsyncIterator[ResponseStreamEvent]:
        """Yield response text as OpenAI produces it, followed by usage.

        The terminal event is always emitted after a successful stream, even
        when the provider omits token usage. Cancelling a voice turn closes
        the upstream HTTP stream promptly instead of continuing billable work.
        """
        messages = [{"role": "system", "content": system_prompt}]
        if knowledge_context:
            messages.append(
                {
                    "role": "system",
                    "content": f"Relevant knowledge base context:\n{knowledge_context}",
                }
            )
        messages.extend(conversation_history)

        stream = await self.openai.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        response_length = 0
        tokens_used = 0
        try:
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    tokens_used = int(getattr(usage, "total_tokens", 0) or 0)
                for choice in getattr(chunk, "choices", []) or []:
                    delta = getattr(getattr(choice, "delta", None), "content", None)
                    if isinstance(delta, str) and delta:
                        response_length += len(delta)
                        yield ResponseStreamEvent(text=delta)
            logger.info(
                "ai_response_streamed",
                model=model,
                tokens=tokens_used,
                response_length=response_length,
            )
            yield ResponseStreamEvent(tokens_used=tokens_used, is_final=True)
        finally:
            close = getattr(stream, "close", None)
            if callable(close):
                result = close()
                if inspect.isawaitable(result):
                    await result

    async def generate_call_summary(self, transcript_text: str, model: str = "gpt-4o") -> dict:
        """Generate a structured summary from a call transcript."""
        system_prompt = """Analyze this call transcript and return a JSON object with:
- "summary": A concise 2-3 sentence summary of the call
- "key_topics": A list of key topics discussed
- "action_items": A list of action items or follow-ups
- "sentiment": One of "positive", "neutral", "negative"
- "disposition": One of "interested", "not_interested", "callback", "dnc", "voicemail", "unknown"

Return ONLY valid JSON, no markdown."""

        response = await self.openai.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": transcript_text},
            ],
            temperature=0.3,
            max_tokens=500,
            response_format={"type": "json_object"},
        )

        import json

        content = response.choices[0].message.content or "{}"
        return json.loads(content)


conversation_engine = ConversationEngine()
