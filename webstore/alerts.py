"""Telegram alerts for webstore runtime failures."""

from __future__ import annotations

import html
import logging
import traceback

from webstore.config import settings
from webstore.telegram_notify import send_telegram_notifications

logger = logging.getLogger(__name__)


async def notify_webstore_error(
    title: str,
    *,
    details: list[str] | None = None,
    exc: BaseException | None = None,
) -> None:
    if not settings.admin_bot_token or not settings.notification_recipient_ids:
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

    delivered = await send_telegram_notifications(
        settings.admin_bot_token,
        settings.notification_recipient_ids,
        "\n".join(lines),
        disable_web_page_preview=True,
    )
    if delivered == 0:
        logger.warning("Webstore runtime alert was not delivered")
