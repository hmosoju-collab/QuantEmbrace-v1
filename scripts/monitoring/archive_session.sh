#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# archive_session.sh — Save all session data before docker-compose down -v
#
# Usage:  bash scripts/monitoring/archive_session.sh [YYYY-MM-DD]
#   Date defaults to today in IST if not supplied.
#
# Output: session-archives/YYYY-MM-DD/
#   session_report.txt         — paper_session_report.py full output
#   orders.json                — orders table dump (all fills)
#   positions.json             — positions table dump (should all be FLAT by EOD)
#   fills.json                 — fills table dump
#   risk-state.json            — risk state, kill switch, daily P&L
#   strategy-config.json       — strategy configs (caps, paper_trade flag)
#   sessions.json              — Zerodha session tokens
#   regime-log.json            — AI engine regime classifications
#   strategy-recommendations.json — AI engine recommendations
#   live_counters.json         — /tmp/qe_live_counters.json snapshot
#   data_ingestion.log         — full container log
#   strategy_engine.log        — full container log
#   risk_engine.log            — full container log
#   execution_engine.log       — full container log
#   ai_engine.log              — full container log
#   MANIFEST.txt               — archive summary with file list and item counts
#
# Run BEFORE: docker-compose down -v
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"

DATE="${1:-$(TZ=Asia/Kolkata date +%Y-%m-%d)}"
ARCHIVE_DIR="${PROJECT_ROOT}/session-archives/${DATE}"
PREFIX="${DYNAMODB_TABLE_PREFIX:-quantembrace-development}"
LOCALSTACK_ENDPOINT="${AWS_ENDPOINT_URL:-http://localhost:4566}"
REGION="${AWS_DEFAULT_REGION:-ap-south-1}"

# Tables that carry session data.
# Excluded: candle-cache (large, ephemeral, not needed for blocker analysis)
#           features (large, intraday, not needed)
#           latest-prices (tick cache, not needed)
TABLES=(
    "${PREFIX}-orders"
    "${PREFIX}-positions"
    "${PREFIX}-fills"
    "${PREFIX}-risk-state"
    "${PREFIX}-strategy-config"
    "${PREFIX}-sessions"
    "${PREFIX}-regime-log"
    "${PREFIX}-strategy-recommendations"
)

SERVICES=(data_ingestion strategy_engine risk_engine execution_engine ai_engine)

PASS=0
WARN=0
SKIP=0

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  QuantEmbrace — Paper Session Archive"
echo "  Session date : ${DATE}"
echo "  Archive path : ${ARCHIVE_DIR}"
echo "  Table prefix : ${PREFIX}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

mkdir -p "${ARCHIVE_DIR}"

# ── 1. Session report ──────────────────────────────────────────────────────────
echo ""
echo "[1/5] Session report..."
cd "${PROJECT_ROOT}"
if PYTHONPATH="${PROJECT_ROOT}/services" python scripts/monitoring/paper_session_report.py \
       --date "${DATE}" \
       > "${ARCHIVE_DIR}/session_report.txt" 2>&1; then
    echo "      PASS → session_report.txt"
    PASS=$((PASS + 1))
else
    echo "      WARN → paper_session_report.py exited non-zero (output saved anyway)"
    WARN=$((WARN + 1))
fi

# ── 2. DynamoDB table dumps ────────────────────────────────────────────────────
echo ""
echo "[2/5] DynamoDB table dumps..."
for table in "${TABLES[@]}"; do
    # Derive output filename by stripping the prefix, e.g.
    # quantembrace-development-orders → orders.json
    label="${table#"${PREFIX}-"}"
    outfile="${ARCHIVE_DIR}/${label}.json"

    if aws dynamodb scan \
           --table-name "${table}" \
           --no-paginate \
           --endpoint-url "${LOCALSTACK_ENDPOINT}" \
           --region "${REGION}" \
           --output json \
           > "${outfile}" 2>/dev/null; then
        count=$(python3 -c \
            "import json,sys; d=json.load(open('${outfile}')); print(d.get('Count',0))" \
            2>/dev/null || echo "?")
        echo "      PASS → ${label}.json  (${count} items)"
        PASS=$((PASS + 1))
    else
        echo "      SKIP → ${table} (not found or LocalStack unavailable)"
        rm -f "${outfile}"
        SKIP=$((SKIP + 1))
    fi
done

# ── 3. Docker service logs ─────────────────────────────────────────────────────
echo ""
echo "[3/5] Docker logs..."
for svc in "${SERVICES[@]}"; do
    outfile="${ARCHIVE_DIR}/${svc}.log"
    if docker logs "${svc}" > "${outfile}" 2>&1; then
        lines=$(wc -l < "${outfile}" | tr -d ' ')
        echo "      PASS → ${svc}.log  (${lines} lines)"
        PASS=$((PASS + 1))
    else
        echo "      SKIP → ${svc} container not found or not running"
        rm -f "${outfile}"
        SKIP=$((SKIP + 1))
    fi
done

# ── 4. Live counters snapshot ──────────────────────────────────────────────────
echo ""
echo "[4/5] Live counters..."
COUNTERS_PATH="${QE_MONITORING_COUNTERS_PATH:-/tmp/qe_live_counters.json}"
if [ -f "${COUNTERS_PATH}" ]; then
    cp "${COUNTERS_PATH}" "${ARCHIVE_DIR}/live_counters.json"
    echo "      PASS → live_counters.json"
    PASS=$((PASS + 1))
else
    echo "      SKIP → ${COUNTERS_PATH} not found (execution_engine may not have run)"
    SKIP=$((SKIP + 1))
fi

# ── 5. Manifest ────────────────────────────────────────────────────────────────
echo ""
echo "[5/5] Writing manifest..."
{
    echo "session_date: ${DATE}"
    echo "archived_at:  $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "table_prefix: ${PREFIX}"
    echo "pass: ${PASS}  warn: ${WARN}  skip: ${SKIP}"
    echo ""
    echo "files:"
    for f in "${ARCHIVE_DIR}"/*; do
        [ -f "$f" ] || continue
        fname="$(basename "$f")"
        [ "$fname" = "MANIFEST.txt" ] && continue
        size=$(du -sh "$f" | cut -f1)
        echo "  - ${fname}  (${size})"
    done
} > "${ARCHIVE_DIR}/MANIFEST.txt"
echo "      PASS → MANIFEST.txt"

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Archive complete  (pass=${PASS} warn=${WARN} skip=${SKIP})"
echo "  Output: ${ARCHIVE_DIR}"
echo ""
echo "  Next step: docker-compose down -v"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
