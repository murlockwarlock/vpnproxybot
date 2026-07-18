import asyncio
import logging
from typing import Optional
from urllib.parse import urlparse

from sqlalchemy import select

from bot.config import settings
from bot.database import async_session
from bot.models import BotSettings, Server
from bot.services.proxy_manager import MarzbanAPI
from bot.services.relay_health import check_relay_health as check_relay_routes_health

logger = logging.getLogger(__name__)

_RETRIES = 3
_RETRY_DELAY = 5  # seconds between retries
_ALERT_REPEAT = 3  # send alert this many times
_ALERT_INTERVAL = 60  # seconds between repeated alerts
_RELAY_STATE_KEY = "relay_health_state"
_RELAY_FAIL_COUNT_KEY = "relay_health_fail_count"
_NODE_STATE_KEY = "xray_node_health_state"
_CROSS_UPSTREAM_STATE_KEY = "cross_upstream_health_state"
_CROSS_UPSTREAM_FAIL_COUNT_KEY = "cross_upstream_health_fail_count"
_CROSS_UPSTREAMS = ()


async def _ping_server(server: Server) -> bool:
    """Try to login and get system status. Returns True if OK."""
    for attempt in range(1, _RETRIES + 1):
        try:
            async with MarzbanAPI(server) as api:
                status = await api.get_node_status()
                if status:
                    return True
        except Exception as e:
            logger.warning(
                f"Health check attempt {attempt}/{_RETRIES} failed for "
                f"{server.name}: {e}"
            )
            if attempt < _RETRIES:
                await asyncio.sleep(_RETRY_DELAY)
    return False


async def _check_xray_nodes(server: Server, session, bot=None) -> None:
    """
    Check each Marzban node's Xray status via /api/nodes.
    Alerts if any node is in 'error' or unexpected state.
    """
    try:
        async with MarzbanAPI(server) as api:
            nodes = await api.get_nodes()
    except Exception as e:
        logger.warning(f"Could not fetch nodes from {server.name}: {e}")
        return

    if not nodes:
        return

    # Build a state string: "NodeName:status, ..."
    state_parts = []
    problems = []
    for node in nodes:
        name = node.get("name", "?")
        status = node.get("status", "?")
        address = node.get("address", "?")
        state_parts.append(f"{name}:{status}")
        if status not in ("connected",):
            problems.append((name, address, status))

    current_state = ", ".join(sorted(state_parts))
    previous_state = await _get_setting(session, _NODE_STATE_KEY, "")

    if problems:
        logger.warning(
            "Xray node health issues: %s",
            "; ".join(f"{n} ({a}) = {s}" for n, a, s in problems),
        )
        if previous_state != current_state and bot:
            lines = "\n".join(
                f"• <b>{n}</b> (<code>{a}</code>) — <b>{s}</b>"
                for n, a, s in problems
            )
            await _alert_admins(
                bot,
                f"🚨 <b>Xray нода не работает!</b>\n\n"
                f"{lines}\n\n"
                f"Проверьте: <code>docker compose restart</code> на ноде "
                f"или перезапустите Marzban master.",
                repeat=_ALERT_REPEAT,
            )
    else:
        # All nodes connected — if we had a previous problem, notify recovery
        if previous_state and previous_state != current_state and bot:
            # Check if previous state had problems (simple heuristic)
            if any(
                s not in ("connected",)
                for s in (
                    part.split(":", 1)[1] if ":" in part else ""
                    for part in previous_state.split(", ")
                )
            ):
                await _alert_admins(
                    bot,
                    "✅ <b>Xray ноды в норме</b>\n\n"
                    "Все ноды снова в статусе connected.",
                )

    await _set_setting(session, _NODE_STATE_KEY, current_state)


async def _alert_admins(bot, text: str, repeat: int = 1) -> None:
    """Send alert to all admins, optionally repeating multiple times."""
    for attempt in range(repeat):
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
            except Exception as e:
                logger.warning(f"Failed to alert admin {admin_id}: {e}")
        if attempt < repeat - 1:
            await asyncio.sleep(_ALERT_INTERVAL)


async def _get_setting(session, key: str, default: str = "") -> str:
    row = await session.get(BotSettings, key)
    return row.value if row else default


async def _set_setting(session, key: str, value: str) -> None:
    row = await session.get(BotSettings, key)
    if row:
        row.value = value
    else:
        session.add(BotSettings(key=key, value=value))


def _should_check_cross_upstreams() -> bool:
    """Run Darimiru -> NL checks only from non-NL deployments that proxy /sub to NL."""
    hosts = {
        urlparse(settings.subscription_base_url).hostname,
        urlparse(settings.webstore_api_base_url).hostname,
    }
    return "darimiru.ru" in hosts


async def _tcp_connect_ok(host: str, port: int, timeout: float = 8.0) -> bool:
    try:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception as e:
        logger.warning("Cross-upstream check failed for %s:%s: %s", host, port, e)
        return False


async def _check_cross_upstreams(session, bot=None) -> None:
    if not _should_check_cross_upstreams():
        return

    results = await asyncio.gather(
        *(_tcp_connect_ok(host, port) for _, host, port in _CROSS_UPSTREAMS)
    )
    problems = [
        f"{name}: <code>{host}:{port}</code> недоступен"
        for (name, host, port), ok in zip(_CROSS_UPSTREAMS, results)
        if not ok
    ]
    current_state = "ok" if not problems else "; ".join(problems)
    previous_state = await _get_setting(session, _CROSS_UPSTREAM_STATE_KEY, "")

    if not problems:
        fail_count = int(await _get_setting(session, _CROSS_UPSTREAM_FAIL_COUNT_KEY, "0") or "0")
        if previous_state and previous_state != "ok" and fail_count > 0 and bot:
            await _alert_admins(
                bot,
                "✅ <b>NL upstream снова доступен</b>\n\n"
                "Darimiru снова может доставать подписки с NL master.",
            )
        await _set_setting(session, _CROSS_UPSTREAM_FAIL_COUNT_KEY, "0")
        await _set_setting(session, _CROSS_UPSTREAM_STATE_KEY, current_state)
        return

    fail_count = int(await _get_setting(session, _CROSS_UPSTREAM_FAIL_COUNT_KEY, "0") or "0") + 1
    await _set_setting(session, _CROSS_UPSTREAM_FAIL_COUNT_KEY, str(fail_count))
    logger.warning("Cross-upstream health failed (%s consecutive): %s", fail_count, "; ".join(problems))

    if fail_count >= 2 and previous_state != current_state and bot:
        await _alert_admins(
            bot,
            "🚨 <b>NL upstream недоступен</b>\n\n"
            + "\n".join(f"• {problem}" for problem in problems)
            + "\n\nИз-за этого Darimiru может отдавать <code>502</code> при обновлении <code>/sub/...</code>.",
            repeat=1,
        )
    await _set_setting(session, _CROSS_UPSTREAM_STATE_KEY, current_state)


async def _check_relay_health(session, bot=None) -> None:
    result = await check_relay_routes_health()
    if result.skipped:
        logger.info(result.summary)
        return

    previous_state = await _get_setting(session, _RELAY_STATE_KEY, "")
    current_state = result.summary

    if result.ok:
        fail_count = int(await _get_setting(session, _RELAY_FAIL_COUNT_KEY, "0") or "0")
        if previous_state and previous_state != current_state and fail_count > 0 and bot:
            await _alert_admins(
                bot,
                "✅ <b>Relay-маршруты в норме</b>\n\n"
                "Проверка relay/SNI снова проходит успешно.",
            )
        await _set_setting(session, _RELAY_FAIL_COUNT_KEY, "0")
        await _set_setting(session, _RELAY_STATE_KEY, current_state)
        return

    fail_count = int(await _get_setting(session, _RELAY_FAIL_COUNT_KEY, "0") or "0") + 1
    await _set_setting(session, _RELAY_FAIL_COUNT_KEY, str(fail_count))
    logger.warning("Relay health check failed (%s consecutive): %s", fail_count, "; ".join(result.problems))

    # Avoid Telegram spam on transient SSH/upstream timeouts. Alert only after
    # two consecutive failures, and do not repeat relay alerts three times.
    if fail_count >= 2 and previous_state != current_state and bot:
        problems_html = "\n".join(f"• {problem}" for problem in result.problems[:15])
        await _alert_admins(
            bot,
            "🚨 <b>Проблема с relay-маршрутами</b>\n\n"
            f"{problems_html}",
            repeat=1,
        )
    await _set_setting(session, _RELAY_STATE_KEY, current_state)


async def check_server_health(bot=None) -> None:
    """
    Periodic health check: marks a server inactive only after
    all retries fail, and restores it when it comes back.
    Sends alerts to admins on status changes.
    """
    logger.info("Running periodic server health check...")

    async with async_session() as session:
        result = await session.execute(select(Server))
        servers = result.scalars().all()

        for server in servers:
            if not server.api_url:
                continue

            ok = await _ping_server(server)

            if not ok and server.is_active:
                logger.warning(
                    f"Server {server.name} failed all {_RETRIES} health checks - marking inactive."
                )
                server.is_active = False
                session.add(server)
                if bot:
                    await _alert_admins(
                        bot,
                        f"🚨 <b>СЕРВЕР НЕДОСТУПЕН!</b>\n\n"
                        f"Сервер: <b>{server.name}</b>\n"
                        f"IP: <code>{server.host}</code>\n"
                        f"Не ответил после {_RETRIES} попыток.\n\n"
                        f"Сервер отключен от выдачи ключей.",
                        repeat=_ALERT_REPEAT,
                    )
            elif ok and not server.is_active:
                logger.info(f"Server {server.name} is back online - marking active.")
                server.is_active = True
                session.add(server)
                if bot:
                    await _alert_admins(
                        bot,
                        f"✅ <b>Сервер снова онлайн</b>\n\n"
                        f"Сервер: <b>{server.name}</b>\n"
                        f"IP: <code>{server.host}</code>\n\n"
                        f"Выдача ключей возобновлена.",
                    )

        await _check_relay_health(session, bot=bot)
        await _check_cross_upstreams(session, bot=bot)

        # Check Xray status on individual nodes via /api/nodes
        for server in servers:
            if server.api_url:
                await _check_xray_nodes(server, session, bot=bot)
                break  # nodes list is the same for all servers (one Marzban master)

        await session.commit()

    logger.info("Periodic server health check completed.")
