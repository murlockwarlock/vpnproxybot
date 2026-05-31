#!/usr/bin/env python3
"""Deploy @anewkabot / @uskoritelinternetabot to NL server (YOUR_SERVER_IP) via paramiko."""

import hashlib
import os
import shlex
import sys

import paramiko

from scripts.deploy_guard import ensure_clean_git, load_local_env, require_env

HOST = "YOUR_SERVER_IP"
USER = "root"
load_local_env(os.path.dirname(os.path.abspath(__file__)))
PASSWORD = require_env("ANEWKA_SSH_PASSWORD")
APP_DIR = "/opt/anewkabot/current"
WEBSTORE_RUNTIME_DIR = "/opt/webstore"
VENV = "/opt/anewkabot/venv"
SERVICE_NAME = "anewkabot.service"
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
    "deploy_darimiru_vps.sh", "deploy_anewkabot_vps.sh",
    "ssh_exec.py", "ssh_transfer.py",
    "migrate.py", "walkthrough.md.resolved",
    "deploy_anewkabot.py",
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
    print(f"=== Deploying @anewkabot to {HOST} ===")
    print(f"Revision: {revision}\n")

    ssh = ssh_connect()
    sftp = ssh.open_sftp()

    # 1. Create directory structure
    print("[1/5] Creating directories...")
    run_cmd(ssh, f"mkdir -p {APP_DIR}/bot {APP_DIR}/webstore/assets {WEBSTORE_RUNTIME_DIR}/webstore/assets")

    # 2. Upload bot/ and webstore/ directories
    print("\n[2/5] Uploading bot code...")
    upload_dir(sftp, os.path.join(LOCAL_DIR, "bot"), f"{APP_DIR}/bot", "bot/")

    print("\n[2b/5] Uploading webstore code...")
    upload_dir(sftp, os.path.join(LOCAL_DIR, "webstore"), f"{APP_DIR}/webstore", "webstore/")

    print("\n[2c/5] Uploading webstore runtime code...")
    upload_dir(sftp, os.path.join(LOCAL_DIR, "webstore"), f"{WEBSTORE_RUNTIME_DIR}/webstore", "webstore-runtime/")

    sftp.file(f"{APP_DIR}/REVISION", "w").write(revision + "\n")
    sftp.file(f"{WEBSTORE_RUNTIME_DIR}/REVISION", "w").write(revision + "\n")
    print(f"  -> REVISION {revision}")

    # 3. Upload requirements.txt
    print("\n[3/5] Uploading requirements.txt...")
    sftp.put(os.path.join(LOCAL_DIR, "requirements.txt"), f"{APP_DIR}/requirements.txt")
    print("  -> requirements.txt")

    sftp.close()

    # 4. Install deps only when requirements.txt changes
    print("\n[4/5] Installing dependencies if needed...")
    req_hash = file_sha256(os.path.join(LOCAL_DIR, "requirements.txt"))
    remote_hash, _ = run_cmd(ssh, f"cat {shlex.quote(REQ_MARKER)} 2>/dev/null || true")
    if remote_hash.strip() == req_hash:
        print("  requirements.txt unchanged — skipping pip install")
    else:
        run_cmd(ssh, f"{VENV}/bin/pip install --no-cache-dir -q -r {APP_DIR}/requirements.txt 2>&1 | tail -5")
        run_cmd(ssh, f"printf %s {shlex.quote(req_hash)} > {shlex.quote(REQ_MARKER)}")

    # 5. Validate and restart
    print("\n[5/5] Validating and restarting anewkabot.service...")
    run_cmd(ssh, (
        f"cd {APP_DIR} && {VENV}/bin/python -m py_compile "
        f"bot/webhooks.py bot/handlers/payment.py "
        f"bot/services/scheduler.py bot/services/subscription_service.py"
    ), check=True)
    run_cmd(ssh, f"systemctl restart {SERVICE_NAME}")
    run_cmd(ssh, f"sleep 2 && systemctl is-active {SERVICE_NAME}")

    print("\n=== Last 5 log lines ===")
    run_cmd(ssh, f"journalctl -u {SERVICE_NAME} -n 5 --no-pager")

    # Also restart webstore-loonapie (same codebase, separate env)
    out, _ = run_cmd(ssh, "systemctl is-active webstore-loonapie.service 2>/dev/null || true", check=False)
    if out.strip() in ("active", "inactive"):
        print("\n=== Restarting webstore-loonapie ===")
        run_cmd(ssh, "systemctl restart webstore-loonapie.service && sleep 1 && systemctl is-active webstore-loonapie.service")

    ssh.close()
    print("\n=== Deploy complete! ===")


if __name__ == "__main__":
    main()
