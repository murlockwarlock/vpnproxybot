#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run on NL master as root" >&2
  exit 1
fi

INCLUDE_CERTS=0
if [[ "${1:-}" == "--include-certs" ]]; then
  INCLUDE_CERTS=1
fi

TS="$(date +%Y%m%d_%H%M%S)"
OUT_DIR="/root/nl-stack-backups"
WORK_DIR="${OUT_DIR}/work_${TS}"
ARCHIVE="${OUT_DIR}/nl-stack_${TS}.tar.gz"

mkdir -p "${WORK_DIR}"/{env,anewkabot,webstore,marzban,bin,nginx/sites-enabled,nginx/conf.d,nginx/stream.d,systemd,compose}

copy_if_exists() {
  local src="$1"
  local dst="$2"
  if [[ -e "${src}" ]]; then
    cp -a "${src}" "${dst}"
  fi
}

sqlite_backup_or_copy() {
  local src="$1"
  local dst="$2"
  if command -v sqlite3 >/dev/null 2>&1; then
    sqlite3 "${src}" ".backup '${dst}'"
  else
    cp -a "${src}" "${dst}"
  fi
}

copy_if_exists /opt/anewkabot/current/.env "${WORK_DIR}/env/anewkabot.env"
copy_if_exists /opt/webstore/.env.loonapie "${WORK_DIR}/env/webstore.env"
copy_if_exists /opt/marzban/.env "${WORK_DIR}/env/marzban.env"

sqlite_backup_or_copy /opt/anewkabot/current/vpn_bot.db "${WORK_DIR}/anewkabot/vpn_bot.db"
sqlite_backup_or_copy /opt/webstore/webstore_loonapie.db "${WORK_DIR}/webstore/webstore_loonapie.db"
copy_if_exists /opt/webstore/webstore.db "${WORK_DIR}/webstore/webstore.db"

copy_if_exists /var/lib/marzban/. "${WORK_DIR}/marzban/"
if [[ -f /var/lib/marzban/db.sqlite3 ]]; then
  sqlite_backup_or_copy /var/lib/marzban/db.sqlite3 "${WORK_DIR}/marzban/db.sqlite3"
fi
copy_if_exists /usr/local/bin/xray "${WORK_DIR}/bin/xray"

copy_if_exists /etc/nginx/sites-enabled/loonapie "${WORK_DIR}/nginx/sites-enabled/loonapie"
copy_if_exists /etc/nginx/sites-enabled/marzban "${WORK_DIR}/nginx/sites-enabled/marzban"
copy_if_exists /etc/nginx/sites-enabled/darimiru "${WORK_DIR}/nginx/sites-enabled/darimiru"
copy_if_exists /etc/nginx/conf.d/marzban.conf "${WORK_DIR}/nginx/conf.d/marzban.conf"
copy_if_exists /etc/nginx/stream.d/xray-sni.conf "${WORK_DIR}/nginx/stream.d/xray-sni.conf"

copy_if_exists /etc/systemd/system/anewkabot.service "${WORK_DIR}/systemd/anewkabot.service"
copy_if_exists /etc/systemd/system/webstore-loonapie.service "${WORK_DIR}/systemd/webstore-loonapie.service"
copy_if_exists /opt/marzban/docker-compose.yml "${WORK_DIR}/compose/marzban.docker-compose.yml"

if [[ "${INCLUDE_CERTS}" -eq 1 ]]; then
  mkdir -p "${WORK_DIR}/letsencrypt"
  copy_if_exists /etc/letsencrypt/. "${WORK_DIR}/letsencrypt/"
else
  cat > "${WORK_DIR}/CERTS_NOT_INCLUDED.txt" <<'EOF'
TLS certificates were not included. On restore, either:
1. rerun this script with --include-certs, or
2. point DNS to the new server and issue fresh certificates for loonapie.xyz and vpn.psysoldatov.ru.
EOF
fi

cat > "${WORK_DIR}/MANIFEST.txt" <<EOF
Created: ${TS}
Source host: $(hostname -f 2>/dev/null || hostname)
Includes: Anewka bot DB/env, webstore DB/env, Marzban data/env, nginx configs, xray binary, systemd references.
Certs included: ${INCLUDE_CERTS}
EOF

chmod +x "${WORK_DIR}/bin/xray" 2>/dev/null || true
mkdir -p "${OUT_DIR}"
tar -C "${WORK_DIR}" -czf "${ARCHIVE}" .
rm -rf "${WORK_DIR}"

echo "${ARCHIVE}"
