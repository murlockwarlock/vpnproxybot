"""Aiohttp webhook handlers for YooKassa and Robokassa payments."""

from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import hmac
import ipaddress
import logging
from datetime import datetime, timedelta
from html import escape
from typing import Any

import aiohttp
from aiohttp import web
from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError

from bot.config import settings
from bot.database import async_session
from bot.keyboards.client import back_to_menu_kb
from bot.models import (
    AdaptSubscription,
    BalanceTransactionKind,
    BalanceTopUp,
    BalanceTransaction,
    BotSettings,
    Payment,
    PaymentMethod,
    PaymentStatus,
    Platform,
    Partner,
    PartnerLink,
    RecurringPaymentProfile,
    RobokassaPayment,
    ReferralConfig,
    Server,
    Subscription,
    SubStatus,
    Tariff,
    TariffType,
    User,
    ProxyAccount,
    WebPartnerEarning,
    WebReferralEarning,
)
from bot.services import vpn_manager
from bot.services.adapt_routing import build_adapt_client_name, is_adapt_subscription
from bot.services.adapt_subscription_proxy import fetch_adapt_mirror_payload
from bot.services.client_names import build_client_name
from bot.services.cluster import LeaseManager
from bot.services.device_slots import get_included_device_slots, get_max_device_slots
from bot.services.balance_service import async_credit_user_balance, credit_user_balance, debit_user_balance, get_daily_charge_rub, get_user_balance
from bot.services.balance_mode_service import disable_balance_mode, enable_balance_mode
from bot.services.money import kopecks_to_rub_float, rub_to_kopecks
from bot.services.notifications import notify_admins_issue, notify_admins_payment, notify_expiring
from bot.services.payment_logger import plog
from bot.services.payment_service import credit_referral, log_referral_payment
from bot.services.purchase_intent import decode_intent
from bot.services.provisioning_issues import AccessProvisionError
from bot.services.subscription_service import create_mtproto_subscription, create_or_extend_paid_access
from bot.services.vhq_routing import is_vhq_tariff
from bot.services.vhq_subscription_proxy import fetch_vhq_mirror_payload, resolve_vhq_mirror_token
from bot.utils.texts import MTPROTO_KEY_DELIVERED, SELECT_DEVICE_AFTER_PAYMENT

logger = logging.getLogger(__name__)
lease_manager = LeaseManager(settings.instance_id)
_INTERNAL_SECRET_HEADER = "X-Internal-Secret"

# Official YooKassa IP ranges for webhook notifications
_YOOKASSA_NETWORKS = [
    ipaddress.ip_network("185.71.76.0/27"),
    ipaddress.ip_network("185.71.77.0/27"),
    ipaddress.ip_network("77.75.153.0/25"),
    ipaddress.ip_network("77.75.154.128/25"),
    ipaddress.ip_network("77.75.156.11/32"),
    ipaddress.ip_network("77.75.156.35/32"),
]


def _is_yookassa_ip(ip_str: str) -> bool:
    """Check if the IP address belongs to YooKassa."""
    try:
        addr = ipaddress.ip_address(ip_str)
        return any(addr in net for net in _YOOKASSA_NETWORKS)
    except ValueError:
        return False


def _verify_internal_secret(request: web.Request) -> bool:
    expected = settings.webstore_bridge_secret.strip()
    actual = request.headers.get(_INTERNAL_SECRET_HEADER, "").strip()
    return bool(expected and actual and hmac.compare_digest(expected, actual))


def _build_delivery_issue_text(issue: AccessProvisionError) -> str:
    text = issue.client_message
    if settings.support_username:
        text += f"\nЕсли вопрос срочный, напишите в поддержку {settings.support_username}."
    return text


async def _notify_delivery_issue(
    bot,
    *,
    telegram_id: int,
    full_name: str,
    username: str | None,
    tariff_label: str,
    platform: str,
    payment_source: str,
    issue: AccessProvisionError,
) -> None:
    uname = f"@{username}" if username else "—"
    await notify_admins_issue(
        bot,
        title="Ошибка выдачи доступа",
        details=[
            f"Пользователь: {full_name} ({telegram_id}, {uname})",
            f"Тариф: {tariff_label}",
            f"Платформа: {platform}",
            f"Источник оплаты: {payment_source}",
            f"Провайдер: {issue.provider}",
            f"Код: {issue.code}",
            f"HTTP статус: {issue.status or '—'}",
            f"Причина: {issue.admin_message}",
        ],
    )


async def _notify_unexpected_payment_failure(
    bot,
    *,
    chat_id: int,
    provider: str,
    payment_ref: str,
    exc: Exception,
) -> None:
    """Ensure both the customer and every staff role hear about a webhook failure."""
    if provider == "YooKassa":
        from bot.services.yookassa_reconciliation import queue_customer_payment_ack_for_provider

        queued = await queue_customer_payment_ack_for_provider(
            bot,
            provider_payment_id=payment_ref,
            recipient_telegram_id=chat_id,
        )
        if not queued:
            # There is no local payment row to attach an outbox record to yet.
            # Keep the fallback customer-safe; the durable path is used whenever
            # the verified payment has reached the local database.
            try:
                await notify_expiring(
                    bot,
                    chat_id,
                    "✅ Оплата получена. Ваш VPN-доступ готовится. "
                    "Мы автоматически отправим его сюда после активации",
                )
            except Exception as delivery_exc:
                logger.warning(
                    "Fallback YooKassa acknowledgement failed for %s: %s",
                    payment_ref,
                    delivery_exc,
                )
    else:
        support = f" {settings.support_username}" if settings.support_username else ""
        await notify_expiring(
            bot,
            chat_id,
            "⚠️ Оплата получена, но выдача доступа задержалась. "
            f"Мы повторим автоматически. Если доступ нужен срочно, напишите в поддержку{support}.",
        )
    await notify_admins_issue(
        bot,
        title=f"Неожиданная ошибка выдачи после оплаты ({provider})",
        details=[
            f"Платёж: {payment_ref}",
            f"Чат: {chat_id}",
            f"Ошибка: {type(exc).__name__}: {exc}",
        ],
    )


async def _resolve_web_referrer(session, raw_ref: str) -> tuple[User | None, str | None]:
    raw = (raw_ref or "").strip()
    if not raw:
        return None, None

    try:
        telegram_id = int(raw)
    except ValueError:
        result = await session.execute(
            select(User).where(User.referral_code == raw.upper())
        )
        user = result.scalar_one_or_none()
        return user, (user.referral_code or raw.upper()) if user else (None, None)

    result = await session.execute(
        select(User).where(User.telegram_id == telegram_id)
    )
    user = result.scalar_one_or_none()
    return user, str(telegram_id) if user else (None, None)


async def _resolve_web_partner_link(
    session,
    raw_ref: str,
) -> tuple[Partner | None, PartnerLink | None, str | None]:
    raw = (raw_ref or "").strip()
    if not raw.lower().startswith("p_"):
        return None, None, None

    code = raw[2:].strip().lower()
    if not code:
        return None, None, None

    result = await session.execute(
        select(PartnerLink).where(PartnerLink.code == code)
    )
    link = result.scalar_one_or_none()
    if not link or not link.is_active:
        return None, None, None

    partner = await session.get(Partner, link.partner_id)
    if not partner or not partner.is_active:
        return None, None, None
    if partner.valid_until and partner.valid_until < datetime.utcnow():
        return None, None, None

    return partner, link, f"p_{link.code}"


async def _resolve_web_tracking_target(
    session,
    raw_ref: str,
) -> tuple[str | None, User | None, Partner | None, PartnerLink | None, str | None]:
    partner, link, normalized_partner_ref = await _resolve_web_partner_link(session, raw_ref)
    if partner:
        return "partner", None, partner, link, normalized_partner_ref

    referrer, normalized_ref = await _resolve_web_referrer(session, raw_ref)
    if referrer:
        return "referral", referrer, None, None, normalized_ref

    return None, None, None, None, None


def _platform_from_str(platform_str: str) -> Platform:
    platform_str, _, _ = decode_intent(platform_str)
    return {
        "android": Platform.ANDROID,
        "ios": Platform.IOS,
        "mac": Platform.MAC,
        "windows": Platform.WINDOWS,
        "android_tv": Platform.ANDROID_TV,
        "tgproxy": Platform.ANDROID,
    }.get(platform_str, Platform.ANDROID)


def _is_deferred_platform(platform_str: str) -> bool:
    platform_str, _, _ = decode_intent(platform_str)
    return platform_str in {"deferred", "after_payment", "pending"}


def _purchase_access_kwargs(platform_str: str) -> dict:
    _, action, target_subscription_id = decode_intent(platform_str)
    if action == "auto":
        return {}
    return {"purchase_action": action, "target_subscription_id": target_subscription_id}


def _delivery_platform_kb(subscription_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🤖 Android", callback_data=f"deliver_{subscription_id}_android"),
            InlineKeyboardButton(text="🍎 iOS", callback_data=f"deliver_{subscription_id}_ios"),
        ],
        [
            InlineKeyboardButton(text="💻 Windows", callback_data=f"deliver_{subscription_id}_windows"),
            InlineKeyboardButton(text="🍏 Mac", callback_data=f"deliver_{subscription_id}_mac"),
        ],
        [InlineKeyboardButton(text="📺 Android TV", callback_data=f"deliver_{subscription_id}_android_tv")],
    ])


async def _wait_for_completed_payment(provider_payment_id: str, timeout_seconds: int) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        async with async_session() as session:
            result = await session.execute(
                select(Payment.id).where(
                    Payment.provider_payment_id == provider_payment_id,
                    Payment.status == PaymentStatus.COMPLETED,
                )
            )
            if result.scalar_one_or_none():
                return True
        await asyncio.sleep(0.5)
    return False


async def _wait_for_completed_robokassa(inv_id: int, timeout_seconds: int) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while asyncio.get_running_loop().time() < deadline:
        async with async_session() as session:
            robokassa_payment = await session.get(RobokassaPayment, inv_id)
            if robokassa_payment and robokassa_payment.status == "completed":
                return True
        await asyncio.sleep(0.5)
    return False


async def _complete_balance_topup(
    *,
    telegram_user_id: int,
    amount_rub: float,
    provider: str,
    provider_payment_id: str,
) -> tuple[int | None, float]:
    amount_kopecks = rub_to_kopecks(amount_rub)
    amount_rub = kopecks_to_rub_float(amount_kopecks)
    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_user_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            return None, 0.0

        existing_tx = await session.execute(
            select(Payment).where(Payment.provider_payment_id == provider_payment_id)
        )
        payment = existing_tx.scalar_one_or_none()
        if payment and payment.status == PaymentStatus.COMPLETED:
            return user.id, round(float(user.balance_rub or 0.0), 2)

        if not payment:
            payment = Payment(
                user_id=user.id,
                subscription_id=None,
                amount=amount_kopecks,
                currency="RUB",
                method={
                    "telegram": PaymentMethod.TELEGRAM,
                    "yookassa": PaymentMethod.YOOKASSA,
                    "robokassa": PaymentMethod.ROBOKASSA,
                }.get(provider, PaymentMethod.YOOKASSA),
                status=PaymentStatus.COMPLETED,
                provider_payment_id=provider_payment_id,
                purchase_kopecks=amount_kopecks,
                discount_kopecks=0,
            )
            session.add(payment)
        else:
            payment.status = PaymentStatus.COMPLETED

        result = await session.execute(
            select(BalanceTopUp).where(BalanceTopUp.provider_payment_id == provider_payment_id)
        )
        topup = result.scalar_one_or_none()
        if not topup:
            topup = BalanceTopUp(
                user_id=user.id,
                telegram_id=telegram_user_id,
                amount_rub=amount_rub,
                amount_kopecks=amount_kopecks,
                provider=provider,
                status="completed",
                provider_payment_id=provider_payment_id,
                completed_at=datetime.utcnow(),
            )
            session.add(topup)
            await async_credit_user_balance(
                session,
                user,
                amount_rub,
                BalanceTransactionKind.TOPUP,
                "Пополнение баланса",
                source_type=f"{provider}_topup",
                source_id=provider_payment_id,
            )
        elif topup.status != "completed":
            topup.status = "completed"
            topup.completed_at = datetime.utcnow()
            await async_credit_user_balance(
                session,
                user,
                amount_rub,
                BalanceTransactionKind.TOPUP,
                "Пополнение баланса",
                source_type=f"{provider}_topup",
                source_id=provider_payment_id,
            )

        await session.commit()
        return user.id, round(float(user.balance_rub or 0.0), 2)


async def _ensure_paid_webhook_payment(
    *,
    user_id: int,
    provider_payment_id: str,
    amount_rub: float,
    method: PaymentMethod,
    tariff_id: int,
    platform: str,
) -> int:
    """Persist the paid operation before calling a non-idempotent VPN API."""
    amount_kopecks = rub_to_kopecks(amount_rub)
    amount_rub = kopecks_to_rub_float(amount_kopecks)
    async with async_session() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == provider_payment_id)
        )
        if not payment:
            payment = Payment(
                user_id=user_id,
                subscription_id=None,
                amount=amount_kopecks,
                currency="RUB",
                method=method,
                status=PaymentStatus.PENDING,
                provider_payment_id=provider_payment_id,
                tariff_id=tariff_id,
                platform=platform,
                purchase_kopecks=amount_kopecks,
                discount_kopecks=0,
                provider_status="succeeded",
                provider_paid=True,
                provider_amount_kopecks=amount_kopecks,
                provider_currency="RUB",
                provider_verified_at=datetime.utcnow(),
            )
            session.add(payment)
        else:
            payment.status = PaymentStatus.PENDING
            payment.tariff_id = tariff_id
            payment.platform = platform
            payment.provider_status = "succeeded"
            payment.provider_paid = True
            payment.provider_amount_kopecks = amount_kopecks
            payment.provider_currency = "RUB"
            payment.provider_verified_at = datetime.utcnow()
        await session.commit()
        await session.refresh(payment)
        return payment.id


# ── Shared key-delivery helper ────────────────────────


async def send_subscription_delivery_messages(
    bot: Bot,
    chat_id: int,
    telegram_user_id: int,
    subscription_id: int,
    delivery_platform: Any,
    vpn_key_str: str | None,
    proxy_link: str | None,
    expires_str: str,
    needs_platform_choice: bool,
    payment_id: int | None = None,
    resend_primary: bool = True,
    resend_secondary: bool = True,
    record_db_error: bool = True,
) -> tuple[bool, str | None]:
    """Deliver subscription access messages (VPN key, platform selector, MTProto link, WhatsApp bonus).

    Returns (success, error_message).
    """
    delivery_err: str | None = None
    key_delivered = False
    if resend_primary and vpn_key_str and subscription_id:
        try:
            from bot.handlers.payment import _send_subscription_key_for_platform
            delivered = await _send_subscription_key_for_platform(
                bot=bot,
                chat_id=chat_id,
                telegram_id=telegram_user_id,
                subscription_id=subscription_id,
                platform=delivery_platform,
            )
            if not delivered:
                delivery_err = "key_send_failed: handler returned false"
                logger.error("Webhook could not deliver subscription ID=%s", subscription_id)
            else:
                key_delivered = True
        except Exception as exc:
            delivery_err = f"key_send_failed: {exc}"
            logger.error(f"Failed to send key to chat {chat_id}: {exc}")

    # The picker is only an auxiliary instruction. It must never replace the
    # directly usable key, and a picker failure must not cause a key resend.
    if key_delivered and needs_platform_choice:
        try:
            await bot.send_message(
                chat_id,
                SELECT_DEVICE_AFTER_PAYMENT,
                parse_mode="HTML",
                reply_markup=_delivery_platform_kb(subscription_id),
            )
        except Exception as exc:
            logger.warning("Failed to send auxiliary platform picker to chat %s: %s", chat_id, exc)

    if resend_secondary and proxy_link:
        try:
            await bot.send_message(
                chat_id,
                MTPROTO_KEY_DELIVERED.format(proxy_links=proxy_link, expires=expires_str),
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception as exc:
            if not delivery_err:
                delivery_err = f"proxy_link_failed: {exc}"
            logger.error(f"Failed to send MTProto link to chat {chat_id}: {exc}")

    if resend_secondary:
        try:
            async with async_session() as wa_session:
                wa_enabled_row = await wa_session.get(BotSettings, "whatsapp_proxy_enabled")
                wa_host_row = await wa_session.get(BotSettings, "whatsapp_proxy_host")
            if wa_enabled_row and wa_enabled_row.value == "1" and wa_host_row and wa_host_row.value:
                from bot.utils.texts import WHATSAPP_PROXY_BONUS
                await bot.send_message(
                    chat_id,
                    WHATSAPP_PROXY_BONUS.format(proxy_host=wa_host_row.value),
                    parse_mode="HTML",
                )
        except Exception as exc:
            logger.warning(f"Failed to send WhatsApp bonus to {chat_id}: {exc}")

    if payment_id and record_db_error:
        try:
            async with async_session() as session:
                p = await session.get(Payment, payment_id)
                if p:
                    cur_err = p.telegram_delivery_error or ""
                    # Do not overwrite if delivery is in progress or in retry state
                    if not cur_err.startswith("delivery_in_progress") and not cur_err.startswith("retry_failed"):
                        if delivery_err:
                            p.telegram_delivery_error = str(delivery_err)[:255]
                        else:
                            p.telegram_delivery_error = None
                        await session.commit()
        except Exception as p_exc:
            logger.error("Failed to persist telegram_delivery_error for payment %s: %s", payment_id, p_exc)

    return (delivery_err is None, delivery_err)


async def _process_and_deliver(
    app: web.Application | Bot | Any,
    telegram_user_id: int,
    chat_id: int,
    tariff_id: int,
    platform_str: str,
    provisioning_payment_id: int | None = None,
    paid_amount_rub: float | None = None,
    notify_admin: bool = True,
    deliver_customer_messages: bool = True,
) -> tuple[int | None, int | None]:
    """Create or renew the paid subscription and deliver its stable link.

    Returns (user_db_id, subscription_id) or (None, None) on failure.
    """
    if hasattr(app, "__getitem__") and "bot" in app:
        bot = app["bot"]
    else:
        bot = app
    platform = _platform_from_str(platform_str)
    logger.info(
        "Webhook delivery started: telegram_user_id=%s chat_id=%s tariff_id=%s platform=%s",
        telegram_user_id,
        chat_id,
        tariff_id,
        platform.value,
    )

    async with async_session() as session:
        tariff = await session.get(Tariff, tariff_id)
        if not tariff:
            logger.error(f"Tariff not found: {tariff_id}")
            return None, None

        result = await session.execute(
            select(User).where(User.telegram_id == telegram_user_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            logger.error(f"User not found: telegram_id={telegram_user_id}")
            return None, None

        provisioning_payment = (
            await session.get(Payment, provisioning_payment_id)
            if provisioning_payment_id is not None
            else None
        )

        is_tg_proxy_only = tariff.tariff_type == TariffType.TG_PROXY
        is_both = tariff.tariff_type == TariffType.BOTH

        saved_platform = user.platform if _is_deferred_platform(platform_str) and user.platform and not is_tg_proxy_only else None
        _, plat_action, target_sub_id = decode_intent(platform_str)
        needs_platform_choice = (
            _is_deferred_platform(platform_str)
            and plat_action not in {"renew", "upgrade"}
            and not is_tg_proxy_only
        )
        if plat_action in {"renew", "upgrade"} and _is_deferred_platform(platform_str):
            target_sub = await session.get(Subscription, target_sub_id) if target_sub_id else None
            sub_plat = target_sub.platform if (target_sub and target_sub.platform) else None
            delivery_platform = sub_plat or saved_platform or platform
        else:
            delivery_platform = saved_platform or platform

        if user.platform is None and not is_tg_proxy_only and not _is_deferred_platform(platform_str):
            user.platform = platform

        subscription = None
        vpn_key = None
        proxy_link = None

        # VPN part
        if not is_tg_proxy_only:
            bonus = 0 if is_vhq_tariff(tariff) else (user.bonus_days or 0)
            if bonus > 0:
                user.bonus_days = 0
                logger.info(f"Applying {bonus} bonus days for user {telegram_user_id}")
            try:
                subscription, vpn_key = await create_or_extend_paid_access(
                    session,
                    user=user,
                    tariff=tariff,
                    platform=delivery_platform,
                    bonus_days=bonus,
                    provisioning_payment=provisioning_payment,
                    **_purchase_access_kwargs(platform_str),
                )
            except AccessProvisionError as issue:
                if not deliver_customer_messages:
                    raise
                if issue.provider == "vhq" and issue.code == "supplier_fulfillment_pending":
                    logger.info("VHQ renewal pending background completion for user=%s", telegram_user_id)
                    try:
                        await bot.send_message(
                            chat_id,
                            f"⏳ {issue.client_message}",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                    return user.id, None
                plog(
                    "ОШИБКА_ВЫДАЧИ",
                    provider=issue.provider.upper(),
                    user_id=telegram_user_id,
                    tariff=tariff.label,
                    method="Webhook",
                    code=issue.code,
                    status=issue.status or "",
                )
                await _notify_delivery_issue(
                    bot,
                    telegram_id=telegram_user_id,
                    full_name=user.full_name or "",
                    username=user.username,
                    tariff_label=tariff.label,
                    platform=delivery_platform.value if hasattr(delivery_platform, "value") else str(delivery_platform),
                    payment_source="YooKassa / Robokassa",
                    issue=issue,
                )
                await notify_expiring(
                    bot,
                    chat_id,
                    f"❌ {_build_delivery_issue_text(issue)}",
                )
                return user.id, None
            if not subscription or not vpn_key:
                logger.error(
                    "Webhook VPN delivery failed: telegram_user_id=%s tariff_id=%s",
                    telegram_user_id,
                    tariff.id,
                )
                try:
                    await bot.send_message(
                        chat_id,
                        "❌ Оплата прошла, но выдача доступа задержалась. Мы уже получили уведомление и проверяем проблему."
                    )
                except Exception:
                    pass
                return user.id, None

        # MTProto part
        if is_tg_proxy_only or is_both:
            if is_tg_proxy_only:
                from datetime import datetime, timedelta
                from bot.services.subscription_service import get_primary_active_server
                server = await get_primary_active_server(session)
                if not server:
                    return None, None
                now = datetime.utcnow()
                expires_at = now + timedelta(days=tariff.days)
                client_name = f"mtproto_tg{user.telegram_id}"
                included_slots = await get_included_device_slots(session)
                subscription = Subscription(
                    user_id=user.id,
                    server_id=server.id,
                    tariff_months=tariff.days // 30,
                    tariff_days=tariff.days,
                    vpn_key=None,
                    client_name=client_name,
                    platform=Platform.ANDROID,
                    device_slots=included_slots,
                    expires_at=expires_at,
                )
                session.add(subscription)
                await session.flush()
                logger.info(
                    "Webhook created TG-only lightweight subscription: user_id=%s subscription_id=%s server_id=%s expires_at=%s",
                    user.id,
                    subscription.id,
                    server.id,
                    expires_at.isoformat(),
                )

            mtproto_account, proxy_link = await create_mtproto_subscription(
                session, user=user, tariff=tariff, subscription=subscription,
            )
            if not mtproto_account and is_tg_proxy_only:
                logger.error(
                    "Webhook MTProto delivery failed: telegram_user_id=%s tariff_id=%s",
                    telegram_user_id,
                    tariff.id,
                )

        subscription_id = subscription.id if subscription else None
        user_db_id = user.id
        if provisioning_payment is not None and subscription_id is not None:
            provisioning_payment.subscription_id = subscription_id
            if provisioning_payment.method == PaymentMethod.YOOKASSA:
                provisioning_payment.provisioning_failure_code = "financial_finalization_pending"
            else:
                provisioning_payment.provisioning_failure_code = None
        expires_str = subscription.expires_at.strftime("%d.%m.%Y") if subscription else "N/A"
        vpn_key_str = str(vpn_key) if vpn_key else None
        user_full_name = user.full_name or ""
        user_username = user.username
        tariff_label = tariff.label
        tariff_price_rub = float(tariff.price_rub)
        actual_paid_amount_rub = (
            kopecks_to_rub_float(rub_to_kopecks(paid_amount_rub))
            if paid_amount_rub is not None
            else (
                kopecks_to_rub_float(provisioning_payment.amount)
                if provisioning_payment and provisioning_payment.currency == "RUB"
                else tariff_price_rub
            )
        )
        delivery_platform_value = delivery_platform.value if hasattr(delivery_platform, "value") else str(delivery_platform)

        await session.commit()
        logger.info(
            "Webhook delivery DB commit complete: telegram_user_id=%s user_db_id=%s subscription_id=%s vpn=%s mtproto=%s",
            telegram_user_id,
            user_db_id,
            subscription_id,
            bool(vpn_key_str),
            bool(proxy_link),
        )

    if deliver_customer_messages:
        delivery_ok, delivery_err = await send_subscription_delivery_messages(
            bot=bot,
            chat_id=chat_id,
            telegram_user_id=telegram_user_id,
            subscription_id=subscription_id,
            delivery_platform=delivery_platform,
            vpn_key_str=vpn_key_str,
            proxy_link=proxy_link,
            expires_str=expires_str,
            needs_platform_choice=needs_platform_choice,
            payment_id=provisioning_payment_id,
        )
        if not delivery_ok:
            logger.warning(
                "Subscription %s provisioned but Telegram delivery failed to user %s: %s",
                subscription_id,
                telegram_user_id,
                delivery_err,
            )

    if notify_admin:
        from bot.services.notifications import notify_admins_payment
        await notify_admins_payment(
            bot,
            telegram_id=telegram_user_id,
            full_name=user_full_name,
            username=user_username,
            amount_rub=actual_paid_amount_rub,
            tariff_label=tariff_label,
            method="💳 YooKassa / Robokassa",
            platform=delivery_platform_value if not is_tg_proxy_only else "telegram",
            price_rub=tariff_price_rub,
        )

    logger.info(
        f"Key delivered via webhook: telegram_id={telegram_user_id}, subscription_id={subscription_id}, "
        f"platform={delivery_platform_value}, vpn={bool(vpn_key_str)}, mtproto={bool(proxy_link)}"
    )
    return user_db_id, subscription_id


async def _process_device_deliver(
    app: web.Application | Bot | Any,
    telegram_user_id: int,
    chat_id: int,
    sub_id: int,
    payment_id: int | None = None,
    provider_payment_id: str | None = None,
) -> tuple[int | None, int | None]:
    """Add an extra device slot to an existing subscription without creating a new key.

    Returns (user_db_id, subscription_id) or (None, None) on failure.
    """
    if hasattr(app, "__getitem__") and "bot" in app:
        bot = app["bot"]
    else:
        bot = app

    async with async_session() as session:
        # Check idempotency via durable payment status
        payment = None
        if payment_id is not None:
            payment = await session.get(Payment, payment_id)
        elif provider_payment_id:
            payment = await session.scalar(
                select(Payment).where(Payment.provider_payment_id == provider_payment_id)
            )

        if not payment:
            logger.error(
                "Device delivery failed: durable Payment record missing (payment_id=%s, provider_id=%s)",
                payment_id,
                provider_payment_id,
            )
            return None, None

        sub = await session.get(Subscription, sub_id)
        owner = await session.get(User, sub.user_id) if sub else None
        if not sub or not owner or owner.telegram_id != telegram_user_id:
            logger.error(f"Subscription {sub_id} not found or user mismatch.")
            return None, None

        if payment.user_id != owner.id:
            logger.error(
                "Device delivery failed: payment owner %s does not match subscription owner %s",
                payment.user_id,
                owner.id,
            )
            return None, None

        if payment.subscription_id and payment.subscription_id != sub.id:
            logger.error(
                "Device delivery failed: payment target sub %s does not match requested sub %s",
                payment.subscription_id,
                sub.id,
            )
            return None, None

        if payment.provider_payment_id is None and provider_payment_id:
            payment.provider_payment_id = provider_payment_id

        if payment.status == PaymentStatus.COMPLETED:
            logger.info("Device slot already delivered for payment %s / %s", payment.id, provider_payment_id)
            return sub.user_id, sub.id

        max_slots = await get_max_device_slots(session)
        if max_slots is not None and sub.device_slots >= max_slots:
            try:
                await bot.send_message(
                    chat_id,
                    "❌ Лимит устройств для этой подписки уже достигнут."
                )
            except Exception:
                pass
            return None, None

        if not sub.vpn_key:
            try:
                await bot.send_message(
                    chat_id,
                    "❌ Не найден основной ключ подписки. Обратитесь в поддержку."
                )
            except Exception:
                pass
            return None, None

        # Extra devices use the same subscription link. The slot counter is
        # accounting/UI state; Marzban keeps the same user/key.
        sub.device_slots += 1
        new_slot = sub.device_slots

        payment.status = PaymentStatus.COMPLETED
        payment.subscription_id = sub.id
        payment.telegram_chat_id = payment.telegram_chat_id or int(chat_id)

        from bot.services.payment_notification_outbox import enqueue_subscription_delivery

        delivery_platform = sub.platform.value if hasattr(sub.platform, "value") else str(sub.platform)
        await enqueue_subscription_delivery(
            session,
            payment_id=payment.id,
            recipient_telegram_id=int(chat_id),
            telegram_user_id=int(telegram_user_id),
            subscription_id=sub.id,
            delivery_platform=delivery_platform,
            needs_platform_choice=False,
        )

        user_db_id = sub.user_id
        await session.commit()

    from bot.services.payment_notification_outbox import process_payment_notification_outbox

    delivery_stats = await process_payment_notification_outbox(
        bot,
        payment_id=payment.id,
        limit=10,
        session_factory=async_session,
    )
    if delivery_stats.get("sent", 0) < 1:
        logger.warning(
            "Device key delivery remains in durable outbox for payment %s: %s",
            payment.id,
            delivery_stats,
        )
        return None, None

    logger.info(
        f"Device key delivered via webhook: telegram_id={telegram_user_id}, slot={new_slot}"
    )
    return user_db_id, sub_id


# ── YooKassa webhook ──────────────────────────────────


async def handle_yookassa(request: web.Request) -> web.Response:
    """Handle YooKassa payment.succeeded notification."""
    remote_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or request.remote
    if not _is_yookassa_ip(remote_ip):
        logger.info("YooKassa webhook from non-standard IP %s (proxy chain expected)", remote_ip)

    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Bad JSON")

    event = data.get("event", "")
    if event != "payment.succeeded":
        return web.Response(text="OK")

    obj = data.get("object", {})
    yookassa_payment_id = obj.get("id")
    if not yookassa_payment_id:
        return web.Response(status=400, text="Missing payment id")

    from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment

    try:
        success, code = await verify_and_fulfill_yookassa_payment(
            request.app,
            yookassa_payment_id,
            source="webhook",
            unverified_webhook_payload=data,
        )
    except Exception as exc:
        logger.exception("Unexpected YooKassa fulfillment failure: %s", yookassa_payment_id)
        chat_id = None
        try:
            chat_id = int(obj.get("metadata", {}).get("chat_id", 0)) or None
        except (ValueError, TypeError):
            pass
        if chat_id:
            await _notify_unexpected_payment_failure(
                request.app["bot"],
                chat_id=chat_id,
                provider="YooKassa",
                payment_ref=yookassa_payment_id,
                exc=exc,
            )
        return web.Response(status=503, text="Fulfillment will be retried")

    if success:
        return web.Response(text="OK")

    if code in ("already_completed", "payment_canceled") or code.startswith("payment_canceled"):
        return web.Response(text="OK")
    if code == "locked_by_another_execution":
        return web.Response(status=409, text="Processing in progress")
    if code in ("provider_unavailable", "lease_expired") or code.startswith("delivery_failed"):
        return web.Response(status=503, text="Fulfillment will be retried")

    return web.Response(text="OK")


# ── Robokassa webhooks ────────────────────────────────


async def handle_robokassa_result(request: web.Request) -> web.Response:
    """Handle Robokassa Result URL (server-to-server notification)."""
    if request.method == "POST":
        raw = await request.post()
    else:
        raw = request.rel_url.query

    # Normalize keys to lowercase - Robokassa may send OutSum or outsum
    data = {k.lower(): v for k, v in raw.items()}

    try:
        out_sum = data["outsum"]
        inv_id = int(data["invid"])
        signature = data["signaturevalue"]
    except KeyError as exc:
        logger.error(f"Missing Robokassa param: {exc}")
        return web.Response(status=400, text="Missing params")

    expected = hashlib.md5(
        f"{out_sum}:{inv_id}:{settings.robokassa_password_2}".encode()
    ).hexdigest().upper()

    if str(signature).upper() != expected:
        logger.error(
            f"Robokassa signature mismatch: got={str(signature).upper()}, "
            f"expected={expected}, InvId={inv_id}"
        )
        return web.Response(status=403, text="Invalid signature")

    async with async_session() as session:
        robokassa_payment = await session.get(RobokassaPayment, inv_id)
        balance_topup = await session.get(BalanceTopUp, inv_id)

        if not robokassa_payment and not balance_topup:
            logger.error(f"RobokassaPayment not found: InvId={inv_id}")
            return web.Response(text=f"OK{inv_id}")

        if robokassa_payment and robokassa_payment.status == "completed":
            logger.info(f"Duplicate Robokassa webhook: InvId={inv_id}")
            return web.Response(text=f"OK{inv_id}")
        if balance_topup and balance_topup.status == "completed":
            logger.info(f"Duplicate Robokassa balance topup: InvId={inv_id}")
            return web.Response(text=f"OK{inv_id}")

        if balance_topup and not robokassa_payment:
            telegram_user_id = int(balance_topup.telegram_id or 0)
            chat_id = int(balance_topup.telegram_id or 0)
            tariff_id = None
            platform_str = None
            user_db_id_for_payment = balance_topup.user_id
            is_balance_topup = True
        else:
            user = await session.get(User, robokassa_payment.user_id)
            if not user:
                logger.error(f"User not found for RobokassaPayment {inv_id}")
                return web.Response(text=f"OK{inv_id}")

            telegram_user_id = user.telegram_id
            chat_id = robokassa_payment.telegram_chat_id
            tariff_id = robokassa_payment.tariff_id
            platform_str = robokassa_payment.platform
            user_db_id_for_payment = robokassa_payment.user_id
            is_balance_topup = False

    lock_name = f"payment:robokassa:{inv_id}"
    acquired = await lease_manager.acquire_or_renew(
        lock_name,
        settings.webhook_lock_ttl_seconds,
    )
    if not acquired:
        if await _wait_for_completed_robokassa(inv_id, settings.webhook_lock_wait_seconds):
            logger.info("Robokassa webhook already processed by another instance: InvId=%s", inv_id)
            return web.Response(text=f"OK{inv_id}")
        return web.Response(status=409, text="Processing in progress")

    try:
        async with async_session() as session:
            result = await session.execute(
                select(Payment).where(
                    Payment.provider_payment_id == str(inv_id),
                    Payment.status == PaymentStatus.COMPLETED,
                )
            )
            if result.scalar_one_or_none():
                logger.info("Duplicate Robokassa payment record: InvId=%s", inv_id)
                robokassa_payment = await session.get(RobokassaPayment, inv_id)
                if robokassa_payment:
                    robokassa_payment.status = "completed"
                    await session.commit()
                return web.Response(text=f"OK{inv_id}")

        if is_balance_topup:
            user_db_id, new_balance = await _complete_balance_topup(
                telegram_user_id=telegram_user_id,
                amount_rub=float(out_sum),
                provider="robokassa",
                provider_payment_id=str(inv_id),
            )
            subscription_id = None
            if user_db_id is not None:
                async with async_session() as session:
                    user_rec = await session.get(User, user_db_id)
                try:
                    await request.app["bot"].send_message(
                        chat_id,
                        f"✅ <b>Баланс пополнен</b>\n\n"
                        f"Зачислено: <b>{float(out_sum):.2f} ₽</b>\n"
                        f"Сейчас на балансе: <b>{new_balance:.2f} ₽</b>",
                        parse_mode="HTML",
                        reply_markup=back_to_menu_kb(),
                    )
                except Exception:
                    pass
                if user_rec:
                    await notify_admins_payment(
                        request.app["bot"],
                        telegram_id=telegram_user_id,
                        full_name=user_rec.full_name or "",
                        username=user_rec.username,
                        amount_rub=float(out_sum),
                        tariff_label="Пополнение баланса",
                        method="💳 Robokassa",
                        platform="Баланс",
                    )
        else:
            provisioning_payment_id = await _ensure_paid_webhook_payment(
                user_id=user_db_id_for_payment,
                provider_payment_id=str(inv_id),
                amount_rub=float(out_sum),
                method=PaymentMethod.ROBOKASSA,
                tariff_id=tariff_id,
                platform=platform_str,
            )
            user_db_id, subscription_id = await _process_and_deliver(
                request.app,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id,
                tariff_id=tariff_id,
                platform_str=platform_str,
                provisioning_payment_id=provisioning_payment_id,
                paid_amount_rub=float(out_sum),
            )

        if user_db_id is not None and subscription_id is not None and not is_balance_topup:
            async with async_session() as session:
                payment = await session.scalar(
                    select(Payment).where(Payment.provider_payment_id == str(inv_id))
                )
                if not payment:
                    logger.error("Robokassa payment record disappeared: InvId=%s", inv_id)
                    return web.Response(status=503, text="Payment record missing")
                payment.status = PaymentStatus.COMPLETED
                payment.subscription_id = subscription_id
                payment.provisioning_failure_code = None

                robokassa_payment = await session.get(RobokassaPayment, inv_id)
                discount = 0.0
                if robokassa_payment:
                    robokassa_payment.status = "completed"
                    discount = robokassa_payment.discount_applied

                if discount > 0:
                    user_rec = await session.get(User, user_db_id_for_payment)
                    if user_rec:
                        actual_discount = min(discount, max(0.0, get_user_balance(user_rec)))
                        if actual_discount > 0:
                            debit_user_balance(
                                session,
                                user_rec,
                                actual_discount,
                                BalanceTransactionKind.TOPUP,
                                "Списание с баланса при оплате тарифа",
                                source_type="payment_discount",
                                source_id=str(inv_id),
                            )

                await session.flush()
                await credit_referral(session, user_db_id_for_payment, payment.id, float(out_sum))
                await log_referral_payment(session, user_db_id_for_payment, float(out_sum), bot=request.app["bot"])
                plog("ОПЛАТА", provider="Robokassa", user_id=telegram_user_id,
                     amount=f"{float(out_sum):.2f}", inv_id=inv_id)
                from bot.services.payment_service import credit_cashback, credit_partner
                await credit_partner(session, user_db_id_for_payment, payment.id, float(out_sum), bot=request.app["bot"])
                await credit_cashback(session, user_db_id_for_payment, payment.id, float(out_sum), bot=request.app["bot"])
                await session.commit()
    except Exception as exc:
        logger.exception("Unexpected Robokassa fulfillment failure: InvId=%s", inv_id)
        await _notify_unexpected_payment_failure(
            request.app["bot"],
            chat_id=chat_id,
            provider="Robokassa",
            payment_ref=str(inv_id),
            exc=exc,
        )
        return web.Response(status=503, text="Fulfillment will be retried")
    finally:
        await lease_manager.release(lock_name)

    return web.Response(text=f"OK{inv_id}")


async def handle_robokassa_success(request: web.Request) -> web.Response:
    """Robokassa Success URL - verify signature with password_1, redirect to bot."""
    if request.method == "POST":
        raw = await request.post()
    else:
        raw = request.rel_url.query
    data = {k.lower(): v for k, v in raw.items()}

    try:
        out_sum = data["outsum"]
        inv_id = int(data["invid"])
        signature = data["signaturevalue"]
    except KeyError:
        bot = request.app["bot"]
        bot_info = await bot.get_me()
        return web.HTTPFound(f"https://t.me/{bot_info.username}")

    expected = hashlib.md5(
        f"{out_sum}:{inv_id}:{settings.robokassa_password_1}".encode()
    ).hexdigest()
    if expected.lower() != str(signature).lower():
        logger.warning(f"Robokassa success: bad signature for InvId={inv_id}")

    bot = request.app["bot"]
    bot_info = await bot.get_me()
    return web.HTTPFound(f"https://t.me/{bot_info.username}")


async def handle_robokassa_fail(request: web.Request) -> web.Response:
    """Robokassa Fail URL - redirect back to bot."""
    bot = request.app["bot"]
    bot_info = await bot.get_me()
    return web.HTTPFound(f"https://t.me/{bot_info.username}")


async def handle_internal_web_referral_resolve(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    raw_ref = request.query.get("ref", "").strip()
    if not raw_ref:
        return web.json_response({"error": "Missing ref"}, status=400)

    async with async_session() as session:
        tracking_kind, referrer, partner, _partner_link, normalized_ref = await _resolve_web_tracking_target(
            session, raw_ref
        )
        if not tracking_kind:
            return web.json_response({"status": "invalid"}, status=404)
        config = await session.get(ReferralConfig, 1)
        if tracking_kind == "referral" and (not config or not config.is_enabled):
            return web.json_response({"status": "disabled"}, status=409)

    return web.json_response({
        "status": "ok",
        "ref_code": normalized_ref,
        "tracking_kind": tracking_kind,
        "telegram_id": str(referrer.telegram_id if referrer else (partner.telegram_id or "")),
        "username": referrer.username or "" if referrer else "",
        "full_name": referrer.full_name or "" if referrer else (partner.name if partner else ""),
        "commission_percent": (
            float(config.commission_percent)
            if tracking_kind == "referral"
            else float(partner.commission_percent or 0.0)
        ),
    })


async def handle_internal_web_referral_credit(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    order_id = (body.get("order_id") or "").strip()
    raw_ref = (body.get("ref_code") or "").strip()
    buyer_contact = (body.get("buyer_contact") or "").strip() or None
    tariff_label = (body.get("tariff_label") or "").strip() or None
    try:
        amount_rub = float(body.get("amount_rub") or 0)
    except (TypeError, ValueError):
        amount_rub = 0.0

    if not order_id or not raw_ref or amount_rub <= 0:
        return web.json_response({"error": "Missing order_id, ref_code or amount_rub"}, status=400)

    async with async_session() as session:
        existing = await session.execute(
            select(WebReferralEarning).where(WebReferralEarning.web_order_id == order_id)
        )
        row = existing.scalar_one_or_none()
        if row:
            referrer = await session.get(User, row.referrer_id)
            return web.json_response({
                "status": "already_credited",
                "earning_rub": row.earning_amount_rub,
                "telegram_id": str(referrer.telegram_id) if referrer else None,
                "tracking_kind": "referral",
            })

        existing_partner = await session.execute(
            select(WebPartnerEarning).where(WebPartnerEarning.web_order_id == order_id)
        )
        partner_row = existing_partner.scalar_one_or_none()
        if partner_row:
            partner = await session.get(Partner, partner_row.partner_id)
            return web.json_response({
                "status": "already_credited",
                "earning_rub": partner_row.earning_amount_rub,
                "telegram_id": str(partner.telegram_id) if partner and partner.telegram_id else None,
                "partner_id": partner_row.partner_id,
                "tracking_kind": "partner",
            })

        tracking_kind, referrer, partner, partner_link, normalized_ref = await _resolve_web_tracking_target(
            session, raw_ref
        )
        if not tracking_kind:
            return web.json_response({"status": "invalid"}, status=404)
        config = await session.get(ReferralConfig, 1)
        if tracking_kind == "referral" and (not config or not config.is_enabled):
            return web.json_response({"status": "disabled"}, status=409)

        commission_percent = (
            float(config.commission_percent)
            if tracking_kind == "referral"
            else float(partner.commission_percent or 0.0)
        )
        earning = round(amount_rub * commission_percent / 100, 2)
        if earning <= 0:
            return web.json_response({"status": "disabled"}, status=409)

        if tracking_kind == "partner":
            session.add(
                WebPartnerEarning(
                    partner_id=partner.id,
                    partner_link_id=partner_link.id if partner_link else None,
                    web_order_id=order_id,
                    ref_code=normalized_ref,
                    buyer_contact=buyer_contact,
                    tariff_label=tariff_label,
                    payment_amount_rub=amount_rub,
                    earning_amount_rub=earning,
                    depth=1,
                    commission_percent=commission_percent,
                )
            )
            partner.partner_balance = round((partner.partner_balance or 0.0) + earning, 2)
        else:
            session.add(
                WebReferralEarning(
                    referrer_id=referrer.id,
                    web_order_id=order_id,
                    ref_code=normalized_ref,
                    buyer_contact=buyer_contact,
                    payment_amount_rub=amount_rub,
                    earning_amount_rub=earning,
                )
            )
            credit_user_balance(
                session,
                referrer,
                earning,
                BalanceTransactionKind.REFERRAL_BONUS,
                "Бонус за оплату друга через сайт",
                source_type="web_order",
                source_id=order_id,
            )
            # Credit multi-tier partner commission if referrer belongs to a partner chain
            if referrer.referral_root_partner_id:
                root_partner = await session.get(Partner, referrer.referral_root_partner_id)
                if root_partner and root_partner.is_active:
                    ref_depth = (referrer.referral_depth or 1) + 1
                    if ref_depth == 2:
                        p_pct = float(root_partner.commission_l2_percent or 0.0)
                    else:
                        p_pct = float(root_partner.commission_l3_percent or 0.0)
                    if p_pct > 0:
                        p_earn = round(amount_rub * p_pct / 100, 2)
                        if p_earn > 0:
                            session.add(
                                WebPartnerEarning(
                                    partner_id=root_partner.id,
                                    partner_link_id=None,
                                    web_order_id=f"{order_id}_p{root_partner.id}",
                                    ref_code=normalized_ref,
                                    buyer_contact=buyer_contact,
                                    tariff_label=tariff_label,
                                    payment_amount_rub=amount_rub,
                                    earning_amount_rub=p_earn,
                                    depth=ref_depth,
                                    commission_percent=p_pct,
                                )
                            )
                            root_partner.partner_balance = round((root_partner.partner_balance or 0.0) + p_earn, 2)

        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            if tracking_kind == "partner":
                existing_partner = await session.execute(
                    select(WebPartnerEarning).where(WebPartnerEarning.web_order_id == order_id)
                )
                partner_row = existing_partner.scalar_one_or_none()
                return web.json_response({
                    "status": "already_credited",
                    "earning_rub": partner_row.earning_amount_rub if partner_row else earning,
                    "telegram_id": str(partner.telegram_id) if partner and partner.telegram_id else None,
                    "partner_id": partner.id if partner else None,
                    "tracking_kind": "partner",
                })

            existing = await session.execute(
                select(WebReferralEarning).where(WebReferralEarning.web_order_id == order_id)
            )
            row = existing.scalar_one_or_none()
            return web.json_response({
                "status": "already_credited",
                "earning_rub": row.earning_amount_rub if row else earning,
                "telegram_id": str(referrer.telegram_id),
                "tracking_kind": "referral",
            })

    if tracking_kind == "partner":
        if partner and partner.telegram_id:
            try:
                buyer_suffix = f"\nПокупатель: {buyer_contact}" if buyer_contact else ""
                tariff_suffix = f"\nТариф: {tariff_label}" if tariff_label else ""
                await request.app["bot"].send_message(
                    partner.telegram_id,
                    (
                        f"💰 Партнёрское начисление с сайта: <b>{earning:.2f}₽</b>\n"
                        f"Заказ: <code>{order_id}</code>{tariff_suffix}{buyer_suffix}"
                    ),
                    parse_mode="HTML",
                )
            except Exception as exc:
                logger.warning("Failed to notify partner %s about web earning: %s", partner.telegram_id, exc)

        return web.json_response({
            "status": "credited",
            "earning_rub": earning,
            "telegram_id": str(partner.telegram_id) if partner and partner.telegram_id else None,
            "partner_id": partner.id if partner else None,
            "ref_code": normalized_ref,
            "tracking_kind": "partner",
        })

    try:
        buyer_suffix = f" ({buyer_contact})" if buyer_contact else ""
        tariff_suffix = f" за «{tariff_label}»" if tariff_label else ""
        await request.app["bot"].send_message(
            referrer.telegram_id,
            (
                f"💰 На ваш баланс начислено <b>{earning:.2f}₽</b> "
                f"за веб-оплату по вашей ссылке{tariff_suffix}.\n"
                f"Заказ: <code>{order_id}</code>{buyer_suffix}"
            ),
            parse_mode="HTML",
        )
    except Exception as exc:
        logger.warning("Failed to notify referrer %s about web referral: %s", referrer.telegram_id, exc)

    return web.json_response({
        "status": "credited",
        "earning_rub": earning,
        "telegram_id": str(referrer.telegram_id),
        "ref_code": normalized_ref,
        "tracking_kind": "referral",
    })


async def handle_internal_balance_profile(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    telegram_id_raw = request.query.get("telegram_id", "").strip()
    if not telegram_id_raw:
        return web.json_response({"error": "Missing telegram_id"}, status=400)

    try:
        telegram_id = int(telegram_id_raw)
    except ValueError:
        return web.json_response({"error": "Invalid telegram_id"}, status=400)

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            return web.json_response({"error": "User not found"}, status=404)
        daily_rate = await get_daily_charge_rub(session)

        enabled = bool(user.balance_mode_enabled and user.balance_autodebit_enabled)
        status_text = "Ежедневные списания включены" if enabled else "Ежедневные списания выключены"
        if user.balance_grace_until:
            status_text = "Баланс ушёл в минус. Пополните его до следующего списания."
        elif not enabled and user.next_daily_charge_at:
            status_text = "Новые списания выключены. Доступ останется до указанной даты."

        return web.json_response({
            "balance_rub": round(float(user.balance_rub or 0.0), 2),
            "balance_mode_enabled": bool(user.balance_mode_enabled),
            "balance_autodebit_enabled": bool(user.balance_autodebit_enabled),
            "next_daily_charge_at": user.next_daily_charge_at.isoformat() if user.next_daily_charge_at else None,
            "balance_grace_until": user.balance_grace_until.isoformat() if user.balance_grace_until else None,
            "daily_charge_rub": daily_rate,
            "status_text": status_text,
        })


async def handle_internal_balance_config(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    async with async_session() as session:
        daily_rate = await get_daily_charge_rub(session)
        config = await session.get(ReferralConfig, 1)
        cb_enabled_row = await session.get(BotSettings, "cashback_enabled")
        cb_pct_row = await session.get(BotSettings, "cashback_percent")
        cb_enabled = cb_enabled_row.value == "1" if cb_enabled_row else False
        try:
            cb_pct = float(cb_pct_row.value) if (cb_pct_row and cb_pct_row.value) else 10.0
        except (ValueError, TypeError):
            cb_pct = 10.0

    return web.json_response({
        "daily_charge_rub": daily_rate,
        "referral_commission_percent": float(config.commission_percent) if config else 0.0,
        "cashback_enabled": cb_enabled,
        "cashback_percent": cb_pct,
    })


async def handle_internal_balance_credit(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    try:
        telegram_id = int(body.get("telegram_id") or 0)
        amount_rub = float(body.get("amount_rub") or 0)
    except (TypeError, ValueError):
        return web.json_response({"error": "Invalid telegram_id or amount_rub"}, status=400)

    if telegram_id <= 0 or amount_rub <= 0:
        return web.json_response({"error": "Invalid telegram_id or amount_rub"}, status=400)

    source_type = (body.get("source_type") or "webstore_topup").strip()[:64]
    source_id = (body.get("source_id") or "").strip()[:128]
    description = (body.get("description") or "Пополнение баланса").strip()[:255]

    async with async_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if not user:
            return web.json_response({"error": "User not found"}, status=404)

        if source_id:
            existing = await session.execute(
                select(BalanceTopUp).where(BalanceTopUp.provider_payment_id == source_id)
            )
            topup_rows = existing.scalars().all()
            if topup_rows:
                topup = topup_rows[0]
                same_tg = (topup.telegram_id == telegram_id)
                same_amt = (abs(float(topup.amount_rub or 0.0) - amount_rub) < 0.01)
                same_provider = (topup.provider == source_type[:32])
                is_completed = (topup.status == "completed")
                if len(topup_rows) != 1 or not (same_tg and same_amt and same_provider and is_completed):
                    logger.warning(
                        "Conflicting topup parameters for source_id=%s: existing tg=%s amt=%s prov=%s stat=%s vs requested tg=%s amt=%s prov=%s",
                        source_id,
                        topup.telegram_id,
                        topup.amount_rub,
                        topup.provider,
                        topup.status,
                        telegram_id,
                        amount_rub,
                        source_type[:32],
                    )
                    return web.json_response(
                        {"error": "Conflicting topup parameters for existing source_id"},
                        status=409,
                    )
                return web.json_response({
                    "status": "already_credited",
                    "balance_rub": round(float(user.balance_rub or 0.0), 2),
                })

        try:
            topup = BalanceTopUp(
                user_id=user.id,
                telegram_id=telegram_id,
                amount_rub=amount_rub,
                provider=source_type[:32],
                status="completed",
                provider_payment_id=source_id or None,
                completed_at=datetime.utcnow(),
            )
            session.add(topup)

            new_balance = await async_credit_user_balance(
                session,
                user,
                amount_rub,
                BalanceTransactionKind.TOPUP,
                description,
                source_type=source_type,
                source_id=source_id or None,
            )
            await session.commit()
        except IntegrityError:
            await session.rollback()
            # Loser of concurrent race: re-read winner's committed record
            async with async_session() as s2:
                u2 = (
                    await s2.execute(select(User).where(User.telegram_id == telegram_id))
                ).scalar_one_or_none()
                ex2 = (
                    await s2.execute(
                        select(BalanceTopUp).where(BalanceTopUp.provider_payment_id == source_id)
                    )
                ).scalars().all()
                if ex2:
                    winner_topup = ex2[0]
                    same_tg = (winner_topup.telegram_id == telegram_id)
                    same_amt = (abs(float(winner_topup.amount_rub or 0.0) - amount_rub) < 0.01)
                    same_provider = (winner_topup.provider == source_type[:32])
                    is_completed = (winner_topup.status == "completed")
                    if len(ex2) == 1 and same_tg and same_amt and same_provider and is_completed:
                        return web.json_response({
                            "status": "already_credited",
                            "balance_rub": round(float(u2.balance_rub or 0.0), 2) if u2 else 0.0,
                        })
                logger.warning("Integrity error with conflicting existing row for source_id=%s", source_id)
                return web.json_response(
                    {"error": "Conflicting concurrent topup"},
                    status=409,
                )

        return web.json_response({
            "status": "credited",
            "balance_rub": round(new_balance, 2),
        })


async def handle_internal_balance_credit_status(request: web.Request) -> web.Response:
    """Read-only check for existing balance topup by source_id.

    Returns found status, original telegram_id, amount_rub, completed flag, and provider.
    Does NOT mutate balance or perform credit.
    Handles potential historical duplicate rows safely without scalar_one_or_none() crashes.
    """
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    source_id = (request.query.get("source_id") or "").strip()[:128]
    if not source_id:
        return web.json_response({"error": "Missing source_id"}, status=400)

    async with async_session() as session:
        existing = await session.execute(
            select(BalanceTopUp).where(BalanceTopUp.provider_payment_id == source_id)
        )
        rows = existing.scalars().all()
        if not rows:
            return web.json_response({
                "ok": True,
                "found": False,
                "completed": False,
                "ambiguous": False,
            })

        unique_tgs = {r.telegram_id for r in rows}
        unique_amounts = {round(float(r.amount_rub or 0.0), 2) for r in rows}
        unique_providers = {r.provider for r in rows}
        unique_statuses = {r.status for r in rows}

        if len(rows) != 1 or len(unique_tgs) > 1 or len(unique_amounts) > 1 or len(unique_providers) > 1 or len(unique_statuses) > 1:
            logger.warning(
                "Conflicting historical rows found for source_id=%s: tg=%s amounts=%s providers=%s statuses=%s",
                source_id,
                unique_tgs,
                unique_amounts,
                unique_providers,
                unique_statuses,
            )
            return web.json_response({
                "ok": True,
                "found": True,
                "ambiguous": True,
                "completed": False,
                "error": "Conflicting historical rows found for source_id",
            })

        topup = rows[0]
        return web.json_response({
            "ok": True,
            "found": True,
            "ambiguous": False,
            "source_id": topup.provider_payment_id,
            "telegram_id": topup.telegram_id,
            "amount_rub": round(float(topup.amount_rub or 0.0), 2),
            "status": topup.status,
            "completed": topup.status == "completed",
            "provider": topup.provider,
        })


async def handle_internal_balance_history(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    try:
        telegram_id = int((request.query.get("telegram_id") or "").strip())
    except ValueError:
        return web.json_response({"error": "Invalid telegram_id"}, status=400)

    limit_raw = (request.query.get("limit") or "20").strip()
    try:
        limit = max(1, min(50, int(limit_raw)))
    except ValueError:
        limit = 20

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            return web.json_response({"error": "User not found"}, status=404)

        tx_result = await session.execute(
            select(BalanceTransaction)
            .where(BalanceTransaction.user_id == user.id)
            .order_by(BalanceTransaction.created_at.desc(), BalanceTransaction.id.desc())
            .limit(limit)
        )
        items = tx_result.scalars().all()

    return web.json_response({
        "items": [
            {
                "direction": item.direction.value,
                "amount_rub": round(float(item.amount_rub or 0.0), 2),
                "description": item.description,
                "balance_after_rub": round(float(item.balance_after_rub or 0.0), 2),
                "created_at": item.created_at.isoformat() if item.created_at else None,
            }
            for item in items
        ]
    })


async def handle_internal_balance_toggle(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    try:
        telegram_id = int(body.get("telegram_id") or 0)
    except (TypeError, ValueError):
        return web.json_response({"error": "Invalid telegram_id"}, status=400)

    enabled = bool(body.get("enabled"))

    async with async_session() as session:
        result = await session.execute(select(User).where(User.telegram_id == telegram_id))
        user = result.scalar_one_or_none()
        if not user:
            return web.json_response({"error": "User not found"}, status=404)

        if enabled:
            ok, error = await enable_balance_mode(session, user)
            if not ok:
                return web.json_response({"error": error or "Не удалось включить режим"}, status=400)
        else:
            await disable_balance_mode(session, user)
        await session.commit()

        return web.json_response({
            "balance_mode_enabled": bool(user.balance_mode_enabled),
            "balance_autodebit_enabled": bool(user.balance_autodebit_enabled),
            "next_daily_charge_at": user.next_daily_charge_at.isoformat() if user.next_daily_charge_at else None,
            "balance_grace_until": user.balance_grace_until.isoformat() if user.balance_grace_until else None,
        })


_DELIVERED_WEB_ORDERS: set[str] = set()
_DELIVERED_WEB_ORDERS_QUEUE: deque[str] = deque(maxlen=1000)


async def handle_internal_web_order_delivered(request: web.Request) -> web.Response:
    """Handle notification and DB sync when a webstore order is delivered.

    If the renewed/purchased access belongs to a Telegram bot user,
    syncs the bot subscription DB and sends the user the exact same
    renewal messages and platform guide as bot-native purchases.
    """
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    order_id = str(body.get("order_id") or "").strip()
    adapt_uuid = str(body.get("adapt_uuid") or "").strip()
    marzban_username = str(body.get("marzban_username") or "").strip()
    target_order_id = str(body.get("target_order_id") or "").strip()
    subscription_url = str(body.get("subscription_url") or "").strip()
    access_expires_at_raw = body.get("access_expires_at")
    tariff_label = str(body.get("tariff_label") or "").strip()
    force = bool(body.get("force"))
    amount_rub = float(body.get("amount_rub") or 0.0)
    cashback_eligible = bool(body.get("cashback_eligible", True))
    cashback_only = bool(body.get("cashback_only"))

    if not order_id:
        return web.json_response({"error": "Missing order_id"}, status=400)

    # Robust fallback extraction for adapt_uuid
    if not adapt_uuid and target_order_id and "uuid:" in target_order_id:
        adapt_uuid = target_order_id.split("uuid:", 1)[1].strip()
    if not adapt_uuid and subscription_url:
        import re
        match = re.search(r"/(?:adapt-sub|sub)/([0-9a-f-]{36})", subscription_url, re.IGNORECASE)
        if match:
            adapt_uuid = match.group(1)

    # 1. Resolve subscription in bot DB
    async with async_session() as session:
        sub = None
        adapt_record = None
        if adapt_uuid:
            adapt_record = await session.scalar(
                select(AdaptSubscription).where(
                    func.lower(AdaptSubscription.adapt_uuid) == adapt_uuid.lower()
                ).limit(1)
            )
            if adapt_record:
                sub = await session.get(Subscription, adapt_record.subscription_id)
            if not sub:
                sub = await session.scalar(
                    select(Subscription).where(
                        Subscription.client_name == f"adapt_{adapt_uuid}"
                    ).order_by(Subscription.id.desc()).limit(1)
                )
            if not sub:
                sub = await session.scalar(
                    select(Subscription).where(
                        Subscription.vpn_key.contains(adapt_uuid)
                    ).order_by(Subscription.id.desc()).limit(1)
                )

        if not sub and target_order_id and target_order_id.startswith("sub_"):
            try:
                sub_id_int = int(target_order_id.removeprefix("sub_"))
                sub = await session.get(Subscription, sub_id_int)
            except ValueError:
                pass

        if not sub and marzban_username:
            sub = await session.scalar(
                select(Subscription).where(Subscription.client_name == marzban_username).order_by(Subscription.id.desc()).limit(1)
            )

        if not sub:
            return web.json_response({
                "ok": True,
                "matched": False,
                "delivered": False,
                "reason": "subscription_not_found",
                "cashback_status": "not_eligible",
                "cashback_rub": 0.0,
            })

        sub_id = sub.id
        sub_platform = sub.platform

        user = await session.get(User, sub.user_id)
        if not user or not user.telegram_id:
            return web.json_response({
                "ok": True,
                "matched": True,
                "delivered": False,
                "reason": "no_telegram_user",
                "subscription_id": sub_id,
                "cashback_status": "not_eligible",
                "cashback_rub": 0.0,
            })

        user_id = user.id
        telegram_id = user.telegram_id
        username = user.username
        full_name = user.full_name
        user_platform = user.platform

        # 2. Sync expiration date and status in bot DB (skip during cashback-only retries)
        if not cashback_only and access_expires_at_raw:
            from bot.services.webstore_bridge import _parse_web_datetime
            new_expires = _parse_web_datetime(access_expires_at_raw)
            if new_expires:
                sub.expires_at = new_expires
                sub.status = SubStatus.ACTIVE
                if sub.billing_mode == "demo":
                    sub.billing_mode = "tariff"
                if adapt_uuid:
                    from bot.services.adapt_subscription_proxy import build_adapt_mirror_url
                    sub.vpn_key = build_adapt_mirror_url(adapt_uuid)
                elif subscription_url and not sub.vpn_key:
                    sub.vpn_key = subscription_url
                if not adapt_record:
                    adapt_record = await session.scalar(
                        select(AdaptSubscription).where(AdaptSubscription.subscription_id == sub_id).limit(1)
                    )
                if adapt_record:
                    adapt_record.end_date = new_expires
                    adapt_record.is_frozen = False
                await session.commit()

        # 3. Resolve bot instance for delivery and cashback notification
        bot = request.app.get("bot")
        if not bot and settings.bot_token:
            from bot.telegram_client import create_telegram_bot
            bot = create_telegram_bot(settings.bot_token)

        # 4. Durable cashback idempotency processing BEFORE delivery dedupe early return
        cashback_status = "not_eligible"
        cashback_rub = 0.0

        if not cashback_eligible:
            cashback_status = "web_already_credited"
        elif amount_rub <= 0:
            cashback_status = "not_eligible"
        else:
            from bot.services.payment_service import credit_cashback
            cb_result = await credit_cashback(
                session=session,
                user_db_id=user_id,
                payment_id=None,
                amount_rub=amount_rub,
                bot=None,
                source_type="web_order_cashback",
                source_id=order_id,
            )
            cashback_status = cb_result.get("status", "failed")
            cashback_rub = float(cb_result.get("cashback_rub") or 0.0)

            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                existing_tx = await session.scalar(
                    select(BalanceTransaction).where(
                        BalanceTransaction.user_id == user_id,
                        BalanceTransaction.kind == BalanceTransactionKind.CASHBACK,
                        BalanceTransaction.source_type == "web_order_cashback",
                        BalanceTransaction.source_id == order_id,
                    ).limit(1)
                )
                if existing_tx:
                    cashback_status = "already_credited"
                    cashback_rub = float(existing_tx.amount_rub)
                    cb_result["status"] = "already_credited"
                else:
                    raise

            # ONLY AFTER successful commit: send the cashback Telegram notification
            if cb_result.get("status") == "credited" and bot and telegram_id:
                notify_text = cb_result.get("notify_text")
                if not notify_text:
                    pct_val = cb_result.get("cashback_pct", 10.0)
                    pct_fmt = int(pct_val) if isinstance(pct_val, (int, float)) and float(pct_val).is_integer() else pct_val
                    notify_text = (
                        f"🎁 Вам начислен кешбэк <b>+{cashback_rub:.2f} ₽</b> ({pct_fmt}% от оплаты {amount_rub:.2f} ₽) на ваш баланс!\n\n"
                        f"Вы можете использовать баланс для оплаты и продления подписки."
                    )
                try:
                    await bot.send_message(
                        telegram_id,
                        notify_text,
                        parse_mode="HTML",
                    )
                except Exception as e:
                    logger.error("Failed to send web cashback notification to %s for order %s: %s", telegram_id, order_id, e)

        # In cashback-only mode (used by scheduler retry), return immediately after cashback idempotency
        # WITHOUT touching delivery dedupe, Subscription.expires_at, sending renewal keys, or bonus messages.
        if cashback_only:
            return web.json_response({
                "ok": True,
                "matched": True,
                "delivered": False,
                "telegram_id": telegram_id,
                "username": username,
                "full_name": full_name,
                "subscription_id": sub_id,
                "cashback_status": cashback_status,
                "cashback_rub": cashback_rub,
            })

        # Check deduplication before sending Telegram delivery messages
        global _DELIVERED_WEB_ORDERS, _DELIVERED_WEB_ORDERS_QUEUE
        if not force and order_id in _DELIVERED_WEB_ORDERS:
            logger.info("Web order %s already delivered to user %s, skipping duplicate", order_id, telegram_id)
            return web.json_response({
                "ok": True,
                "matched": True,
                "delivered": True,
                "duplicate": True,
                "telegram_id": telegram_id,
                "username": username,
                "full_name": full_name,
                "subscription_id": sub_id,
                "cashback_status": cashback_status,
                "cashback_rub": cashback_rub,
            })

        # 5. Deliver keys and messages to user using the standard bot renewal flow
        delivered = False
        if bot:
            try:
                from bot.handlers.payment import _send_subscription_key_for_platform
                raw_platform = user_platform or sub_platform or Platform.ANDROID
                try:
                    target_platform = Platform(raw_platform)
                except (ValueError, TypeError):
                    target_platform = Platform.ANDROID

                delivered = await _send_subscription_key_for_platform(
                    bot=bot,
                    chat_id=telegram_id,
                    telegram_id=telegram_id,
                    subscription_id=sub_id,
                    platform=target_platform,
                )
                # WhatsApp proxy bonus if enabled
                wa_enabled_row = await session.get(BotSettings, "whatsapp_proxy_enabled")
                wa_host_row = await session.get(BotSettings, "whatsapp_proxy_host")
                if wa_enabled_row and wa_enabled_row.value == "1" and wa_host_row and wa_host_row.value:
                    from bot.utils.texts import WHATSAPP_PROXY_BONUS
                    await bot.send_message(
                        telegram_id,
                        WHATSAPP_PROXY_BONUS.format(proxy_host=wa_host_row.value),
                        parse_mode="HTML",
                    )
            except Exception as exc:
                logger.error("Failed to deliver web order renewal to user %s: %s", telegram_id, exc)

        if delivered:
            _DELIVERED_WEB_ORDERS.add(order_id)
            _DELIVERED_WEB_ORDERS_QUEUE.append(order_id)
            if len(_DELIVERED_WEB_ORDERS) > 1000:
                _DELIVERED_WEB_ORDERS = set(_DELIVERED_WEB_ORDERS_QUEUE)

        plog(
            "ВЫДАЧА_ВЕБ_ПРОДЛЕНИЕ",
            user_id=telegram_id,
            order_id=order_id,
            tariff=tariff_label or "",
            subscription_id=sub_id,
        )

        return web.json_response({
            "ok": True,
            "matched": True,
            "delivered": delivered,
            "telegram_id": telegram_id,
            "username": username,
            "full_name": full_name,
            "subscription_id": sub_id,
            "cashback_status": cashback_status,
            "cashback_rub": cashback_rub,
        })


async def handle_internal_web_order_resolve_user(request: web.Request) -> web.Response:
    """Resolve associated Telegram user for a web order before/after payment."""
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    adapt_uuid = request.query.get("adapt_uuid", "").strip()
    marzban_username = request.query.get("marzban_username", "").strip()
    target_order_id = request.query.get("target_order_id", "").strip()
    subscription_url = request.query.get("subscription_url", "").strip()

    if not adapt_uuid and target_order_id and "uuid:" in target_order_id:
        adapt_uuid = target_order_id.split("uuid:", 1)[1].strip()
    if not adapt_uuid and subscription_url:
        import re
        match = re.search(r"/(?:adapt-sub|sub)/([0-9a-f-]{36})", subscription_url, re.IGNORECASE)
        if match:
            adapt_uuid = match.group(1)

    async with async_session() as session:
        sub = None
        if adapt_uuid:
            adapt_record = await session.scalar(
                select(AdaptSubscription).where(
                    func.lower(AdaptSubscription.adapt_uuid) == adapt_uuid.lower()
                ).limit(1)
            )
            if adapt_record:
                sub = await session.get(Subscription, adapt_record.subscription_id)
            if not sub:
                sub = await session.scalar(
                    select(Subscription).where(
                        Subscription.client_name == f"adapt_{adapt_uuid}"
                    ).order_by(Subscription.id.desc()).limit(1)
                )
            if not sub:
                sub = await session.scalar(
                    select(Subscription).where(
                        Subscription.vpn_key.contains(adapt_uuid)
                    ).order_by(Subscription.id.desc()).limit(1)
                )

        if not sub and target_order_id and target_order_id.startswith("sub_"):
            try:
                sub_id_int = int(target_order_id.removeprefix("sub_"))
                sub = await session.get(Subscription, sub_id_int)
            except ValueError:
                pass

        if not sub and marzban_username:
            sub = await session.scalar(
                select(Subscription).where(Subscription.client_name == marzban_username).order_by(Subscription.id.desc()).limit(1)
            )

        if not sub:
            return web.json_response({"ok": True, "user": None})

        user = await session.get(User, sub.user_id)
        if not user or not user.telegram_id:
            return web.json_response({"ok": True, "user": None})

        return web.json_response({
            "ok": True,
            "user": {
                "telegram_id": user.telegram_id,
                "username": user.username,
                "full_name": user.full_name,
                "subscription_id": sub.id,
            }
        })



async def handle_vhq_subscription_proxy(request: web.Request) -> web.Response:
    token_payload = resolve_vhq_mirror_token(request.match_info.get("token", ""))
    if not token_payload:
        return web.Response(status=404, text="Not found")

    upstream_url: str | None = None
    expires_at = None
    key_id = None
    tariff_label = None
    kind = token_payload.get("kind")
    if kind == "subscription":
        subscription_id = int(token_payload["subscription_id"])
        async with async_session() as session:
            subscription = await session.get(Subscription, subscription_id)
            if not subscription or not subscription.client_name.startswith("vhq_") or not subscription.vpn_key:
                return web.Response(status=404, text="Not found")
            upstream_url = subscription.vpn_key
            expires_at = subscription.expires_at
            key_id = str(subscription.id)
            if subscription.tariff_id:
                tariff = await session.get(Tariff, subscription.tariff_id)
                if tariff:
                    tariff_label = tariff.label
    elif kind == "web_order":
        web_order_id = str(token_payload.get("web_order_id") or "").strip()
        if web_order_id:
            try:
                from webstore.database import async_session as web_async_session
                from webstore.models import WebOrder
                async with web_async_session() as session:
                    web_order = await session.scalar(
                        select(WebOrder).where(WebOrder.order_id == web_order_id).limit(1)
                    )
                    if web_order and web_order.vhq_upstream_url:
                        upstream_url = web_order.vhq_upstream_url
                        expires_at = web_order.access_expires_at
                        key_id = web_order.order_id
                        tariff_label = web_order.tariff_label
            except Exception as exc:
                logger.warning("Failed to lookup web_order for vhq proxy: %s", exc)
    else:
        # Legacy token with kind == "upstream", format: {"u": ..., "o": ...}
        order_id = str(token_payload.get("order_id") or "").strip() or None
        embedded_upstream = str(token_payload.get("upstream_url") or "").strip()
        key_id = order_id
        if order_id:
            try:
                from webstore.database import async_session as web_async_session
                from webstore.models import WebOrder
                async with web_async_session() as session:
                    web_order = await session.scalar(
                        select(WebOrder).where(WebOrder.order_id == order_id).limit(1)
                    )
                    if web_order and web_order.vhq_upstream_url:
                        upstream_url = web_order.vhq_upstream_url
                        expires_at = web_order.access_expires_at
                        tariff_label = web_order.tariff_label
            except Exception as exc:
                logger.warning("Failed to lookup web_order for legacy token vhq proxy: %s", exc)

        if not upstream_url:
            upstream_url = embedded_upstream

    if not upstream_url:
        return web.Response(status=404, text="Not found")

    try:
        status, body, headers = await fetch_vhq_mirror_payload(
            upstream_url,
            request.headers,
            expires_at=expires_at,
            key_id=key_id,
            tariff_label=tariff_label,
        )
    except aiohttp.ClientError as exc:
        logger.warning("VHQ proxy request failed: %s", exc)
        return web.Response(status=502, text="Subscription service temporarily unavailable")
    except Exception as exc:
        logger.exception("Unexpected VHQ proxy failure: %s", exc)
        return web.Response(status=500, text="Internal proxy error")

    return web.Response(status=status, body=body, headers=headers)


# ── Adapt Group: subscription proxy + webhook ─────────

_ADAPT_WEBHOOK_IP = "139.60.162.7"


async def handle_adapt_subscription_proxy(request: web.Request) -> web.Response:
    """Proxy GET /adapt-sub/{uuid} → Adapt subscription endpoint.

    UUIDs may come from two sources:
    - Bot DB (adapt_subscriptions table) — bot-side purchases
    - Webstore DB (web_orders table) — webstore purchases stored in a separate DB

    We validate UUID format and proxy to upstream Adapt, which handles
    its own authentication.  Non-existent UUIDs will get a 404 from upstream.
    """
    adapt_uuid = request.match_info.get("uuid", "").strip()
    if not adapt_uuid:
        return web.Response(status=404, text="Not found")

    # Basic UUID format validation to prevent abuse / scanning
    import re
    if not re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", adapt_uuid, re.IGNORECASE):
        return web.Response(status=404, text="Not found")

    try:
        status, body, headers = await fetch_adapt_mirror_payload(
            adapt_uuid,
            request_headers=request.headers,
            query_params=request.query,
        )
    except Exception as exc:
        logger.warning("Adapt proxy request failed for uuid=%s: %s", adapt_uuid, exc)
        return web.Response(status=502, text="Subscription service temporarily unavailable")

    return web.Response(status=status, body=body, headers=headers)


def _is_valid_adapt_uuid(val: str) -> bool:
    import re
    return bool(re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", val, re.IGNORECASE))


async def handle_adapt_subscription_devices(request: web.Request) -> web.Response:
    """Return JSON list of devices for this subscription."""
    adapt_uuid = request.match_info.get("uuid", "").strip()
    if not _is_valid_adapt_uuid(adapt_uuid):
        return web.json_response({"ok": False, "error": "Invalid subscription"}, status=400)
    try:
        from bot.services.adapt_api import AdaptAPI
        devices = await asyncio.wait_for(AdaptAPI().get_devices(adapt_uuid), timeout=8.0)
        return web.json_response({"ok": True, "devices": devices})
    except (asyncio.TimeoutError, TimeoutError):
        return web.json_response({"ok": False, "error": "Timeout fetching devices"}, status=504)
    except Exception as exc:
        logger.warning("Failed to fetch devices for adapt_uuid=%s: %s", adapt_uuid, exc)
        return web.json_response({"ok": False, "error": "Failed to fetch devices"}, status=500)


async def handle_adapt_subscription_device_delete(request: web.Request) -> web.Response:
    """Delete a device by ID for this subscription."""
    adapt_uuid = request.match_info.get("uuid", "").strip()
    if not _is_valid_adapt_uuid(adapt_uuid):
        return web.json_response({"ok": False, "error": "Invalid subscription"}, status=400)

    try:
        data = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "Invalid JSON"}, status=400)

    raw_dev_id = data.get("device_id")
    if raw_dev_id is None:
        return web.json_response({"ok": False, "error": "Missing device_id"}, status=400)

    try:
        device_id = int(raw_dev_id)
    except (ValueError, TypeError):
        return web.json_response({"ok": False, "error": "Invalid device_id"}, status=400)

    try:
        from bot.services.adapt_api import AdaptAPI
        api = AdaptAPI()
        success = await asyncio.wait_for(api.delete_device(adapt_uuid, device_id), timeout=8.0)
        if success:
            logger.info("Deleted adapt device %s for uuid=%s", device_id, adapt_uuid)
            return web.json_response({"ok": True})
        return web.json_response({"ok": False, "error": "Provider declined device deletion"}, status=502)
    except (asyncio.TimeoutError, TimeoutError):
        return web.json_response({"ok": False, "error": "Timeout deleting device"}, status=504)
    except Exception as exc:
        logger.error("Failed to delete device %s for adapt_uuid=%s: %s", device_id, adapt_uuid, exc)
        return web.json_response({"ok": False, "error": "Error deleting device"}, status=500)


async def handle_adapt_webhook(request: web.Request) -> web.Response:
    """Handle incoming webhook events from Adapt Group.

    Signature: HMAC-SHA256 of raw body with ADAPT_WEBHOOK_SECRET,
    sent in X-Webhook-Signature header.
    IP: 139.60.162.7
    """
    # IP whitelist check (optional but recommended)
    peername = request.transport.get_extra_info("peername") if request.transport else None
    client_ip = (peername[0] if peername else "") or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()

    raw_body = await request.read()

    # Signature verification — fail closed if secret is not configured or signature is missing/invalid
    webhook_secret = (settings.adapt_webhook_secret or "").strip()
    if not webhook_secret:
        logger.warning("Adapt webhook rejected: ADAPT_WEBHOOK_SECRET is not configured")
        return web.Response(status=403, text="Webhook authentication disabled (missing secret)")

    sig_header = request.headers.get("X-Webhook-Signature", "").strip()
    if not sig_header:
        logger.warning("Adapt webhook rejected: missing X-Webhook-Signature header from IP=%s", client_ip)
        return web.Response(status=403, text="Missing signature header")

    expected_sig = hmac.new(
        webhook_secret.encode(),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected_sig, sig_header):
        logger.warning("Adapt webhook: invalid signature from IP=%s", client_ip)
        return web.Response(status=403, text="Invalid signature")

    try:
        import json as _json
        payload = _json.loads(raw_body)
    except Exception:
        return web.Response(status=400, text="Invalid JSON")

    event = str(payload.get("event", "")).strip()
    event_data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
    subscription_uuid = str(event_data.get("subscription_uuid", "")).strip()
    external_user_id = str(event_data.get("external_user_id", "")).strip()

    logger.info(
        "Adapt webhook event=%s subscription_uuid=%s external_user_id=%s",
        event,
        subscription_uuid,
        external_user_id,
    )

    if not event or not subscription_uuid:
        return web.json_response({"ok": True})

    # Web purchases use a deterministic external_user_id.  Forward the signed,
    # already verified event so a lost /subs/create response cannot strand a
    # paid customer or cause a duplicate create on retry.
    if external_user_id.startswith("web_") and settings.webstore_bridge_secret:
        try:
            url = f"{settings.webstore_api_base_url.rstrip('/')}/api/store/internal/adapt-event"
            timeout = aiohttp.ClientTimeout(total=8)
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.post(
                    url,
                    headers={"X-Internal-Secret": settings.webstore_bridge_secret},
                    json={"event": event, "data": event_data},
                ) as response:
                    if response.status >= 300:
                        logger.warning("Adapt web event bridge rejected status=%s external_user_id=%s", response.status, external_user_id)
        except Exception as exc:
            logger.warning("Adapt web event bridge failed external_user_id=%s: %s", external_user_id, exc)

    if event == "subs.created" and external_user_id.startswith("tgpay_"):
        delivered = await _reconcile_adapt_telegram_create(
            request,
            external_user_id=external_user_id,
            event_data=event_data,
        )
        if not delivered:
            # Adapt documents retry webhook delivery for non-2xx responses.
            return web.Response(status=503, text="Telegram delivery pending")

    async with async_session() as session:
        from sqlalchemy import select as _select
        from bot.models import AdaptSubscription as _AS
        result = await session.execute(
            _select(_AS).where(_AS.adapt_uuid == subscription_uuid).limit(1)
        )
        adapt_record = result.scalar_one_or_none()

        if event == "subs.expired" and adapt_record:
            sub = await session.get(Subscription, adapt_record.subscription_id)
            if sub and sub.status == SubStatus.ACTIVE:
                sub.status = SubStatus.EXPIRED
                await session.commit()
                logger.info(
                    "Adapt webhook: marked subscription %s as expired (adapt_uuid=%s)",
                    sub.id,
                    subscription_uuid,
                )

        elif event in (
            "subs.expires_in_72_hours",
            "subs.expires_in_48_hours",
            "subs.expires_in_24_hours",
            "subs.expired_24_hours_ago",
            "subs.traffic_threshold_reached",
        ) and adapt_record:
            # Forward notification to the user
            sub = await session.get(Subscription, adapt_record.subscription_id)
            if sub:
                sub.adapt_sub = adapt_record
                canonical_uuid = str(adapt_record.adapt_plan_uuid or "").strip()
                current_tariff = None
                if canonical_uuid:
                    current_tariff = await session.scalar(
                        _select(Tariff)
                        .where(Tariff.adapt_plan_uuid == canonical_uuid)
                        .order_by(Tariff.id.desc())
                        .limit(1)
                    )
                if not current_tariff and sub.tariff_id:
                    current_tariff = await session.get(Tariff, sub.tariff_id)
                sub.adapt_current_tariff = current_tariff
                user = await session.get(User, sub.user_id)
                if user:
                    await _adapt_notify_user(event, user, sub, adapt_record, request.app)

    return web.json_response({"ok": True})


async def _reconcile_adapt_telegram_create(
    request: web.Request,
    *,
    external_user_id: str,
    event_data: dict,
) -> bool:
    """Finish a paid bot create whose HTTP response was lost."""
    try:
        payment_id = int(external_user_id.removeprefix("tgpay_"))
    except ValueError:
        return True
    adapt_uuid = str(event_data.get("subscription_uuid") or "").strip()
    if not adapt_uuid:
        return True

    from bot.models import AdaptSubscription
    from bot.services.adapt_subscription_proxy import build_adapt_mirror_url
    from bot.services.subscription_service import (
        _disable_balance_autodebit_after_tariff_purchase,
        get_primary_active_server,
    )
    from bot.services.notifications import notify_expiring, notify_staff_text
    from bot.services.device_slots import get_included_device_slots

    async with async_session() as session:
        payment = await session.get(Payment, payment_id)
        if not payment or payment.status != PaymentStatus.COMPLETED:
            logger.error("Adapt tg reconciliation payment missing/not completed payment_id=%s", payment_id)
            return True
        if payment.subscription_id and await session.get(Subscription, payment.subscription_id):
            return True
        user = await session.get(User, payment.user_id)
        tariff = await session.get(Tariff, payment.tariff_id) if payment.tariff_id else None
        if not user or not tariff:
            logger.error("Adapt tg reconciliation user/tariff missing payment_id=%s", payment_id)
            return True

        subscription = await session.get(Subscription, payment.subscription_id) if payment.subscription_id else None
        created = False
        if not subscription:
            server = await get_primary_active_server(session)
            if not server:
                logger.error("Adapt tg reconciliation has no active server payment_id=%s", payment_id)
                return False
            raw_end = str(event_data.get("end_date") or "").strip()
            try:
                expires_at = datetime.fromisoformat(raw_end.replace("Z", "+00:00")).replace(tzinfo=None)
            except (TypeError, ValueError):
                expires_at = datetime.utcnow() + timedelta(days=int(event_data.get("days") or tariff.days))
            try:
                platform = Platform(payment.platform or Platform.ANDROID.value)
            except ValueError:
                platform = Platform.ANDROID
            included_slots = await get_included_device_slots(session)
            key = build_adapt_mirror_url(adapt_uuid)
            subscription = Subscription(
                user_id=user.id,
                server_id=server.id,
                tariff_months=tariff.days // 30,
                tariff_days=tariff.days,
                billing_mode="tariff",
                vpn_key=key,
                client_name=build_adapt_client_name(adapt_uuid),
                platform=platform,
                device_slots=int(event_data.get("devices") or included_slots),
                expires_at=expires_at,
                tariff_id=tariff.id,
            )
            session.add(subscription)
            await session.flush()
            session.add(
                AdaptSubscription(
                    subscription_id=subscription.id,
                    adapt_uuid=adapt_uuid,
                    adapt_plan_uuid=str(event_data.get("plan_uuid") or tariff.adapt_plan_uuid),
                    end_date=expires_at,
                    traffic_limit_bytes=event_data.get("traffic_limit_bytes"),
                )
            )
            payment.subscription_id = subscription.id
            payment.provisioning_failure_code = None
            _disable_balance_autodebit_after_tariff_purchase(user)
            created = True
            await session.commit()
            plog(
                "ОПЛАТА_АВТО_СВЕРКА",
                provider="Adapt",
                user_id=user.telegram_id,
                payment_id=payment.id,
                subscription_id=subscription.id,
            )

        from bot.services.vhq_subscription_proxy import get_subscription_display_key
        key = get_subscription_display_key(subscription) or build_adapt_mirror_url(adapt_uuid)
        expires = subscription.expires_at.strftime("%d.%m.%Y") if subscription.expires_at else "—"
        sent = await notify_expiring(
            request.app["bot"],
            user.telegram_id,
            "✅ <b>Оплата подтверждена, доступ готов</b>\n\n"
            f"Ключ: <code>{key}</code>\n"
            f"Действует до: <b>{expires}</b>\n\n"
            "Если нужна помощь с подключением, напишите в поддержку.",
            reply_markup=back_to_menu_kb(),
        )
        if created:
            await notify_staff_text(
                request.app["bot"],
                "🔄 <b>Adapt-доступ восстановлен по webhook</b>\n\n"
                f"Пользователь: <code>{user.telegram_id}</code>\n"
                f"Платёж: <code>{payment.id}</code>\n"
                f"Подписка: <code>{subscription.id}</code>",
            )
        return sent


async def _adapt_notify_user(
    event: str,
    user: "User",
    subscription: "Subscription",
    adapt_record,
    app,
) -> None:
    """Send relevant notification to the user based on Adapt webhook event."""
    bot = app.get("bot")
    if not bot:
        return

    if event == "subs.expires_in_72_hours":
        text = "⚠️ Ваш доступ истекает через 3 дня. Продлите его в боте."
    elif event == "subs.expires_in_48_hours":
        text = "⚠️ Ваш доступ истекает через 2 дня. Продлите его в боте."
    elif event == "subs.expires_in_24_hours":
        text = "⚠️ Ваш доступ истекает завтра. Продлите его сейчас."
    elif event == "subs.expired_24_hours_ago":
        tariff = getattr(subscription, "adapt_current_tariff", None)
        if not tariff:
            tariff = (
                subscription.__dict__.get("tariff")
                if hasattr(subscription, "__dict__")
                else getattr(subscription, "tariff", None)
            )
        tariff_label = escape(str(getattr(tariff, "label", None) or "Подписка Adapt"))
        tariff_days = getattr(tariff, "days", None) or getattr(subscription, "tariff_days", None) or "—"
        device_count = getattr(subscription, "device_slots", None) or getattr(tariff, "device_count", None) or "—"
        expires = (
            subscription.expires_at.strftime("%d.%m.%Y")
            if getattr(subscription, "expires_at", None)
            else "—"
        )
        text = (
            "❌ <b>Подписка истекла</b>\n\n"
            f"Тариф: <b>{tariff_label}</b>\n"
            f"Срок тарифа: <b>{tariff_days} дн.</b>\n"
            f"Устройств: <b>{device_count}</b>\n"
            f"Действовала до: <b>{expires}</b>\n\n"
            "Чтобы восстановить доступ и сохранить эту же ссылку подписки,\n"
            "нажмите «Продлить»."
        )
    elif event == "subs.traffic_threshold_reached":
        text = "⚡️ Ваш трафик почти исчерпан. Докупите трафик в боте."
    else:
        return

    try:
        if event == "subs.expired_24_hours_ago":
            await bot.send_message(
                user.telegram_id,
                text,
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="↻ Продлить",
                                callback_data=f"purchase_renew_{subscription.id}",
                            )
                        ]
                    ]
                ),
            )
        else:
            await bot.send_message(user.telegram_id, text)
    except Exception as exc:
        logger.warning(
            "Failed to send Adapt notification to user %s: %s", user.telegram_id, exc
        )


# ── Route registration ────────────────────────────────


def setup_webhooks(app: web.Application, prefix: str) -> None:
    """Register all payment webhook routes."""
    app.router.add_get(f"{prefix}/vhq-sub/{{token}}", handle_vhq_subscription_proxy)
    app.router.add_get(f"{prefix}/adapt-sub/{{uuid}}", handle_adapt_subscription_proxy)
    app.router.add_get(f"{prefix}/adapt-sub/{{uuid}}/devices", handle_adapt_subscription_devices)
    app.router.add_post(f"{prefix}/adapt-sub/{{uuid}}/devices/delete", handle_adapt_subscription_device_delete)
    if prefix:
        app.router.add_get("/adapt-sub/{uuid}", handle_adapt_subscription_proxy)
        app.router.add_get("/adapt-sub/{uuid}/devices", handle_adapt_subscription_devices)
        app.router.add_post("/adapt-sub/{uuid}/devices/delete", handle_adapt_subscription_device_delete)
    app.router.add_post(f"{prefix}/adapt-webhook", handle_adapt_webhook)
    app.router.add_post(f"{prefix}/webhooks/yookassa", handle_yookassa)
    app.router.add_route("*", f"{prefix}/webhooks/robokassa/result", handle_robokassa_result)
    app.router.add_route("*", f"{prefix}/webhooks/robokassa/success", handle_robokassa_success)
    app.router.add_route("*", f"{prefix}/webhooks/robokassa/fail", handle_robokassa_fail)
    app.router.add_get(f"{prefix}/internal/web-referral/resolve", handle_internal_web_referral_resolve)
    app.router.add_post(f"{prefix}/internal/web-referral/credit", handle_internal_web_referral_credit)
    app.router.add_get(f"{prefix}/internal/balance-profile", handle_internal_balance_profile)
    app.router.add_get(f"{prefix}/internal/balance-config", handle_internal_balance_config)
    app.router.add_post(f"{prefix}/internal/balance-credit", handle_internal_balance_credit)
    app.router.add_get(f"{prefix}/internal/balance-credit-status", handle_internal_balance_credit_status)
    app.router.add_get(f"{prefix}/internal/balance-history", handle_internal_balance_history)
    app.router.add_post(f"{prefix}/internal/balance-toggle", handle_internal_balance_toggle)
    app.router.add_post(f"{prefix}/internal/web-order/delivered", handle_internal_web_order_delivered)
    app.router.add_get(f"{prefix}/internal/web-order/resolve-user", handle_internal_web_order_resolve_user)
    if prefix:
        app.router.add_get("/internal/balance-credit-status", handle_internal_balance_credit_status)
        app.router.add_post("/internal/web-order/delivered", handle_internal_web_order_delivered)
        app.router.add_get("/internal/web-order/resolve-user", handle_internal_web_order_resolve_user)
