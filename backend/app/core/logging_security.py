"""Process-wide redaction for callback capabilities in application logs.

Twilio signs the complete callback URL, so the per-dispatch callback claim has
to remain in the URL received by the application.  That makes ordinary HTTP
access logging a potential disclosure boundary.  Install this module before
the ASGI app starts accepting traffic so both Uvicorn access records and VAV's
structured application records are scrubbed before a handler can emit them.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

TWILIO_CALLBACK_CLAIM_LOG_KEY = "vav_callback_claim"
CALLBACK_CLAIM_REDACTION = "[REDACTED]"

_CALLBACK_CLAIM_TEXT_PATTERN = re.compile(
    r"(?i)(?P<prefix>(?:[?&]|\b[\"']?)vav_callback_claim[\"']?\s*(?:=|:)\s*[\"']?)"
    r"(?P<value>[^&\s,}\"']*)"
)


def redact_twilio_callback_claim_text(value: str) -> str:
    """Remove callback-claim values from URLs and rendered structured logs."""

    if TWILIO_CALLBACK_CLAIM_LOG_KEY not in value.lower():
        return value
    return _CALLBACK_CLAIM_TEXT_PATTERN.sub(
        lambda match: f"{match.group('prefix')}{CALLBACK_CLAIM_REDACTION}",
        value,
    )


def _is_callback_claim_key(value: object) -> bool:
    if isinstance(value, bytes):
        try:
            value = value.decode("ascii")
        except UnicodeDecodeError:
            return False
    return isinstance(value, str) and value.lower() == TWILIO_CALLBACK_CLAIM_LOG_KEY


def redact_twilio_callback_claims(value: Any, *, _depth: int = 0) -> Any:
    """Return a log-safe copy of common lazy-logging argument structures."""

    if isinstance(value, str):
        return redact_twilio_callback_claim_text(value)
    if isinstance(value, bytes):
        if b"vav_callback_claim" not in value.lower():
            return value
        decoded = value.decode("utf-8", errors="surrogateescape")
        return redact_twilio_callback_claim_text(decoded).encode(
            "utf-8",
            errors="surrogateescape",
        )
    if isinstance(value, bytearray):
        return bytearray(redact_twilio_callback_claims(bytes(value)))

    # Log payloads are expected to be shallow. Fail closed at excessive depth
    # instead of recursively rendering a secret-bearing or cyclic object.
    if _depth >= 12:
        return "[REDACTED-DEEP-LOG-VALUE]"
    if isinstance(value, Mapping):
        return {
            redact_twilio_callback_claims(key, _depth=_depth + 1): (
                CALLBACK_CLAIM_REDACTION
                if _is_callback_claim_key(key)
                else redact_twilio_callback_claims(item, _depth=_depth + 1)
            )
            for key, item in value.items()
        }
    if isinstance(value, tuple):
        if len(value) == 2 and _is_callback_claim_key(value[0]):
            return (value[0], CALLBACK_CLAIM_REDACTION)
        return tuple(redact_twilio_callback_claims(item, _depth=_depth + 1) for item in value)
    if isinstance(value, list):
        if len(value) == 2 and _is_callback_claim_key(value[0]):
            return [value[0], CALLBACK_CLAIM_REDACTION]
        return [redact_twilio_callback_claims(item, _depth=_depth + 1) for item in value]
    if isinstance(value, set):
        return {redact_twilio_callback_claims(item, _depth=_depth + 1) for item in value}
    if isinstance(value, frozenset):
        return frozenset(redact_twilio_callback_claims(item, _depth=_depth + 1) for item in value)
    return value


def _redact_log_record(record: logging.LogRecord) -> logging.LogRecord:
    for key, value in tuple(record.__dict__.items()):
        # The formatter wrapper scrubs the final rendered traceback. Mutating
        # exc_info here would alter exception objects owned by application code.
        if key == "exc_info":
            continue
        record.__dict__[key] = redact_twilio_callback_claims(value)
    return record


class _CallbackClaimRedactingFormatter(logging.Formatter):
    """Wrap a configured formatter and scrub its final rendered output."""

    def __init__(self, delegate: logging.Formatter | None) -> None:
        super().__init__()
        self.delegate = delegate or logging.Formatter()

    def format(self, record: logging.LogRecord) -> str:
        return redact_twilio_callback_claim_text(self.delegate.format(record))


def _install_record_factory() -> None:
    current_factory = logging.getLogRecordFactory()
    if getattr(current_factory, "_vav_callback_claim_redacting_factory", False):
        return

    def redacting_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        return _redact_log_record(current_factory(*args, **kwargs))

    redacting_factory._vav_callback_claim_redacting_factory = True  # type: ignore[attr-defined]
    logging.setLogRecordFactory(redacting_factory)


def _wrap_configured_formatters() -> None:
    loggers: list[logging.Logger] = [logging.getLogger()]
    loggers.extend(
        logger
        for logger in logging.Logger.manager.loggerDict.values()
        if isinstance(logger, logging.Logger)
    )
    seen_handlers: set[int] = set()
    for logger in loggers:
        for handler in logger.handlers:
            handler_identity = id(handler)
            if handler_identity in seen_handlers:
                continue
            seen_handlers.add(handler_identity)
            if not isinstance(handler.formatter, _CallbackClaimRedactingFormatter):
                handler.setFormatter(_CallbackClaimRedactingFormatter(handler.formatter))


def install_callback_claim_log_redaction() -> None:
    """Install idempotent process-wide callback-capability log redaction."""

    _install_record_factory()
    # Uvicorn configures its handlers before importing ``app.main`` in the
    # production command. Calling this again during lifespan startup also
    # catches handlers installed by programmatic ASGI launchers.
    _wrap_configured_formatters()
