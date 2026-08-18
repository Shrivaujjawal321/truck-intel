#!/usr/bin/env bash
# Fail the build if a credential is published, or is about to be.
#
# Written 2026-08-18 after a review found the LIVE Postgres owner password was
# the dev default committed to this repo — which is public — while the container
# was also published on 0.0.0.0:5432. Both are fixed; this stops the regression.
#
# Runs in two modes, and needs no arguments:
#   with a .env  (a developer machine) — every check below
#   without one  (CI)                  — the static checks only
#
# It never prints a secret value. Key names and file paths only.
set -uo pipefail
cd "$(dirname "$0")/.."

fail=0
bad() { printf '  FAIL  %s\n' "$*" >&2; fail=1; }
ok()  { printf '  ok    %s\n' "$*"; }

echo "preflight: secrets"

# 1. .env must never be tracked ------------------------------------------------
if git ls-files --error-unmatch .env >/dev/null 2>&1; then
    bad ".env is tracked by git — remove it from the index: git rm --cached .env"
else
    ok ".env is not tracked"
fi

# 1b. nor any rotation backup of it -------------------------------------------
# `make rotate-track-password` and manual rotations leave .env.bak-<stamp>
# files. They hold the PREVIOUS credentials, which is the same blast radius as
# .env, and a bare `git add -A` would sweep them in.
bak_tracked="$(git ls-files -- '.env.bak-*' 2>/dev/null)"
if [ -n "$bak_tracked" ]; then
    bad ".env backup(s) tracked by git: $(printf '%s' "$bak_tracked" | tr '\n' ' ')"
else
    ok "no .env backups are tracked"
fi

# 2. ...and must never have been, in any branch or tag -------------------------
if [ -n "$(git log --all --oneline -- .env 2>/dev/null)" ]; then
    bad ".env appears in git history — the values in it are leaked and need rotating"
else
    ok ".env has never been committed"
fi

# 3. .env must not be readable by other users ---------------------------------
if [ -f .env ]; then
    mode="$(stat -c '%a' .env)"
    case "$mode" in
        *[1-7]) bad ".env is mode $mode — world-readable. chmod 600 .env" ;;
        *)      ok ".env is mode $mode" ;;
    esac
fi

# 4. no known-bad password literal back in the code ---------------------------
# Scoped to code, not docs: data/reviews/ documents this incident by name and
# those credentials are already dead.
known_bad='truckintel_dev|truckintel_track_dev|REPLACE_ME_ACTUAL|changeme'
hits="$(git grep -nIE "$known_bad" -- '*.py' '*.sh' '*.sql' '*.yml' '*.yaml' 'Makefile' '*.txt' 2>/dev/null)"
if [ -n "$hits" ]; then
    bad "a known dev-default credential is back in tracked code:"
    printf '%s\n' "$hits" | sed 's/^/          /' >&2
else
    ok "no known dev-default credentials in tracked code"
fi

# 5. nothing currently live may appear in a tracked file ----------------------
# The check that actually matters, and the only one that would have caught the
# original problem: it compares against what this machine is really using.
if [ -f .env ]; then
    leaked=0
    while IFS= read -r key; do
        raw="$(grep -E "^${key}=" .env | head -1 | cut -d= -f2- )"
        [ -n "$raw" ] || continue
        case "$key" in
            *DATABASE_URL) secret="$(printf '%s' "$raw" | sed -nE 's|^[a-z+]+://[^:/@]+:([^@]+)@.*|\1|p')" ;;
            *)             secret="$raw" ;;
        esac
        # Ignore placeholders and anything too short to be a real secret.
        [ -n "$secret" ] && [ ${#secret} -ge 8 ] || continue
        case "$secret" in REPLACE_ME*|UNSET*) continue ;; esac
        if git grep -qIF -- "$secret" 2>/dev/null; then
            bad "the live value of $key appears in a tracked file — rotate it and remove the literal"
            leaked=1
        fi
    done <<'KEYS'
DATABASE_URL
TRACK_DATABASE_URL
EIA_API_KEY
TELEGRAM_BOT_TOKEN
KEYS
    [ "$leaked" -eq 0 ] && ok "no live .env value appears in any tracked file"
fi

# 6. the database must not be published to the network ------------------------
if git grep -nIE '^\s*-p\s+(0\.0\.0\.0:)?5432:5432' -- '*.sh' '*.yml' 'Makefile' >/dev/null 2>&1; then
    bad "a script publishes Postgres on all interfaces — bind it to 127.0.0.1:5432:5432"
    git grep -nIE '^\s*-p\s+(0\.0\.0\.0:)?5432:5432' -- '*.sh' '*.yml' 'Makefile' | sed 's/^/          /' >&2
else
    ok "no script publishes Postgres on 0.0.0.0"
fi

# The running container is a runtime fact, not a repo fact — only checkable here.
if command -v docker >/dev/null 2>&1 && docker ps --format '{{.Names}}' 2>/dev/null | grep -qx truckintel-pg; then
    if docker port truckintel-pg 5432 2>/dev/null | grep -q '^0\.0\.0\.0'; then
        bad "the RUNNING truckintel-pg is published on 0.0.0.0:5432 — docker rm -f truckintel-pg && scripts/db_up.sh"
    else
        ok "running container is bound to loopback"
    fi
fi

if [ "$fail" -ne 0 ]; then
    echo "preflight: FAILED" >&2
    exit 1
fi
echo "preflight: clean"
