"""Payment service - Telegram Stars, YooKassa, Robokassa."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import urllib.parse
from datetime import timedelta
from typing import Any

from aiogram import Bot
from aiogram.types import LabeledPrice
from sqlalchemy.exc import IntegrityError

from bot.config import settings
from bot.services.balance_service import async_credit_user_balance, credit_user_balance
from bot.services.money import (
    kopecks_to_rub_string,
    rub_to_kopecks,
)

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
        prices=[LabeledPrice(label=f"Ускоритель интернета {tariff_label}", amount=max(100, rub_to_kopecks(price_rub)))],  # kopecks, must be int
    )
    logger.info(
        f"Telegram Pay invoice sent to {chat_id}: tariff_id={tariff_id}, {platform}"
    )


async def log_referral_payment(session, user_db_id: int, amount: float, bot=None, payment_id: int | None = None) -> None:
    """Log payment from a referred user; optionally award bonus days to the referrer."""
    from sqlalchemy import func, select as _sel
    from sqlalchemy.exc import IntegrityError
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

    # 1. Deduplicate by payment_id at Python level if provided
    if payment_id is not None:
        existing = await session.scalar(
            _sel(ReferralPaymentLog).where(ReferralPaymentLog.payment_id == payment_id).limit(1)
        )
        if existing:
            logger.info("Referral payment already logged for payment %s", payment_id)
            return

    # 2. Insert ReferralPaymentLog atomically in a savepoint (guarded by uq_referral_payment_logs_payment_id)
    try:
        async with session.begin_nested():
            session.add(
                ReferralPaymentLog(
                    referrer_id=referrer.id,
                    referred_user_id=user_db_id,
                    payment_id=payment_id,
                    amount=amount,
                )
            )
            await session.flush()
    except IntegrityError:
        if payment_id is not None:
            existing = await session.scalar(
                _sel(ReferralPaymentLog).where(ReferralPaymentLog.payment_id == payment_id).limit(1)
            )
            if existing:
                logger.info("Referral payment log already exists for payment %s (concurrent race)", payment_id)
                return
        logger.error("Unexpected IntegrityError in log_referral_payment for user %s, payment %s", user_db_id, payment_id)
        raise

    # 3. Bonus days: awarded only once per payment (and once per user if pay_bonus_first_only is set)
    if config.pay_bonus_enabled:
        already_paid = False
        if config.pay_bonus_first_only:
            log_count = await session.scalar(
                _sel(func.count()).select_from(ReferralPaymentLog).where(
                    ReferralPaymentLog.referred_user_id == user_db_id
                )
            ) or 0
            already_paid = (log_count > 1)

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

    logger.info(
        f"Referral payment logged: referrer={referrer.telegram_id}, "
        f"referred={user.telegram_id}, amount={amount:.2f}, payment_id={payment_id}"
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
    from sqlalchemy.exc import IntegrityError
    from bot.models import BalanceTransactionKind, ReferralConfig, ReferralEarning, User

    if amount_rub <= 0:
        return

    if payment_id is not None:
        from bot.models import Payment, PaymentMethod
        payment = await session.get(Payment, payment_id)
        if payment and payment.method == PaymentMethod.BALANCE:
            logger.info(f"Skipping referral credit because payment {payment_id} was paid from balance.")
            return

        existing_earning = await session.scalar(
            _select(ReferralEarning).where(ReferralEarning.payment_id == payment_id).limit(1)
        )
        if existing_earning:
            logger.info(f"Referral already credited for payment {payment_id}: {existing_earning.amount_rub} RUB")
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

    try:
        async with session.begin_nested():
            session.add(
                ReferralEarning(
                    referrer_id=referrer.id,
                    referred_id=user_db_id,
                    payment_id=payment_id,
                    amount_rub=earning,
                )
            )
            await async_credit_user_balance(
                session,
                referrer,
                earning,
                BalanceTransactionKind.REFERRAL_BONUS,
                "Бонус за приглашение друга",
                source_type="payment",
                source_id=str(payment_id) if payment_id is not None else None,
            )
            await session.flush()
    except IntegrityError:
        if payment_id is not None:
            existing = await session.scalar(
                _select(ReferralEarning).where(ReferralEarning.payment_id == payment_id).limit(1)
            )
            if existing:
                logger.info(f"Referral already credited for payment {payment_id} (concurrent race)")
                return
        logger.error("Unexpected IntegrityError in credit_referral for payer %s, payment %s", user_db_id, payment_id)
        raise
    
    # Notify referrer about commission
    if bot and earning > 0:
        try:
            from bot.utils.texts import fmt_user
            user_info = fmt_user(user.telegram_id, user.username, user.full_name)
            pct = int(config.commission_percent) if config.commission_percent.is_integer() else config.commission_percent
            await bot.send_message(
                referrer.telegram_id,
                f"💰 Вам начислено <b>{earning}₽</b> ({pct}% от фактической оплаты {amount_rub:.2f}₽) на баланс за платеж пользователя {user_info}",
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
    """Credit partner commission if the paying user belongs to a partner chain."""
    from datetime import datetime
    from sqlalchemy import select as _select
    from bot.models import Partner, PartnerEarning, User

    if amount_rub <= 0:
        return

    if payment_id is not None:
        from bot.models import Payment, PaymentMethod
        payment = await session.get(Payment, payment_id)
        if payment and payment.method == PaymentMethod.BALANCE:
            logger.info(f"Skipping partner credit because payment {payment_id} was paid from balance.")
            return

        existing_earning = await session.scalar(
            _select(PartnerEarning).where(PartnerEarning.payment_id == payment_id).limit(1)
        )
        if existing_earning:
            logger.info(f"Partner commission already credited for payment {payment_id}: {existing_earning.amount} RUB")
            return

    user = await session.get(User, user_db_id)
    if not user:
        return

    partner_id = user.referral_root_partner_id or user.partner_id
    if not partner_id:
        return

    partner = await session.get(Partner, partner_id)
    if not partner or not partner.is_active:
        return
    if partner.valid_until and partner.valid_until < datetime.utcnow():
        return
    if partner.telegram_id and partner.telegram_id == user.telegram_id:
        return

    depth = user.referral_depth if (user.referral_depth and user.referral_depth > 0) else 1
    if depth == 1:
        pct = float(partner.commission_percent or 0.0)
    elif depth == 2:
        pct = float(partner.commission_l2_percent or 0.0)
    else:  # depth >= 3
        pct = float(partner.commission_l3_percent or 0.0)

    if pct <= 0:
        return

    earning = round(amount_rub * pct / 100, 2)
    if earning <= 0:
        return

    try:
        async with session.begin_nested():
            session.add(
                PartnerEarning(
                    partner_id=partner.id,
                    user_id=user_db_id,
                    payment_id=payment_id,
                    amount=earning,
                    depth=depth,
                    commission_percent=pct,
                )
            )
            partner.partner_balance = round((partner.partner_balance or 0.0) + earning, 2)
            await session.flush()
    except IntegrityError:
        if payment_id is not None:
            from sqlalchemy import select as _pe_sel
            existing = await session.scalar(
                _pe_sel(PartnerEarning).where(
                    PartnerEarning.payment_id == payment_id,
                    PartnerEarning.depth == depth,
                ).limit(1)
            )
            if existing:
                logger.info(f"Partner commission already credited for payment {payment_id} (concurrent race)")
                return
        logger.error("Unexpected IntegrityError in credit_partner for user %s, payment %s", user_db_id, payment_id)
        raise

    if bot and partner.telegram_id:
        try:
            from bot.utils.texts import fmt_user
            user_info = fmt_user(user.telegram_id, user.username, user.full_name)
            pct_fmt = int(pct) if pct.is_integer() else pct
            level_str = f"ур. {depth}" if depth > 1 else "1-й ур."
            await bot.send_message(
                partner.telegram_id,
                f"💰 Партнёрское начисление ({level_str}): <b>{earning}₽</b> ({pct_fmt}% от фактической оплаты {amount_rub:.2f}₽) за платеж пользователя {user_info}",
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to notify partner {partner.telegram_id}: {e}")

    logger.info(
        f"Partner credit (depth={depth}): partner={partner.name} (id={partner.id}), "
        f"user={user.telegram_id}, +{earning:.2f}₽ ({pct}%)"
    )


async def credit_cashback(
    session,
    user_db_id: int,
    payment_id: int | None,
    amount_rub: float,
    bot: Bot = None,
    source_type: str = "payment",
    source_id: str | None = None,
) -> dict[str, Any]:
    """Credit cashback to user balance for real money payments.

    Returns a dict:
        {"status": "credited" | "already_credited" | "disabled" | "not_eligible" | "failed", "cashback_rub": float}
    """
    from sqlalchemy import select
    from sqlalchemy.exc import IntegrityError
    from bot.models import BalanceTransaction, BalanceTransactionKind, BotSettings, Payment, PaymentMethod, User

    amount_rub = float(amount_rub or 0.0)
    if amount_rub <= 0:
        return {"status": "not_eligible", "cashback_rub": 0.0}

    if payment_id is not None:
        payment = await session.get(Payment, payment_id)
        if payment and payment.method == PaymentMethod.BALANCE:
            logger.info(f"Skipping cashback because payment {payment_id} was paid from balance.")
            return {"status": "not_eligible", "cashback_rub": 0.0}

    effective_source_type = str(source_type or "payment").strip()
    effective_source_id = (
        str(source_id).strip()
        if source_id is not None
        else (str(payment_id) if payment_id is not None else None)
    )

    if effective_source_id:
        existing_tx = await session.scalar(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == user_db_id,
                BalanceTransaction.kind == BalanceTransactionKind.CASHBACK,
                BalanceTransaction.source_type == effective_source_type,
                BalanceTransaction.source_id == effective_source_id,
            ).limit(1)
        )
        if existing_tx:
            logger.info(
                "Cashback already credited: user_db_id=%s, source=%s:%s, amount=%s",
                user_db_id,
                effective_source_type,
                effective_source_id,
                existing_tx.amount_rub,
            )
            return {"status": "already_credited", "cashback_rub": float(existing_tx.amount_rub)}

    cashback_enabled_row = await session.get(BotSettings, "cashback_enabled")
    if not cashback_enabled_row or cashback_enabled_row.value != "1":
        return {"status": "disabled", "cashback_rub": 0.0}

    cashback_pct_row = await session.get(BotSettings, "cashback_percent")
    try:
        cashback_pct = float(cashback_pct_row.value) if (cashback_pct_row and cashback_pct_row.value) else 10.0
    except (ValueError, TypeError):
        cashback_pct = 10.0

    if cashback_pct <= 0:
        return {"status": "disabled", "cashback_rub": 0.0}

    user = await session.get(User, user_db_id)
    if not user:
        return {"status": "failed", "cashback_rub": 0.0}

    cashback_amount = round(amount_rub * cashback_pct / 100, 2)
    if cashback_amount <= 0:
        return {"status": "not_eligible", "cashback_rub": 0.0}

    custom_tpl_row = await session.get(BotSettings, "cashback_notify_text")
    custom_tpl = custom_tpl_row.value if custom_tpl_row else None

    try:
        async with session.begin_nested():
            await async_credit_user_balance(
                session,
                user,
                cashback_amount,
                BalanceTransactionKind.CASHBACK,
                f"Кешбэк {int(cashback_pct) if cashback_pct.is_integer() else cashback_pct}% за оплату",
                source_type=effective_source_type,
                source_id=effective_source_id,
            )
            await session.flush()
    except IntegrityError:
        logger.info(
            "Cashback already credited: user_db_id=%s, source=%s:%s (concurrent race)",
            user_db_id,
            effective_source_type,
            effective_source_id,
        )
        existing_tx = await session.scalar(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == user_db_id,
                BalanceTransaction.kind == BalanceTransactionKind.CASHBACK,
                BalanceTransaction.source_type == effective_source_type,
                BalanceTransaction.source_id == effective_source_id,
            ).limit(1)
        )
        if existing_tx:
            return {"status": "already_credited", "cashback_rub": float(existing_tx.amount_rub)}
        logger.error("Unexpected IntegrityError during cashback crediting for user %s: no matching transaction found", user_db_id)
        raise

    pct_fmt = int(cashback_pct) if cashback_pct.is_integer() else cashback_pct
    default_notify_text = (
        f"🎁 Вам начислен кешбэк <b>+{cashback_amount:.2f} ₽</b> ({pct_fmt}% от оплаты {amount_rub:.2f} ₽) на ваш баланс!\n\n"
        f"Вы можете использовать баланс для оплаты и продления подписки."
    )
    notify_text = default_notify_text
    if custom_tpl:
        try:
            notify_text = custom_tpl.format(
                amount=f"{cashback_amount:.2f}",
                pct=str(pct_fmt),
                payment_amount=f"{amount_rub:.2f}",
            )
        except Exception as e:
            logger.warning("Failed to format custom cashback template %r: %s; using default", custom_tpl, e)
            notify_text = default_notify_text

    if bot and user.telegram_id:
        try:
            await bot.send_message(
                user.telegram_id,
                notify_text,
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Failed to notify user {user.telegram_id} about cashback: {e}")

    logger.info(f"Cashback credited: user={user.telegram_id}, +{cashback_amount:.2f}₽ ({cashback_pct}%)")
    return {
        "status": "credited",
        "cashback_rub": cashback_amount,
        "cashback_amount": cashback_amount,
        "cashback_pct": cashback_pct,
        "notify_text": notify_text,
    }


async def create_yookassa_payment(
    user_id: int,
    tariff_id: int = 0,
    platform: str = "",
    chat_id: int = 0,
    return_url: str = "",
    tariff_label: str = "",
    price_rub: float | str | int | None = None,
    *,
    price_kopecks: int | None = None,
    idempotence_key: str | None = None,
    local_payment_id: int | None = None,
    metadata: dict[str, Any] | None = None,
    description: str | None = None,
) -> tuple[str, str]:
    """Create a Yookassa payment. Returns (confirmation_url, yookassa_payment_id).

    Now accepts tariff_id instead of server_id+tariff_idx.
    tariff_label/price_rub can be passed in or left empty (will be looked up externally if needed).
    Supports custom metadata and description overrides.
    """
    import yookassa  # noqa: PLC0415

    if idempotence_key is None and local_payment_id is not None:
        idempotence_key = f"bot-payment-{local_payment_id}"
    if not idempotence_key or len(idempotence_key) > 64:
        raise ValueError("YooKassa payment creation requires a stable idempotence key")
    if price_kopecks is None:
        if price_rub is None:
            raise ValueError("YooKassa payment creation requires a canonical amount")
        price_kopecks = rub_to_kopecks(price_rub)
    if isinstance(price_kopecks, bool) or not isinstance(price_kopecks, int) or price_kopecks <= 0:
        raise ValueError("YooKassa payment amount must be positive integer kopecks")

    yookassa.Configuration.account_id = settings.yookassa_shop_id
    yookassa.Configuration.secret_key = settings.yookassa_secret_key

    def _create() -> yookassa.Payment:
        meta = {
            "user_id": str(user_id),
            "tariff_id": str(tariff_id),
            "platform": platform,
            "chat_id": str(chat_id),
        }
        if metadata:
            meta.update({str(k): str(v) for k, v in metadata.items()})

        desc = description or f"Весь интернет {tariff_label}"
        payload = {
            "amount": {"value": kopecks_to_rub_string(price_kopecks), "currency": "RUB"},
            "confirmation": {"type": "redirect", "return_url": return_url},
            "capture": True,
            "description": desc,
            "metadata": meta,
        }
        if settings.yookassa_save_payment_method and settings.recurring_payments_enabled:
            payload["save_payment_method"] = True
        return yookassa.Payment.create(payload, idempotence_key)

    payment = await asyncio.to_thread(_create)
    return payment.confirmation.confirmation_url, payment.id


async def create_recurring_yookassa_payment(
    payment_method_id: str,
    amount_rub: float | str | int | None,
    description: str,
    user_id: int,
    tariff_id: int,
    idempotence_key: str,
    *,
    amount_kopecks: int | None = None,
    telegram_user_id: int | None = None,
    platform: str | None = None,
    subscription_id: int | None = None,
    recurring_profile_id: int | None = None,
) -> tuple[str, str | None, str | None]:
    """Create auto-charge payment using saved payment_method_id.

    Returns (result, payment_id, yookassa_status).
    result: 'succeeded', 'pending', 'failed', 'deactivate', 'provider_error'.
    """
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

    if not idempotence_key or len(idempotence_key) > 64:
        raise ValueError("Recurring YooKassa payment requires a stable idempotence key")
    if amount_kopecks is None:
        if amount_rub is None:
            raise ValueError("Recurring YooKassa payment requires a canonical amount")
        amount_kopecks = rub_to_kopecks(amount_rub)
    if isinstance(amount_kopecks, bool) or not isinstance(amount_kopecks, int) or amount_kopecks <= 0:
        raise ValueError("Recurring YooKassa amount must be positive integer kopecks")

    metadata = {
        # Recurring payments use the same metadata contract as ordinary
        # subscription payments.  ``user_id`` is always the Telegram identity
        # consumed by reconciliation; the local DB identity is carried
        # separately for correlation and is never trusted on its own.
        "user_id": str(telegram_user_id if telegram_user_id is not None else user_id),
        "tariff_id": str(tariff_id),
        "recurring": "true",
        "operation_key": idempotence_key,
    }
    if platform:
        metadata["platform"] = str(platform)
    if subscription_id is not None:
        metadata["sub_id"] = str(subscription_id)
    if recurring_profile_id is not None:
        metadata["recurring_profile_id"] = str(recurring_profile_id)

    payload = {
        "amount": {"value": kopecks_to_rub_string(amount_kopecks), "currency": "RUB"},
        "capture": True,
        "payment_method_id": payment_method_id,
        "description": description,
        "metadata": metadata,
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
