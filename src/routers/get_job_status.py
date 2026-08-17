"""GET /jobs/{worker_job_id} — poll status (spec 01 §3).

200 with the progress + terminal result; 404 WORKER_JOB_NOT_FOUND for an unknown/
pruned id (python-api maps 404 → job failed `WORKER_JOB_LOST`).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends

from src.auth import require_worker_token
from src.envelope import ok, worker_job_not_found
from src.jobs.registry import get_job

router = APIRouter()


@router.get("/jobs/{worker_job_id}", dependencies=[Depends(require_worker_token)])
async def get_job_status(worker_job_id: str):
    job = get_job(worker_job_id)
    if job is None:
        raise worker_job_not_found()
    return ok(job.status_payload())
