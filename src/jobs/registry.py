"""In-memory job registry + bounded FIFO queue + Chromium semaphore.

`workers=1` is MANDATORY — this state is per-process; a multi-worker uvicorn would
split-brain (same constraint as swap-service, ADR-053). All access is from the
single event loop, so no locks are needed.

Design `02-render-pipeline.md` §1. Progress maps (`spreads`/`color_conversion`/
`merge`) mirror `background_jobs.step_details` (spec 06) verbatim so python-api can
copy them without transformation.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from src.config.settings import settings

QUEUED = "queued"
RUNNING = "running"
COMPLETED = "completed"
FAILED = "failed"
CANCELLED = "cancelled"
_TERMINAL = frozenset({COMPLETED, FAILED, CANCELLED})


@dataclass
class WorkerJob:
    """One render job's state + progress. `request` is the validated submit body."""

    source_job_id: str
    request: Any  # ExportPdfWorkerRequest (Pydantic)
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    status: str = QUEUED
    cancel_requested: bool = False
    # Progress — CÙNG vocabulary với background_jobs.step_details (spec 06).
    spreads: dict[str, Any] = field(default_factory=dict)
    color_conversion: dict[str, Any] | None = None
    merge: dict[str, Any] | None = None
    result: dict[str, Any] | None = None
    finalized_at: float | None = None
    created_at: float = field(default_factory=time.time)

    @property
    def is_terminal(self) -> bool:
        return self.status in _TERMINAL

    def status_payload(self) -> dict[str, Any]:
        """The `GET /jobs/{id}` `data` shape (spec 01 §3)."""
        data: dict[str, Any] = {
            "worker_job_id": self.id,
            "source_job_id": self.source_job_id,
            "status": self.status,
            "spreads": self.spreads,
        }
        if self.color_conversion is not None:
            data["color_conversion"] = self.color_conversion
        if self.merge is not None:
            data["merge"] = self.merge
        if self.is_terminal and self.result is not None:
            data["result"] = self.result
        return data


# ─── module-level state (single event loop) ──────────────────────────────────
_JOBS: dict[str, WorkerJob] = {}
_QUEUE: asyncio.Queue[str] = asyncio.Queue(maxsize=settings.queue_max)
_SEM: asyncio.Semaphore = asyncio.Semaphore(settings.export_sem_cap)


def get_job(worker_job_id: str) -> WorkerJob | None:
    return _JOBS.get(worker_job_id)


def register_job(job: WorkerJob) -> None:
    _JOBS[job.id] = job


def try_enqueue(job: WorkerJob) -> int:
    """Put the job on the FIFO queue + register it. Returns the queue position
    (depth after the put). Raises `asyncio.QueueFull` when the queue is full —
    the caller maps that to 503 QUEUE_FULL and the job is NOT registered."""
    _QUEUE.put_nowait(job.id)  # raises asyncio.QueueFull when full
    register_job(job)
    return _QUEUE.qsize()


def queue_depth() -> int:
    return _QUEUE.qsize()


def running_count() -> int:
    return sum(1 for j in _JOBS.values() if j.status == RUNNING)


def health_counts() -> dict[str, int]:
    return {
        "jobs_in_memory": len(_JOBS),
        "queue_depth": queue_depth(),
        "running": running_count(),
    }
