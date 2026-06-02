#!/usr/bin/env bash
# PostToolUse hook: fires after every Edit/Write tool call.
# If a code or architecture file was changed and HLD, LLD, or README have
# not been updated in the current working tree, outputs a reminder to Claude.
#
# Exit 0 always — this is a non-blocking reminder, not an enforcer.

set -uo pipefail

# Read tool input JSON from stdin
input=$(cat)

file_path=$(python3 - <<'PYEOF' 2>/dev/null
import json, sys
try:
    d = json.load(sys.stdin)
    print(d.get("tool_input", {}).get("file_path", ""))
except Exception:
    print("")
PYEOF
<<<"$input") || file_path=""

[[ -z "$file_path" ]] && exit 0

# Find git root relative to the edited file
git_root=$(git -C "$(dirname "$file_path")" rev-parse --show-toplevel 2>/dev/null) || exit 0

# Relative path within the repo
rel_path="${file_path#$git_root/}"

# ── Skip list: editing these files does not require doc updates ─────────────
declare -a SKIP_PREFIXES=(
    "docs/"
    "CLAUDE.md"
    "README.md"
    "memory/"
    ".claude/"
    "hooks/"
    ".github/"
    "tests/"
    "architecture/"
    "governance/"
)
for skip in "${SKIP_PREFIXES[@]}"; do
    [[ "$rel_path" == $skip* ]] && exit 0
done

# ── Trigger list: these paths may require HLD / LLD / README updates ────────
declare -a TRIGGER_PREFIXES=(
    "services/"
    "configs/"
    "infra/terraform/"
    "scripts/"
)
triggered=false
for trigger in "${TRIGGER_PREFIXES[@]}"; do
    [[ "$rel_path" == $trigger* ]] && triggered=true && break
done
[[ "$triggered" == false ]] && exit 0

# ── Check which docs are NOT yet updated in the working tree ────────────────
# git status --porcelain captures new (??), modified, staged, and renamed files.
# cut -c4- strips the 2-char status + space prefix; sed handles rename "old -> new".
modified=$(git -C "$git_root" status --porcelain 2>/dev/null \
    | cut -c4- \
    | sed 's/.* -> //' \
    || echo "")

declare -a MISSING=()
[[ "$modified" != *"docs/hld.md"* ]]    && MISSING+=("docs/hld.md")
[[ "$modified" != *"docs/lld.md"* ]]    && MISSING+=("docs/lld.md")
[[ "$modified" != *"docs/README.md"* ]] && MISSING+=("docs/README.md")

if [[ ${#MISSING[@]} -gt 0 ]]; then
    echo ""
    echo "DOC_UPDATE_REMINDER ─────────────────────────────────────────"
    echo "  Changed : $rel_path"
    echo "  Not yet updated: ${MISSING[*]}"
    echo ""
    echo "  Review whether this change requires updates to:"
    for doc in "${MISSING[@]}"; do
        echo "    • $doc"
    done
    echo ""
    echo "  Sections to consider:"
    echo "    docs/hld.md  → component diagrams, signal flow, tech stack, cost model"
    echo "    docs/lld.md  → class hierarchy, DynamoDB schemas, env vars, state machines"
    echo "    docs/README.md → setup steps, service list, env var reference"
    echo "─────────────────────────────────────────────────────────────"
fi

exit 0
