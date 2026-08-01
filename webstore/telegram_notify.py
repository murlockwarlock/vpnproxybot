"""Telegram delivery for webstore notifications."""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from webstore.config import settings


logger = logging.getLogger(__name__)


def _proxy_connector_from_url(proxy_url: str) -> aiohttp.BaseConnector:
    from aiohttp_socks import ProxyConnector

    return ProxyConnector.from_url(proxy_url)


def _telegram_connector() -> aiohttp.BaseConnector:
    proxy_url = settings.telegram_proxy.strip()
    if proxy_url:
        return _proxy_connector_from_url(proxy_url)
    return aiohttp.TCPConnector()


async def send_telegram_notifications(
    bot_token: str,
    recipient_ids: set[int] | list[int],
    text: str,
    *,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool = False,
) -> int:
    recipients = sorted({int(item) for item in recipient_ids if item})
    if not bot_token or not recipients:
        return 0

    api_url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    delivered = 0
    timeout = aiohttp.ClientTimeout(total=15)
    try:
        connector = _telegram_connector()
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as http:
            for recipient_id in recipients:
                for attempt in range(1, 4):
                    try:
                        async with http.post(
                            api_url,
                            json={
                                "chat_id": recipient_id,
                                "text": text,
                                "parse_mode": parse_mode,
                                "disable_web_page_preview": disable_web_page_preview,
                            },
                        ) as response:
                            payload = await response.json(content_type=None)
                            if response.status == 200 and isinstance(payload, dict) and payload.get("ok"):
                                delivered += 1
                                break
                            retry_after = (
                                payload.get("parameters", {}).get("retry_after")
                                if isinstance(payload, dict)
                                else None
                            )
                            retryable = response.status == 429 or response.status >= 500
                            logger.warning(
                                "Telegram notification rejected recipient_id=%s status=%s attempt=%s retryable=%s",
                                recipient_id,
                                response.status,
                                attempt,
                                retryable,
                            )
                            if not retryable or attempt == 3:
                                break
                            await asyncio.sleep(min(float(retry_after or attempt), 5.0))
                    except Exception as exc:
                        logger.warning(
                            "Telegram notification failed recipient_id=%s attempt=%s error=%s",
                            recipient_id,
                            attempt,
                            type(exc).__name__,
                        )
                        if attempt < 3:
                            await asyncio.sleep(float(attempt))
    except Exception as exc:
        logger.warning("Telegram notification transport unavailable: %s", type(exc).__name__)
    return delivered
