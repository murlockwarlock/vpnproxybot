from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot.config import settings


def devices_kb(sub_id: int, can_buy: bool, price_rub: int) -> InlineKeyboardMarkup:
    """Keyboard for My Devices. If can_buy is True, show buy button."""
    builder = InlineKeyboardBuilder()
    if can_buy:
        builder.row(
            InlineKeyboardButton(
                text=f"➕ Дополнительное устройство ({price_rub}₽)",
                callback_data=f"buy_device_{sub_id}",
            )
        )
    builder.row(
        InlineKeyboardButton(text="◀️ Назад в профиль", callback_data="profile")
    )
    return builder.as_markup()


def buy_device_pay_kb(sub_id: int, stars_enabled: bool = True) -> InlineKeyboardMarkup:
    """Keyboard for selecting payment method for extra device slot."""
    builder = InlineKeyboardBuilder()

    if stars_enabled:
        builder.row(
            InlineKeyboardButton(
                text="⭐ Telegram Stars",
                callback_data=f"paydev_stars_{sub_id}",
            )
        )

    if settings.telegram_payment_provider_token:
        builder.row(
            InlineKeyboardButton(
                text="💳 Оплатить",
                callback_data=f"paydev_telegram_{sub_id}",
            )
        )

    if settings.yookassa_shop_id and settings.yookassa_secret_key:
        builder.row(
            InlineKeyboardButton(
                text="💳 YooKassa (карта / банк)",
                callback_data=f"paydev_yookassa_{sub_id}",
            )
        )

    if settings.robokassa_merchant_login:
        builder.row(
            InlineKeyboardButton(
                text="💰 Robokassa",
                callback_data=f"paydev_robokassa_{sub_id}",
            )
        )

    builder.row(
        InlineKeyboardButton(text="◀️ Отмена", callback_data="my_devices")
    )
    return builder.as_markup()
