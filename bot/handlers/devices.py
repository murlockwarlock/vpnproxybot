from __future__ import annotations

import asyncio
import html
import logging

from aiogram import F, Router
from aiogram.types import CallbackQuery, LabeledPrice
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from bot.config import settings
from bot.database import async_session
from bot.keyboards.devices import buy_device_pay_kb, devices_kb, adapt_devices_kb, device_subscriptions_kb
from bot.models import BotSettings, ProxyAccount, Subscription
from bot.services import vpn_manager
from bot.services.device_slots import get_included_device_slots, get_max_device_slots
from bot.services.payment_service import (
    create_yookassa_payment,
)
from bot.services.vhq_routing import is_vhq_subscription
from bot.services.adapt_routing import is_adapt_subscription, get_adapt_uuid_from_subscription
from bot.services.adapt_api import AdaptAPI, AdaptAPIError
from bot.services.vhq_subscription_proxy import get_subscription_display_key
from bot.services.webstore_bridge import fetch_linked_web_profile, sync_linked_web_subscriptions
from bot.utils.device_info import adapt_device_details, describe_user_agent, format_activity
from bot.utils.texts import BUY_DEVICE_SLOT

logger = logging.getLogger(__name__)
router = Router()


def _format_dt_msk(dt) -> str:
    if not dt:
        return "-"
    from datetime import timezone, timedelta
    msk = timezone(timedelta(hours=3))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(msk).strftime("%d.%m.%Y %H:%M")


@router.callback_query((F.data == "my_devices") | F.data.startswith("manage_devices_"))
async def show_my_devices(callback: CallbackQuery, *, acknowledge: bool = True) -> None:
    """Show the user's current devices and allow buying more slots."""
    if acknowledge:
        await callback.answer("Загружаем устройства…")
    telegram_id = callback.from_user.id
    web_profile = await fetch_linked_web_profile(telegram_id)
    await sync_linked_web_subscriptions(telegram_id, web_profile)

    async with async_session() as session:
        # Get active subscription
        result = await session.execute(
            select(Subscription)
            .options(selectinload(Subscription.tariff))
            .where(Subscription.user.has(telegram_id=telegram_id))
            .where(Subscription.status == "ACTIVE")
            .order_by(Subscription.expires_at.desc())
        )
        subscriptions = result.scalars().all()

        if callback.data == "my_devices" and len(subscriptions) > 1:
            await callback.message.edit_text(
                "📱 <b>Выберите подписку</b>\n\nУстройства управляются отдельно для каждой ссылки.",
                reply_markup=device_subscriptions_kb(subscriptions),
                parse_mode="HTML",
            )
            return

        selected_id = None
        if callback.data.startswith("manage_devices_"):
            try:
                selected_id = int(callback.data.removeprefix("manage_devices_"))
            except ValueError:
                await callback.message.answer("Не удалось открыть подписку. Попробуйте ещё раз или напишите в поддержку.")
                return
        
        # Prioritize Adapt subscriptions first
        subscription = next((sub for sub in subscriptions if sub.id == selected_id), None) if selected_id else next(
            (sub for sub in subscriptions if is_adapt_subscription(sub)),
            None
        )
        
        # Fallback to any non-VHQ subscription (e.g. Marzban)
        if not subscription:
            subscription = next(
                (sub for sub in subscriptions if not is_vhq_subscription(sub)),
                subscriptions[0] if subscriptions else None,
            )

        if not subscription:
            await callback.message.answer("У вас нет активной подписки.")
            return
        if is_vhq_subscription(subscription):
            expires_str = _format_dt_msk(subscription.expires_at)
            tariff_name = subscription.tariff.label if subscription.tariff else "Ваш тариф"
            sub_url = get_subscription_display_key(subscription) or subscription.vpn_key or "-"
            text = (
                "📱 <b>Управление устройствами</b>\n\n"
                f"📦 Тариф: <b>{html.escape(tariff_name)}</b>\n"
                f"📅 Подписка до: <b>{expires_str} МСК</b>\n"
                f"🔗 Ссылка подписки: <code>{html.escape(sub_url)}</code>\n\n"
                "Отдельные данные об устройствах для этой подписки недоступны. "
                "Если нужно отключить устройство, напишите в поддержку."
            )
            await callback.message.edit_text(
                text,
                reply_markup=devices_kb(subscription.id, False, 0),
                disable_web_page_preview=True,
            )
            return

        # Handle Adapt subscriptions specially
        if is_adapt_subscription(subscription):
            adapt_uuid = get_adapt_uuid_from_subscription(subscription)
            if not adapt_uuid:
                await callback.message.answer("Не удалось загрузить устройства. Напишите в поддержку.")
                return
            
            api = AdaptAPI()
            devices_result, status_result = await asyncio.gather(
                api.get_devices(adapt_uuid),
                api.get_status(adapt_uuid),
                return_exceptions=True,
            )
            if isinstance(devices_result, Exception):
                exc = devices_result
                logger.error("Failed to get device list for sub %s: %s", subscription.id, exc)
                await callback.message.answer(
                    "Не удалось получить список устройств. Попробуйте позже или напишите в поддержку."
                )
                return
            devices = devices_result

            if isinstance(status_result, dict) and status_result.get("devices") is not None:
                try:
                    provider_limit = max(1, int(status_result["devices"]))
                    if subscription.device_slots != provider_limit:
                        subscription.device_slots = provider_limit
                        await session.commit()
                except (TypeError, ValueError):
                    logger.warning("Invalid device limit for sub %s: %r", subscription.id, status_result.get("devices"))
            elif isinstance(status_result, Exception):
                logger.warning("Failed to refresh device limit for sub %s: %s", subscription.id, status_result)

            used_slots = len(devices)
            expires_str = _format_dt_msk(subscription.expires_at)
            limit = subscription.device_slots or 1
            if getattr(subscription, "billing_mode", None) == "demo":
                tariff_name = "Демо-доступ"
            else:
                tariff_name = subscription.tariff.label if subscription.tariff else "Ваш тариф"
            sub_url = get_subscription_display_key(subscription) or subscription.vpn_key or "-"
            
            text = (
                f"📱 <b>Управление устройствами</b>\n\n"
                f"📦 Тариф: <b>{html.escape(tariff_name)}</b>\n"
                f"📅 Подписка до: <b>{expires_str} МСК</b>\n"
                f"🔗 Ссылка подписки: <code>{html.escape(sub_url)}</code>\n"
                f"Подключено устройств: <b>{used_slots}</b> из <b>{limit}</b>\n"
            )
            
            if not devices:
                text += "\nУстройства пока не подключены."
            else:
                for idx, dev in enumerate(devices, 1):
                    info = adapt_device_details(dev, idx)
                    text += f"\n\n🖥 <b>{idx}. {html.escape(info['name'])}</b>"
                    if info["model"] and info["model"] != info["name"]:
                        text += f"\n📱 Модель: <b>{html.escape(info['model'])}</b>"
                    text += (
                        f"\n💻 ОС: <b>{html.escape(info['os'])}</b>"
                        f"\n🕓 Последняя активность: <b>{info['last_activity']}</b>"
                        f"\n🌐 Последний IP: <code>{html.escape(info['ip'])}</code>"
                    )

            kb = adapt_devices_kb(subscription.id, devices)
            try:
                await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
            except Exception as exc:
                if "message is not modified" not in str(exc).lower():
                    logger.warning("Failed to render device list for sub %s: %s", subscription.id, exc)
                    await callback.message.answer(
                        "Не удалось обновить экран устройств. Попробуйте ещё раз или напишите в поддержку."
                    )
            return

        # Get bot settings for max devices and prices (Old Logic for Non-Adapt)
        settings_result = await session.execute(select(BotSettings))
        all_settings = settings_result.scalars().all()
        s_dict = {s.key: s.value for s in all_settings}
        
        included_slots = await get_included_device_slots(session)
        max_allowed = await get_max_device_slots(session)
        price_rub = int(s_dict.get("extra_device_price_rub", "50"))
        
        # Get actual proxy accounts (keys) for this user to show them
        proxy_result = await session.execute(
            select(ProxyAccount)
            .options(selectinload(ProxyAccount.server))
            .where(ProxyAccount.subscription_id == subscription.id)
        )
        proxies = proxy_result.scalars().all()

        activity = await asyncio.gather(*(
            vpn_manager.get_key_activity(proxy.server, proxy.marzban_username)
            for proxy in proxies
        ))

        current_slots = max(subscription.device_slots or 0, included_slots)
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
        
    expires_str = _format_dt_msk(subscription.expires_at)
    if getattr(subscription, "billing_mode", None) == "demo":
        tariff_name = "Демо-доступ"
    else:
        tariff_name = subscription.tariff.label if subscription.tariff else "Лайт"
    sub_url = get_subscription_display_key(subscription) or subscription.vpn_key or "-"
    text = (
        f"📱 <b>Управление устройствами</b>\n\n"
        f"📦 Тариф: <b>{html.escape(tariff_name)}</b>\n"
        f"📅 Подписка до: <b>{expires_str} МСК</b>\n"
        f"🔗 Ссылка подписки: <code>{html.escape(sub_url)}</code>\n"
        f"Доступно устройств: <b>{current_slots}</b>\n"
        f"{extra_slots_text}\n\n"
        f"Данные подключения:"
    )
    
    if not proxies:
        text += "\n\nАктивный ключ не найден. Напишите в поддержку."
    for index, (proxy, key_info) in enumerate(zip(proxies, activity), 1):
        location = proxy.server.location or proxy.server.name
        os_label = describe_user_agent(key_info.get("sub_last_user_agent"))
        last_activity = format_activity(key_info.get("online_at"))
        text += (
            f"\n\n🔑 <b>{index}. {html.escape(location)}</b>"
            f"\n💻 ОС последнего клиента: <b>{html.escape(os_label)}</b>"
            f"\n🕓 Последняя активность: <b>{last_activity}</b>"
            f"\n🔗 <code>{html.escape(proxy.sub_url)}</code>"
        )

    can_buy = max_allowed is None or current_slots < max_allowed
    kb = devices_kb(subscription.id, can_buy, price_rub)

    try:
        await callback.message.edit_text(text, reply_markup=kb, disable_web_page_preview=True)
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning("Failed to render device list for sub %s: %s", subscription.id, exc)
            await callback.message.answer(
                "Не удалось обновить экран устройств. Попробуйте ещё раз или напишите в поддержку."
            )


@router.callback_query(F.data.startswith("del_adapt_dev_"))
async def delete_adapt_device(callback: CallbackQuery) -> None:
    """Handle deletion of an Adapt device."""
    await callback.answer("Удаляем…")
    parts = callback.data.split("_", 4)
    try:
        sub_id = int(parts[3])
        device_id = int(parts[4])
    except (IndexError, ValueError):
        await callback.message.answer("Не удалось определить устройство. Попробуйте ещё раз или напишите в поддержку.")
        return

    async with async_session() as session:
        result = await session.execute(
            select(Subscription)
            .where(Subscription.id == sub_id)
            .where(Subscription.user.has(telegram_id=callback.from_user.id))
        )
        sub = result.scalar_one_or_none()
        if not sub:
            await callback.message.answer("Подписка не найдена. Напишите в поддержку.")
            return
            
        adapt_uuid = get_adapt_uuid_from_subscription(sub)
        if not adapt_uuid:
            await callback.message.answer("Не найдены служебные данные подписки. Напишите в поддержку.")
            return

    try:
        success = await AdaptAPI().delete_device(adapt_uuid, device_id)
        if success:
            await callback.message.answer("✅ Устройство успешно удалено!")
        else:
            await callback.message.answer("Не удалось удалить устройство. Попробуйте позже или напишите в поддержку.")
    except AdaptAPIError as exc:
        logger.error(f"Failed to delete adapt device {device_id} for sub {sub_id}: {exc}")
        await callback.message.answer("Не удалось удалить устройство. Попробуйте позже или напишите в поддержку.")

    # Refresh the device list
    callback.data = f"manage_devices_{sub_id}"
    await show_my_devices(callback, acknowledge=False)


@router.callback_query(F.data.startswith("buy_device_"))
async def buy_device_slot(callback: CallbackQuery) -> None:
    """Show payment methods for buying an extra device slot."""
    await callback.answer()
    try:
        sub_id = int(callback.data.split("_")[2])
    except (IndexError, ValueError):
        await callback.message.answer("Не удалось определить подписку. Попробуйте ещё раз или напишите в поддержку.")
        return

    async with async_session() as session:
        subscription = await session.scalar(
            select(Subscription)
            .where(Subscription.id == sub_id)
            .where(Subscription.user.has(telegram_id=callback.from_user.id))
        )
        if not subscription:
            await callback.message.answer("Подписка не найдена. Напишите в поддержку.")
            return
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
    except Exception as exc:
        if "message is not modified" not in str(exc).lower():
            logger.warning("Failed to show device payment methods for sub %s: %s", sub_id, exc)
            await callback.message.answer("Не удалось показать способы оплаты. Попробуйте ещё раз или напишите в поддержку.")


@router.callback_query(F.data.startswith("paydev_"))
async def process_paydev(callback: CallbackQuery) -> None:
    """Handle payment method selection for device slot."""
    await callback.answer()
    parts = callback.data.split("_")
    try:
        method = parts[1]
        sub_id = int(parts[2])
    except (IndexError, ValueError):
        await callback.message.answer("Не удалось определить подписку. Попробуйте ещё раз или напишите в поддержку.")
        return
    telegram_id = callback.from_user.id
    chat_id = callback.message.chat.id

    async with async_session() as session:
        settings_result = await session.execute(select(BotSettings))
        all_settings = settings_result.scalars().all()
        s_dict = {s.key: s.value for s in all_settings}
        
        price_rub = int(s_dict.get("extra_device_price_rub", "50"))
        price_stars = int(s_dict.get("extra_device_price_stars", "100"))

        sub = await session.scalar(
            select(Subscription)
            .where(Subscription.id == sub_id)
            .where(Subscription.user.has(telegram_id=telegram_id))
        )
        if not sub:
            await callback.message.answer("Подписка не найдена. Напишите в поддержку.")
            return
        user_db_id = sub.user_id

    invoice_payload = f"dev_{sub_id}"
    title = "Дополнительное устройство"
    description = "Оплата слота для дополнительного устройства."

    if method == "stars":
        await bot_send_stars_invoice(callback, title, description, invoice_payload, price_stars)
    elif method == "telegram":
        if price_rub < 70:
            await callback.message.answer(
                f"Этот способ оплаты недоступен для суммы {price_rub} ₽. Минимальная сумма — 70 ₽."
            )
            return
        await bot_send_telegram_invoice(callback, title, description, invoice_payload, price_rub)
    elif method == "yookassa":
        await bot_send_yookassa(callback, title, description, invoice_payload, price_rub, user_db_id, sub_id)
    elif method == "robokassa":
        await callback.message.answer("Этот способ оплаты для дополнительных устройств пока недоступен.")
    else:
        await callback.message.answer("Неизвестный способ оплаты. Попробуйте ещё раз или напишите в поддержку.")


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
        await callback.message.answer("Не удалось создать платёж. Попробуйте ещё раз или напишите в поддержку.")
