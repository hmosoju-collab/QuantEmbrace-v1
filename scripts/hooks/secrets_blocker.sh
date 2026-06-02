#!/usr/bin/env bash
# PreToolUse hook: fires before every Write/Edit tool call.
# Scans new file content for hardcoded credential patterns and BLOCKS the write
# if a high-confidence credential is found.
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

# ── Only fire on specific file types ────────────────────────────────────────
case "$file_path" in
    *.py|*.env|*.yaml|*.yml|*.json|*.sh|*.tf) : ;;
    *) exit 0 ;;
esac

# ── Skip safe directories ────────────────────────────────────────────────────
# Normalize the path for prefix matching
for skip_dir in "tests/" "docs/" "memory/" ".claude/" "scripts/hooks/"; do
    if [[ "$file_path" == *"/$skip_dir"* ]] || [[ "$file_path" == "$skip_dir"* ]]; then
        exit 0
    fi
done
# Also skip by directory component matching
if echo "$file_path" | grep -qE '/(tests|docs|memory|\.claude)/'; then
    exit 0
fi

# ── Run credential pattern scan using Python ────────────────────────────────
result=$(python3 - <<'PYEOF' 2>/dev/null
import re, sys

content = sys.stdin.read()

# High-confidence credential patterns (key + value, not just keys)
PATTERNS = [
    # api_key/secret/access_token/zerodha_token/alpaca_key = "literal_value"
    (
        "credential_assignment",
        re.compile(
            r'(?i)(api_key|api_secret|access_token|zerodha.*token|alpaca.*key)\s*=\s*["\'][^"\'${\s<>]{10,}["\']',
            re.MULTILINE
        ),
    ),
    # JWT tokens: eyJxxxxxxxxx
    (
        "jwt_token",
        re.compile(r'eyJ[A-Za-z0-9_-]{20,}', re.MULTILINE),
    ),
    # Alpaca paper/live API key prefix
    (
        "alpaca_api_key",
        re.compile(r'\bPKTEST[A-Za-z0-9]{10,}\b', re.MULTILINE),
    ),
    # 40-char hex string assigned to a variable (sha1/token literals)
    (
        "hex_token_assignment",
        re.compile(
            r'(?i)(token|secret|key|password|passwd|pwd)\s*=\s*["\'][0-9a-f]{40}["\']',
            re.MULTILINE
        ),
    ),
]

# Exclusion markers — lines containing these are ignored
EXCLUSION_MARKERS = [
    "your_", "example", "placeholder", "{", "$", "<", "test_",
    "# ", "os.environ", "os.getenv", "environ.get", "getenv",
]

violations = []
for name, pattern in PATTERNS:
    for match in pattern.finditer(content):
        matched_text = match.group(0)
        # Find the full line containing this match
        line_start = content.rfind("\n", 0, match.start()) + 1
        line_end = content.find("\n", match.end())
        full_line = content[line_start:line_end if line_end != -1 else len(content)]

        # Check exclusion markers
        excluded = False
        for marker in EXCLUSION_MARKERS:
            if marker in full_line:
                excluded = True
                break

        if not excluded:
            # Calculate line number
            line_no = content[:match.start()].count("\n") + 1
            violations.append(f"  [{name}] line {line_no}: {full_line.strip()[:120]}")

if violations:
    print("BLOCKED")
    for v in violations:
        print(v)
else:
    print("OK")
PYEOF
<<<"$content") || exit 0

# ── Evaluate result ──────────────────────────────────────────────────────────
if [[ "${result:0:7}" == "BLOCKED" ]]; then
    echo ""
    echo "SECRETS_BLOCKER ─────────────────────────────────────────────"
    echo "  BLOCKED: Hardcoded credential detected in: $file_path"
    echo ""
    echo "  Violations found:"
    echo "$result" | tail -n +2
    echo ""
    echo "  Rule: Never hardcode credentials in source files."
    echo "  Fix:  Store secrets in AWS Secrets Manager at:"
    echo "          quantembrace/{env}/{service}/{name}"
    echo "        Retrieve via SecretsManager client at runtime."
    echo "─────────────────────────────────────────────────────────────"
    exit 1
fi

exit 0
