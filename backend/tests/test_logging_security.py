"""Regression coverage for callback-capability log redaction."""

import io
import logging

from app.core.logging_security import (
    CALLBACK_CLAIM_REDACTION,
    install_callback_claim_log_redaction,
    redact_twilio_callback_claims,
)
from app.tasks.worker import _install_worker_callback_claim_redaction


def test_redacts_callback_claim_from_urls_bytes_and_structured_fields():
    secret = "callback-secret-that-must-never-reach-logs"
    value = {
        "request_url": (
            f"https://api.example.test/hook?keep=1&vav_callback_claim={secret}&second=2"
        ),
        "nested": {
            "vav_callback_claim": secret,
            "pairs": [("vav_callback_claim", secret)],
        },
        "raw_query": f"vav_callback_claim={secret}".encode(),
        "encoded_url": (
            "Url=https%3A%2F%2Fapi.example.test%2Fhook%3F"
            f"VAV_CALLBACK_CLAIM%3D{secret}%26second%3D2"
        ),
        "double_encoded_url": (
            "Url=https%253A%252F%252Fapi.example.test%252Fhook%253F"
            f"vav_callback_claim%253D{secret}%2526second%253D2"
        ),
    }

    redacted = redact_twilio_callback_claims(value)

    assert secret not in repr(redacted)
    assert redacted["request_url"].endswith(
        f"vav_callback_claim={CALLBACK_CLAIM_REDACTION}&second=2"
    )
    assert redacted["nested"]["vav_callback_claim"] == CALLBACK_CLAIM_REDACTION
    assert redacted["nested"]["pairs"][0][1] == CALLBACK_CLAIM_REDACTION
    assert redacted["raw_query"] == (f"vav_callback_claim={CALLBACK_CLAIM_REDACTION}".encode())
    assert secret not in redacted["encoded_url"]
    assert f"VAV_CALLBACK_CLAIM%3D{CALLBACK_CLAIM_REDACTION}" in redacted["encoded_url"]
    assert secret not in redacted["double_encoded_url"]
    assert f"vav_callback_claim%253D{CALLBACK_CLAIM_REDACTION}" in redacted["double_encoded_url"]


def test_installed_record_factory_redacts_uvicorn_access_log_arguments():
    install_callback_claim_log_redaction()
    secret = "uvicorn-access-callback-secret"
    record = logging.getLogger("uvicorn.access").makeRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        (
            "127.0.0.1:1234",
            "POST",
            f"/api/v1/webhooks/twilio/voice/123?vav_callback_claim={secret}",
            "1.1",
            200,
        ),
        None,
    )

    rendered = record.getMessage()

    assert secret not in rendered
    assert f"vav_callback_claim={CALLBACK_CLAIM_REDACTION}" in rendered


def test_installed_formatter_redacts_rendered_exception_and_extra_url():
    secret = "exception-callback-secret"
    callback_url = f"https://api.example.test/hook?vav_callback_claim={secret}"
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s %(request_url)s"))
    logger = logging.getLogger("app.tests.callback-redaction")
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        # Reinstallation is intentional: lifespan startup uses it to wrap any
        # handlers a programmatic launcher added after app import.
        install_callback_claim_log_redaction()
        try:
            raise RuntimeError(callback_url)
        except RuntimeError:
            logger.exception(
                "callback failed: %s",
                callback_url,
                extra={"request_url": callback_url},
            )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    rendered = stream.getvalue()
    assert secret not in rendered
    assert rendered.count(CALLBACK_CLAIM_REDACTION) >= 2


def test_installation_is_idempotent_for_existing_handlers():
    handler = logging.StreamHandler(io.StringIO())
    logger = logging.getLogger("app.tests.callback-redaction-idempotence")
    logger.addHandler(handler)
    try:
        install_callback_claim_log_redaction()
        first_formatter = handler.formatter
        install_callback_claim_log_redaction()
        assert handler.formatter is first_formatter
    finally:
        logger.removeHandler(handler)


def test_celery_logger_setup_redacts_handler_added_after_worker_import():
    secret = "late-celery-handler-callback-secret"
    callback_url = (
        f"Url=https%3A%2F%2Fapi.example.test%2Fhook%3Fvav_callback_claim%3D{secret}%26keep%3D1"
    )
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger("celery.task.vav-redaction-regression")
    previous_level = logger.level
    previous_propagate = logger.propagate
    logger.setLevel(logging.ERROR)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        _install_worker_callback_claim_redaction()
        try:
            raise RuntimeError(callback_url)
        except RuntimeError:
            logger.exception("campaign provider failed")
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous_level)
        logger.propagate = previous_propagate

    rendered = stream.getvalue()
    assert secret not in rendered
    assert CALLBACK_CLAIM_REDACTION in rendered
