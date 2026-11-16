import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot.handlers import admin as admin_handler


pytestmark = pytest.mark.asyncio


def _callback(edit_text=None):
    message = SimpleNamespace(
        edit_text=edit_text or AsyncMock(),
        answer=AsyncMock(),
    )
    return SimpleNamespace(
        from_user=SimpleNamespace(id=999),
        data="adm_logs",
        message=message,
        answer=AsyncMock(),
    )


def _enable_admin(monkeypatch):
    monkeypatch.setattr(admin_handler, "_is_admin", lambda _user_id: True)


def _assert_html_and_back_button(call):
    assert call.kwargs["parse_mode"] == "HTML"
    markup = call.kwargs["reply_markup"]
    assert markup.inline_keyboard[0][0].callback_data == "adm_back"


async def test_admin_logs_escapes_arbitrary_log_content(monkeypatch):
    _enable_admin(monkeypatch)
    lines = ['<> <tag> & "quoted" \'apostrophe\' — Привет']
    monkeypatch.setattr("bot.services.payment_logger.get_payment_log_tail", lambda _limit: lines)
    callback = _callback()

    await admin_handler.admin_logs(callback)

    callback.message.edit_text.assert_awaited_once()
    _assert_html_and_back_button(callback.message.edit_text.await_args)
    rendered = callback.message.edit_text.await_args.args[0]
    assert "&lt;&gt;" in rendered
    assert "&lt;tag&gt;" in rendered
    assert "&amp;" in rendered
    assert "&quot;quoted&quot;" in rendered
    assert "&#x27;apostrophe&#x27;" in rendered
    assert "Привет" in rendered
    assert "<tag>" not in rendered
    callback.answer.assert_awaited_once()


async def test_admin_logs_limits_long_journal_and_keeps_latest_events(monkeypatch):
    _enable_admin(monkeypatch)
    lines = [f"event-{index} " + ("x" * 100) for index in range(60)]
    monkeypatch.setattr("bot.services.payment_logger.get_payment_log_tail", lambda _limit: lines)
    callback = _callback()

    await admin_handler.admin_logs(callback)

    rendered = callback.message.edit_text.await_args.args[0]
    _assert_html_and_back_button(callback.message.edit_text.await_args)
    assert len(rendered) <= admin_handler._MAX_MSG
    assert "event-59" in rendered
    assert "event-0" not in rendered
    assert rendered.startswith("📋 <b>")
    assert rendered.endswith("</pre>")


async def test_admin_logs_limits_one_very_long_line_without_breaking_html(monkeypatch):
    _enable_admin(monkeypatch)
    lines = ["<tag>&" * 5000]
    monkeypatch.setattr("bot.services.payment_logger.get_payment_log_tail", lambda _limit: lines)
    callback = _callback()

    await admin_handler.admin_logs(callback)

    rendered = callback.message.edit_text.await_args.args[0]
    _assert_html_and_back_button(callback.message.edit_text.await_args)
    assert len(rendered) <= admin_handler._MAX_MSG
    assert rendered.endswith("</pre>")
    assert "<tag>" not in rendered
    assert "&lt;tag&gt;" in rendered
    body = rendered.split("<pre>", 1)[1][:-len("</pre>")]
    assert not re.search(r"&[^;]*$", body)


async def test_admin_logs_uses_safe_fallback_when_edit_fails(monkeypatch):
    _enable_admin(monkeypatch)
    lines = ["failure <tag> & details"]
    monkeypatch.setattr("bot.services.payment_logger.get_payment_log_tail", lambda _limit: lines)
    edit_text = AsyncMock(side_effect=RuntimeError("edit failed"))
    callback = _callback(edit_text)

    await admin_handler.admin_logs(callback)

    edit_text.assert_awaited_once()
    callback.message.answer.assert_awaited_once()
    _assert_html_and_back_button(callback.message.answer.await_args)
    fallback_text = callback.message.answer.await_args.args[0]
    assert "&lt;tag&gt;" in fallback_text
    assert "&amp;" in fallback_text
    assert "<tag>" not in fallback_text
    callback.answer.assert_awaited_once()


async def test_admin_logs_handles_empty_journal(monkeypatch):
    _enable_admin(monkeypatch)
    monkeypatch.setattr("bot.services.payment_logger.get_payment_log_tail", lambda _limit: [])
    callback = _callback()

    await admin_handler.admin_logs(callback)

    callback.message.edit_text.assert_awaited_once()
    _assert_html_and_back_button(callback.message.edit_text.await_args)
    rendered = callback.message.edit_text.await_args.args[0]
    assert rendered == "📋 <b>Логи оплат</b>\n\nФайл логов пуст или ещё не создан."
    callback.answer.assert_awaited_once()
