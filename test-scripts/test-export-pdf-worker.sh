#!/bin/bash
# Test script for: Export-PDF Worker HTTP contract (ADR-055, spec 01)
# Created: 2026-08-16
#
# Bookend integration test (python-api-workflow). Covers the paths that do NOT
# need the full render stack: healthz shape, auth 401, submit validation 400,
# submit 202 + poll shape, poll 404. A full render → PDF → storage assert needs
# python-api + FE print page + storage-service and is exercised in Phase 5 E2E
# (set RUN_FULL=1 to attempt it here against a live stack).

set -u
BASE_URL="${BASE_URL:-http://127.0.0.1:3203}"
WORKER_TOKEN="${WORKER_TOKEN:-change-me-worker-token}"
RUN_FULL="${RUN_FULL:-0}"

pass=0; fail=0
check() { # desc, expected, actual
  if [ "$2" == "$3" ]; then echo "✅ $1 (=$3)"; pass=$((pass+1));
  else echo "❌ $1 (expected $2, got $3)"; fail=$((fail+1)); fi
}

echo "=== 1. GET /healthz (no auth) ==="
HZ=$(curl -s "$BASE_URL/healthz")
echo "  $HZ"
echo "$HZ" | grep -q '"chromium_ok"' && echo "✅ healthz has chromium_ok" && pass=$((pass+1)) || { echo "❌ healthz missing chromium_ok"; fail=$((fail+1)); }
echo "$HZ" | grep -q '"icc_default_ok":true' && echo "✅ icc_default_ok=true" && pass=$((pass+1)) || echo "⚠️  icc_default_ok not true (deploy: ensure icc-profiles/ present)"

echo "=== 2. POST /export-pdf without token → 401 ==="
CODE=$(curl -s -o /dev/null -w '%{http_code}' -X POST "$BASE_URL/export-pdf" \
  -H "Content-Type: application/json" -d '{}')
check "missing token → 401" 401 "$CODE"

echo "=== 3. POST /export-pdf empty spreads → 400 VALIDATION_ERROR ==="
BODY=$(curl -s -X POST "$BASE_URL/export-pdf" \
  -H "Content-Type: application/json" -H "X-Worker-Token: $WORKER_TOKEN" \
  -d '{"source_job_id":"t","source":"book","book_id":"b","spreads":[],"language":"en_US","edition":"classic","page_unit":"spread","bleed_mm":3,"dpi":300,"color_mode":"cmyk","upload":{"bucket":"storybook-assets","key_prefix":"exports/b"}}')
echo "  $BODY"
echo "$BODY" | grep -q '"VALIDATION_ERROR"' && echo "✅ empty spreads → VALIDATION_ERROR" && pass=$((pass+1)) || { echo "❌ empty spreads not rejected"; fail=$((fail+1)); }

echo "=== 4. GET /jobs/does-not-exist → 404 WORKER_JOB_NOT_FOUND ==="
CODE=$(curl -s -o /dev/null -w '%{http_code}' "$BASE_URL/jobs/nope" -H "X-Worker-Token: $WORKER_TOKEN")
check "unknown job → 404" 404 "$CODE"

echo "=== 5. POST /export-pdf valid body → 202 queued ==="
SUB=$(curl -s -X POST "$BASE_URL/export-pdf" \
  -H "Content-Type: application/json" -H "X-Worker-Token: $WORKER_TOKEN" \
  -d '{"source_job_id":"t1","source":"book","book_id":"b1","spreads":[{"spread_id":"sp0","is_dps":false}],"language":"en_US","edition":"classic","page_unit":"spread","bleed_mm":3,"dpi":300,"color_mode":"cmyk","upload":{"bucket":"storybook-assets","key_prefix":"exports/b1"}}')
echo "  $SUB"
WJID=$(echo "$SUB" | sed -n 's/.*"worker_job_id":"\([^"]*\)".*/\1/p')
echo "$SUB" | grep -q '"status":"queued"' && echo "✅ submit → queued" && pass=$((pass+1)) || { echo "❌ submit did not return queued"; fail=$((fail+1)); }

if [ -n "$WJID" ]; then
  echo "=== 6. GET /jobs/$WJID (poll shape) ==="
  ST=$(curl -s "$BASE_URL/jobs/$WJID" -H "X-Worker-Token: $WORKER_TOKEN")
  echo "  $ST"
  echo "$ST" | grep -q '"worker_job_id"' && echo "✅ poll returns status shape" && pass=$((pass+1)) || { echo "❌ poll shape missing"; fail=$((fail+1)); }

  if [ "$RUN_FULL" == "1" ]; then
    echo "=== 6b. poll to terminal (RUN_FULL=1) ==="
    for i in $(seq 1 60); do
      ST=$(curl -s "$BASE_URL/jobs/$WJID" -H "X-Worker-Token: $WORKER_TOKEN")
      S=$(echo "$ST" | sed -n 's/.*"status":"\([^"]*\)".*/\1/p')
      echo "  [$i] status=$S"
      case "$S" in completed|failed|cancelled) break;; esac
      sleep 2
    done
    check "reaches a terminal status" 1 "$([ "$S" == completed ] || [ "$S" == failed ] || [ "$S" == cancelled ] && echo 1 || echo 0)"
  fi
fi

echo ""; echo "RESULT: $pass passed / $fail failed"
[ "$fail" -eq 0 ] && { echo "✅ Test PASSED"; exit 0; } || { echo "❌ Test FAILED"; exit 1; }
