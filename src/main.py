"""Storybook Export-PDF Worker — app entry point (ADR-055).

Bind 127.0.0.1:3203 (loopback-only; python-api is the sole S2S consumer). Compute
worker: headless Chromium capture → ICC CMYK → PDF/X → PUT storage-service. NO DB,
NO Supabase, NO AI.

`workers=1` is MANDATORY — the job registry + FIFO queue are in-memory (a
multi-worker uvicorn would split-brain, same constraint as swap-service/ADR-053).
Run: `uv run uvicorn src.main:app --host 127.0.0.1 --port 3203` (implicit 1 worker).

The lifespan starts/stops the in-memory queue consumer + prune loops; every route
but `/healthz` requires the `X-Worker-Token`.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.config.settings import settings
from src.envelope import register_exception_handlers
from src.jobs import consumer
from src.routers.cancel_job import router as cancel_router
from src.routers.get_job_status import router as status_router
from src.routers.healthz import router as healthz_router
from src.routers.submit_export_pdf import router as submit_router

logging.basicConfig(
    level="INFO",
    stream=sys.stdout,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("exppdf.main")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Startup: spawn the queue consumer + prune loops. Shutdown: drain running
    jobs (≤ SHUTDOWN_TIMEOUT_SEC) then stop the loops."""
    await consumer.start()
    try:
        yield
    finally:
        await consumer.stop()


app = FastAPI(
    title="Storybook Export-PDF Worker",
    description="Headless Chromium → ICC CMYK → PDF/X compute worker (ADR-055).",
    version="0.1.0",
    lifespan=lifespan,
)

register_exception_handlers(app)
app.include_router(healthz_router)
app.include_router(submit_router)
app.include_router(status_router)
app.include_router(cancel_router)

logger.info(
    "boot bind=%s:%s storage_service=%s print_render=%s sem_cap=%s queue_max=%s",
    settings.host, settings.port, settings.storage_service_url,
    settings.print_render_base_url, settings.export_sem_cap, settings.queue_max,
)
