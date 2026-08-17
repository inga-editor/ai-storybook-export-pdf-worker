"""S2S auth — `X-Worker-Token` matched constant-time against EXPORT_PDF_WORKER_TOKEN.

Fail-closed: empty configured token OR missing/mismatched header → 401
INVALID_WORKER_TOKEN. The worker binds loopback (127.0.0.1:3203), so this token is
defense-in-depth on top of network isolation, not the sole barrier. Applied to
every route except `/healthz`.
"""

from __future__ import annotations

from secrets import compare_digest

from fastapi import Header

from src.config.settings import settings
from src.envelope import invalid_worker_token


async def require_worker_token(
    x_worker_token: str | None = Header(default=None, alias="X-Worker-Token"),
) -> None:
    configured = settings.export_pdf_worker_token
    if not configured:
        raise invalid_worker_token("worker token not configured")
    if x_worker_token is None or not compare_digest(x_worker_token, configured):
        raise invalid_worker_token()
