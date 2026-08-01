"""Notification helpers used by the scheduler and payment handlers."""

from __future__ import annotations

import html
import asyncio
import logging

from aiogram import Bot

from bot.config import settings

logger = logging.getLogger(__name__)


async def _send_admin_text(bot: Bot, text: str) -> None:
    admin_ids = set(settings.admin_ids)
    admin_ids.update(settings.owner_ids)
    admin_ids.update(settings.support_agent_ids)
    if not admin_ids:
        logger.warning("No admin IDs configured for admin notification")
        return

    sent = 0
    failed = 0
    for admin_id in admin_ids:
        for attempt in range(1, 4):
            try:
                await bot.send_message(admin_id, text, parse_mode="HTML")
                sent += 1
                break
            except Exception as e:
                retry_after = float(getattr(e, "retry_after", 0) or 0)
                logger.warning("Failed to notify admin %s attempt=%s: %s", admin_id, attempt, e)
                if attempt == 3:
                    failed += 1
                    break
                await asyncio.sleep(min(retry_after or attempt, 5.0))
    logger.info("Admin notification finished: sent=%s failed=%s", sent, failed)


async def notify_staff_text(bot: Bot, text: str) -> None:
    """Send a preformatted operational message to all staff with retries."""
    await _send_admin_text(bot, text)


async def notify_expiring(bot: Bot, user_id: int, text: str, reply_markup=None) -> bool:
    """Send an expiry warning to a user. Returns True if successful."""
    for attempt in range(1, 4):
        try:
            await bot.send_message(user_id, text, parse_mode="HTML", reply_markup=reply_markup)
            return True
        except Exception as e:
            logger.warning("Failed to notify user %s attempt=%s: %s", user_id, attempt, e)
            if attempt < 3:
                await asyncio.sleep(min(float(getattr(e, "retry_after", 0) or attempt), 5.0))
    return False


async def notify_expired(bot: Bot, user_id: int, text: str, reply_markup=None) -> bool:
    """Send an expiry notification. Returns True if successful."""
    return await notify_expiring(bot, user_id, text, reply_markup=reply_markup)


async def notify_admins_payment(
    bot: Bot,
    telegram_id: int,
    full_name: str,
    username: str | None,
    amount_rub: float,
    tariff_label: str,
    method: str,
    platform: str,
) -> None:
    """Notify all admins about a completed payment."""
    from bot.database import async_session
    from bot.models import BotSettings

    async with async_session() as session:
        row = await session.get(BotSettings, "notify_admins_payment")
        if row and row.value == "0":
            logger.info(
                "Admin payment notifications disabled: telegram_id=%s amount_rub=%.2f tariff=%s method=%s",
                telegram_id,
                amount_rub,
                tariff_label,
                method,
            )
            return  # disabled

    uname = f"@{username}" if username else "—"
    title = "💎 <b>Оплата с баланса!</b>" if "баланс" in method.lower() else "💰 <b>Новая оплата!</b>"
    text = (
        f"{title}\n\n"
        f"👤 {full_name} (<code>{telegram_id}</code>, {uname})\n"
        f"🛒 Тариф: <b>{tariff_label}</b>\n"
        f"💵 Сумма: <b>{amount_rub:.0f} ₽</b>\n"
        f"💳 Способ: {method}\n"
        f"📱 Платформа: {platform}"
    )
    if not settings.notification_recipient_ids:
        logger.warning(
            "No ADMIN_IDS configured for payment notification: telegram_id=%s amount_rub=%.2f tariff=%s",
            telegram_id,
            amount_rub,
            tariff_label,
        )
        return

    logger.info(
        "Sending admin payment notifications: telegram_id=%s amount_rub=%.2f tariff=%s method=%s admins=%s",
        telegram_id,
        amount_rub,
        tariff_label,
        method,
        settings.notification_recipient_ids,
    )
    await _send_admin_text(bot, text)


async def notify_admins_issue(
    bot: Bot,
    *,
    title: str,
    details: list[str],
) -> None:
    """Broadcast an operational issue to all admins."""
    safe_details = [html.escape(line) for line in details if line]
    text = f"🚨 <b>{html.escape(title)}</b>"
    if safe_details:
        text += "\n\n" + "\n".join(safe_details)
    await _send_admin_text(bot, text)
