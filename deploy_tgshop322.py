#!/usr/bin/env python3
"""Deploy @tgshop322bot to 81.200.156.43 via paramiko (password auth)."""

import os
import paramiko

from scripts.deploy_guard import ensure_clean_git, load_local_env, require_env

HOST = "81.200.156.43"
USER = "root"
load_local_env(os.path.dirname(os.path.abspath(__file__)))
PASSWORD = require_env("TGSHOP322_SSH_PASSWORD")
APP_DIR = "/opt/vpn-bot/current"
CONTAINER = "vpn-bot-germany"
LOCAL_DIR = os.path.dirname(os.path.abspath(__file__))

EXCLUDE_DIRS = {
    "__pycache__", ".git", "chroma_db", "backups", "tmp",
    "tests", "landing", "server-setup", "scripts", ".mypy_cache",
}
EXCLUDE_FILES = {
    ".env", ".env.darimiru", ".env.example",
    "vpn_bot.db", "vpn_bot.db-shm", "vpn_bot.db-wal",
    "darimiru_bot.db", "bot.log",
    "deploy_darimiru_vps.sh", "deploy_darimiru.py",
    "deploy_anewkabot_vps.sh", "deploy_anewka.py",
    "deploy_tgshop322.py", "ssh_exec.py", "ssh_transfer.py",
    "migrate.py", "walkthrough.md.resolved",
}


def ssh_connect():
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(HOST, username=USER, password=PASSWORD, timeout=15)
    return ssh


def run_cmd(ssh, cmd):
    print(f"  $ {cmd}")
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=180)
    out = stdout.read().decode()
    err = stderr.read().decode()
    if out.strip():
        print(out.rstrip())
    if err.strip():
        print(f"  STDERR: {err.rstrip()}")
    return out, err


def upload_dir(sftp, local_dir, remote_dir, rel_prefix=""):
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
            print(f"  -> {rel_prefix}{item}")
            sftp.put(local_path, remote_path)


def main():
    revision = ensure_clean_git(LOCAL_DIR)
    print(f"=== Deploying @tgshop322bot to {HOST} ===")
    print(f"Revision: {revision}\n")

    ssh = ssh_connect()
    sftp = ssh.open_sftp()

    print("[1/5] Creating directories...")
    run_cmd(ssh, f"mkdir -p {APP_DIR}/bot")

    print("\n[2/5] Uploading bot code...")
    bot_local = os.path.join(LOCAL_DIR, "bot")
    upload_dir(sftp, bot_local, f"{APP_DIR}/bot", "bot/")

    sftp.file(f"{APP_DIR}/REVISION", "w").write(revision + "\n")
    print(f"  -> REVISION {revision}")

    print("\n[3/5] Uploading requirements.txt...")
    sftp.put(os.path.join(LOCAL_DIR, "requirements.txt"), f"{APP_DIR}/requirements.txt")
    print("  -> requirements.txt")

    print("\n[4/5] Checking .env...")
    try:
        sftp.stat(f"{APP_DIR}/.env")
        print("  .env already exists on server — skipping")
    except FileNotFoundError:
        print("  .env not found on server — please create it manually")

    sftp.close()

    print("\n[5/5] Restarting Docker container...")
    run_cmd(ssh, f"docker restart {CONTAINER}")
    run_cmd(ssh, f"sleep 3 && docker inspect --format='{{{{.State.Status}}}}' {CONTAINER}")

    print("\n=== Last 10 lines of log ===")
    run_cmd(ssh, f"docker logs {CONTAINER} --tail 10 2>&1")

    ssh.close()
    print("\n=== Deploy complete! ===")


if __name__ == "__main__":
    main()
