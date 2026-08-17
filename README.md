# Storybook Export-PDF Worker

Compute worker that renders a book/remix to a print-ready 300 DPI PDF — extracted
from `ai-storybook-python-api` per [ADR-055](../docs/technical-decisions/adr-055-export-pdf-worker-extraction.md).

**Pattern B (worker, not full-service extraction):** the job `export_pdf` +
`background_jobs` lifecycle (enqueue/dedup/cancel/download, distribution leaf,
reaper) STAYS in python-api; python-api's handler is a poll-based orchestrator.
This worker owns only the heavy compute so a slow/crashing render can no longer
starve the API's event loop or CPU.

- FastAPI, Python 3.12 + uv, bind **127.0.0.1:3203** (loopback-only, under nginx).
- **`workers=1` MANDATORY** — in-memory FIFO queue + job registry (multi-worker
  would split-brain, same constraint as swap-service / ADR-053).
- **No DB, no Supabase, no AI.** Auth: `X-Worker-Token` S2S (all routes but `/healthz`).

## Pipeline (per job, `02-render-pipeline.md`)

```
R0 mkdtemp
R1 per-spread: sign render token (TTL 600s) → Chromium capture (300 DPI, DSF=1)
   → optional DPS center-split (page_unit='single') → PNG→PDF page(s)
R2 merge per-page PDFs (pypdf)
R3 CMYK: resolve ICC (customer URL SSRF-guarded → bundled → FOGRA39 default) →
   Pillow ImageCms RGB→CMYK → pikepdf PDF/X OutputIntent      (skipped for RGB)
R4 PUT the PDF to the storage-service (S2S) → media_url
```

Cooperative cancel is checked at spread boundaries + before color/merge/upload —
never a hard-kill mid-screenshot. Chromium: one browser per job.

## HTTP contract (`01-http-contract.md`)

| Route | Auth | Purpose |
|---|---|---|
| `POST /export-pdf` | `X-Worker-Token` | Submit a self-contained render plan → 202 `{worker_job_id, status:'queued', queue_position}` (503 `QUEUE_FULL` when full) |
| `GET /jobs/{id}` | `X-Worker-Token` | Poll progress + terminal result (404 `WORKER_JOB_NOT_FOUND`) |
| `POST /jobs/{id}/cancel` | `X-Worker-Token` | Cooperative cancel |
| `GET /healthz` | none | `chromium_ok` + `icc_default_ok` + queue counts |

Progress vocabulary (`spreads`/`color_conversion`/`merge`) mirrors
`background_jobs.step_details` (spec 06) verbatim — python-api copies it without
transformation.

## Env

| Var | Default | Notes |
|---|---|---|
| `HOST` / `PORT` | `127.0.0.1` / `3203` | Loopback bind |
| `EXPORT_PDF_WORKER_TOKEN` | — | X-Worker-Token; = python-api |
| `PRINT_RENDER_BASE_URL` | `http://localhost:3000` | FE print route base |
| `PRINT_RENDER_TOKEN_SECRET` | — | HS256; **must = python-api** (its verifier decodes worker tokens) |
| `STORAGE_SERVICE_URL` | `http://127.0.0.1:8200` | S2S write (loopback) |
| `STORAGE_SERVICE_API_KEY` | — | storage-service `X-API-Key` |
| `STORAGE_PUBLIC_BASE_URL` | — | fallback `media_url` build only |
| `ICC_PROFILE_DIR` | (empty → repo `icc-profiles/`) | bundled ICC dir |
| `DEFAULT_ICC_PROFILE_ID` | `fogra39` | default CMYK profile |
| `EXPORT_SEM_CAP` / `QUEUE_MAX` | `1` / `4` | Chromium cap / FIFO depth |
| `WORKER_JOB_TIMEOUT_SEC` | `1800` | per-job wall-clock (= reaper stale) |
| `SHUTDOWN_TIMEOUT_SEC` | `25` | graceful drain budget |

## Run

```bash
uv sync
uv run playwright install chromium-headless-shell   # server: --with-deps chromium (needs sudo)
cp .env.example .env                          # fill secrets
./scripts/run-service.sh                      # uvicorn 127.0.0.1:3203, workers=1 pinned
uv run pytest                                 # unit suite
./test-scripts/test-export-pdf-worker.sh      # integration bookend
```

**⚠️ Re-run `playwright install` after every `playwright` package bump.** Playwright
resolves the browser from a version-pinned dir in `~/.cache/ms-playwright/`
(e.g. `chromium_headless_shell-1234/`); after `uv sync` bumps the package the old
binary is not found and jobs fail at render with `RENDER_CRASH`
`"chromium launch failed: Executable doesn't exist at ..."` (prod incident
2026-08-17). Install under the same user the service runs as (cache is per-`$HOME`).
No restart needed — the binary is resolved at browser launch. Verify:
`curl -s localhost:3203/healthz` → `chromium_ok: true`.

## Deploy

Independent **systemd** unit (restart decoupled from python-api — the motivation
for the split). `uv sync` + `playwright install --with-deps chromium` (repeat the
install on every playwright bump — see warning in Run; smoke-test
`/healthz` → `chromium_ok` after each deploy), bind
`127.0.0.1:3203`, **single worker** (never scale the uvicorn worker count — the
queue/registry are in-process). Host has 32 GB RAM; NOT Cloud Run (ADR-033
constraint unchanged). Local gitlink only — no GitHub remote in v1.

## Layout

```
src/
├── main.py            FastAPI app + lifespan (consumer/prune) + routers
├── config/settings.py pydantic-settings
├── auth.py            X-Worker-Token dependency
├── envelope.py        {success, data?, error?} + exception handlers
├── jobs/
│   ├── registry.py    WorkerJob + _JOBS/_QUEUE/_SEM state
│   ├── pipeline.py    _run(job) 5-phase render
│   └── consumer.py    queue consumer + prune + graceful shutdown
├── models/worker_job.py   request/response Pydantic
├── routers/           submit / poll / cancel / healthz
└── services/          pdf_render, color_convert, icc_registry, render_token,
                       ssrf_guard, storage_hosts (moved/copied from python-api),
                       storage_upload (new S2S PUT)
icc-profiles/          Coated_FOGRA39.icc (license-vetted)
```
