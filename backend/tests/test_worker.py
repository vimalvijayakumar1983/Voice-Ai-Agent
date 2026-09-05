"""Celery worker registration and queue-consumption tests."""

from app.tasks.call_tasks import (
    process_completed_call,
    reconcile_call_dispatch,
    reconcile_direct_call_terminal,
    sweep_stale_call_dispatches,
    sweep_stale_direct_calls,
    sweep_stale_realtime_calls,
)
from app.tasks.campaign_tasks import (
    dispatch_provider_callback_outbox,
    run_campaign,
    sweep_provider_callback_outbox,
    sweep_running_campaigns,
)
from app.tasks.knowledge_tasks import (
    cleanup_provider_artifact,
    repair_website_source,
    sweep_provider_cleanup_outbox,
    sweep_stale_knowledge_repairs,
)
from app.tasks.webhook_tasks import fire_webhook_event, sweep_pending_webhook_deliveries
from app.tasks.worker import celery_app


def test_worker_registers_all_application_tasks():
    celery_app.loader.import_default_modules()

    assert {
        "app.tasks.campaign_tasks.run_campaign",
        "app.tasks.campaign_tasks.sweep_running_campaigns",
        "app.tasks.campaign_tasks.dispatch_provider_callback_outbox",
        "app.tasks.campaign_tasks.sweep_provider_callback_outbox",
        "app.tasks.call_tasks.process_completed_call",
        "app.tasks.call_tasks.reconcile_call_dispatch",
        "app.tasks.call_tasks.reconcile_direct_call_terminal",
        "app.tasks.call_tasks.sweep_stale_call_dispatches",
        "app.tasks.call_tasks.sweep_stale_direct_calls",
        "app.tasks.call_tasks.sweep_stale_realtime_calls",
        "app.tasks.knowledge_tasks.repair_website_source",
        "app.tasks.knowledge_tasks.cleanup_provider_artifact",
        "app.tasks.knowledge_tasks.sweep_provider_cleanup_outbox",
        "app.tasks.knowledge_tasks.sweep_stale_knowledge_repairs",
        "app.tasks.webhook_tasks.fire_webhook_event",
        "app.tasks.webhook_tasks.sweep_pending_webhook_deliveries",
    }.issubset(celery_app.tasks)
    assert run_campaign.app is celery_app
    assert sweep_running_campaigns.app is celery_app
    assert dispatch_provider_callback_outbox.app is celery_app
    assert sweep_provider_callback_outbox.app is celery_app
    assert process_completed_call.app is celery_app
    assert reconcile_call_dispatch.app is celery_app
    assert reconcile_direct_call_terminal.app is celery_app
    assert sweep_stale_call_dispatches.app is celery_app
    assert sweep_stale_direct_calls.app is celery_app
    assert sweep_stale_realtime_calls.app is celery_app
    assert repair_website_source.app is celery_app
    assert repair_website_source.reject_on_worker_lost is True
    assert cleanup_provider_artifact.app is celery_app
    assert sweep_provider_cleanup_outbox.app is celery_app
    assert sweep_stale_knowledge_repairs.app is celery_app
    assert fire_webhook_event.app is celery_app
    assert sweep_pending_webhook_deliveries.app is celery_app


def test_default_worker_consumes_every_routed_queue():
    declared_queues = set(celery_app.amqp.queues.keys())
    routed_queues = {route["queue"] for route in celery_app.conf.task_routes.values()}
    resolved_queues = {
        celery_app.amqp.router.route({}, task_name, (), {})["queue"].name
        for task_name in (
            "app.tasks.campaign_tasks.run_campaign",
            "app.tasks.campaign_tasks.sweep_running_campaigns",
            "app.tasks.campaign_tasks.dispatch_provider_callback_outbox",
            "app.tasks.campaign_tasks.sweep_provider_callback_outbox",
            "app.tasks.call_tasks.process_completed_call",
            "app.tasks.call_tasks.reconcile_call_dispatch",
            "app.tasks.call_tasks.reconcile_direct_call_terminal",
            "app.tasks.call_tasks.sweep_stale_call_dispatches",
            "app.tasks.call_tasks.sweep_stale_direct_calls",
            "app.tasks.call_tasks.sweep_stale_realtime_calls",
            "app.tasks.knowledge_tasks.repair_website_source",
            "app.tasks.knowledge_tasks.cleanup_provider_artifact",
            "app.tasks.knowledge_tasks.sweep_provider_cleanup_outbox",
            "app.tasks.knowledge_tasks.sweep_stale_knowledge_repairs",
            "app.tasks.webhook_tasks.fire_webhook_event",
            "app.tasks.webhook_tasks.sweep_pending_webhook_deliveries",
        )
    }

    assert declared_queues == {"celery", "campaigns", "calls", "knowledge", "webhooks"}
    assert routed_queues <= declared_queues
    assert resolved_queues == {"campaigns", "calls", "knowledge", "webhooks"}
    assert celery_app.conf.task_create_missing_queues is False
    assert celery_app.conf.beat_schedule["sweep-stale-call-dispatches"]["schedule"] == 300.0
    assert celery_app.conf.beat_schedule["sweep-stale-direct-calls"]["schedule"] == 300.0
    assert celery_app.conf.beat_schedule["sweep-stale-realtime-calls"]["schedule"] == 300.0
    assert celery_app.conf.beat_schedule["sweep-running-campaigns"]["schedule"] == 300.0
    assert celery_app.conf.beat_schedule["sweep-provider-callback-outbox"]["schedule"] == 60.0
    assert celery_app.conf.beat_schedule["sweep-provider-cleanup-outbox"]["schedule"] == 60.0
    assert celery_app.conf.beat_schedule["sweep-stale-knowledge-repairs"]["schedule"] == 300.0
    assert celery_app.conf.beat_schedule["sweep-pending-webhook-deliveries"]["schedule"] == 60.0
