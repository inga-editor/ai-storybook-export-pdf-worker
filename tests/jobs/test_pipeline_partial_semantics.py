"""Pipeline `run_job` partial/failure/cancel semantics — parity with the old
python-api handler's isolation rules (spec 06). Chromium capture + storage PUT +
ICC + raster→PDF are mocked so the test is deterministic and browser-free."""

from __future__ import annotations

import io
from unittest.mock import AsyncMock, MagicMock

from PIL import Image

from src.jobs.pipeline import run_job
from src.jobs.registry import WorkerJob
from src.models.worker_job import ExportPdfWorkerRequest
from src.services.color_convert import IccResolution
from src.services.pdf_render import RenderError

MODULE = "src.jobs.pipeline"


def _png(w=120, h=60) -> bytes:
    buf = io.BytesIO()
    Image.new("RGBA", (w, h), (200, 30, 30, 255)).save(buf, "PNG")
    return buf.getvalue()


def _req(*, n=2, color_mode="cmyk", page_unit="spread", is_dps=False) -> ExportPdfWorkerRequest:
    return ExportPdfWorkerRequest(
        source_job_id="sj", source="book", book_id="b1",
        spreads=[{"spread_id": f"sp{i}", "is_dps": is_dps} for i in range(n)],
        language="en_US", page_unit=page_unit, color_mode=color_mode,
        upload={"bucket": "storybook-assets", "key_prefix": "exports/b1"},
    )


def _icc(cmyk=True) -> IccResolution:
    if not cmyk:
        return IccResolution(icc_bytes=None, source=None, profile_id=None, profile=None)
    return IccResolution(
        icc_bytes=b"ICCDATA", source="default", profile_id="fogra39",
        profile={"rendering_intent": "RelativeColorimetric",
                 "black_point_handling": True, "output_intent_id": "FOGRA39"},
    )


def _fake_merge(paths, out):
    with open(out, "wb") as fh:
        fh.write(b"%PDF-1.4\n")
    return len(paths)


def _patch(monkeypatch, *, capture=None, put=None, icc=None, merge=None, assemble=None):
    from src.jobs import pipeline as mod
    capture = capture or AsyncMock(return_value=_png())
    put = put or AsyncMock(return_value="https://storage/exports/b1/123.pdf")
    resolve = AsyncMock(return_value=icc if icc is not None else _icc(True))
    merge = merge or _fake_merge
    monkeypatch.setattr(f"{MODULE}.capture_spread", capture)
    monkeypatch.setattr(f"{MODULE}.put_object", put)
    monkeypatch.setattr(f"{MODULE}.resolve_icc", resolve)
    monkeypatch.setattr(f"{MODULE}.png_to_cmyk_pdf", MagicMock(side_effect=lambda *a, **k: a[3]))
    monkeypatch.setattr(f"{MODULE}.png_to_pdf", MagicMock(side_effect=lambda *a, **k: a[2]))
    monkeypatch.setattr(f"{MODULE}.merge_pdfs", merge)
    monkeypatch.setattr(f"{MODULE}.assemble_pdfx", assemble or MagicMock())
    return {"capture": capture, "put": put}


async def _run(req, monkeypatch, **kw):
    mocks = _patch(monkeypatch, **kw)
    job = WorkerJob(source_job_id=req.source_job_id, request=req)
    await run_job(job)
    return job, mocks


class TestHappy:
    async def test_completed_cmyk(self, monkeypatch):
        job, mocks = await _run(_req(n=2), monkeypatch)
        assert job.status == "completed"
        r = job.result
        assert r["spreads_rendered"] == 2
        assert r["spreads_failed"] == 0
        assert r["color_mode"] == "cmyk"
        assert r["profile_source"] == "default"
        assert r["profile_id"] == "fogra39"  # worker-owned resolved ICC id (M1 parity)
        assert r["storage_key"].startswith("exports/b1/")
        assert r["storage_key"].endswith(".pdf")
        assert r["media_url"] == "https://storage/exports/b1/123.pdf"
        assert r["page_count"] == 2
        # python-api-owned fields the worker MUST NOT emit (overlaid on finalize)
        for owned in ("source", "book_id", "exported_at", "spreads_skipped"):
            assert owned not in r
        assert mocks["capture"].await_count == 2

    async def test_rgb_opt_out_skips_color(self, monkeypatch):
        job, _ = await _run(_req(n=1, color_mode="rgb"), monkeypatch, icc=_icc(cmyk=False))
        assert job.status == "completed"
        assert job.result["color_mode"] == "rgb"
        assert job.result["output_intent"] is None

    async def test_dps_single_yields_two_pages(self, monkeypatch):
        job, _ = await _run(_req(n=1, page_unit="single", is_dps=True), monkeypatch)
        assert job.status == "completed"
        assert job.result["page_count"] == 2  # left + right


class TestPartial:
    async def test_one_spread_fails_still_completed(self, monkeypatch):
        capture = AsyncMock(side_effect=[RenderError("RENDER_TIMEOUT", "boom"), _png()])
        job, _ = await _run(_req(n=2), monkeypatch, capture=capture)
        assert job.status == "completed"
        assert job.result["spreads_rendered"] == 1
        assert job.result["spreads_failed"] == 1
        assert any(e["code"] == "RENDER_TIMEOUT" for e in job.result["errors"])
        # failed spread recorded as a per-spread failure object
        assert job.spreads["sp0"]["state"] == "failed"

    async def test_all_spreads_fail_returns_failed(self, monkeypatch):
        capture = AsyncMock(side_effect=RenderError("RENDER_CRASH", "x"))
        job, mocks = await _run(_req(n=2), monkeypatch, capture=capture)
        assert job.status == "failed"
        assert any(e["code"] == "ALL_SPREADS_FAILED" for e in job.result["errors"])
        mocks["put"].assert_not_awaited()

    async def test_merge_failure_returns_failed(self, monkeypatch):
        def _boom(paths, out):
            raise RuntimeError("merge blew up")
        job, mocks = await _run(_req(n=1), monkeypatch, merge=_boom)
        assert job.status == "failed"
        assert any(e["stage"] == "merge" for e in job.result["errors"])
        assert job.merge["status"] == "failed"
        mocks["put"].assert_not_awaited()

    async def test_upload_failure_returns_failed(self, monkeypatch):
        from src.services.storage_upload import StorageUploadError
        put = AsyncMock(side_effect=StorageUploadError("507 no space"))
        job, _ = await _run(_req(n=1), monkeypatch, put=put)
        assert job.status == "failed"
        assert any(e["code"] == "UPLOAD_FAILED" for e in job.result["errors"])


class TestCancel:
    async def test_cancel_before_first_spread_no_capture_no_upload(self, monkeypatch):
        mocks = _patch(monkeypatch)
        job = WorkerJob(source_job_id="sj", request=_req(n=2))
        job.cancel_requested = True
        await run_job(job)
        assert job.status == "cancelled"
        mocks["capture"].assert_not_awaited()
        mocks["put"].assert_not_awaited()

    async def test_cancel_at_spread_boundary_counts_rendered(self, monkeypatch):
        # capture succeeds for spread 0 and flips the cancel flag; the loop-top
        # check before spread 1 stops with rendered=1, no upload.
        job = WorkerJob(source_job_id="sj", request=_req(n=2))

        async def _capture_then_cancel(url):
            job.cancel_requested = True
            return _png()

        mocks = _patch(monkeypatch, capture=AsyncMock(side_effect=_capture_then_cancel))
        await run_job(job)
        assert job.status == "cancelled"
        assert job.result["spreads_rendered"] == 1
        mocks["put"].assert_not_awaited()
