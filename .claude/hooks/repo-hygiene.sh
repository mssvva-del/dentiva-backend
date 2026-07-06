#!/usr/bin/env bash
# Repo-hygiene advisory hook (runs on Stop). Deterministic, NON-BLOCKING: it prints
# warnings and always exits 0 — it never fails a turn, it just keeps the repo from
# quietly rotting (doc bloat, junk files, secrets/.env slipping into a commit).
#
# Wired in .claude/settings.json. Checks are cheap (git + wc), so it's fine per-turn.
set -uo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$root" || exit 0
command -v git >/dev/null 2>&1 || exit 0
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || exit 0

warn() { echo "⚠️  hygiene: $*"; }

# 1. CLAUDE.md bloat — the guide must stay lean to be read every session.
if [ -f CLAUDE.md ]; then
  n=$(wc -l < CLAUDE.md | tr -d ' ')
  [ "$n" -gt 120 ] && warn "CLAUDE.md is ${n} lines (>120) — trim it."
fi

# 2. Oversized tracked docs (skip vendored trees).
while IFS= read -r f; do
  [ -f "$f" ] || continue
  n=$(wc -l < "$f" | tr -d ' ')
  [ "$n" -gt 400 ] && warn "${f} is ${n} lines — consider splitting/archiving."
done < <(git ls-files '*.md' 2>/dev/null | grep -vE '\.terraform|node_modules|\.venv')

# 3. Junk files anywhere in the working tree (tracked or not).
junk=$(git status --porcelain 2>/dev/null | awk '{print $NF}' \
  | grep -iE '\.(tmp|bak|old|log|zip|sqlite3?|dump)$|\.DS_Store$' || true)
[ -n "$junk" ] && warn "junk files present: $(echo "$junk" | tr '\n' ' ')"

# 4. Potential secrets in the STAGED diff (before they get committed).
if ! git diff --cached --quiet 2>/dev/null; then
  hits=$(git diff --cached 2>/dev/null | grep -iErn \
    'BEGIN (RSA|OPENSSH|EC|PRIVATE) KEY|sk-[A-Za-z0-9]{20,}|gsk_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}' \
    || true)
  [ -n "$hits" ] && warn "possible SECRET in staged diff — review before committing. ROTATE if it's real (git history keeps it)."
fi

# 5. A .env must never be staged.
git diff --cached --name-only 2>/dev/null | grep -qE '(^|/)\.env$' \
  && warn ".env is staged — it must stay gitignored (unstage it)."

# 6. Nudge toward L3 skills when there are none yet.
[ -d .claude/skills ] || warn "no .claude/skills/ — recurring procedures (add-endpoint, migration, deploy) belong there."

exit 0
