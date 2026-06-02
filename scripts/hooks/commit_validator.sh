#!/usr/bin/env bash
# PreToolUse hook: fires before every Bash tool call.
# If the command contains "git commit", validates that the commit message
# follows the Conventional Commits specification.
#
# Exit 1 to BLOCK; exit 0 to allow.

set -uo pipefail

# Read tool input JSON from stdin
input=$(cat)

# ── Parse command from stdin ─────────────────────────────────────────────────
command_str=$(python3 - <<'PYEOF' 2>/dev/null
import json, sys

try:
    d = json.load(sys.stdin)
    cmd = d.get("tool_input", {}).get("command", "")
    print(cmd)
except Exception:
    print("")
PYEOF
<<<"$input") || exit 0

[[ -z "$command_str" ]] && exit 0

# ── Only act on git commit commands ─────────────────────────────────────────
if [[ "$command_str" != *"git commit"* ]]; then
    exit 0
fi

# ── Extract commit message from -m flag ──────────────────────────────────────
commit_msg=$(python3 - <<'PYEOF' 2>/dev/null
import re, sys

command = sys.stdin.read().strip()

# Match -m "..." or -m '...' — handles escaped quotes inside HEREDOC-style strings
# Try double-quoted first, then single-quoted
patterns = [
    re.compile(r'-m\s+"((?:[^"\\]|\\.)*)"'),
    re.compile(r"-m\s+'((?:[^'\\]|\\.)*)'"),
]

for pattern in patterns:
    match = pattern.search(command)
    if match:
        print(match.group(1))
        sys.exit(0)

# Check for heredoc-style: -m "$(cat <<'EOF'\n...\nEOF\n)"
heredoc = re.search(r'-m\s+"?\$\(cat\s+<<\'?EOF\'?\n(.*?)\nEOF', command, re.DOTALL)
if heredoc:
    print(heredoc.group(1).strip())
    sys.exit(0)

# No -m flag found
sys.exit(1)
PYEOF
<<<"$command_str") || {
    # No -m flag — interactive commit or multiline heredoc we can't parse; allow it
    exit 0
}

[[ -z "$commit_msg" ]] && exit 0

# ── Validate against Conventional Commits spec ───────────────────────────────
validation=$(python3 - <<'PYEOF' 2>/dev/null
import re, sys

msg = sys.stdin.read().strip()

# Get the first line (subject line) only
subject = msg.split("\n")[0].strip()

# Conventional commits pattern:
#   type(optional-scope)!: description (min 5 chars)
pattern = re.compile(
    r'^(feat|fix|refactor|docs|test|chore|perf|style|hotfix|revert)'
    r'(\([a-z0-9][a-z0-9-]*\))?'
    r'!?'
    r':\s'
    r'.{5,}$'
)

if pattern.match(subject):
    print("OK")
else:
    print(f"INVALID:{subject}")
PYEOF
<<<"$commit_msg") || exit 0

# ── Block if invalid ─────────────────────────────────────────────────────────
if [[ "${validation:0:7}" == "INVALID" ]]; then
    bad_msg=$(echo "$validation" | cut -d: -f2-)
    echo ""
    echo "COMMIT_VALIDATOR ────────────────────────────────────────────"
    echo ""
    echo "  BLOCKED: Commit message does not follow Conventional Commits."
    echo ""
    echo "  Your message: $bad_msg"
    echo ""
    echo "  Required format:"
    echo "    <type>(<optional-scope>)!: <description (min 5 chars)>"
    echo ""
    echo "  Valid types: feat | fix | refactor | docs | test | chore |"
    echo "               perf | style | hotfix | revert"
    echo ""
    echo "  Examples:"
    echo "    feat(execution): add paper simulator slippage model"
    echo "    fix(risk): prevent kill switch self-refire on startup"
    echo "    refactor(strategy): extract candle buffer into shared util"
    echo "    docs: update live trading gate checklist"
    echo "    chore(infra): bump terraform aws provider to 5.50"
    echo "    test(risk): add validators coverage for age floor rule"
    echo ""
    echo "  Notes:"
    echo "    - Scope must be lowercase alphanumeric with hyphens"
    echo "    - Use ! before : for breaking changes (e.g., feat!:)"
    echo "    - Description must be at least 5 characters"
    echo "─────────────────────────────────────────────────────────────"
    exit 1
fi

exit 0
