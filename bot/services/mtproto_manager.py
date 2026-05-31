"""MTProto proxy secret management - add/remove secrets via SSH on multiple servers."""

from __future__ import annotations

import logging
import secrets as stdlib_secrets

import paramiko

from bot.config import settings

logger = logging.getLogger(__name__)

MANAGE_SCRIPT = "/opt/mtproto-proxy/manage_secrets.py"

# TLS domain hex for link building (www.ietf.org)
_TLS_DOMAIN_HEX = "7777772e696574662e6f7267"
_SERVER_LABELS = {
    "72.56.71.124": "🇳🇱 Нидерланды",
    "45.92.174.214": "🇪🇪 Эстония",
    "81.200.156.43": "🇩🇪 Германия",
}


def _restart_command() -> str:
    """Restart MTProto proxy in a way that works for systemd-managed hosts."""
    return (
        "if systemctl cat mtproto-proxy.service >/dev/null 2>&1; then "
        "systemctl restart mtproto-proxy.service && systemctl is-active mtproto-proxy.service; "
        "else "
        "pkill -f mtprotoproxy.py || true; sleep 1; "
        "cd /opt/mtproto-proxy && nohup python3 mtprotoproxy.py > /dev/null 2>&1 & "
        "sleep 1; "
        "(pgrep -af mtprotoproxy.py >/dev/null && echo active); "
        "fi"
    )


def generate_secret() -> str:
    """Generate a random 32-char hex secret for MTProto proxy."""
    return stdlib_secrets.token_hex(16)


def build_mtproto_label(telegram_id: int) -> str:
    """Build an MTProto label unique per bot deployment."""
    return f"{settings.bot_client_prefix}_tg{telegram_id}"


def _ssh_exec_on(server: dict, command: str) -> str:
    """Execute a command on a specific MTProto proxy server via SSH."""
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            server["host"],
            username=server.get("ssh_user", "root"),
            password=server.get("ssh_password", ""),
            timeout=10,
        )
        stdin, stdout, stderr = ssh.exec_command(command)
        result = stdout.read().decode().strip()
        err = stderr.read().decode().strip()
        if err:
            logger.warning("SSH stderr on %s: %s", server.get("label", server["host"]), err)
        return result
    finally:
        ssh.close()


async def add_secret(label: str, secret: str, *, restart: bool = True) -> bool:
    """Add a secret to ALL MTProto proxy servers.

    Args:
        restart: If True (default), restart proxy after adding.
                 Set to False for bulk operations, then call restart_proxies() once at the end.
    """
    if settings.mock_mode:
        logger.info("[MOCK] Added MTProto secret %s for %s", secret[:8], label)
        return True

    servers = settings.mtproto_servers
    if not servers:
        logger.error("No MTProto servers configured")
        return False

    all_ok = True
    for srv in servers:
        try:
            cmd = f"python3 {MANAGE_SCRIPT} add {label} {secret}"
            if restart:
                cmd += f" && {_restart_command()}"
            result = _ssh_exec_on(srv, cmd)
            logger.info("MTProto add_secret on %s: %s", srv.get("label", srv["host"]), result)
            if "OK" not in result:
                all_ok = False
        except Exception as e:
            logger.error("Failed to add MTProto secret on %s: %s", srv.get("label", srv["host"]), e)
            all_ok = False

    return all_ok


async def restart_proxies() -> bool:
    """Restart MTProto proxy on ALL servers. Call after bulk add_secret(restart=False)."""
    if settings.mock_mode:
        return True

    servers = settings.mtproto_servers
    if not servers:
        return True

    all_ok = True
    for srv in servers:
        try:
            result = _ssh_exec_on(
                srv,
                _restart_command(),
            )
            logger.info("MTProto restart on %s: %s", srv.get("label", srv["host"]), result)
            if "active" not in result:
                all_ok = False
        except Exception as e:
            logger.error("Failed to restart MTProto on %s: %s", srv.get("label", srv["host"]), e)
            all_ok = False

    return all_ok


async def remove_secret(label: str) -> bool:
    """Remove a secret from ALL MTProto proxy servers and restart them."""
    if settings.mock_mode:
        logger.info("[MOCK] Removed MTProto secret for %s", label)
        return True

    servers = settings.mtproto_servers
    if not servers:
        return True

    all_ok = True
    for srv in servers:
        try:
            result = _ssh_exec_on(
                srv,
                f"python3 {MANAGE_SCRIPT} remove {label} && {_restart_command()}",
            )
            logger.info("MTProto remove_secret on %s: %s", srv.get("label", srv["host"]), result)
            if "OK" not in result:
                all_ok = False
        except Exception as e:
            logger.error("Failed to remove MTProto secret on %s: %s", srv.get("label", srv["host"]), e)
            all_ok = False

    return all_ok


def _build_link(host: str, port: int, secret: str) -> str:
    return f"https://t.me/proxy?server={host}&port={port}&secret=ee{secret}{_TLS_DOMAIN_HEX}"


def _display_label(server: dict) -> str:
    host = server.get("host", "")
    fallback = _SERVER_LABELS.get(host, host)
    label = str(server.get("label", "") or "").strip()
    if not label:
        return fallback

    broken_markers = ("ud83c", "u041", "\\ud83c", "\\u041")
    if any(marker in label for marker in broken_markers):
        return fallback

    return label


def build_tg_proxy_link(secret: str) -> str:
    """Build a tg://proxy link for the first server (legacy compat)."""
    servers = settings.mtproto_servers
    if not servers:
        return ""
    srv = servers[0]
    host = srv["host"]
    port = srv.get("port", 4430)
    return f"tg://proxy?server={host}&port={port}&secret=ee{secret}{_TLS_DOMAIN_HEX}"


def build_tg_proxy_link_https(secret: str) -> str:
    """Build an https://t.me/proxy link for the first server (legacy compat)."""
    servers = settings.mtproto_servers
    if not servers:
        return ""
    srv = servers[0]
    return _build_link(srv["host"], srv.get("port", 4430), secret)


def build_all_proxy_links(secret: str) -> list[tuple[str, str]]:
    """Build proxy links for ALL servers.

    Returns list of (label, https_link) tuples.
    """
    servers = settings.mtproto_servers
    result = []
    for srv in servers:
        label = _display_label(srv)
        link = _build_link(srv["host"], srv.get("port", 4430), secret)
        result.append((label, link))
    return result
