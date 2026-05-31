from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, LabeledPrice
from sqlalchemy import select

from bot.config import settings
from bot.database import async_session
from bot.keyboards.devices import buy_device_pay_kb, devices_kb
from bot.models import BotSettings, ProxyAccount, Subscription
from bot.services.device_slots import get_included_device_slots, get_max_device_slots
from bot.services.payment_service import (
    create_yookassa_payment,
)
from bot.services.vhq_routing import is_vhq_subscription
from bot.utils.texts import BUY_DEVICE_SLOT, DEVICES_HEADER

logger = logging.getLogger(__name__)
router = Router()


@router.callback_query(F.data == "my_devices")
async def show_my_devices(callback: CallbackQuery) -> None:
    """Show the user's current devices and allow buying more slots."""
    telegram_id = callback.from_user.id

    async with async_session() as session:
        # Get active subscription
        result = await session.execute(
            select(Subscription)
            .where(Subscription.user.has(telegram_id=telegram_id))
            .where(Subscription.status == "ACTIVE")
            .order_by(Subscription.expires_at.desc())
        )
        subscriptions = result.scalars().all()
        subscription = next(
            (sub for sub in subscriptions if not is_vhq_subscription(sub)),
            subscriptions[0] if subscriptions else None,
        )

        if not subscription:
            await callback.answer("У вас нет активной подписки.", show_alert=True)
            return
        if is_vhq_subscription(subscription):
            await callback.answer("Для этого тарифа дополнительные устройства недоступны.", show_alert=True)
            return

        # Get bot settings for max devices and prices
        settings_result = await session.execute(select(BotSettings))
        all_settings = settings_result.scalars().all()
        s_dict = {s.key: s.value for s in all_settings}
        
        included_slots = await get_included_device_slots(session)
        max_allowed = await get_max_device_slots(session)
        price_rub = int(s_dict.get("extra_device_price_rub", "50"))
        
        # Get actual proxy accounts (keys) for this user to show them
        proxy_result = await session.execute(
            select(ProxyAccount).where(ProxyAccount.subscription_id == subscription.id)
        )
        proxies = proxy_result.scalars().all()

        used_slots = len(proxies)
        current_slots = max(subscription.device_slots, included_slots)
        extra_slots = max(0, current_slots - included_slots)

        if max_allowed is None:
            extra_slots_text = (
                f"Дополнительно куплено слотов: <b>{extra_slots}</b>\n"
                "Дополнительные устройства можно докупать без лимита."
            )
        else:
            remaining = max(0, max_allowed - current_slots)
            extra_slots_text = (
                f"Дополнительно куплено слотов: <b>{extra_slots}</b>\n"
                f"Ещё можно докупить: <b>{remaining}</b>"
            )
        
    expires_str = subscription.expires_at.strftime("%d.%m.%Y")
    text = DEVICES_HEADER.format(
        expires=expires_str,
        used_slots=used_slots,
        included_slots=included_slots,
        extra_slots_text=extra_slots_text,
    )
    
    for proxy in proxies:
        text += f"\n\n🔑 <b>Локация {proxy.server_id}</b>\n<code>{proxy.sub_url}</code>"

    can_buy = max_allowed is None or current_slots < max_allowed
    kb = devices_kb(subscription.id, can_buy, price_rub)

    try:
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("buy_device_"))
async def buy_device_slot(callback: CallbackQuery) -> None:
    """Show payment methods for buying an extra device slot."""
    sub_id = int(callback.data.split("_")[2])

    async with async_session() as session:
        settings_result = await session.execute(select(BotSettings))
        all_settings = settings_result.scalars().all()
        s_dict = {s.key: s.value for s in all_settings}
        
        price_rub = int(s_dict.get("extra_device_price_rub", "50"))
        price_stars = int(s_dict.get("extra_device_price_stars", "0"))
        stars_enabled = s_dict.get("stars_enabled", "1") == "1"

    stars_part = f" / {price_stars}⭐" if stars_enabled and price_stars > 0 else ""

    text = BUY_DEVICE_SLOT.format(
        price_rub=price_rub,
        stars_part=stars_part,
    )
    kb = buy_device_pay_kb(sub_id, stars_enabled)

    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except Exception:
        pass
    await callback.answer()


@router.callback_query(F.data.startswith("paydev_"))
async def process_paydev(callback: CallbackQuery) -> None:
    """Handle payment method selection for device slot."""
    parts = callback.data.split("_")
    method = parts[1]
    sub_id = int(parts[2])
    telegram_id = callback.from_user.id
    chat_id = callback.message.chat.id

    async with async_session() as session:
        settings_result = await session.execute(select(BotSettings))
        all_settings = settings_result.scalars().all()
        s_dict = {s.key: s.value for s in all_settings}
        
        price_rub = int(s_dict.get("extra_device_price_rub", "50"))
        price_stars = int(s_dict.get("extra_device_price_stars", "100"))

        sub = await session.get(Subscription, sub_id)
        if not sub:
            await callback.answer("Подписка не найдена.")
            return
        user_db_id = sub.user_id

    invoice_payload = f"dev_{sub_id}"
    title = "Дополнительное устройство"
    description = "Оплата слота для дополнительного устройства."

    if method == "stars":
        await bot_send_stars_invoice(callback, title, description, invoice_payload, price_stars)
    elif method == "telegram":
        if price_rub < 70:
            await callback.answer(
                f"Telegram Pay недоступен для суммы {price_rub} ₽. Минимум для этого способа - 70 ₽.",
                show_alert=True,
            )
            return
        await bot_send_telegram_invoice(callback, title, description, invoice_payload, price_rub)
    elif method == "yookassa":
        await bot_send_yookassa(callback, title, description, invoice_payload, price_rub, user_db_id, sub_id)
    elif method == "robokassa":
        await callback.answer("Robokassa для устройств пока в разработке.", show_alert=True)
    
    await callback.answer()


async def bot_send_stars_invoice(callback: CallbackQuery, title: str, description: str, payload: str, price: int):
    prices = [LabeledPrice(label=title, amount=price)]
    await callback.bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=title,
        description=description,
        payload=payload,
        provider_token="",
        currency="XTR",
        prices=prices,
    )

async def bot_send_telegram_invoice(callback: CallbackQuery, title: str, description: str, payload: str, price: int):
    prices = [LabeledPrice(label=title, amount=price * 100)]
    await callback.bot.send_invoice(
        chat_id=callback.message.chat.id,
        title=title,
        description=description,
        payload=payload,
        provider_token=settings.telegram_payment_provider_token,
        currency="RUB",
        prices=prices,
    )

async def bot_send_yookassa(callback: CallbackQuery, title: str, description: str, payload: str, price: int, user_id: int, sub_id: int):
    metadata = {
        "is_device": "1",
        "sub_id": sub_id,
        "user_id": callback.from_user.id,
        "chat_id": callback.message.chat.id,
    }
    url, payment_id = await create_yookassa_payment(price, title, metadata)
    if url:
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Оплатить", url=url)]])
        await callback.message.edit_text(f"Для оплаты перейдите по ссылке:\n\n{url}", reply_markup=kb)
    else:
        await callback.answer("Ошибка при создании платежа.", show_alert=True)
