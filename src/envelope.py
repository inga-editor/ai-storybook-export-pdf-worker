"""Response envelope + error taxonomy for the Export-PDF Worker.

Envelope (parity python-api / storage-service): `{success, data?, error?}`.

HTTP error codes (design `01-http-contract.md` §6):
  INVALID_WORKER_TOKEN(401), VALIDATION_ERROR(400), QUEUE_FULL(503),
  WORKER_JOB_NOT_FOUND(404), INTERNAL_ERROR(500).

In-pipeline errors (RENDER_TIMEOUT, COLOR_CONVERSION_ERROR, UPLOAD_FAILED, …) are
NOT HTTP errors — they accumulate in `data.result.errors[]` / per-spread state.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


def ok(data: Any) -> dict:
    return {"success": True, "data": data}


class WorkerError(Exception):
    """HTTP-surface error carrying a stable `code` + status. Rendered to the
    `{success:false, error:{code, message}}` envelope by the handlers."""

    def __init__(self, code: str, http_status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message


def invalid_worker_token(message: str = "Invalid or missing worker token") -> WorkerError:
    return WorkerError("INVALID_WORKER_TOKEN", 401, message)


def queue_full(message: str = "Worker queue is full") -> WorkerError:
    return WorkerError("QUEUE_FULL", 503, message)


def worker_job_not_found(message: str = "Worker job not found") -> WorkerError:
    return WorkerError("WORKER_JOB_NOT_FOUND", 404, message)


def _envelope(code: str, message: str) -> dict:
    return {"success": False, "error": {"code": code, "message": message}}


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(WorkerError)
    async def _worker_error_handler(_request: Request, exc: WorkerError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=_envelope(exc.code, exc.message))

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(_request: Request, exc: RequestValidationError) -> JSONResponse:
        """Pydantic body/query errors → 400 VALIDATION_ERROR (parity python-api)."""
        errors = exc.errors()
        first = errors[0] if errors else {}
        loc = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        msg = first.get("msg", "Validation error")
        return JSONResponse(
            status_code=400,
            content=_envelope("VALIDATION_ERROR", f"{loc}: {msg}" if loc else msg),
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(_request: Request, _exc: Exception) -> JSONResponse:
        """Last resort: static 500 (no leak). Full trace goes to the logger."""
        import logging

        logging.getLogger("exppdf.errors").exception("internal_error")
        return JSONResponse(status_code=500, content=_envelope("INTERNAL_ERROR", "Internal server error"))
