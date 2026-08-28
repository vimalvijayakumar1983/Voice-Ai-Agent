"""API v1 router - aggregates all endpoint routers."""

from fastapi import APIRouter

from app.api.v1.endpoints import (
    agents,
    analytics,
    audit,
    auth,
    billing,
    calls,
    campaigns,
    compliance,
    integrations,
    knowledge,
    webhooks,
    workflows,
)

api_router = APIRouter(prefix="/api/v1")

api_router.include_router(auth.router)
api_router.include_router(agents.router)
api_router.include_router(knowledge.router)
api_router.include_router(calls.router)
api_router.include_router(workflows.router)
api_router.include_router(campaigns.router)
api_router.include_router(analytics.router)
api_router.include_router(audit.router)
api_router.include_router(integrations.router)
api_router.include_router(compliance.router)
api_router.include_router(billing.router)
api_router.include_router(webhooks.router)
