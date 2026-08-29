"""Voice AI Agent Platform - FastAPI Application."""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api.v1.router import api_router
from app.core.config import settings
from app.core.readiness import database_schema_is_ready
from app.middleware.request_body_limit import RequestBodyLimitMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware

logger = structlog.get_logger()
APP_VERSION = "0.3.0"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting_voice_ai_agent", env=settings.app_env)
    yield
    logger.info("shutting_down_voice_ai_agent")


app = FastAPI(
    title="Voice AI Agent Platform",
    description="Enterprise-grade Voice AI Agent SaaS Platform",
    version=APP_VERSION,
    docs_url="/docs" if settings.app_debug else None,
    redoc_url="/redoc" if settings.app_debug else None,
    lifespan=lifespan,
)

# The pure-ASGI limit counts request chunks before FastAPI/Pydantic body
# buffering. It sits inside CORS/security wrappers so 413 responses retain the
# normal browser and hardening headers.
app.add_middleware(
    RequestBodyLimitMiddleware,
    max_bytes=settings.max_request_body_bytes,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(SecurityHeadersMiddleware)

# Routes
app.include_router(api_router)


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "version": APP_VERSION,
        "providers": {
            "smallest": bool(settings.smallest_api_key),
            "sarvam": bool(settings.sarvam_api_key),
        },
    }


@app.get("/ready")
async def readiness_check():
    """Report whether this release can safely serve database-backed traffic."""

    if not await database_schema_is_ready():
        return JSONResponse(
            status_code=503,
            content={"status": "not_ready"},
        )
    return {"status": "ready", "version": APP_VERSION}
