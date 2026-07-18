"""Background worker - mailing queue processor."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from sqlalchemy import select

from bot.config import settings
from bot.database import async_session
from bot.models import Mailing, SubStatus, Subscription, User

logger = logging.getLogger(__name__)

AUDIENCES_MAP = {
    "all":                  "Всем пользователям",
    "self":                 "👤 Только себе (тест)",
    "active_subscription":  "Активным подписчикам",
    "inactive_subscription":"С истёкшей подпиской",
    "no_subscription":      "Без подписки",
    "referred":             "Пришедшим по реферальной ссылке",
}


async def process_mailings(bot: Bot, stop_event: asyncio.Event | None = None) -> None:
    """Main mailing worker loop - polls the queue and delivers pending mailings."""
    logger.info("Mailing worker started.")

    async with async_session() as session:
        stuck_result = await session.execute(
            select(Mailing).where(Mailing.status == "sending")
        )
        stuck_mailings = stuck_result.scalars().all()
        for mailing in stuck_mailings:
            mailing.status = "pending"
        if stuck_mailings:
            await session.commit()
            logger.warning("Recovered %s stuck mailings", len(stuck_mailings))

    while True:
        if stop_event and stop_event.is_set():
            logger.info("Mailing worker stopping.")
            return

        await asyncio.sleep(15)

        async with async_session() as session:
            result = await session.execute(
                select(Mailing)
                .where(Mailing.status == "pending")
                .order_by(Mailing.created_at)
                .limit(1)
            )
            mailing = result.scalar_one_or_none()
            if not mailing:
                continue

            mailing.status = "sending"
            mailing.start_time = datetime.utcnow()
            await session.commit()

            # Build target user list
            audience = mailing.target_audience
            target_stmt = _build_audience_query(audience, mailing.creator_id)

            if target_stmt is None:
                async with async_session() as s2:
                    m = await s2.get(Mailing, mailing.id)
                    if m:
                        m.status = "failed"
                        await s2.commit()
                continue

            rows = (await session.execute(target_stmt)).all()
            user_ids = [r[0] for r in rows]

        success_count = 0
        failure_count = 0

        for chat_id in user_ids:
            if stop_event and stop_event.is_set():
                logger.info("Mailing worker interrupted by stop signal.")
                return
            try:
                await _send_one(bot, chat_id, mailing)
                success_count += 1
            except TelegramRetryAfter as exc:
                await asyncio.sleep(exc.retry_after)
                try:
                    await _send_one(bot, chat_id, mailing)
                    success_count += 1
                except Exception:
                    failure_count += 1
            except (TelegramForbiddenError, TelegramBadRequest):
                failure_count += 1
            except Exception as exc:
                logger.error(f"Mailing send error to {chat_id}: {exc}")
                failure_count += 1

            await asyncio.sleep(0.05)   # Rate-limit: ~20 msg/s

        async with async_session() as session:
            m = await session.get(Mailing, mailing.id)
            if m:
                m.status = "completed"
                m.end_time = datetime.utcnow()
                m.success_count = success_count
                m.failure_count = failure_count
                await session.commit()

        # Notify admins
        audience_name = AUDIENCES_MAP.get(audience, audience)
        report = (
            f"✅ Рассылка #{mailing.id} завершена!\n"
            f"Аудитория: {audience_name}\n"
            f"Успешно: {success_count}\n"
            f"Ошибки: {failure_count}"
        )
        for admin_id in settings.admin_ids:
            try:
                await bot.send_message(admin_id, report)
            except Exception:
                pass


def _build_audience_query(audience: str, creator_id: int | None):
    """Return a SELECT query that yields telegram_ids for the given audience."""
    if audience == "all":
        return select(User.telegram_id).where(User.is_blocked == False)  # noqa: E712

    if audience == "self":
        return select(User.telegram_id).where(User.telegram_id == creator_id)

    if audience == "active_subscription":
        return (
            select(User.telegram_id)
            .join(Subscription, Subscription.user_id == User.id)
            .where(Subscription.status == SubStatus.ACTIVE, User.is_blocked == False)  # noqa: E712
            .distinct()
        )

    if audience == "inactive_subscription":
        return (
            select(User.telegram_id)
            .join(Subscription, Subscription.user_id == User.id)
            .where(Subscription.status != SubStatus.ACTIVE, User.is_blocked == False)  # noqa: E712
            .distinct()
        )

    if audience == "no_subscription":
        sub_user_ids = select(Subscription.user_id).distinct()
        return select(User.telegram_id).where(
            User.id.notin_(sub_user_ids),
            User.is_blocked == False,  # noqa: E712
        )

    if audience == "referred":
        return select(User.telegram_id).where(
            User.referred_by.isnot(None),
            User.is_blocked == False,  # noqa: E712
        )

    return None  # Unknown audience → fail


async def _send_one(bot: Bot, chat_id: int, mailing: Mailing) -> None:
    """Send a single mailing message to one recipient."""
    from bot.handlers.mailing import build_user_kb

    text = mailing.text
    media = mailing.media_file_id
    m_type = mailing.media_file_type
    pos = mailing.media_position or "media_top"
    reply_markup = build_user_kb(mailing.buttons_json)

    # Album media type handling
    if m_type == "album":
        from bot.handlers.mailing import _parse_album_media

        if text and not reply_markup and len(text) <= 1024 and pos == "media_top":
            album_media = _parse_album_media(media, caption=text)
            await bot.send_media_group(chat_id, media=album_media)
            return

        async def _send_album() -> None:
            album_media = _parse_album_media(media, caption=None)
            await bot.send_media_group(chat_id, media=album_media)

        async def _text() -> None:
            if text:
                await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
            elif reply_markup:
                await bot.send_message(chat_id, "🔗", reply_markup=reply_markup)

        if pos == "media_top":
            await _send_album()
            await _text()
        else:
            await _text()
            await _send_album()
        return

    # Caption-combined send (photo/video with text ≤ 1024 chars)
    if media and text and len(text) <= 1024 and pos == "media_top":
        send_fn = bot.send_photo if m_type == "photo" else bot.send_video
        await send_fn(chat_id, media, caption=text, parse_mode="HTML", reply_markup=reply_markup)
        return

    async def _media() -> None:
        if not media:
            return
        if m_type == "photo":
            await bot.send_photo(chat_id, media)
        elif m_type == "video":
            await bot.send_video(chat_id, media)
        else:
            await bot.send_document(chat_id, media)

    async def _text() -> None:
        if text:
            await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)

    if pos == "media_top":
        await _media()
        await _text()
    else:
        await _text()
        await _media()
