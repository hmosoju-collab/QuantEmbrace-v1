#!/usr/bin/env bash
# PreToolUse hook: fires before every Write/Edit tool call.
# Blocks any attempt to set QE_EXECUTION_LIVE_TRADING_ENABLED=true in
# docker-compose files, .env files, or config files.
#
# Exit 1 to BLOCK; exit 0 to allow.

set -uo pipefail

# Read tool input JSON from stdin
input=$(cat)

# ── Parse file_path and content from stdin ──────────────────────────────────
parsed=$(python3 - <<'PYEOF' 2>/dev/null
import json, sys

try:
    d = json.load(sys.stdin)
    tool_input = d.get("tool_input", {})
    file_path = tool_input.get("file_path", "")
    # Write uses "content"; Edit uses "new_string"
    content = tool_input.get("new_string", "") or tool_input.get("content", "") or ""
    print(file_path)
    print("---CONTENT---")
    print(content)
except Exception:
    print("")
    print("---CONTENT---")
PYEOF
<<<"$input") || { exit 0; }

file_path=$(echo "$parsed" | head -1)
content=$(echo "$parsed" | tail -n +3)

[[ -z "$file_path" ]] && exit 0

# ── Only fire on docker-compose and .env files ───────────────────────────────
basename_file=$(basename "$file_path")
should_check=false

case "$basename_file" in
    docker-compose*.yml|docker-compose*.yaml) should_check=true ;;
    .env|.env.*) should_check=true ;;
    *.env) should_check=true ;;
esac

# Also check if the path contains docker-compose
if [[ "$file_path" == *"docker-compose"* ]]; then
    should_check=true
fi

[[ "$should_check" == false ]] && exit 0

# ── Scan for live trading enable flag ────────────────────────────────────────
detected=$(python3 - <<'PYEOF' 2>/dev/null
import re, sys

content = sys.stdin.read()

# Match all forms of QE_EXECUTION_LIVE_TRADING_ENABLED set to true
# Covers: KEY=true, KEY="true", KEY: true, KEY: "true", KEY: 'true' (case-insensitive value)
patterns = [
    re.compile(
        r'QE_EXECUTION_LIVE_TRADING_ENABLED\s*=\s*["\']?true["\']?',
        re.IGNORECASE
    ),
    re.compile(
        r'QE_EXECUTION_LIVE_TRADING_ENABLED\s*:\s*["\']?true["\']?',
        re.IGNORECASE
    ),
]

for pattern in patterns:
    for match in pattern.finditer(content):
        line_no = content[:match.start()].count("\n") + 1
        print(f"line {line_no}: {match.group(0).strip()}")
PYEOF
<<<"$content") || exit 0

# ── Block if detected ────────────────────────────────────────────────────────
if [[ -n "$detected" ]]; then
    echo ""
    echo "LIVE_TRADING_GATE ───────────────────────────────────────────"
    echo ""
    echo "  HARD BLOCK — Setting QE_EXECUTION_LIVE_TRADING_ENABLED=true"
    echo "  requires explicit operator sign-off."
    echo ""
    echo "  Detected in: $file_path"
    echo "  $detected"
    echo ""
    echo "  Required action before enabling live trading:"
    echo "    1. Complete all items in:"
    echo "       docs/operations/live-mode-gate-checklist.md"
    echo "    2. Obtain explicit operator approval (not Claude approval)"
    echo "    3. Ensure RISK_PROFILE=live and all paper safety gates pass"
    echo "    4. Confirm PAPER_SAFE_START → PAPER_EXPAND → LIVE_ADVANCED"
    echo "       promotion gates have all been passed (evaluate_promotion_gate.py)"
    echo ""
    echo "  See: docs/operations/live-mode-gate-checklist.md"
    echo "─────────────────────────────────────────────────────────────"
    exit 1
fi

exit 0
