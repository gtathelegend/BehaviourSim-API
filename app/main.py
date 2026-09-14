"""BehaviorSim API entrypoint and application factory."""

import logging
import threading
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.routes.health import router as health_router
from app.api.v1.router import api_v1_router
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import setup_logging
from app.core.middleware import (
    CorrelationIdMiddleware,
    RequestBodyLimitMiddleware,
    SecurityHeadersMiddleware,
)
from app.worker import SimulationWorker

logger = logging.getLogger("behaviorsim_api")


def verify_behaviorsim_dependency() -> None:
    """Verify that behaviorsim package is installed and importable without executing simulation."""
    try:
        import behaviorsim

        version = getattr(behaviorsim, "__version__", "unknown")
        logger.info("BehaviorSim dependency verified: version %s", version)
    except ImportError as exc:
        logger.critical("Failed to import behaviorsim dependency: %s", exc)
        raise RuntimeError("Required dependency 'behaviorsim' is not available.") from exc


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan context manager handling startup and shutdown events."""
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL)
    logger.info("Starting %s (%s) in %s mode", settings.APP_NAME, settings.API_VERSION, settings.APP_ENV)

    # Validate production configuration if running in production mode
    settings.validate_production_configuration()

    # Verify BehaviorSim dependency at startup
    verify_behaviorsim_dependency()

    # Conditionally launch managed background worker daemon thread
    worker: SimulationWorker | None = None
    worker_thread: threading.Thread | None = None
    if settings.RUN_WORKER_THREAD:
        try:
            worker = SimulationWorker(poll_interval=1.0)
            worker_thread = threading.Thread(
                target=worker.run,
                kwargs={"register_signals": False},
                name="simulation-worker-daemon",
                daemon=True,
            )
            worker_thread.start()
            logger.info("Started background SimulationWorker daemon thread (%s)", worker.worker_id)
        except Exception as exc:
            logger.error("Failed to start background simulation worker: %s", exc, exc_info=True)

    yield

    logger.info("Shutting down %s", settings.APP_NAME)
    if worker is not None and worker_thread is not None:
        worker.request_stop()
        worker_thread.join(timeout=3.0)

    # Cleanly close pooled database connections on container termination
    from app.db.session import _engine
    if _engine is not None:
        _engine.dispose()


def create_app() -> FastAPI:
    """Factory creating and configuring the FastAPI application."""
    settings = get_settings()

    application = FastAPI(
        title=settings.APP_NAME,
        version=settings.API_VERSION,
        docs_url="/docs" if not settings.is_production else None,
        redoc_url="/redoc" if not settings.is_production else None,
        openapi_url="/openapi.json" if not settings.is_production else None,
        lifespan=lifespan,
    )

    # Operational & Security Middlewares
    application.add_middleware(RequestBodyLimitMiddleware, max_bytes=1_048_576)
    application.add_middleware(SecurityHeadersMiddleware, is_production=settings.is_production)
    application.add_middleware(CorrelationIdMiddleware)

    # Production-safe CORS configuration
    cors_origins = settings.CORS_ORIGINS
    if cors_origins:
        application.add_middleware(
            CORSMiddleware,
            allow_origins=cors_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )

    # Register error handlers
    register_error_handlers(application)

    # Register routes
    # Infrastructure-level health check probe
    application.include_router(health_router)

    # Versioned API routes for future endpoints (/v1)
    application.include_router(api_v1_router, prefix="/v1")

    return application


app = create_app()
