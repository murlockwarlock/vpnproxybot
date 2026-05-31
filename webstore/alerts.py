"""Telegram alerts for webstore runtime failures."""

from __future__ import annotations

import html
import logging
import traceback

import aiohttp

from webstore.config import settings

logger = logging.getLogger(__name__)


async def notify_webstore_error(
    title: str,
    *,
    details: list[str] | None = None,
    exc: BaseException | None = None,
) -> None:
    if not settings.admin_bot_token or not settings.admin_ids:
        logger.warning("Webstore alert skipped: admin bot token or admin ids are not configured")
        return

    lines = [
        f"🚨 <b>{html.escape(title)}</b>",
        "",
        f"Сайт: <b>{html.escape(settings.site_name)}</b>",
        f"URL: <code>{html.escape(settings.subscription_base_url)}</code>",
        f"Порт: <code>{settings.port}</code>",
    ]
    if details:
        lines.append("")
        lines.extend(html.escape(str(item)) for item in details if item)
    if exc is not None:
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        lines.extend(["", "<b>Exception:</b>", f"<pre>{html.escape(tb[-2500:])}</pre>"])

    api_url = f"https://api.telegram.org/bot{settings.admin_bot_token}/sendMessage"
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8)) as http:
            for admin_id in settings.admin_ids:
                try:
                    await http.post(
                        api_url,
                        json={
                            "chat_id": admin_id,
                            "text": "\n".join(lines),
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                        },
                    )
                except Exception as send_exc:
                    logger.warning("Failed to send webstore alert to %s: %s", admin_id, send_exc)
    except Exception as exc:
        logger.warning("Failed to create Telegram alert session: %s", exc)
