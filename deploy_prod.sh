#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -f "$ROOT_DIR/.env.deploy" ]]; then
    set -a
    source "$ROOT_DIR/.env.deploy"
    set +a
fi

HOST="${ANEWKA_PROD_HOST:-}"
USER_NAME="${ANEWKA_PROD_USER:-root}"
PASSWORD="${ANEWKA_PROD_PASSWORD:-${ANEWKA_SSH_PASSWORD:-}}"
SSH_KEY="${ANEWKA_PROD_SSH_KEY:-$HOME/.ssh/yc_key}"
REMOTE_DIR="${ANEWKA_REMOTE_DIR:-/opt/anewkabot/current}"
REMOTE_PY="${ANEWKA_REMOTE_PY:-/opt/anewkabot/venv/bin/python}"
SERVICE="${ANEWKA_SERVICE:-anewkabot.service}"
ALLOW_DIRTY=0

for arg in "$@"; do
    case "$arg" in
        --allow-dirty) ALLOW_DIRTY=1 ;;
        -h|--help)
            echo "Usage: ./deploy_prod.sh [--allow-dirty]"
            exit 0
            ;;
        *)
            echo "Unknown argument: $arg" >&2
            exit 1
            ;;
    esac
done

if [[ -z "$HOST" ]]; then
    echo "Set ANEWKA_PROD_HOST in .env.deploy or the environment." >&2
    exit 1
fi

if [[ "$ALLOW_DIRTY" -ne 1 ]]; then
    if ! git diff --quiet || ! git diff --cached --quiet || [[ -n "$(git ls-files --others --exclude-standard)" ]]; then
        echo "Refusing to deploy a dirty worktree." >&2
        exit 1
    fi
fi

REVISION="$(git rev-parse --short HEAD)"
ARCHIVE="$(mktemp -t "anewkabot-${REVISION}.XXXXXX.tar.gz")"
REMOTE_ARCHIVE="/tmp/anewkabot-${REVISION}.tar.gz"
trap 'rm -f "$ARCHIVE"' EXIT
git archive --format=tar.gz --output="$ARCHIVE" HEAD

SSH_CMD=(ssh -o StrictHostKeyChecking=no)
if [[ -f "$SSH_KEY" ]]; then
    SSH_CMD+=(-i "$SSH_KEY")
fi
if [[ -n "$PASSWORD" ]]; then
    export SSHPASS="$PASSWORD"
    SSH_CMD=(sshpass -e "${SSH_CMD[@]}")
fi

echo "Deploying Anewka revision $REVISION to ${USER_NAME}@${HOST}:${REMOTE_DIR}"
cat "$ARCHIVE" | "${SSH_CMD[@]}" "${USER_NAME}@${HOST}" "cat > '${REMOTE_ARCHIVE}'"
"${SSH_CMD[@]}" "${USER_NAME}@${HOST}" \
    "set -e; trap 'rm -f ${REMOTE_ARCHIVE}' EXIT; \
     cd '${REMOTE_DIR}'; \
     tar xzf '${REMOTE_ARCHIVE}'; \
     printf '%s\n' '${REVISION}' > REVISION; \
     find bot -type f -name '._*.py' -delete; \
     '${REMOTE_PY}' -m compileall -q bot; \
     systemctl restart '${SERVICE}'; \
     sleep 5; \
     systemctl is-active --quiet '${SERVICE}'; \
     test \"\$(cat REVISION)\" = '${REVISION}'"

echo "Anewka deploy complete: $REVISION"
