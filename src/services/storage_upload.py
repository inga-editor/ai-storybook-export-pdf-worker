"""Async S2S client for the self-hosted storage service (ADR-054) — PUT the
finished PDF. The worker's ONLY storage seam (no sign/delete — the orchestrator's
`/download` endpoint mints signed URLs).

HTTP contract (storage-service `03-http-api.md`):
    PUT {STORAGE_SERVICE_URL}/api/storage/objects/{bucket}/{key}?upsert=false
        headers: X-API-Key, Content-Type: application/pdf   body: raw bytes
        201 (new) | 200 (upsert) → {"success":true,"data":{bucket,key,url,...}}

Async (the pipeline is async, unlike python-api's sync-in-to_thread client). One
retry with a 2s backoff for a transient transport error (loopback → rare). The
returned `media_url` prefers the service-built `data.url` (authoritative, the ONE
source of truth); falls back to `{STORAGE_PUBLIC_BASE_URL}/files/{bucket}/{key}`
only when the response omits it. Never logs the API key or the body bytes.
"""

from __future__ import annotations

import asyncio
import logging
from urllib.parse import quote

import httpx

from src.config.settings import settings

logger = logging.getLogger("exppdf.storage_upload")

_HTTP_TIMEOUT = httpx.Timeout(connect=10.0, read=120.0, write=120.0, pool=10.0)
_RETRY_BACKOFF_SEC = 2.0


class StorageUploadError(Exception):
    """PUT failed (non-2xx after retry, or transport error). Maps to a `upload`
    stage failure (UPLOAD_FAILED) in the pipeline."""


def _object_url(bucket: str, key: str) -> str:
    base = (settings.storage_service_url or "").strip().rstrip("/")
    q_bucket = quote(bucket, safe="")
    q_key = "/".join(quote(seg, safe="") for seg in key.split("/"))
    return f"{base}/api/storage/objects/{q_bucket}/{q_key}"


def _fallback_url(bucket: str, key: str) -> str:
    base = (settings.storage_public_base_url or "").strip().rstrip("/")
    return f"{base}/files/{bucket}/{key}"


async def put_object(bucket: str, key: str, body: bytes, content_type: str) -> str:
    """PUT bytes → the object's public `media_url`. One retry on transport error.

    Raises `StorageUploadError` on a non-2xx response or exhausted retries."""
    url = _object_url(bucket, key)
    headers = {"X-API-Key": settings.storage_service_api_key, "Content-Type": content_type}
    last_exc: Exception | None = None

    for attempt in range(2):  # initial + 1 retry
        try:
            async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
                resp = await client.put(url, params={"upsert": "false"}, headers=headers, content=body)
        except httpx.TransportError as exc:
            last_exc = exc
            logger.warning("exppdf_upload_transport_retry attempt=%d bucket=%s", attempt, bucket)
            if attempt == 0:
                await asyncio.sleep(_RETRY_BACKOFF_SEC)
                continue
            raise StorageUploadError(f"storage PUT transport error: {exc}") from exc

        if resp.status_code >= 400:
            code, message = "HTTP_ERROR", resp.reason_phrase or ""
            try:
                err = (resp.json() or {}).get("error") or {}
                code = err.get("code") or code
                message = err.get("message") or message
            except Exception:  # noqa: BLE001 — non-JSON error body
                pass
            raise StorageUploadError(f"storage PUT {resp.status_code} {code}: {message}")

        try:
            data = (resp.json() or {}).get("data") or {}
        except Exception:  # noqa: BLE001
            data = {}
        service_url = data.get("url")
        return service_url if isinstance(service_url, str) and service_url else _fallback_url(bucket, key)

    raise StorageUploadError(f"storage PUT failed: {last_exc}")
