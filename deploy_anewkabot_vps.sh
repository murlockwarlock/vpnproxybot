#!/usr/bin/env bash
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive

APP_ROOT=/opt/anewkabot
APP_DIR="$APP_ROOT/current"
VENV_DIR="$APP_ROOT/venv"
RUNTIME_REQ="$APP_ROOT/runtime-requirements.txt"
SERVICE_FILE=/etc/systemd/system/anewkabot.service

mkdir -p "$APP_ROOT"

pkill -f "/opt/anewkabot/venv/bin/pip install -r /opt/anewkabot/current/requirements.txt" || true
pkill -f "/opt/anewkabot/venv/bin/pip install -r /opt/anewkabot/runtime-requirements.txt" || true

rm -rf "$VENV_DIR"
python3 -m venv "$VENV_DIR" || {
  apt-get update
  apt-get install -y python3-venv
  python3 -m venv "$VENV_DIR"
}

cat > "$RUNTIME_REQ" <<'EOF'
aiogram>=3.15.0
sqlalchemy[asyncio]>=2.0.36
aiosqlite>=0.20.0
asyncssh>=2.18.0
apscheduler>=3.10.4
python-dotenv>=1.0.1
yookassa>=3.0.0
aiohttp>=3.9.0
openai>=1.12.0
langchain>=0.1.0
langchain-text-splitters>=0.0.1
python-docx>=1.1.0
EOF

"$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel
PIP_ROOT_USER_ACTION=ignore "$VENV_DIR/bin/pip" install -r "$RUNTIME_REQ"

cat > "$SERVICE_FILE" <<'EOF'
[Unit]
Description=Anewka Telegram Bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=/opt/anewkabot/current
EnvironmentFile=/opt/anewkabot/current/.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/opt/anewkabot/venv/bin/python -m bot
Restart=always
RestartSec=5
User=root
Group=root

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable anewkabot.service
systemctl restart anewkabot.service
sleep 5

systemctl --no-pager --full status anewkabot.service
