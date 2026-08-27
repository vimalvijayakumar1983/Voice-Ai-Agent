"""Voice AI Agent Platform - FastAPI Application."""

from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.v1.router import api_router
from app.core.config import settings

logger = structlog.get_logger()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("starting_voice_ai_agent", env=settings.app_env)
    yield
    logger.info("shutting_down_voice_ai_agent")


app = FastAPI(
    title="Voice AI Agent Platform",
    description="Enterprise-grade Voice AI Agent SaaS Platform",
    version="0.2.0",
    docs_url="/docs" if settings.app_debug else None,
    redoc_url="/redoc" if settings.app_debug else None,
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(api_router)


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "version": "0.2.0",
        "providers": {"smallest": bool(settings.smallest_api_key)},
    }
