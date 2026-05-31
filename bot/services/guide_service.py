"""Guide delivery helper - sends platform setup guide, optionally with media."""

from __future__ import annotations

import logging
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup

from bot.database import async_session
from bot.models import PlatformGuide

logger = logging.getLogger(__name__)

PLATFORM_NAMES = {
    "android":    "🤖 Android",
    "ios":        "🍎 iOS",
    "mac":        "🍏 Mac",
    "windows":    "💻 Windows",
    "android_tv": "📺 Android TV",
}


async def send_guide(
    bot: Bot,
    chat_id: int,
    platform: Any,  # str or Platform enum
    guide_text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Send a platform guide. If media is configured in DB, send it before the text."""
    platform_str = platform.value if hasattr(platform, "value") else str(platform)

    async with async_session() as session:
        pg = await session.get(PlatformGuide, platform_str)

    if pg and pg.media_file_id and pg.media_type:
        try:
            send_fn = bot.send_photo if pg.media_type == "photo" else bot.send_video
            await send_fn(chat_id, pg.media_file_id)
        except Exception as e:
            logger.warning(f"Failed to send guide media for {platform_str}: {e}")

    try:
        await bot.send_message(
            chat_id,
            guide_text,
            reply_markup=reply_markup,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.error(f"Failed to send guide text for {platform_str}: {e}")


async def get_platform_guides_map() -> dict[str, PlatformGuide | None]:
    """Return a mapping platform_str → PlatformGuide (or None) for all known platforms."""
    platforms = list(PLATFORM_NAMES.keys())
    async with async_session() as session:
        result = {}
        for p in platforms:
            result[p] = await session.get(PlatformGuide, p)
        return result
