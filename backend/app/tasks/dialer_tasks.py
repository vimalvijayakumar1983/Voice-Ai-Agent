"""Persistent scheduling for all AI dialer modes; no long in-memory countdowns."""

from uuid import UUID

from sqlalchemy import or_, select

from app.tasks.async_runner import run_async
from app.tasks.worker import celery_app


@celery_app.task(name="app.tasks.dialer_tasks.sweep")
def sweep():
    return run_async(_sweep())


async def _sweep():
    from app.core.database import async_session_factory
    from app.models.dialer import DialerCampaign, DialerJob
    from app.services.dialer_dispatch import claim_due_jobs
    from app.services.dialer_policy import ACTIVE

    async with async_session_factory() as db:
        active = (
            select(DialerJob.id)
            .where(
                DialerJob.campaign_id == DialerCampaign.id,
                DialerJob.tenant_id == DialerCampaign.tenant_id,
                DialerJob.state.in_(ACTIVE),
            )
            .exists()
        )
        campaigns = (
            await db.execute(
                select(DialerCampaign.id, DialerCampaign.tenant_id)
                .where(
                    or_(DialerCampaign.status == "running", active),
                )
                .order_by(DialerCampaign.updated_at)
                .limit(200)
            )
        ).all()
    count = 0
    for campaign_id, tenant_id in campaigns:
        async with async_session_factory() as db:
            jobs = await claim_due_jobs(db, campaign_id, tenant_id)
        for job_id in jobs:
            execute.delay(job_id, str(tenant_id))
            count += 1
    return count


@celery_app.task(name="app.tasks.dialer_tasks.execute")
def execute(job_id: str, tenant_id: str):
    return run_async(_execute(UUID(job_id), UUID(tenant_id)))


async def _execute(job_id, tenant_id):
    from app.core.database import async_session_factory
    from app.services.dialer_dispatch import dispatch_job

    async with async_session_factory() as db:
        await dispatch_job(db, job_id, tenant_id)
