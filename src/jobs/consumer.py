"""Queue consumer + prune loop + graceful shutdown (`02-render-pipeline.md` §1).

Single-process, single event loop (`workers=1`). The consumer dequeues one job id
at a time, runs it under the Chromium semaphore with a per-job wall-clock timeout,
and never lets one job's failure kill the loop. The prune loop drops terminal jobs
after the retention window so in-memory state cannot grow unbounded when the
orchestrator dies mid-poll.
"""

from __future__ import annotations

import asyncio
import logging
import time

from src.config.settings import settings
from src.jobs import registry
from src.jobs.pipeline import _finalize, _result, run_job
from src.jobs.registry import CANCELLED, FAILED, RUNNING

logger = logging.getLogger("exppdf.consumer")

_PRUNE_INTERVAL_SEC = 60
_tasks: list[asyncio.Task] = []


async def _consumer_loop() -> None:
    while True:
        wjid = await registry._QUEUE.get()
        job = registry.get_job(wjid)
        if job is None or job.is_terminal:
            # Cancelled-while-queued (cancel endpoint already finalized) or pruned.
            continue
        async with registry._SEM:
            if job.cancel_requested and not job.is_terminal:
                _finalize(job, CANCELLED, _result([], [], rendered=0, failed=0))
                continue
            try:
                await asyncio.wait_for(run_job(job), timeout=settings.worker_job_timeout_sec)
            except asyncio.TimeoutError:
                logger.warning("exppdf_job_timeout job=%s timeout=%ss", job.id, settings.worker_job_timeout_sec)
                if not job.is_terminal:
                    _finalize(job, FAILED, _result(
                        [{"stage": "render", "code": "RENDER_TIMEOUT",
                          "message": f"job exceeded {settings.worker_job_timeout_sec}s"}],
                        [], rendered=0, failed=0))
            except asyncio.CancelledError:
                # Shutdown-initiated abort — mark cancelled, then re-raise so the
                # task done-callback unwinds cleanly.
                if not job.is_terminal:
                    _finalize(job, CANCELLED, _result([], [], rendered=0, failed=0))
                raise
            except Exception:  # noqa: BLE001 — a handler crash must not kill the loop
                logger.exception("exppdf_job_crashed job=%s", job.id)
                if not job.is_terminal:
                    _finalize(job, FAILED, _result(
                        [{"stage": "internal", "message": "worker internal error"}],
                        [], rendered=0, failed=0))


def prune_terminal(now: float | None = None) -> int:
    """Drop terminal jobs finalized longer ago than the retention window. Pure +
    synchronous so it is unit-testable without driving the 60s loop. Returns the
    number pruned."""
    now = now if now is not None else time.time()
    stale = [
        jid for jid, j in list(registry._JOBS.items())
        if j.finalized_at is not None and (now - j.finalized_at) > settings.job_retention_sec
    ]
    for jid in stale:
        registry._JOBS.pop(jid, None)
    return len(stale)


async def _prune_loop() -> None:
    while True:
        await asyncio.sleep(_PRUNE_INTERVAL_SEC)
        pruned = prune_terminal()
        if pruned:
            logger.info("exppdf_pruned count=%d", pruned)


async def start() -> None:
    """Spawn the consumer + prune tasks (called from the lifespan startup)."""
    _tasks.append(asyncio.create_task(_consumer_loop(), name="exppdf-consumer"))
    _tasks.append(asyncio.create_task(_prune_loop(), name="exppdf-prune"))
    logger.info("exppdf_consumer_started sem_cap=%d queue_max=%d", settings.export_sem_cap, settings.queue_max)


async def stop() -> None:
    """Graceful shutdown: signal running jobs to cancel at the next boundary, wait
    up to SHUTDOWN_TIMEOUT_SEC for them to drain, then cancel the loops. A job that
    doesn't finish → python-api polls 404/still-running → WORKER_JOB_LOST."""
    for job in registry._JOBS.values():
        if job.status == RUNNING:
            job.cancel_requested = True

    deadline = time.monotonic() + settings.shutdown_timeout_sec
    while registry.running_count() > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.2)

    for task in _tasks:
        task.cancel()
    await asyncio.gather(*_tasks, return_exceptions=True)
    _tasks.clear()
    logger.info("exppdf_consumer_stopped")
