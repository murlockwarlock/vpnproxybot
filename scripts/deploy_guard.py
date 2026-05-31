"""Local deploy safeguards.

Production deploys must come from a committed repository state. Secrets are read
from environment variables or from local .env.deploy, which is intentionally
ignored by git.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def load_local_env(root: str | os.PathLike[str]) -> None:
    env_path = Path(root) / ".env.deploy"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        print(
            f"ERROR: missing {name}. Put it in local .env.deploy or export it before deploy.",
            file=sys.stderr,
        )
        sys.exit(2)
    return value


def _git(root: str | os.PathLike[str], *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=check,
    )


def git_revision(root: str | os.PathLike[str]) -> str:
    result = _git(root, "rev-parse", "HEAD")
    return result.stdout.strip()


def ensure_clean_git(root: str | os.PathLike[str]) -> str:
    revision = git_revision(root)
    status = _git(root, "status", "--porcelain", check=True).stdout.strip()
    if status and os.getenv("ALLOW_DIRTY_DEPLOY", "").strip() != "1":
        print("ERROR: refusing to deploy from dirty git tree.", file=sys.stderr)
        print("Commit or stash changes first. For emergency only: ALLOW_DIRTY_DEPLOY=1.", file=sys.stderr)
        print(status, file=sys.stderr)
        sys.exit(2)
    if status:
        print("WARNING: ALLOW_DIRTY_DEPLOY=1 set; deploying uncommitted changes.", file=sys.stderr)
    return revision
