"""5-phase render pipeline `run_job(job)` (`02-render-pipeline.md` §2).

Ported from the python-api in-process handler (spec 06 §Flow PHASE 1→4). The ONLY
behavioural changes vs. the old handler: progress writes go to the in-memory
`WorkerJob` (not `ctx.report()` to the DB), cancel reads `job.cancel_requested`
(not a DB `check_cancel()`), and the upload leg PUTs the storage-service (S2S,
timestamp key) instead of the content-addressed `persist()`.

The step_details vocabulary (`spreads`/`color_conversion`/`merge`) and error
taxonomy are kept BYTE-COMPATIBLE with the old handler so python-api mirrors them
verbatim into `background_jobs.step_details` and the FE contract is unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import tempfile
import time

from src.config.settings import settings
from src.jobs.registry import CANCELLED, COMPLETED, FAILED, RUNNING, WorkerJob
from src.models.worker_job import ExportPdfWorkerRequest
from src.services.color_convert import (
    ColorConversionError,
    IccFetchError,
    assemble_pdfx,
    png_to_cmyk_pdf,
    resolve_icc,
)
from src.services.icc_registry import IccProfileUnavailableError, UnknownIccProfileError
from src.services.pdf_render import (
    RenderError,
    capture_spread,
    crop_dps_halves,
    merge_pdfs,
    png_to_pdf,
)
from src.services.render_token import RenderTokenClaims, sign_render_token
from src.services.storage_upload import StorageUploadError, put_object

logger = logging.getLogger("exppdf.pipeline")

_TOKEN_TTL_SEC = 600
_PDF_CONTENT_TYPE = "application/pdf"


# ─── result builder ──────────────────────────────────────────────────────────


def _result(errors: list, warnings: list, *, rendered: int, failed: int, **extra) -> dict:
    """Worker-owned result slice (spec 01 §3). Excludes source/book_id/remix_id/
    exported_at/profile_id/spreads_skipped — python-api appends those on finalize."""
    result = {"spreads_rendered": rendered, "spreads_failed": failed,
              "errors": errors, "warnings": warnings}
    result.update(extra)
    return result


def _write(path: str, data: bytes) -> None:
    with open(path, "wb") as fh:
        fh.write(data)


def _read(path: str) -> bytes:
    with open(path, "rb") as fh:
        return fh.read()


# ─── pipeline ────────────────────────────────────────────────────────────────


async def run_job(job: WorkerJob) -> None:
    """Execute the 5-phase render for `job`, mutating its in-memory state to a
    terminal status + result. Never raises (the consumer relies on this)."""
    req: ExportPdfWorkerRequest = job.request
    job.status = RUNNING
    job.spreads = {e.spread_id: "pending" for e in req.spreads}
    job.color_conversion = {"status": "pending"}
    job.merge = {"status": "pending"}

    errors: list[dict] = []
    warnings: list[dict] = []
    route_id = req.remix_id if req.source == "remix" else req.book_id
    tmpdir = tempfile.mkdtemp(prefix=f"exppdf-{job.id}-")

    page_pngs: list[str] = []
    failed_count = 0
    rendered = 0

    try:
        # ── R1: per-spread capture ───────────────────────────────────────────
        for entry in req.spreads:
            if job.cancel_requested:
                _finalize(job, CANCELLED, _result(errors, warnings, rendered=rendered, failed=failed_count))
                return
            sid = entry.spread_id
            job.spreads[sid] = "rendering"
            started = time.time()
            logger.info("exppdf_spread source_job_id=%s job=%s spread=%s", req.source_job_id, job.id, sid)
            try:
                claims = RenderTokenClaims(
                    source=req.source,
                    book_id=req.book_id,
                    remix_id=req.remix_id if req.source == "remix" else None,
                    spread_id=sid,
                    edition=req.edition,
                    language=req.language,
                    bleed_mm=float(req.bleed_mm),
                    exp=int(time.time()) + _TOKEN_TTL_SEC,
                )
                token = sign_render_token(claims)
                url = f"{settings.print_render_base_url}/print/{route_id}?token={token}"
                png = await capture_spread(url)

                if req.page_unit == "single" and entry.is_dps:
                    left, right = await asyncio.to_thread(crop_dps_halves, png)
                    for idx, half in enumerate((left, right)):
                        p = os.path.join(tmpdir, f"page_{sid}_{idx}.png")
                        _write(p, half)
                        page_pngs.append(p)
                else:
                    p = os.path.join(tmpdir, f"page_{sid}.png")
                    _write(p, png)
                    page_pngs.append(p)

                rendered += 1
                job.spreads[sid] = {"state": "done",
                                    "duration_ms": int((time.time() - started) * 1000),
                                    "render_dpi": req.dpi}
            except RenderError as exc:
                job.spreads[sid] = {"state": "failed", "stage": "render",
                                    "code": exc.code, "message": exc.message,
                                    "duration_ms": int((time.time() - started) * 1000)}
                errors.append({"stage": "render", "spread_id": sid,
                               "code": exc.code, "message": exc.message})
                failed_count += 1
                logger.warning("exppdf_spread_failed job=%s spread=%s code=%s", job.id, sid, exc.code)

        if not page_pngs:
            errors.append({"stage": "render", "code": "ALL_SPREADS_FAILED",
                           "message": "no spread rendered"})
            _finalize(job, FAILED, _result(errors, warnings, rendered=0, failed=failed_count,
                                           color_mode=req.color_mode))
            return

        # ── R3: COLOR (resolve ICC + per-page PDF) ───────────────────────────
        if job.cancel_requested:
            _finalize(job, CANCELLED, _result(errors, warnings, rendered=rendered, failed=failed_count))
            return

        icc_params = {
            "color_mode": req.color_mode,
            "icc_profile_url": str(req.icc_profile_url) if req.icc_profile_url else None,
            "icc_profile_id": req.icc_profile_id,
        }
        try:
            icc = await resolve_icc(icc_params)
        except (IccFetchError, UnknownIccProfileError, IccProfileUnavailableError) as exc:
            job.color_conversion = {"status": "failed", "message": str(exc)}
            errors.append({"stage": "color", "code": "INVALID_ICC_PROFILE", "message": str(exc)})
            _finalize(job, FAILED, _result(errors, warnings, rendered=rendered, failed=failed_count,
                                           color_mode=req.color_mode))
            return

        effective_color = "cmyk" if icc.icc_bytes else "rgb"
        if icc.icc_bytes:
            job.color_conversion = {"status": "running"}

        page_pdfs: list[str] = []
        try:
            for i, png_path in enumerate(page_pngs):
                png_bytes = _read(png_path)
                pdf_path = os.path.join(tmpdir, f"page_{i}.pdf")
                if icc.icc_bytes:
                    intent = (icc.profile or {}).get("rendering_intent", "RelativeColorimetric")
                    black_point = (icc.profile or {}).get("black_point_handling", True)
                    await asyncio.to_thread(
                        png_to_cmyk_pdf, png_bytes, icc.icc_bytes, req.dpi, pdf_path,
                        rendering_intent=intent, black_point=black_point,
                    )
                else:
                    await asyncio.to_thread(png_to_pdf, png_bytes, req.dpi, pdf_path)
                page_pdfs.append(pdf_path)
        except ColorConversionError as exc:
            job.color_conversion = {"status": "failed", "message": str(exc)}
            errors.append({"stage": "color", "code": "COLOR_CONVERSION_ERROR", "message": str(exc)})
            _finalize(job, FAILED, _result(errors, warnings, rendered=rendered, failed=failed_count,
                                           color_mode=req.color_mode))
            return

        if icc.icc_bytes:
            job.color_conversion = {"status": "done"}
            warnings.append({"message": f"converted to CMYK via {icc.source} ICC profile"})

        # ── R2: MERGE (+ PDF/X assemble) ─────────────────────────────────────
        if job.cancel_requested:
            _finalize(job, CANCELLED, _result(errors, warnings, rendered=rendered, failed=failed_count))
            return

        job.merge = {"status": "running"}
        merged_path = os.path.join(tmpdir, "merged.pdf")
        try:
            page_count = await asyncio.to_thread(merge_pdfs, page_pdfs, merged_path)
            if icc.icc_bytes and icc.profile is not None:
                await asyncio.to_thread(
                    assemble_pdfx, merged_path, icc.icc_bytes,
                    output_intent=req.output_intent,
                    output_condition_id=icc.profile.get("output_intent_id", "FOGRA39"),
                )
            job.merge = {"status": "done", "pages_merged": page_count}
        except Exception as exc:  # noqa: BLE001 — merge/assemble failure → no output
            job.merge = {"status": "failed", "message": str(exc)}
            errors.append({"stage": "merge", "message": str(exc)})
            _finalize(job, FAILED, _result(errors, warnings, rendered=rendered, failed=failed_count,
                                           color_mode=effective_color))
            return

        # ── R4: UPLOAD (S2S PUT storage-service) ─────────────────────────────
        if job.cancel_requested:
            _finalize(job, CANCELLED, _result(errors, warnings, rendered=rendered, failed=failed_count))
            return

        pdf_bytes = _read(merged_path)
        file_size = len(pdf_bytes)
        key = f"{req.upload.key_prefix}/{int(time.time() * 1000)}.pdf"
        try:
            media_url = await put_object(req.upload.bucket, key, pdf_bytes, _PDF_CONTENT_TYPE)
        except StorageUploadError as exc:
            errors.append({"stage": "upload", "code": "UPLOAD_FAILED", "message": str(exc)})
            _finalize(job, FAILED, _result(errors, warnings, rendered=rendered, failed=failed_count,
                                           color_mode=effective_color))
            return

        logger.info("exppdf_done source_job_id=%s job=%s pages=%d color=%s size=%d failed=%d",
                    req.source_job_id, job.id, page_count, effective_color, file_size, failed_count)
        _finalize(job, COMPLETED, _result(
            errors, warnings, rendered=rendered, failed=failed_count,
            storage_key=key, media_url=media_url, page_count=page_count,
            file_size_bytes=file_size, color_mode=effective_color,
            profile_id=icc.profile_id,       # resolved id (default→'fogra39', bundled→id, customer→None)
            profile_source=icc.source,
            output_intent=req.output_intent if icc.icc_bytes else None,
        ))
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _finalize(job: WorkerJob, status: str, result: dict) -> None:
    """Set the terminal status + result + finalize timestamp (idempotent-safe: a
    cancel that already finalized a queued job leaves this a no-op via the caller)."""
    if job.is_terminal:
        return
    job.status = status
    job.result = result
    job.finalized_at = time.time()
