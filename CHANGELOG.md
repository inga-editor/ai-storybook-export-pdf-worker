# Changelog — Export-PDF Worker

## 2026-08-16 — Initial extraction (ADR-055)

Extracted the export-PDF compute path out of `ai-storybook-python-api` into a
standalone FastAPI worker (port 3203, loopback, `workers=1`).

- Scaffold: config, `X-Worker-Token` auth, `{success,data?,error?}` envelope, healthz.
- Moved verbatim from python-api: `pdf_render`, `color_convert`, `icc_registry`,
  `render_token`, `ssrf_guard`, `storage_hosts` + `icc-profiles/Coated_FOGRA39.icc`.
- New: `storage_upload` (async S2S PUT to storage-service).
- In-memory FIFO queue + job registry + 5-phase pipeline (`02-render-pipeline.md`).
- HTTP contract: submit / poll / cancel (`01-http-contract.md`).

python-api's `export_pdf` handler became a poll-based orchestrator; job lifecycle
(`background_jobs`, enqueue/dedup/cancel/download, distribution leaf, reaper) stays
in python-api — the FE/realtime contract is unchanged.
