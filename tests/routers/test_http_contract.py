"""HTTP contract tests (`01-http-contract.md`) via the ASGI TestClient. Lifespan is
NOT run (no consumer), so a submitted job stays `queued` — exactly what these
shape/status-code assertions need (no Chromium)."""

from __future__ import annotations

import time

from src.jobs.registry import COMPLETED, WorkerJob, register_job

VALID = {
    "source_job_id": "t", "source": "book", "book_id": "b1",
    "spreads": [{"spread_id": "sp0", "is_dps": False}],
    "language": "en_US",
    "upload": {"bucket": "storybook-assets", "key_prefix": "exports/b1"},
}


class TestAuth:
    def test_submit_without_token_401(self, client):
        r = client.post("/export-pdf", json=VALID)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "INVALID_WORKER_TOKEN"

    def test_submit_wrong_token_401(self, client):
        r = client.post("/export-pdf", json=VALID, headers={"X-Worker-Token": "wrong"})
        assert r.status_code == 401

    def test_poll_without_token_401(self, client):
        assert client.get("/jobs/anything").status_code == 401

    def test_healthz_needs_no_auth(self, client):
        assert client.get("/healthz").status_code == 200


class TestSubmit:
    def test_valid_202_queued(self, client, auth):
        r = client.post("/export-pdf", json=VALID, headers=auth)
        assert r.status_code == 202
        data = r.json()["data"]
        assert data["status"] == "queued"
        assert data["queue_position"] == 1
        assert data["worker_job_id"]

    def test_empty_spreads_400(self, client, auth):
        body = {**VALID, "spreads": []}
        r = client.post("/export-pdf", json=body, headers=auth)
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "VALIDATION_ERROR"

    def test_low_dpi_400(self, client, auth):
        body = {**VALID, "dpi": 50}
        r = client.post("/export-pdf", json=body, headers=auth)
        assert r.status_code == 400

    def test_queue_full_503(self, client, auth):
        # QUEUE_MAX=4 (conftest); no consumer drains → the 5th submit → 503.
        for _ in range(4):
            assert client.post("/export-pdf", json=VALID, headers=auth).status_code == 202
        r = client.post("/export-pdf", json=VALID, headers=auth)
        assert r.status_code == 503
        assert r.json()["error"]["code"] == "QUEUE_FULL"


class TestPoll:
    def test_unknown_job_404(self, client, auth):
        r = client.get("/jobs/does-not-exist", headers=auth)
        assert r.status_code == 404
        assert r.json()["error"]["code"] == "WORKER_JOB_NOT_FOUND"

    def test_poll_queued_job_shape(self, client, auth):
        wjid = client.post("/export-pdf", json=VALID, headers=auth).json()["data"]["worker_job_id"]
        r = client.get(f"/jobs/{wjid}", headers=auth)
        assert r.status_code == 200
        data = r.json()["data"]
        assert data["worker_job_id"] == wjid
        assert data["source_job_id"] == "t"
        assert data["status"] == "queued"
        assert data["spreads"] == {"sp0": "pending"}
        assert "result" not in data  # non-terminal


class TestCancel:
    def test_unknown_job_404(self, client, auth):
        assert client.post("/jobs/nope/cancel", headers=auth).status_code == 404

    def test_cancel_queued_202_cancelling(self, client, auth):
        wjid = client.post("/export-pdf", json=VALID, headers=auth).json()["data"]["worker_job_id"]
        r = client.post(f"/jobs/{wjid}/cancel", headers=auth)
        assert r.status_code == 202
        assert r.json()["data"]["status"] == "cancelling"
        # queued job is finalized cancelled immediately (dequeue-mark)
        assert client.get(f"/jobs/{wjid}", headers=auth).json()["data"]["status"] == "cancelled"

    def test_cancel_terminal_idempotent_200(self, client, auth):
        from src.models.worker_job import ExportPdfWorkerRequest
        job = WorkerJob(
            source_job_id="t",
            request=ExportPdfWorkerRequest(**VALID),
        )
        job.status = COMPLETED
        job.finalized_at = time.time()
        register_job(job)
        r = client.post(f"/jobs/{job.id}/cancel", headers=auth)
        assert r.status_code == 200
        assert r.json()["data"]["status"] == "completed"
