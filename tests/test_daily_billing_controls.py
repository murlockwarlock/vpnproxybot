from unittest.mock import AsyncMock, Mock

import pytest
from aiogram import Bot
from aiogram.methods import AnswerCallbackQuery
from aiogram.types import CallbackQuery, User

from bot.handlers import profile
from bot.handlers.payment import _topup_text
from bot.keyboards.client import profile_kb


@pytest.mark.parametrize("mode,autodebit", [(False, False), (False, True), (True, False), (True, True)])
@pytest.mark.parametrize("has_choice", [False, True])
def test_profile_has_no_daily_billing_controls(mode, autodebit, has_choice):
    keyboard = profile_kb(
        has_active_subs=True,
        balance_mode_enabled=mode,
        balance_autodebit_enabled=autodebit,
        has_daily_charge_tariff_choice=has_choice,
        has_recurring=True,
        recurring_active=True,
    )
    callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert "balance_toggle" not in callbacks
    assert "daily_charge_tariff_choice" not in callbacks
    assert not any((value or "").startswith("set_daily_tariff_") for value in callbacks)
    assert "buy" in callbacks and "ref_link" in callbacks
    assert "recurring_toggle_off" in callbacks
    if mode or autodebit:
        assert "balance_menu" in callbacks and "balance_history" in callbacks


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["balance_toggle", "daily_charge_tariff_choice", "set_daily_tariff_2"])
async def test_old_daily_buttons_are_rejected_by_router(monkeypatch, data):
    database = Mock(side_effect=AssertionError("Retired controls must not change persistent state"))
    monkeypatch.setattr(profile, "async_session", database)
    session = AsyncMock()
    session.return_value = True
    bot = Bot(token="123456:" + "A" * 35, session=session)
    callback = CallbackQuery(id="old-button", from_user=User(id=123, is_bot=False, first_name="User"), chat_instance="test", data=data).as_(bot)
    await profile.router.callback_query.trigger(callback)
    database.assert_not_called()
    session.assert_awaited_once()
    method = session.call_args.args[1]
    assert isinstance(method, AnswerCallbackQuery)
    assert method.text == "Ежедневные списания больше недоступны" and method.show_alert


def test_topup_does_not_advertise_daily_billing():
    text = _topup_text(100, 3.17)
    assert "ежедневн" not in text and "включить" not in text
    assert "100.00" in text
