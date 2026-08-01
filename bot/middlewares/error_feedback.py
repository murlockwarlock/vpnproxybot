"""Last-resort user feedback and staff alerting for handler failures."""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import TelegramObject

from bot.config import settings
from bot.services.notifications import notify_admins_issue

logger = logging.getLogger(__name__)


def _is_expired_callback_error(exc: Exception) -> bool:
    """Telegram rejects callback acknowledgements after their short lifetime."""
    if not isinstance(exc, TelegramBadRequest):
        return False
    text = str(exc).lower()
    return "query is too old" in text or "query id is invalid" in text


class ErrorFeedbackMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        try:
            return await handler(event, data)
        except Exception as exc:
            if getattr(event, "callback_query", None) is not None and _is_expired_callback_error(exc):
                # This commonly happens when an update was queued across a restart.  It is
                # not an application incident and often occurs after the useful edit/send
                # has already completed, so avoid misleading the user and paging staff.
                logger.warning("Ignored expired Telegram callback: %s", exc)
                return None
            logger.exception("Unhandled Telegram update error")
            callback = getattr(event, "callback_query", None)
            message = getattr(event, "message", None)
            support = settings.support_username or "поддержку"
            user_text = (
                "Не удалось завершить действие. Повторите попытку чуть позже.\n"
                f"Если оплата уже прошла или вопрос срочный, напишите в поддержку {support}."
            )

            if callback is not None:
                try:
                    await callback.answer()
                except Exception:
                    pass
                try:
                    if callback.message:
                        await callback.message.answer(user_text)
                except Exception:
                    pass
            elif message is not None:
                try:
                    await message.answer(user_text)
                except Exception:
                    pass

            bot = data.get("bot")
            if bot is not None:
                try:
                    user = callback.from_user if callback is not None else getattr(message, "from_user", None)
                    await notify_admins_issue(
                        bot,
                        title="Ошибка обработчика Telegram",
                        details=[
                            f"Пользователь: {getattr(user, 'id', '—')}",
                            f"Тип события: {type(event).__name__}",
                            f"Ошибка: {type(exc).__name__}: {exc}",
                        ],
                    )
                except Exception:
                    logger.exception("Failed to notify staff about Telegram handler error")
            return None
