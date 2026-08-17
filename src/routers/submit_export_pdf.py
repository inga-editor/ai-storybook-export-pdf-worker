"""POST /export-pdf — submit a render job (spec 01 §2).

202 accepted → `{worker_job_id, status:'queued', queue_position}`; 400 on a bad
body (Pydantic); 401 without the worker token; 503 QUEUE_FULL when the FIFO queue
is full (no job row created — python-api maps it to a `WORKER_BUSY` failure).
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.auth import require_worker_token
from src.envelope import queue_full
from src.jobs.registry import WorkerJob, try_enqueue
from src.models.worker_job import ExportPdfWorkerRequest

logger = logging.getLogger("exppdf.submit")

router = APIRouter()


@router.post("/export-pdf", dependencies=[Depends(require_worker_token)])
async def submit_export_pdf(body: ExportPdfWorkerRequest):
    job = WorkerJob(source_job_id=body.source_job_id, request=body)
    # Seed the progress map so an immediate poll (before the consumer runs) shows
    # every spread pending rather than an empty object.
    job.spreads = {e.spread_id: "pending" for e in body.spreads}
    try:
        queue_position = try_enqueue(job)
    except asyncio.QueueFull:
        logger.warning("exppdf_queue_full source_job_id=%s", body.source_job_id)
        raise queue_full()

    logger.info("exppdf_submitted job=%s source_job_id=%s spreads=%d pos=%d",
                job.id, body.source_job_id, len(body.spreads), queue_position)
    return JSONResponse(status_code=202, content={
        "success": True,
        "data": {"worker_job_id": job.id, "status": "queued", "queue_position": queue_position},
    })
