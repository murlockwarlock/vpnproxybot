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
    """Send a platform guide. If custom text/media/buttons are configured in DB, use them."""
    platform_str = platform.value if hasattr(platform, "value") else str(platform)

    async with async_session() as session:
        pg = await session.get(PlatformGuide, platform_str)

    # 1. Determine final text to send (custom DB text if set, else fallback default)
    final_text = (pg.guide_text if pg and pg.guide_text else guide_text) or ""

    # 2. Determine final reply markup (combine custom buttons with default navigation buttons)
    final_kb = reply_markup
    if pg and pg.buttons_json:
        from bot.handlers.mailing import _parse_buttons
        custom_buttons = _parse_buttons(pg.buttons_json)
        if custom_buttons:
            # Group custom buttons by row
            rows_map: dict[int, list] = {}
            for b in custom_buttons:
                row_num = b.get("row", 0)
                rows_map.setdefault(row_num, [])
                from aiogram.types import InlineKeyboardButton
                if b.get("type") == "url":
                    rows_map[row_num].append(InlineKeyboardButton(text=b["text"], url=b["data"]))
                else:
                    rows_map[row_num].append(InlineKeyboardButton(text=b["text"], callback_data=b["data"]))
            
            kb_rows = [rows_map[r] for r in sorted(rows_map)]
            # Append navigation buttons if any
            if reply_markup and reply_markup.inline_keyboard:
                kb_rows.extend(reply_markup.inline_keyboard)
            
            final_kb = InlineKeyboardMarkup(inline_keyboard=kb_rows)

    # 3. Send media first if configured
    if pg and pg.media_file_id and pg.media_type:
        try:
            if pg.media_type == "album":
                from bot.handlers.mailing import _parse_album_media
                album_media = _parse_album_media(pg.media_file_id)
                await bot.send_media_group(chat_id, media=album_media)
            elif pg.media_type == "photo":
                await bot.send_photo(chat_id, pg.media_file_id)
            elif pg.media_type == "video":
                await bot.send_video(chat_id, pg.media_file_id)
        except Exception as e:
            logger.warning(f"Failed to send guide media for {platform_str}: {e}")

    # 4. Send the guide text + inline keyboard
    try:
        await bot.send_message(
            chat_id,
            final_text,
            reply_markup=final_kb,
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
