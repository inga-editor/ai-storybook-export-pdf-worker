"""GET /healthz — liveness (no auth). Spec 01 §5.

Reports `chromium_ok` (Playwright launch smoke-check, cached 60s so health polls
don't spawn a browser every hit) + `icc_default_ok` (the default bundled ICC binary
loads — a missing binary is a deploy error) + in-memory registry counts.
"""

from __future__ import annotations

import logging
import time

from fastapi import APIRouter

from src.config.settings import settings
from src.jobs.registry import health_counts
from src.services.icc_registry import get_profile, load_bundled_icc

logger = logging.getLogger("exppdf.healthz")

router = APIRouter()

_CHROMIUM_CACHE_TTL_SEC = 60
_chromium_cache: tuple[float, bool] | None = None  # (checked_at, ok)


async def _chromium_ok() -> bool:
    """Launch + close a headless Chromium once per 60s; cache the verdict."""
    global _chromium_cache
    now = time.monotonic()
    if _chromium_cache is not None and (now - _chromium_cache[0]) < _CHROMIUM_CACHE_TTL_SEC:
        return _chromium_cache[1]
    ok = False
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
            )
            await browser.close()
        ok = True
    except Exception as exc:  # noqa: BLE001 — health probe never raises
        logger.warning("healthz_chromium_probe_failed err=%s", exc)
    _chromium_cache = (now, ok)
    return ok


def _icc_default_ok() -> bool:
    try:
        load_bundled_icc(get_profile(settings.default_icc_profile_id))
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("healthz_icc_default_failed err=%s", exc)
        return False


@router.get("/healthz")
async def healthz() -> dict:
    chromium_ok = await _chromium_ok()
    icc_default_ok = _icc_default_ok()
    counts = health_counts()
    return {
        "ok": chromium_ok and icc_default_ok,
        "chromium_ok": chromium_ok,
        "icc_default_ok": icc_default_ok,
        **counts,
    }
