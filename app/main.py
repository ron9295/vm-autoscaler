"""FastAPI app factory. Run with:

uvicorn app.main:create_app --factory --host 0.0.0.0 --port 8080
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.api.routes import router
from app.config import Settings, check_backend_requirements, load_app_config
from app.container import build_container
from app.domain.ports import DeploymentSyncer, GitOpsBackend
from app.observability import Metrics, configure_logging
from app.services.scaling_service import Clock, utc_now

logger = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    *,
    gitops: GitOpsBackend | None = None,
    syncer: DeploymentSyncer | None = None,
    clock: Clock = utc_now,
) -> FastAPI:
    """Create the app. Config is loaded here so a bad config stops the pod immediately."""
    settings = settings or Settings()  # values come from environment variables
    configure_logging(settings.log_level)
    config = load_app_config(settings.config_path)
    check_backend_requirements(settings, config)
    metrics = Metrics()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        container = build_container(
            settings, config, metrics, gitops=gitops, syncer=syncer, clock=clock
        )
        app.state.container = container
        logger.info(
            "vm-autoscaler started",
            extra={
                "fields": {
                    "mode": config.mode.value,
                    "targets": len(config.targets),
                    "gitops_backend": settings.gitops_backend,
                    "argocd_sync_method": settings.argocd_sync_method,
                }
            },
        )
        try:
            yield
        finally:
            await container.close()

    app = FastAPI(title="vm-autoscaler", version="0.1.0", lifespan=lifespan)
    app.include_router(router)
    return app
