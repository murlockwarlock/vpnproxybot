#!/usr/bin/env bash
set -euo pipefail

BUNDLE="${1:-}"
if [[ -z "${BUNDLE}" || ! -f "${BUNDLE}" ]]; then
  echo "Usage: $0 /path/to/nl-stack_YYYYmmdd_HHMMSS.tar.gz" >&2
  exit 1
fi

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMPORT_DIR="${ROOT_DIR}/data/import"

rm -rf "${IMPORT_DIR}"
mkdir -p "${IMPORT_DIR}" \
  "${ROOT_DIR}/data/env" \
  "${ROOT_DIR}/data/anewkabot" \
  "${ROOT_DIR}/data/webstore" \
  "${ROOT_DIR}/data/marzban" \
  "${ROOT_DIR}/data/bin" \
  "${ROOT_DIR}/data/backups" \
  "${ROOT_DIR}/data/letsencrypt"

tar -C "${IMPORT_DIR}" -xzf "${BUNDLE}"

copy_if_exists() {
  local src="$1"
  local dst="$2"
  if [[ -e "${src}" ]]; then
    cp -a "${src}" "${dst}"
  fi
}

copy_if_exists "${IMPORT_DIR}/env/anewkabot.env" "${ROOT_DIR}/data/env/anewkabot.env"
copy_if_exists "${IMPORT_DIR}/env/webstore.env" "${ROOT_DIR}/data/env/webstore.env"
copy_if_exists "${IMPORT_DIR}/env/marzban.env" "${ROOT_DIR}/data/env/marzban.env"

copy_if_exists "${IMPORT_DIR}/anewkabot/vpn_bot.db" "${ROOT_DIR}/data/anewkabot/vpn_bot.db"
copy_if_exists "${IMPORT_DIR}/webstore/webstore_loonapie.db" "${ROOT_DIR}/data/webstore/webstore_loonapie.db"
copy_if_exists "${IMPORT_DIR}/webstore/webstore.db" "${ROOT_DIR}/data/webstore/webstore.db"
copy_if_exists "${IMPORT_DIR}/marzban/." "${ROOT_DIR}/data/marzban/"
copy_if_exists "${IMPORT_DIR}/bin/xray" "${ROOT_DIR}/data/bin/xray"
copy_if_exists "${IMPORT_DIR}/letsencrypt/." "${ROOT_DIR}/data/letsencrypt/"

# Prefer live nginx configs from NL. The compose stack uses host networking, so
# 127.0.0.1 upstreams from production stay valid inside nginx.
copy_if_exists "${IMPORT_DIR}/nginx/sites-enabled/loonapie" "${ROOT_DIR}/nginx/conf.d/loonapie.conf"
copy_if_exists "${IMPORT_DIR}/nginx/sites-enabled/marzban" "${ROOT_DIR}/nginx/conf.d/marzban.conf"
copy_if_exists "${IMPORT_DIR}/nginx/stream.d/xray-sni.conf" "${ROOT_DIR}/nginx/stream.d/xray-sni.conf"

chmod 600 "${ROOT_DIR}"/data/env/*.env 2>/dev/null || true
chmod +x "${ROOT_DIR}/data/bin/xray" 2>/dev/null || true

if [[ ! -s "${ROOT_DIR}/data/env/anewkabot.env" ]]; then
  echo "Missing data/env/anewkabot.env" >&2
  exit 1
fi
if [[ ! -s "${ROOT_DIR}/data/env/webstore.env" ]]; then
  echo "Missing data/env/webstore.env" >&2
  exit 1
fi
if [[ ! -s "${ROOT_DIR}/data/env/marzban.env" ]]; then
  echo "Missing data/env/marzban.env" >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "docker compose is not installed" >&2
  exit 1
fi

cd "${ROOT_DIR}"
"${COMPOSE[@]}" build bot webstore
"${COMPOSE[@]}" up -d
"${COMPOSE[@]}" ps

echo "Restore started from ${BUNDLE}"
echo "Before switching traffic: point DNS for loonapie.xyz and vpn.psysoldatov.ru to this server, or include/reissue TLS certs."
