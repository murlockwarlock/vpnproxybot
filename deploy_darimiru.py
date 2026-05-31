#!/usr/bin/env python3
"""Deploy @darimiru_bot to YOUR_SERVER_IP via paramiko (password auth)."""

import os
import sys
import stat
import hashlib
import shlex
import paramiko

from scripts.deploy_guard import ensure_clean_git, git_revision, load_local_env, require_env

HOST = "YOUR_SERVER_IP"
USER = "root"
load_local_env(os.path.dirname(os.path.abspath(__file__)))
PASSWORD = require_env("DARIMIRU_SSH_PASSWORD")
APP_DIR = "/root/telegram_bots/darimiru_bot"
VENV = "/root/telegram_bots/venv"
PM2_NAME = "darimiru_bot"
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))
REQ_MARKER = f"{APP_DIR}/.requirements.sha256"

EXCLUDE_DIRS = {
    "__pycache__", ".git", "chroma_db", "backups", "tmp",
    "tests", "landing", "server-setup", "scripts", ".mypy_cache",
}
EXCLUDE_FILES = {
    ".DS_Store", ".env", ".env.darimiru", ".env.example",
    "vpn_bot.db", "vpn_bot.db-shm", "vpn_bot.db-wal",
    "darimiru_bot.db", "bot.log",
    "deploy_darimiru_vps.sh", "deploy_darimiru.py",
    "deploy_anewkabot_vps.sh", "ssh_exec.py", "ssh_transfer.py",
    "migrate.py", "walkthrough.md.resolved",
}


def ssh_connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=15)
    return ssh


def run_cmd(ssh, cmd, check=False):
    print(f"  $ {cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=120)
    out = stdout.read().decode()
    err = stderr.read().decode()
    code = stdout.channel.recv_exit_status()
    if out.strip():
        print(out.rstrip())
    if err.strip():
        print(f"  STDERR: {err.rstrip()}")
    if check and code != 0:
        print(f"  COMMAND FAILED ({code})")
        ssh.close()
        sys.exit(code)
    return out, err


def upload_dir(sftp, local_dir, remote_dir, rel_prefix=""):
    """Recursively upload a directory, skipping unchanged files (mtime-based)."""
    for item in sorted(os.listdir(local_dir)):
        local_path = os.path.join(local_dir, item)
        remote_path = f"{remote_dir}/{item}"

        if os.path.isdir(local_path):
            if item in EXCLUDE_DIRS:
                continue
            try:
                sftp.stat(remote_path)
            except FileNotFoundError:
                sftp.mkdir(remote_path)
            upload_dir(sftp, local_path, remote_path, f"{rel_prefix}{item}/")
        else:
            if item in EXCLUDE_FILES or item.endswith(".pyc"):
                continue
            local_mtime = os.path.getmtime(local_path)
            local_size = os.path.getsize(local_path)
            try:
                remote_stat = sftp.stat(remote_path)
                if (int(remote_stat.st_mtime) == int(local_mtime)
                        and remote_stat.st_size == local_size):
                    continue  # unchanged
            except FileNotFoundError:
                pass
            print(f"  -> {rel_prefix}{item}")
            sftp.put(local_path, remote_path)
            sftp.utime(remote_path, (local_mtime, local_mtime))


def file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main():
    revision = ensure_clean_git(LOCAL_DIR)
    print(f"=== Deploying @darimiru_bot to {HOST} ===")
    print(f"Revision: {revision}\n")

    ssh = ssh_connect()
    sftp = ssh.open_sftp()

    # 1. Create directory structure
    print("[1/6] Creating directories...")
    run_cmd(ssh, f"mkdir -p {APP_DIR}/bot {APP_DIR}/webstore/assets")

    # 2. Upload bot/ and webstore/ directories
    print("\n[2/6] Uploading bot code...")
    bot_local = os.path.join(LOCAL_DIR, "bot")
    upload_dir(sftp, bot_local, f"{APP_DIR}/bot", "bot/")

    print("\n[2b/6] Uploading webstore code...")
    ws_local = os.path.join(LOCAL_DIR, "webstore")
    upload_dir(sftp, ws_local, f"{APP_DIR}/webstore", "webstore/")

    sftp.file(f"{APP_DIR}/REVISION", "w").write(revision + "\n")
    print(f"  -> REVISION {revision}")

    # 3. Upload requirements.txt
    print("\n[3/6] Uploading requirements.txt...")
    sftp.put(os.path.join(LOCAL_DIR, "requirements.txt"), f"{APP_DIR}/requirements.txt")
    print("  -> requirements.txt")

    # 4. Upload .env only if it doesn't exist on the server yet
    print("\n[4/6] Checking .env...")
    try:
        sftp.stat(f"{APP_DIR}/.env")
        print("  .env already exists on server — skipping (edit on server directly)")
    except FileNotFoundError:
        sftp.put(os.path.join(LOCAL_DIR, ".env.darimiru"), f"{APP_DIR}/.env")
        print("  -> .env (initial upload)")

    sftp.close()

    # 5. Install deps only when requirements.txt changes.
    print("\n[5/6] Installing dependencies if needed (shared venv)...")
    req_hash = file_sha256(os.path.join(LOCAL_DIR, "requirements.txt"))
    remote_hash, _ = run_cmd(ssh, f"cat {shlex.quote(REQ_MARKER)} 2>/dev/null || true")
    if remote_hash.strip() == req_hash:
        print("  requirements.txt unchanged — skipping pip install")
    else:
        run_cmd(ssh, f"{VENV}/bin/pip install --no-cache-dir -q -r {APP_DIR}/requirements.txt 2>&1 | tail -5")
        run_cmd(ssh, f"printf %s {shlex.quote(req_hash)} > {shlex.quote(REQ_MARKER)}")

    # 6. Validate and restart via PM2
    print("\n[6/6] Validating and restarting PM2 process...")
    run_cmd(ssh, (
        f"cd {APP_DIR} && {VENV}/bin/python -m py_compile "
        f"bot/webhooks.py bot/handlers/payment.py bot/services/vhq_subscription_proxy.py "
        f"bot/services/scheduler.py bot/services/subscription_service.py"
    ), check=True)
    # Graceful reload if process exists, otherwise start fresh
    out, _ = run_cmd(ssh, f"pm2 id {PM2_NAME} 2>/dev/null || true")
    if out.strip() and out.strip() != "[]":
        run_cmd(ssh, f"pm2 reload {PM2_NAME} --update-env")
    else:
        run_cmd(ssh, (
            f"cd {APP_DIR} && pm2 start {VENV}/bin/python "
            f"--name {PM2_NAME} "
            f"--interpreter none "
            f"--cwd {APP_DIR} "
            f'--log-date-format "YYYY-MM-DD HH:mm:ss" '
            f"--max-restarts 10 "
            f"--restart-delay 5000 "
            f"-- -m bot"
        ))
    run_cmd(ssh, "pm2 save")

    print("\n=== Restarting webstore ===")
    run_cmd(ssh, (
        "systemctl kill -s SIGKILL webstore-darimiru 2>/dev/null; "
        "sleep 1; systemctl start webstore-darimiru; sleep 2; systemctl is-active webstore-darimiru"
    ))

    print("\n=== Checking status ===")
    run_cmd(ssh, f"pm2 jlist | python3 -c \""
        f"import sys,json; procs=[p for p in json.load(sys.stdin) if p.get('name')=='{PM2_NAME}'];"
        f"p=procs[0] if procs else {{}}; "
        f"print(p.get('name','?'), p.get('pm2_env',{{}}).get('status','?'), 'pid:', p.get('pid','?'))\"")

    print("\n=== Last 5 lines of log ===")
    run_cmd(ssh, f"pm2 logs {PM2_NAME} --nostream --lines 5 2>&1")

    ssh.close()
    print("\n=== Deploy complete! ===")


if __name__ == "__main__":
    main()
