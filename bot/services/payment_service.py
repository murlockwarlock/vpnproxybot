"""Payment service - Telegram Stars, YooKassa, Robokassa."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import urllib.parse
from datetime import timedelta

from aiogram import Bot
from aiogram.types import LabeledPrice

from bot.config import settings
from bot.services.balance_service import credit_user_balance

logger = logging.getLogger(__name__)


_PLATFORM_LABELS = {
    "android": "Android",
    "ios": "iOS",
    "mac": "Mac",
    "windows": "Windows",
    "android_tv": "Android TV",
    "deferred": "после оплаты",
    "tgproxy": "Telegram",
}


def _get_platform_label(platform: str) -> str:
    return _PLATFORM_LABELS.get(platform, platform)


async def create_stars_invoice(
    bot: Bot,
    chat_id: int,
    tariff_id: int,
    tariff_label: str,
    price_stars: int,
    platform: str,
    payload: str,
) -> None:
    """Send a Telegram Stars invoice to the user."""
    platform_label = _get_platform_label(platform)
    is_tg = platform == "tgproxy"

    await bot.send_invoice(
        chat_id=chat_id,
        title=f"🛡 {'Telegram-ускоритель' if is_tg else 'Весь интернет'} {tariff_label}",
        description=(
            f"{'Telegram-ускоритель' if is_tg else 'Весь интернет — все серверы'}\n"
            f"Срок: {tariff_label}"
            + (f" • Платформа: {platform_label}" if not is_tg else "")
        ),
        payload=payload,
        currency="XTR",  # Telegram Stars
        prices=[LabeledPrice(label=f"Весь интернет {tariff_label}", amount=price_stars)],
    )
    logger.info(
        f"Stars invoice sent to {chat_id}: tariff_id={tariff_id}, {platform}"
    )


async def create_telegram_pay_invoice(
    bot: Bot,
    chat_id: int,
    tariff_id: int,
    tariff_label: str,
    price_rub: int,
    platform: str,
    payload: str,
    provider_token: str,
) -> None:
    """Send a Telegram Pay (card) invoice using the native Telegram payments API."""
    platform_label = _get_platform_label(platform)
    is_tg = platform == "tgproxy"

    await bot.send_invoice(
        chat_id=chat_id,
        title=f"🛡 {'Telegram-ускоритель' if is_tg else 'Весь интернет'} {tariff_label}",
        description=(
            f"{'Telegram-ускоритель' if is_tg else 'Весь интернет — все серверы'}\n"
            f"Срок: {tariff_label}"
            + (f" • ОС: {platform_label}" if not is_tg else "")
        ),
        payload=payload,
        provider_token=provider_token,
        currency="RUB",
        prices=[LabeledPrice(label=f"Ускоритель интернета {tariff_label}", amount=max(100, int(round(price_rub * 100))))],  # kopecks, must be int
    )
    logger.info(
        f"Telegram Pay invoice sent to {chat_id}: tariff_id={tariff_id}, {platform}"
    )


async def log_referral_payment(session, user_db_id: int, amount: float, bot=None) -> None:
    """Log payment from a referred user; optionally award bonus days to the referrer."""

    from sqlalchemy import func, select as _sel

    from bot.models import ReferralConfig, ReferralPaymentLog, SubStatus, User

    if amount <= 0:
        return

    user = await session.get(User, user_db_id)
    if not user or not user.referred_by:
        return

    config = await session.get(ReferralConfig, 1)
    if not config or not config.is_enabled:
        return

    ref_result = await session.execute(
        _sel(User).where(User.telegram_id == user.referred_by)
    )
    referrer = ref_result.scalar_one_or_none()
    if not referrer:
        return

    if config.pay_bonus_enabled:
        already_paid = False
        if config.pay_bonus_first_only:
            prev_count = await session.scalar(
                _sel(func.count()).select_from(ReferralPaymentLog).where(
                    ReferralPaymentLog.referred_user_id == user_db_id
                )
            ) or 0
            already_paid = prev_count > 0

        if not already_paid and config.pay_bonus_days and config.pay_bonus_days > 0:
            extended = False
            for sub in referrer.subscriptions:
                if sub.status == SubStatus.ACTIVE:
                    sub.expires_at += timedelta(days=config.pay_bonus_days)
                    extended = True
                    break
            if not extended:
                referrer.bonus_days = (referrer.bonus_days or 0) + config.pay_bonus_days

            if bot:
                try:
                    await bot.send_message(
                        user.referred_by,
                        f"💰 Ваш реферал оформил подписку! Вам начислено "
                        f"<b>{config.pay_bonus_days} бонусных дн.</b>",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

    session.add(ReferralPaymentLog(
        referrer_id=referrer.id,
        referred_user_id=user_db_id,
        amount=amount,
    ))

    logger.info(
        f"Referral payment logged: referrer={referrer.telegram_id}, "
        f"referred={user.telegram_id}, amount={amount:.2f}"
    )


async def credit_referral(
    session,
    user_db_id: int,
    payment_id: int | None,
    amount_rub: float,
    bot: Bot = None,
) -> None:
    """Credit referral commission if the paying user was referred and program is active."""
    from sqlalchemy import select as _select  # avoid circular import at module level

    from bot.models import BalanceTransactionKind, ReferralConfig, ReferralEarning, User

    if amount_rub <= 0:
        return

    config = await session.get(ReferralConfig, 1)
    if not config or not config.is_enabled:
        return

    user = await session.get(User, user_db_id)
    if not user or not user.referred_by:
        return

    result = await session.execute(
        _select(User).where(User.telegram_id == user.referred_by)
    )
    referrer = result.scalar_one_or_none()
    if not referrer or referrer.id == user_db_id:
        return

    earning = round(amount_rub * config.commission_percent / 100, 2)
    if earning <= 0:
        return

    session.add(
        ReferralEarning(
            referrer_id=referrer.id,
            referred_id=user_db_id,
            payment_id=payment_id,
            amount_rub=earning,
        )
    )
    credit_user_balance(
        session,
        referrer,
        earning,
        BalanceTransactionKind.REFERRAL_BONUS,
        "Бонус за приглашение друга",
        source_type="payment",
        source_id=str(payment_id) if payment_id is not None else None,
    )
    
    # Notify referrer about commission
    if bot and earning > 0:
        try:
            from bot.utils.texts import fmt_user
            user_info = fmt_user(user.telegram_id, user.username, user.full_name)
            await bot.send_message(
                referrer.telegram_id,
                f"💰 Вам начислено <b>{earning}₽</b> на баланс за платеж пользователя {user_info}",
                parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Failed to notify referrer {referrer.telegram_id} about commission: {e}")

    logger.info(
        f"Referral credit: referrer={referrer.telegram_id}, "
        f"referred={user.telegram_id}, +{earning:.2f}₽"
    )


async def credit_partner(
    session,
    user_db_id: int,
    payment_id: int | None,
    amount_rub: float,
    bot: Bot = None,
) -> None:
    """Credit partner commission if the paying user came via a partner link."""
    from bot.models import Partner, PartnerEarning, User

    if amount_rub <= 0:
        return

    user = await session.get(User, user_db_id)
    if not user or not user.partner_id:
        return

    partner = await session.get(Partner, user.partner_id)
    if not partner or not partner.is_active:
        return
    if partner.telegram_id and partner.telegram_id == user.telegram_id:
        return

    if partner.commission_percent <= 0:
        return

    earning = round(amount_rub * partner.commission_percent / 100, 2)
    if earning <= 0:
        return

    session.add(
        PartnerEarning(
            partner_id=partner.id,
            user_id=user_db_id,
            payment_id=payment_id,
            amount=earning,
        )
    )
    partner.partner_balance = round((partner.partner_balance or 0.0) + earning, 2)

    if bot and partner.telegram_id:
        try:
            from bot.utils.texts import fmt_user
            user_info = fmt_user(user.telegram_id, user.username, user.full_name)
            await bot.send_message(
                partner.telegram_id,
                f"💰 Партнёрское начисление: <b>{earning}₽</b> за платеж пользователя {user_info}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to notify partner {partner.telegram_id}: {e}")

    logger.info(
        f"Partner credit: partner={partner.name} (id={partner.id}), "
        f"user={user.telegram_id}, +{earning:.2f}₽"
    )


async def create_yookassa_payment(
    user_id: int,
    tariff_id: int,
    platform: str,
    chat_id: int,
    return_url: str,
    tariff_label: str = "",
    price_rub: float = 0.0,
) -> tuple[str, str]:
    """Create a Yookassa payment. Returns (confirmation_url, yookassa_payment_id).
    
    Now accepts tariff_id instead of server_id+tariff_idx.
    tariff_label/price_rub can be passed in or left empty (will be looked up externally if needed).
    """
    import yookassa  # noqa: PLC0415

    yookassa.Configuration.account_id = settings.yookassa_shop_id
    yookassa.Configuration.secret_key = settings.yookassa_secret_key

    def _create() -> yookassa.Payment:
        payload = {
            "amount": {"value": f"{price_rub:.2f}", "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": return_url},
            "capture": True,
            "description": f"Весь интернет {tariff_label}",
            "metadata": {
                "user_id": str(user_id),
                "tariff_id": str(tariff_id),
                "platform": platform,
                "chat_id": str(chat_id),
            },
        }
        if settings.yookassa_save_payment_method:
            payload["save_payment_method"] = True
        return yookassa.Payment.create(
            payload
        )

    payment = await asyncio.to_thread(_create)
    return payment.confirmation.confirmation_url, payment.id


async def create_recurring_yookassa_payment(
    payment_method_id: str,
    amount_rub: float,
    description: str,
    user_id: int,
    tariff_id: int,
    idempotence_key: str,
) -> tuple[str, str | None, str | None]:
    """Create auto-charge payment using saved payment_method_id.

    Returns (result, payment_id, yookassa_status).
    result: 'succeeded', 'pending', 'failed', 'deactivate', 'provider_error'.
    """
    import hashlib

    import yookassa
    from yookassa.domain.exceptions import (
        BadRequestError,
        ForbiddenError,
        InternalServerError,
        TooManyRequestsError,
        UnauthorizedError,
    )

    yookassa.Configuration.account_id = settings.yookassa_shop_id
    yookassa.Configuration.secret_key = settings.yookassa_secret_key

    payload = {
        "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
        "capture": True,
        "payment_method_id": payment_method_id,
        "description": description,
        "metadata": {
            "user_id": str(user_id),
            "tariff_id": str(tariff_id),
            "recurring": "true",
        },
    }

    try:
        payment = await asyncio.to_thread(
            yookassa.Payment.create, payload, idempotence_key
        )
        if payment.status == "succeeded":
            return "succeeded", payment.id, payment.status
        if payment.status in ("pending", "waiting_for_capture"):
            return "pending", payment.id, payment.status
        return "failed", payment.id, payment.status

    except BadRequestError as e:
        error_code = (
            (e.content or {}).get("code")
            if isinstance(getattr(e, "content", None), dict)
            else None
        )
        if error_code == "payment_method_not_found":
            return "deactivate", None, None
        logger.error("YooKassa recurring BadRequest: %s", e)
        return "provider_error", None, None

    except (ForbiddenError, InternalServerError, TooManyRequestsError, UnauthorizedError) as e:
        logger.error("YooKassa recurring API error: %s", e)
        return "provider_error", None, None

    except Exception as e:
        logger.error("YooKassa recurring unknown error: %s", e)
        return "provider_error", None, None


def generate_robokassa_url(
    merchant_login: str,
    password1: str,
    inv_id: int,
    amount: float,
    description: str,
) -> str:
    """Generate a Robokassa payment URL."""
    sig = hashlib.md5(
        f"{merchant_login}:{amount:.2f}:{inv_id}:{password1}".encode()
    ).hexdigest()
    params = urllib.parse.urlencode(
        {
            "MerchantLogin": merchant_login,
            "OutSum": f"{amount:.2f}",
            "InvId": inv_id,
            "Description": description,
            "SignatureValue": sig,
        }
    )
    return f"https://auth.robokassa.ru/Merchant/Index.aspx?{params}"
