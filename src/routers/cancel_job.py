"""POST /jobs/{worker_job_id}/cancel — cooperative cancel (spec 01 §4).

Sets the in-memory cancel flag. A RUNNING job checks it at the next spread boundary
(and before color/merge/upload) → finalizes `cancelled` with partial progress; no
hard-kill mid-screenshot. A QUEUED job is finalized `cancelled` immediately so the
consumer skips it. An already-terminal job → 200 idempotent (current status).
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse

from src.auth import require_worker_token
from src.envelope import ok, worker_job_not_found
from src.jobs.pipeline import _finalize, _result
from src.jobs.registry import CANCELLED, QUEUED, get_job

logger = logging.getLogger("exppdf.cancel")

router = APIRouter()


@router.post("/jobs/{worker_job_id}/cancel", dependencies=[Depends(require_worker_token)])
async def cancel_job(worker_job_id: str):
    job = get_job(worker_job_id)
    if job is None:
        raise worker_job_not_found()

    if job.is_terminal:
        # Idempotent — return the current terminal status (spec 01 §4, 200).
        return ok({"worker_job_id": job.id, "status": job.status})

    prev_status = job.status
    job.cancel_requested = True
    if prev_status == QUEUED:
        # Dequeue-mark: finalize now so the consumer skips the stale queue entry.
        _finalize(job, CANCELLED, _result([], [], rendered=0, failed=0))
    logger.info("exppdf_cancel_requested job=%s prev_status=%s", job.id, prev_status)
    return JSONResponse(status_code=202, content={
        "success": True,
        "data": {"worker_job_id": job.id, "status": "cancelling"},
    })
