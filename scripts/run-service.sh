#!/bin/bash
# Canonical run command for the Storybook Export-PDF Worker (ADR-055).
#
# Bind 127.0.0.1 by DEFAULT: every route but /healthz is X-Worker-Token S2S —
# nginx never exposes this worker. Do NOT change HOST to 0.0.0.0 without a
# firewall in front.
#
# workers=1 is MANDATORY and NOT overridable: the FIFO queue + job registry are
# in-process (src/jobs/registry.py) — a second uvicorn worker would split-brain
# submits from polls. Same constraint as swap-service (ADR-053). Scale by
# deploying more hosts, never by raising the worker count.
#
# First run needs Chromium: uv run playwright install chromium
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
exec uv run uvicorn src.main:app \
  --host "${HOST:-127.0.0.1}" \
  --port "${PORT:-3203}" \
  --workers 1
