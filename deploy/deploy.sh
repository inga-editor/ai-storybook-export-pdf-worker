#!/usr/bin/env bash
# Build + deploy export-pdf-worker on the prod server (runs from repo root, via
# the self-hosted runner or by hand). Requires: docker + compose v2, python3
# (health-gate JSON parse), .env in STATE_DIR.
set -euo pipefail

STATE_DIR=/home/tbng84/Projects/AI-Story-Book/ai-storybook-export-pdf-worker
HEALTH_URL=http://localhost:3203/healthz
COMPOSE="docker compose -f deploy/compose.yml"
KEEP_TAGS=5

SHA=$(git rev-parse --short HEAD)
echo "==> deploying export-pdf-worker:$SHA"

# warn (not fail) on env keys present in .env.example but missing on the server —
# a missing optional var is legitimate, a missing required one will fail the health gate
comm -23 <(grep -oE '^[A-Z_]+' .env.example | sort -u) \
         <(grep -oE '^[A-Z_]+' "$STATE_DIR/.env" | sort -u) \
  | sed 's/^/WARN missing in server .env: /' || true

PREV=$(docker inspect -f '{{.Config.Image}}' export-pdf-worker 2>/dev/null || echo "")
echo "==> current image: ${PREV:-<none>}"

docker build -t "export-pdf-worker:$SHA" .

TAG=$SHA $COMPOSE up -d

# Health gate asserts the JSON body, not just HTTP 200: chromium_ok proves the
# baked browser actually launches, icc_default_ok proves the bundled profile loads.
# First probe launches a real Chromium (~seconds). Window is ~90s: healthz caches
# a failed Chromium probe for 60s, so the gate must straddle two cache windows or
# one fast transient launch failure would poison every probe until the loop ends.
ok=""
for _ in $(seq 1 45); do
  sleep 2
  body=$(curl -sf --max-time 40 "$HEALTH_URL" 2>/dev/null) || continue
  if printf '%s' "$body" | python3 -c 'import sys,json;d=json.load(sys.stdin);sys.exit(0 if d.get("chromium_ok") and d.get("icc_default_ok") else 1)'; then
    ok=1
    break
  fi
done

if [ -z "$ok" ]; then
  echo "!! HEALTH GATE FAILED for export-pdf-worker:$SHA — recent logs:"
  journalctl CONTAINER_NAME=export-pdf-worker -n 100 --no-pager || true
  if [ -n "$PREV" ]; then
    echo "!! rolling back to $PREV"
    TAG=${PREV#export-pdf-worker:} $COMPOSE up -d
  else
    echo "!! no previous image to roll back to — container left as-is for inspection"
  fi
  exit 1
fi
echo "==> healthy: export-pdf-worker:$SHA"

# keep the last $KEEP_TAGS images for manual rollback, drop the rest
docker images export-pdf-worker --format '{{.Tag}}' \
  | grep -vx "$SHA" | tail -n "+$KEEP_TAGS" \
  | xargs -r -I{} docker rmi "export-pdf-worker:{}" 2>/dev/null || true
docker image prune -f >/dev/null

echo "==> deploy done: export-pdf-worker:$SHA"
