"""Application settings for the Export-PDF Worker (Pydantic Settings).

Boundary: this service is a COMPUTE worker (ADR-055). It has NO database, NO
Supabase SDK, NO AI dependency — it only renders headless Chromium → ICC CMYK →
PDF/X and PUTs the result to the storage-service. It shares the render-token
secret with python-api (so the print page's `get-render-preview` verifier
accepts worker-signed tokens) and the storage-service `X-API-Key` for S2S PUT.

`workers=1` is MANDATORY (in-memory job registry + queue). See `src/main.py`.

Env names mirror the moved compute modules verbatim (`print_render_token_secret`,
`icc_profile_dir`, `default_icc_profile_id`) so `render_token.py` / `icc_registry.py`
/ `color_convert.py` are byte-for-byte copies of the python-api originals.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration for the Storybook Export-PDF Worker."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- Bind ---------------------------------------------------------------
    host: str = "127.0.0.1"
    port: int = 3203

    # --- S2S auth (worker token) --------------------------------------------
    # X-Worker-Token shared secret (env EXPORT_PDF_WORKER_TOKEN both sides).
    # Empty => every authed route fail-closed (401). Precedent: video-worker.
    export_pdf_worker_token: str = ""

    # --- Print render (Chromium navigates the FE print page) ----------------
    # FE print route base (Plan B `/print/{route_id}?token=...`).
    print_render_base_url: str = "http://localhost:3000"
    # HS256 render-token secret — MUST equal python-api's PRINT_RENDER_TOKEN_SECRET
    # (its `get-render-preview` verifier decodes worker-signed tokens). `render_token`
    # fails fast at import when empty.
    print_render_token_secret: str = ""

    # --- Storage service (S2S PUT of the finished PDF) ----------------------
    # Loopback write endpoint (S2S, X-API-Key). Public domain 403s writes.
    storage_service_url: str = "http://127.0.0.1:8200"
    storage_service_api_key: str = ""
    # Public base to build `media_url` when the service response omits `data.url`
    # (the service is authoritative when present). e.g. https://storage.<domain>.
    storage_public_base_url: str = ""

    # --- ICC color ----------------------------------------------------------
    # Dir holding bundled, license-vetted ICC profiles. Empty → repo-local
    # `icc-profiles/` dir resolved by `icc_registry`.
    icc_profile_dir: str = ""
    # Bundled CMYK profile used when no customer/explicit ICC supplied.
    default_icc_profile_id: str = "fogra39"

    # --- Queue / pipeline ---------------------------------------------------
    # Hard Chromium concurrency cap (~0.5-2GB/instance). Design v1 = 1.
    export_sem_cap: int = 1
    # Bounded FIFO queue depth; over-cap submit → 503 QUEUE_FULL.
    queue_max: int = 4
    # Per-job wall-clock timeout (asyncio.wait_for around `_run`). 30 min —
    # matches python-api REAPER_STALE_SEC so a stuck render never outlives the
    # orchestrator's reaper backstop.
    worker_job_timeout_sec: int = 1800
    # Graceful shutdown budget: set cancel flags on running jobs, wait ≤ this.
    shutdown_timeout_sec: int = 25
    # Terminal-job retention in-memory before the prune loop drops it.
    job_retention_sec: int = 1800

    # --- storage_hosts.py verbatim dependencies (dormant here — the worker only
    #     calls validate_public_url for the customer ICC fetch; these keep the
    #     copied module importing cleanly) ----------------------------------
    supabase_url: str = ""
    storage_internal_read_base_url: str = ""
    storage_url_allowlist: str = ""


settings = Settings()
