#!/usr/bin/env bash
# PostToolUse hook: fires after every Write/Edit tool call on Python files
# in services/execution_engine/ (excluding tests/).
#
# Checks that every broker order placement call (place_order, submit_order)
# has a paper mode guard within 20 lines above it in the same function scope.
#
# This is a NON-BLOCKING warning — always exits 0.

set -uo pipefail

# Read tool input JSON from stdin
input=$(cat)

# ── Parse file_path ──────────────────────────────────────────────────────────
file_path=$(python3 - <<'PYEOF' 2>/dev/null
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("tool_input", {}).get("file_path", ""))
except Exception:
    print("")
PYEOF
<<<"$input") || exit 0

[[ -z "$file_path" ]] && exit 0

# ── Only fire on Python files in services/execution_engine/ ──────────────────
if [[ "$file_path" != *"services/execution_engine/"* ]]; then
    exit 0
fi
if [[ "$file_path" != *.py ]]; then
    exit 0
fi
# Skip test files
if [[ "$file_path" == *"/tests/"* ]] || [[ "$file_path" == *"test_"* ]]; then
    exit 0
fi
# Skip if file doesn't exist
[[ -f "$file_path" ]] || exit 0

# ── Scan the file for unguarded broker calls ─────────────────────────────────
warnings=$(python3 - <<'PYEOF' 2>/dev/null
import re, sys

file_path = sys.stdin.read().strip()

try:
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
except Exception:
    sys.exit(0)

# Patterns that indicate a real broker order call
BROKER_CALL_PATTERNS = [
    re.compile(r'await\s+self\._zerodha\.place_order'),
    re.compile(r'await\s+self\._broker\.place_order'),
    re.compile(r'\bkite\.place_order\b'),
    re.compile(r'\balpaca\.submit_order\b'),
    re.compile(r'await\s+self\._alpaca\.submit_order'),
    re.compile(r'\.place_order\('),
    re.compile(r'\.submit_order\('),
]

# Patterns that indicate a paper mode guard
PAPER_GUARD_PATTERNS = [
    re.compile(r'_paper_trading'),
    re.compile(r'paper_trade'),
    re.compile(r'paper_mode'),
    re.compile(r'PaperSimulator'),
    re.compile(r'is_paper'),
]

LOOKBACK_LINES = 20
unguarded = []

for i, line in enumerate(lines):
    is_broker_call = any(p.search(line) for p in BROKER_CALL_PATTERNS)
    if not is_broker_call:
        continue

    # Check for paper guard in the 20 lines above this call
    start = max(0, i - LOOKBACK_LINES)
    context = "".join(lines[start:i + 1])

    has_guard = any(p.search(context) for p in PAPER_GUARD_PATTERNS)
    if not has_guard:
        unguarded.append(f"  line {i + 1}: {line.rstrip()[:120]}")

if unguarded:
    print("WARN")
    for u in unguarded:
        print(u)
else:
    print("OK")
PYEOF
<<<"$file_path") || exit 0

# ── Emit warning (non-blocking) ──────────────────────────────────────────────
if [[ "${warnings:0:4}" == "WARN" ]]; then
    echo ""
    echo "PAPER_GUARD_CHECK ───────────────────────────────────────────"
    echo "  WARNING: Broker order call(s) without visible paper guard"
    echo "  in: $file_path"
    echo ""
    echo "  Unguarded calls (no _paper_trading / paper_trade check"
    echo "  found within 20 lines above):"
    echo "$warnings" | tail -n +2
    echo ""
    echo "  Required pattern:"
    echo "    if self._paper_trading:"
    echo "        return await self._paper_simulator.place_order(...)"
    echo "    # Only reaches here for real broker:"
    echo "    return await self._zerodha.place_order(...)"
    echo ""
    echo "  Safety rule: paper_trade=True MUST NEVER call real broker API."
    echo "  See: CLAUDE.md § Non-Negotiable Safety Rules"
    echo "─────────────────────────────────────────────────────────────"
fi

# PostToolUse hooks always exit 0
exit 0
