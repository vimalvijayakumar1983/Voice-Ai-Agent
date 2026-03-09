"""Celery worker configuration."""

from celery import Celery

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
    worker_prefetch_multiplier=1,
    task_routes={
        "app.tasks.campaign_tasks.*": {"queue": "campaigns"},
        "app.tasks.call_tasks.*": {"queue": "calls"},
        "app.tasks.webhook_tasks.*": {"queue": "webhooks"},
    },
)

# Auto-discover tasks
celery_app.autodiscover_tasks(["app.tasks"])
