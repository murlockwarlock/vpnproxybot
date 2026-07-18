#!/usr/bin/env python3
"""Audit relay nginx routing and upstream reachability over SSH."""

from __future__ import annotations

import argparse
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RelayHost:
    name: str
    host: str
    user: str = "yc-user"


RELAYS: tuple[RelayHost, ...] = ()

EXPECTED_ROUTES: dict[str, dict[str, str]] = {}

ROUTE_RE = re.compile(r"^\s*(?P<sni>[^\s#]+)\s+(?P<backend>\d+\.\d+\.\d+\.\d+:\d+);$")


def _run_ssh(
    relay: RelayHost,
    ssh_key: str,
    remote_cmd: str,
    *,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    cmd = [
        "ssh",
        "-i",
        ssh_key,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        f"{relay.user}@{relay.host}",
        remote_cmd,
    ]
    return subprocess.run(cmd, check=check, text=True, capture_output=True)


def _fetch_relay_conf(relay: RelayHost, ssh_key: str) -> str:
    proc = _run_ssh(relay, ssh_key, "sudo cat /etc/nginx/stream.d/relay.conf")
    return proc.stdout


def _parse_routes(config_text: str) -> dict[str, str]:
    routes: dict[str, str] = {}
    in_map = False
    for raw_line in config_text.splitlines():
        line = raw_line.rstrip()
        if line.startswith("map "):
            in_map = True
            continue
        if in_map and line.strip() == "}":
            break
        if not in_map:
            continue
        match = ROUTE_RE.match(line)
        if match:
            routes[match.group("sni")] = match.group("backend")
    return routes


def _check_routes(relay_name: str, actual: dict[str, str]) -> list[str]:
    problems: list[str] = []
    expected = EXPECTED_ROUTES[relay_name]
    for sni, backend in expected.items():
        actual_backend = actual.get(sni)
        if actual_backend != backend:
            problems.append(
                f"{relay_name}: route mismatch for {sni}: expected {backend}, got {actual_backend or 'missing'}"
            )
    unexpected = sorted(set(actual) - set(expected))
    for sni in unexpected:
        problems.append(f"{relay_name}: unexpected route present for {sni}: {actual[sni]}")
    return problems


def _check_nginx(relay: RelayHost, ssh_key: str) -> list[str]:
    problems: list[str] = []
    active = _run_ssh(relay, ssh_key, "systemctl is-active nginx", check=False)
    if active.returncode != 0 or active.stdout.strip() != "active":
        problems.append(f"{relay.name}: nginx is not active ({active.stdout.strip() or active.stderr.strip()})")
    syntax = _run_ssh(relay, ssh_key, "sudo nginx -t", check=False)
    if syntax.returncode != 0:
        details = (syntax.stderr or syntax.stdout).strip()
        problems.append(f"{relay.name}: nginx -t failed: {details}")
    return problems


def _check_upstreams(relay: RelayHost, ssh_key: str, routes: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for backend in sorted(set(routes.values())):
        host, port = backend.split(":", 1)
        remote_cmd = f"nc -zvw3 {shlex.quote(host)} {shlex.quote(port)} >/dev/null 2>&1"
        proc = _run_ssh(relay, ssh_key, remote_cmd, check=False)
        if proc.returncode != 0:
            problems.append(f"{relay.name}: upstream unreachable from relay: {backend}")
    return problems


def _format_report(relay: RelayHost, routes: dict[str, str], problems: list[str]) -> str:
    lines = [
        f"[{relay.name}] {relay.host}",
        f"routes: {len(routes)}",
    ]
    if problems:
        lines.append("status: FAIL")
        lines.extend(f"- {problem}" for problem in problems)
    else:
        lines.append("status: OK")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--ssh-key",
        default=str(Path.home() / ".ssh" / "yc_relay"),
        help="Path to SSH private key for relay access",
    )
    args = parser.parse_args()

    overall_problems: list[str] = []
    reports: list[str] = []

    for relay in RELAYS:
        try:
            config_text = _fetch_relay_conf(relay, args.ssh_key)
            routes = _parse_routes(config_text)
            problems = []
            problems.extend(_check_routes(relay.name, routes))
            problems.extend(_check_nginx(relay, args.ssh_key))
            problems.extend(_check_upstreams(relay, args.ssh_key, routes))
        except subprocess.CalledProcessError as exc:
            stderr = (exc.stderr or exc.stdout or "").strip()
            problems = [f"{relay.name}: SSH command failed: {stderr}"]
            routes = {}

        reports.append(_format_report(relay, routes, problems))
        overall_problems.extend(problems)

    print("\n\n".join(reports))
    return 1 if overall_problems else 0


if __name__ == "__main__":
    sys.exit(main())
