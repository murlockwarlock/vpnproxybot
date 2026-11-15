#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

usage() {
    cat <<'EOF'
Usage: ./deploy_darimiru_prod.sh --revision SHA --expected-sha SHA --preflight-only
       ./deploy_darimiru_prod.sh --revision SHA --expected-sha SHA --confirm-production SHA

Deploy the exact committed artifact using the committed HEAD deployment tooling.
Run preflight, review its output, then obtain production approval before execution.
Dedicated QA credentials require DARIMIRU_SMOKE_CONTACT, DARIMIRU_SMOKE_PASSWORD
and DARIMIRU_SMOKE_TELEGRAM_ID. Existing active daily modes are preserved.
Configuration comes from the environment or local ignored .env.deploy.
EOF
}

EXPECTED_SHA=""
REVISION="HEAD"
CONFIRMED_SHA=""
PREFLIGHT_ONLY=0
while (($#)); do
    case "$1" in
        --expected-sha|--revision|--confirm-production)
            [[ "$#" -ge 2 ]] || { echo "Missing argument for $1" >&2; exit 2; }
            case "$1" in
                --expected-sha) EXPECTED_SHA="$2" ;;
                --revision) REVISION="$2" ;;
                --confirm-production) CONFIRMED_SHA="$2" ;;
            esac
            shift 2
            ;;
        --preflight-only) PREFLIGHT_ONLY=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; exit 2 ;;
    esac
done

if [[ -f "$ROOT_DIR/.env.deploy" ]]; then
    set -a
    source "$ROOT_DIR/.env.deploy"
    set +a
fi

unset ALLOW_DIRTY_DEPLOY
HEAD_SHA="$(python3 - "$ROOT_DIR" <<'PY'
import sys

from scripts.deploy_guard import ensure_clean_git

print(ensure_clean_git(sys.argv[1]))
PY
)"

DEPLOY_SHA="$(git rev-parse --verify "$REVISION^{commit}")"
if [[ ! "$EXPECTED_SHA" =~ ^[0-9a-f]{40}$ || "$DEPLOY_SHA" != "$EXPECTED_SHA" ]]; then
    echo "A matching full --expected-sha is required." >&2
    exit 1
fi
python3 - "$ROOT_DIR" "$DEPLOY_SHA" <<'PY'
import sys

from scripts.deploy_guard import ensure_candidate_safety_contract

ensure_candidate_safety_contract(sys.argv[1], sys.argv[2])
PY
if [[ "$PREFLIGHT_ONLY" -ne 1 && "$CONFIRMED_SHA" != "$DEPLOY_SHA" ]]; then
    echo "Production execution requires fresh --confirm-production with the exact full SHA after preflight review." >&2
    exit 1
fi

TRACKED_RUNTIME="$(git ls-files -- .env .env.darimiru .env.webstore '*.db' '*.db-*' '*.sqlite' '*.sqlite-*' '*.sqlite3' '*.sqlite3-*')"
if [[ -n "$TRACKED_RUNTIME" ]]; then
    echo "Refusing to deploy tracked runtime data:" >&2
    echo "$TRACKED_RUNTIME" >&2
    exit 1
fi

HOST="${DARIMIRU_PROD_HOST:-}"
USER_NAME="${DARIMIRU_PROD_USER:-root}"
PASSWORD="${DARIMIRU_PROD_PASSWORD:-}"
SSH_KEY="${DARIMIRU_PROD_SSH_KEY:-}"
REMOTE_DIR="${DARIMIRU_REMOTE_DIR:-/root/telegram_bots/darimiru_bot}"
VENV="${DARIMIRU_VENV:-/root/telegram_bots/venv}"
PM2_NAME="${DARIMIRU_PM2_NAME:-darimiru_bot}"
WEBSTORE_SERVICE="${DARIMIRU_WEBSTORE_SERVICE:-webstore-darimiru.service}"

if [[ -z "$HOST" ]]; then
    echo "Missing DARIMIRU_PROD_HOST." >&2
    exit 1
fi
if [[ -n "$SSH_KEY" && ! -r "$SSH_KEY" ]]; then
    echo "DARIMIRU_PROD_SSH_KEY is not readable." >&2
    exit 1
fi

SSH_OPTIONS=(-o StrictHostKeyChecking=yes -o ConnectTimeout=10)
if [[ -n "$SSH_KEY" ]]; then
    SSH_OPTIONS+=(-i "$SSH_KEY")
    SSH_CMD=(ssh "${SSH_OPTIONS[@]}")
    SCP_CMD=(scp "${SSH_OPTIONS[@]}")
elif [[ -n "$PASSWORD" ]]; then
    if ! command -v sshpass >/dev/null; then
        echo "DARIMIRU_PROD_PASSWORD requires sshpass." >&2
        exit 1
    fi
    export SSHPASS="$PASSWORD"
    SSH_CMD=(sshpass -e ssh "${SSH_OPTIONS[@]}")
    SCP_CMD=(sshpass -e scp "${SSH_OPTIONS[@]}")
else
    SSH_OPTIONS+=(-o BatchMode=yes)
    SSH_CMD=(ssh "${SSH_OPTIONS[@]}")
    SCP_CMD=(scp "${SSH_OPTIONS[@]}")
fi

TARGET="${USER_NAME}@${HOST}"
LOCAL_ARCHIVE=""
LOCAL_TOOLS=""
REMOTE_ARCHIVE=""
REMOTE_TOOLS=""

shell_quote() {
    if [[ "$1" == *"'"* || "$1" == *$'\n'* ]]; then
        echo "Unsafe value for remote command." >&2
        return 1
    fi
    printf "'%s'" "$1"
}

cleanup() {
    local status=$?
    trap - EXIT
    if [[ -n "$LOCAL_ARCHIVE" ]]; then rm -f -- "$LOCAL_ARCHIVE"; fi
    if [[ -n "$LOCAL_TOOLS" ]]; then rm -f -- "$LOCAL_TOOLS"; fi
    if [[ -n "$REMOTE_ARCHIVE" ]]; then
        "${SSH_CMD[@]}" "$TARGET" "rm -f -- $(shell_quote "$REMOTE_ARCHIVE")" >/dev/null 2>&1 || true
    fi
    exit "$status"
}
trap cleanup EXIT

if [[ "$PREFLIGHT_ONLY" -ne 1 ]]; then python3 scripts/darimiru_deploy_runner.py check-config; fi
LOCAL_ARCHIVE="$(mktemp "${TMPDIR:-/tmp}/darimiru-deploy.XXXXXX.tar.gz")"
LOCAL_TOOLS="$(mktemp "${TMPDIR:-/tmp}/darimiru-tools.XXXXXX.tar.gz")"
git archive --format=tar.gz --output="$LOCAL_ARCHIVE" "$DEPLOY_SHA"
git archive --format=tar.gz --output="$LOCAL_TOOLS" "$HEAD_SHA" scripts/darimiru_deploy_runner.py scripts/darimiru_deploy_safety.py scripts/smoke_darimiru_financial.py
REMOTE_ARCHIVE="$("${SSH_CMD[@]}" "$TARGET" "mktemp /tmp/darimiru-deploy.XXXXXX.tar.gz")"
REMOTE_TOOLS="$("${SSH_CMD[@]}" "$TARGET" "mktemp -d /tmp/darimiru-tools.XXXXXX")"
[[ "$REMOTE_ARCHIVE" =~ ^/tmp/darimiru-deploy\.[A-Za-z0-9]+\.tar\.gz$ ]] || { echo "Invalid remote archive path" >&2; exit 1; }
[[ "$REMOTE_TOOLS" =~ ^/tmp/darimiru-tools\.[A-Za-z0-9]+$ ]] || { echo "Invalid remote tooling path" >&2; exit 1; }
"${SCP_CMD[@]}" "$LOCAL_ARCHIVE" "$TARGET:$REMOTE_ARCHIVE"
"${SCP_CMD[@]}" "$LOCAL_TOOLS" "$TARGET:$REMOTE_TOOLS/tools.tar.gz"
"${SSH_CMD[@]}" "$TARGET" "tar -xzf $(shell_quote "$REMOTE_TOOLS/tools.tar.gz") -C $(shell_quote "$REMOTE_TOOLS")"
MODE="run"
CONFIG_MODE="emit-config"
if [[ "$PREFLIGHT_ONLY" -eq 1 ]]; then MODE="preflight"; CONFIG_MODE="emit-preflight-config"; fi
REMOTE_COMMAND="$(shell_quote "$VENV/bin/python") $(shell_quote "$REMOTE_TOOLS/scripts/darimiru_deploy_runner.py") $MODE --archive $(shell_quote "$REMOTE_ARCHIVE") --root $(shell_quote "$REMOTE_DIR") --python $(shell_quote "$VENV/bin/python") --pm2-name $(shell_quote "$PM2_NAME") --service $(shell_quote "$WEBSTORE_SERVICE") --revision $(shell_quote "$DEPLOY_SHA")"
printf 'Tooling SHA: %s; deployment SHA: %s; mode: %s\n' "$HEAD_SHA" "$DEPLOY_SHA" "$MODE"
python3 scripts/darimiru_deploy_runner.py "$CONFIG_MODE" | "${SSH_CMD[@]}" "$TARGET" "$REMOTE_COMMAND"
