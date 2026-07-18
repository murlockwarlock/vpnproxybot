#!/usr/bin/env bash
# Deploy @darimiru_bot to 45.92.174.214
# Uses shared venv at /root/telegram_bots/venv
# Runs via PM2 alongside other bots
set -euo pipefail

REMOTE_HOST="45.92.174.214"
REMOTE_USER="root"
APP_DIR="/root/telegram_bots/darimiru_bot"
VENV="/root/telegram_bots/venv"
PM2_NAME="darimiru_bot"
LOCAL_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== Deploying @darimiru_bot to $REMOTE_HOST ==="

# 1. Create remote directory
ssh "$REMOTE_USER@$REMOTE_HOST" "mkdir -p $APP_DIR"

# 2. Sync bot code (exclude local stuff)
rsync -avz --delete \
    --exclude='.env' \
    --exclude='.env.*' \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='.git' \
    --exclude='vpn_bot.db*' \
    --exclude='darimiru_bot.db*' \
    --exclude='chroma_db' \
    --exclude='bot.log' \
    --exclude='backups' \
    --exclude='tmp' \
    --exclude='tests' \
    --exclude='landing' \
    --exclude='server-setup' \
    "$LOCAL_DIR/bot/" "$REMOTE_USER@$REMOTE_HOST:$APP_DIR/bot/"

# 3. Copy supporting files
scp "$LOCAL_DIR/requirements.txt" "$REMOTE_USER@$REMOTE_HOST:$APP_DIR/"

# 4. Upload .env.darimiru as .env
scp "$LOCAL_DIR/.env.darimiru" "$REMOTE_USER@$REMOTE_HOST:$APP_DIR/.env"

# 5. Install any missing deps into shared venv (safe — pip won't downgrade)
ssh "$REMOTE_USER@$REMOTE_HOST" "$VENV/bin/pip install -q -r $APP_DIR/requirements.txt"

# 6. Stop existing PM2 process if running (safe if doesn't exist)
ssh "$REMOTE_USER@$REMOTE_HOST" "pm2 delete $PM2_NAME 2>/dev/null || true"

# 7. Start via PM2 with shared venv
ssh "$REMOTE_USER@$REMOTE_HOST" "cd $APP_DIR && pm2 start bot/main.py \
    --name $PM2_NAME \
    --interpreter $VENV/bin/python \
    --cwd $APP_DIR \
    --log-date-format 'YYYY-MM-DD HH:mm:ss' \
    --max-restarts 10 \
    --restart-delay 5000"

# 8. Save PM2 config
ssh "$REMOTE_USER@$REMOTE_HOST" "pm2 save"

echo ""
echo "=== Done! Checking status... ==="
ssh "$REMOTE_USER@$REMOTE_HOST" "pm2 show $PM2_NAME"
