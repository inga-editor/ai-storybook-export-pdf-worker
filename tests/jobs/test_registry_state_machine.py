"""Registry + queue + prune mechanics (`02-render-pipeline.md` §1).

Pure state tests — no consumer loop, no Chromium. The end-to-end queued→running→
terminal path is exercised in test_pipeline_partial_semantics + test_http_contract.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from src.jobs import consumer, registry
from src.jobs.registry import CANCELLED, COMPLETED, WorkerJob, try_enqueue
from src.models.worker_job import ExportPdfWorkerRequest


def _req(n_spreads: int = 1) -> ExportPdfWorkerRequest:
    return ExportPdfWorkerRequest(
        source_job_id="sj", source="book", book_id="b1",
        spreads=[{"spread_id": f"sp{i}", "is_dps": False} for i in range(n_spreads)],
        language="en_US", upload={"bucket": "storybook-assets", "key_prefix": "exports/b1"},
    )


def _job() -> WorkerJob:
    return WorkerJob(source_job_id="sj", request=_req())


class TestEnqueue:
    def test_enqueue_registers_and_returns_position(self):
        j = _job()
        pos = try_enqueue(j)
        assert pos == 1
        assert registry.get_job(j.id) is j

    def test_queue_full_raises_and_does_not_register(self):
        # QUEUE_MAX=4 (conftest env). The 5th put must raise QueueFull.
        jobs = [_job() for _ in range(5)]
        for j in jobs[:4]:
            try_enqueue(j)
        with pytest.raises(asyncio.QueueFull):
            try_enqueue(jobs[4])
        assert registry.get_job(jobs[4].id) is None  # NOT registered on overflow


class TestStatusPayload:
    def test_terminal_includes_result(self):
        j = _job()
        j.status = COMPLETED
        j.result = {"spreads_rendered": 1}
        j.finalized_at = time.time()
        payload = j.status_payload()
        assert payload["status"] == "completed"
        assert payload["result"] == {"spreads_rendered": 1}

    def test_non_terminal_omits_result(self):
        j = _job()
        j.result = {"leaked": True}  # should NOT surface while non-terminal
        payload = j.status_payload()
        assert "result" not in payload


class TestPrune:
    def test_prunes_only_stale_terminal(self, monkeypatch):
        from src.config.settings import settings
        monkeypatch.setattr(settings, "job_retention_sec", 1800)
        now = time.time()

        fresh = _job(); fresh.status = COMPLETED; fresh.finalized_at = now - 10
        stale = _job(); stale.status = CANCELLED; stale.finalized_at = now - 3600
        running = _job()  # non-terminal, no finalized_at
        for j in (fresh, stale, running):
            registry.register_job(j)

        pruned = consumer.prune_terminal(now)
        assert pruned == 1
        assert registry.get_job(stale.id) is None
        assert registry.get_job(fresh.id) is fresh
        assert registry.get_job(running.id) is running
