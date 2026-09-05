"""Regression coverage for callback-capability log redaction."""

import io
import logging
from unittest.mock import Mock

import pytest

from app.core.logging_security import (
    CALLBACK_CLAIM_REDACTION,
    install_callback_claim_log_redaction,
    redact_twilio_callback_claims,
)
from app.tasks import campaign_tasks
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


def test_campaign_task_structlog_failure_redacts_encoded_callback_claim(
    capsys,
    monkeypatch,
):
    secret = "campaign-structlog-callback-secret"
    encoded_callback_url = (
        "Url=https%3A%2F%2Fvoice.example.test%2Fcallback%3F"
        f"vav_callback_claim%3D{secret}%26keep%3D1"
    )
    provider_error = RuntimeError(f"provider echoed {encoded_callback_url}")
    retry_scheduled = RuntimeError("retry scheduled")

    async def fail_campaign(_campaign_id, _tenant_id):
        raise provider_error

    monkeypatch.setattr(campaign_tasks, "_run_campaign_async", fail_campaign)
    retry = Mock(return_value=retry_scheduled)
    monkeypatch.setattr(campaign_tasks.run_campaign, "retry", retry)

    with pytest.raises(RuntimeError, match="retry scheduled") as exc_info:
        campaign_tasks.run_campaign.run("campaign-id", "tenant-id")

    captured = capsys.readouterr()
    rendered = f"{captured.out}\n{captured.err}"
    assert secret not in rendered
    assert "campaign_task_failed" in rendered
    assert "RuntimeError" in rendered
    assert CALLBACK_CLAIM_REDACTION in rendered
    retry.assert_called_once()
    retry_kwargs = retry.call_args.kwargs
    assert retry_kwargs["throw"] is False
    assert retry_kwargs["exc"] is not provider_error
    assert type(retry_kwargs["exc"]) is RuntimeError
    assert secret not in str(retry_kwargs)
    assert CALLBACK_CLAIM_REDACTION in str(retry_kwargs["exc"])
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
