#!/usr/bin/env bash
# PostToolUse hook: fires after every Write/Edit tool call on Python files
# in services/ (excluding tests/ and scripts/).
#
# Warns when bare print() calls are found. Services must use structlog,
# not print(), to ensure all output is structured, leveled, and searchable.
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

# ── Only fire on Python files in services/ ───────────────────────────────────
if [[ "$file_path" != *.py ]]; then
    exit 0
fi
if [[ "$file_path" != *"/services/"* ]] && [[ "$file_path" != *"services/"* ]]; then
    exit 0
fi
# Skip test files
if [[ "$file_path" == *"/tests/"* ]] || [[ "$file_path" == *"/test_"* ]]; then
    exit 0
fi
# Skip scripts/ (only services/ is in scope)
if [[ "$file_path" == *"/scripts/"* ]]; then
    exit 0
fi
# Skip if file doesn't exist
[[ -f "$file_path" ]] || exit 0

# ── Scan for bare print() calls ──────────────────────────────────────────────
findings=$(python3 - <<'PYEOF' 2>/dev/null
import re, sys

file_path = sys.stdin.read().strip()

try:
    with open(file_path, "r", encoding="utf-8") as f:
        lines = f.readlines()
except Exception:
    sys.exit(0)

# Match bare print( calls:
# - leading whitespace allowed
# - must not be inside a comment (line not starting with # after stripping)
# - must not be part of a docstring or string literal (simple heuristic)
PRINT_PATTERN = re.compile(r'^\s*print\s*\(')
COMMENT_PATTERN = re.compile(r'^\s*#')

hits = []
for i, line in enumerate(lines):
    # Skip comment lines
    if COMMENT_PATTERN.match(line):
        continue
    if PRINT_PATTERN.match(line):
        hits.append(f"  line {i + 1}: {line.rstrip()[:120]}")

if hits:
    print("WARN")
    for h in hits:
        print(h)
else:
    print("OK")
PYEOF
<<<"$file_path") || exit 0

# ── Emit warning (non-blocking) ──────────────────────────────────────────────
if [[ "${findings:0:4}" == "WARN" ]]; then
    echo ""
    echo "NO_PRINT_STATEMENT ──────────────────────────────────────────"
    echo "  WARNING: Bare print() call(s) found in service code."
    echo "  File: $file_path"
    echo ""
    echo "  Locations:"
    echo "$findings" | tail -n +2
    echo ""
    echo "  Rule: Services must use structlog, not print()."
    echo "  Fix:  Replace print() with structured log calls:"
    echo ""
    echo "    # Import at module top:"
    echo "    import structlog"
    echo "    logger = structlog.get_logger(__name__)"
    echo ""
    echo "    # Replace print('message') with:"
    echo "    logger.info('message', key=value)"
    echo "    logger.warning('message', key=value)"
    echo "    logger.error('message', key=value, exc_info=True)"
    echo ""
    echo "  Reason: print() bypasses log level filtering, structured"
    echo "  fields, CloudWatch correlation, and audit trail."
    echo "─────────────────────────────────────────────────────────────"
fi

# PostToolUse hooks always exit 0
exit 0
