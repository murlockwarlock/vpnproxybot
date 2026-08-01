from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramBadRequest

from bot.middlewares import error_feedback


@pytest.mark.asyncio
async def test_callback_failure_is_acknowledged_and_user_gets_support_message(monkeypatch):
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock()),
    )
    event = SimpleNamespace(callback_query=callback, message=None)
    notify_staff = AsyncMock()
    monkeypatch.setattr(error_feedback, "notify_admins_issue", notify_staff)

    async def failing_handler(_event, _data):
        raise RuntimeError("boom")

    result = await error_feedback.ErrorFeedbackMiddleware()(
        failing_handler,
        event,
        {"bot": AsyncMock()},
    )

    assert result is None
    callback.answer.assert_awaited_once()
    callback.message.answer.assert_awaited_once()
    assert "поддерж" in callback.message.answer.await_args.args[0].lower()
    notify_staff.assert_awaited_once()


@pytest.mark.asyncio
async def test_expired_callback_is_only_logged_without_user_or_staff_alert(monkeypatch):
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=123),
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock()),
    )
    event = SimpleNamespace(callback_query=callback, message=None)
    notify_staff = AsyncMock()
    monkeypatch.setattr(error_feedback, "notify_admins_issue", notify_staff)

    async def failing_handler(_event, _data):
        raise TelegramBadRequest(
            method=SimpleNamespace(),
            message="Bad Request: query is too old and response timeout expired or query ID is invalid",
        )

    result = await error_feedback.ErrorFeedbackMiddleware()(
        failing_handler,
        event,
        {"bot": AsyncMock()},
    )

    assert result is None
    callback.answer.assert_not_awaited()
    callback.message.answer.assert_not_awaited()
    notify_staff.assert_not_awaited()
