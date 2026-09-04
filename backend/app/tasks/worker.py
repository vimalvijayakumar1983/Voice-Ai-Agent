"""Celery worker configuration."""

from celery import Celery
from kombu import Queue

from app.core.config import settings

celery_app = Celery(
    "voice_ai_agent",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    # With late acknowledgements, an abruptly terminated child must put its
    # in-flight delivery back on the queue instead of acknowledging it as lost.
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    # A worker started without ``-Q`` consumes every queue declared here. This
    # keeps the existing Railway command compatible with the routed queues.
    task_default_queue="celery",
    task_queues=(
        Queue("celery"),
        Queue("campaigns"),
        Queue("calls"),
        Queue("knowledge"),
        Queue("webhooks"),
    ),
    task_create_missing_queues=False,
    task_routes={
        "app.tasks.campaign_tasks.*": {"queue": "campaigns"},
        "app.tasks.call_tasks.*": {"queue": "calls"},
        "app.tasks.knowledge_tasks.*": {"queue": "knowledge"},
        "app.tasks.webhook_tasks.*": {"queue": "webhooks"},
    },
    beat_schedule={
        "sweep-stale-call-dispatches": {
            "task": "app.tasks.call_tasks.sweep_stale_call_dispatches",
            "schedule": 300.0,
        },
        "sweep-stale-direct-calls": {
            "task": "app.tasks.call_tasks.sweep_stale_direct_calls",
            "schedule": 300.0,
        },
        "sweep-stale-realtime-calls": {
            "task": "app.tasks.call_tasks.sweep_stale_realtime_calls",
            "schedule": 300.0,
        },
        "sweep-running-campaigns": {
            "task": "app.tasks.campaign_tasks.sweep_running_campaigns",
            "schedule": 300.0,
        },
        "sweep-provider-callback-outbox": {
            "task": "app.tasks.campaign_tasks.sweep_provider_callback_outbox",
            "schedule": 60.0,
        },
        "sweep-provider-cleanup-outbox": {
            "task": "app.tasks.knowledge_tasks.sweep_provider_cleanup_outbox",
            "schedule": 60.0,
        },
        "sweep-stale-knowledge-repairs": {
            "task": "app.tasks.knowledge_tasks.sweep_stale_knowledge_repairs",
            "schedule": 300.0,
        },
        "sweep-pending-webhook-deliveries": {
            "task": "app.tasks.webhook_tasks.sweep_pending_webhook_deliveries",
            "schedule": 60.0,
        },
    },
    # Celery's default autodiscovery only searches for a module named
    # ``tasks.py``. These application tasks are split by domain, so list them
    # explicitly to ensure they are registered when the worker starts.
    imports=(
        "app.tasks.campaign_tasks",
        "app.tasks.call_tasks",
        "app.tasks.knowledge_tasks",
        "app.tasks.webhook_tasks",
    ),
)
