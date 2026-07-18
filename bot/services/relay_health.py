"""Relay route health checks over SSH."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class RelayHost:
    name: str
    host: str
    user: str = "yc-user"


@dataclass(frozen=True)
class RelayHealthResult:
    ok: bool
    summary: str
    problems: tuple[str, ...]
    skipped: bool = False


RELAYS: tuple[RelayHost, ...] = ()

EXPECTED_ROUTES: dict[str, dict[str, str]] = {}

ROUTE_RE = re.compile(r"^\s*(?P<sni>[^\s#]+)\s+(?P<backend>\d+\.\d+\.\d+\.\d+:\d+);$")


def parse_relay_routes(config_text: str) -> dict[str, str]:
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


def check_expected_routes(relay_name: str, routes: dict[str, str]) -> list[str]:
    expected = EXPECTED_ROUTES[relay_name]
    problems: list[str] = []

    for sni, backend in expected.items():
        actual = routes.get(sni)
        if actual != backend:
            problems.append(
                f"{relay_name}: route mismatch for {sni}: expected {backend}, got {actual or 'missing'}"
            )

    for sni in sorted(set(routes) - set(expected)):
        problems.append(f"{relay_name}: unexpected route present for {sni}: {routes[sni]}")

    return problems


async def _run_ssh(relay: RelayHost, ssh_key_path: str, remote_cmd: str) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        "ssh",
        "-i",
        ssh_key_path,
        "-o",
        "StrictHostKeyChecking=no",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        f"{relay.user}@{relay.host}",
        remote_cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate()
    return process.returncode, stdout.decode(), stderr.decode()


async def _check_single_relay(relay: RelayHost, ssh_key_path: str) -> list[str]:
    problems: list[str] = []

    rc, active_stdout, active_stderr = await _run_ssh(relay, ssh_key_path, "systemctl is-active nginx")
    if rc != 0 or active_stdout.strip() != "active":
        details = active_stdout.strip() or active_stderr.strip() or f"exit={rc}"
        problems.append(f"{relay.name}: nginx is not active ({details})")

    rc, syntax_stdout, syntax_stderr = await _run_ssh(relay, ssh_key_path, "sudo nginx -t")
    if rc != 0:
        details = syntax_stderr.strip() or syntax_stdout.strip() or f"exit={rc}"
        problems.append(f"{relay.name}: nginx -t failed ({details})")

    rc, config_stdout, config_stderr = await _run_ssh(
        relay,
        ssh_key_path,
        "sudo cat /etc/nginx/stream.d/relay.conf",
    )
    if rc != 0:
        details = config_stderr.strip() or config_stdout.strip() or f"exit={rc}"
        problems.append(f"{relay.name}: failed to read relay.conf ({details})")
        return problems

    routes = parse_relay_routes(config_stdout)
    problems.extend(check_expected_routes(relay.name, routes))

    for backend in sorted(set(routes.values())):
        host, port = backend.split(":", 1)
        rc, _, _ = await _run_ssh(relay, ssh_key_path, f"timeout 10 nc -zvw8 {host} {port} >/dev/null 2>&1")
        if rc != 0:
            problems.append(f"{relay.name}: upstream unreachable from relay: {backend}")

    return problems


async def check_relay_health(ssh_key_path: str | None = None) -> RelayHealthResult:
    ssh_key = Path(ssh_key_path or (Path.home() / ".ssh" / "yc_relay")).expanduser()
    if not ssh_key.exists():
        return RelayHealthResult(
            ok=True,
            summary="relay health check skipped: ssh key is missing",
            problems=(),
            skipped=True,
        )

    tasks = [_check_single_relay(relay, str(ssh_key)) for relay in RELAYS]
    relay_problems = await asyncio.gather(*tasks)
    problems = tuple(problem for batch in relay_problems for problem in batch)
    if problems:
        summary = json.dumps({"ok": False, "problems": problems}, ensure_ascii=False, sort_keys=True)
        return RelayHealthResult(ok=False, summary=summary, problems=problems)

    summary = json.dumps({"ok": True, "relays": [relay.name for relay in RELAYS]}, ensure_ascii=False, sort_keys=True)
    return RelayHealthResult(ok=True, summary=summary, problems=())
