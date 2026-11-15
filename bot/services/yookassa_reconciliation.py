"""YooKassa payment verification and reconciliation service.

Handles:
- Authenticated YooKassa payment status verification.
- Concurrency control with execution-unique lease tokens.
- Reconciliation of pending subscription, upgrade, topup, and device-slot payments.
- Idempotent fulfillment and financial side-effects committed before notifications.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import logging
import re
from typing import Any
from uuid import uuid4

from aiogram import Bot
from sqlalchemy import select, delete, or_, and_, func
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.database import async_session
from bot.services.notifications import notify_admins_issue, notify_admins_payment
from bot.models import (
    AdaptSubscription,
    AdminActionLog,
    BalanceTopUp,
    BalanceTransaction,
    BalanceTransactionKind,
    BotSettings,
    ManualSubscriptionOperation,
    PartnerEarning,
    Payment,
    PaymentMethod,
    PaymentNotificationOutbox,
    PaymentStatus,
    Platform,
    ProxyAccount,
    RecurringPaymentProfile,
    ReferralEarning,
    ReferralPaymentLog,
    SubStatus,
    Subscription,
    Tariff,
    TariffType,
    User,
)
from bot.services.balance_service import credit_user_balance, debit_user_balance, get_user_balance
from bot.services.cluster import LeaseHeartbeat, LeaseManager
from bot.services.money import (
    kopecks_to_rub_float,
    kopecks_to_rub_string,
    rub_to_kopecks,
)
from bot.services.payment_service import (
    credit_cashback,
    credit_partner,
    credit_referral,
    log_referral_payment,
)
from bot.services.payment_logger import plog
from bot.services.purchase_intent import decode_intent
from bot.services.adapt_api import ADAPT_MIN_CUSTOM_RENEW_DAYS, AdaptAPI, AdaptAPIError
from bot.services.subscription_service import (
    _disable_balance_autodebit_after_tariff_purchase,
    renew_adapt_subscription,
)
from bot.services.payment_notification_outbox import (
    enqueue_payment_notification,
    enqueue_subscription_delivery,
    process_payment_notification_outbox,
)
from bot.utils.texts import YOOKASSA_PAYMENT_RECEIVED

logger = logging.getLogger(__name__)

WAITING_FUNDS_PROVISIONING_CODES = frozenset({"vhq_balance", "adapt_api_402"})
PAYMENT_RETRY_DELAYS = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(minutes=30),
    timedelta(hours=1),
    timedelta(hours=2),
    timedelta(hours=6),
)


def _is_waiting_for_supplier_funds(payment_or_code: Payment | str | None) -> bool:
    code = (
        payment_or_code.provisioning_failure_code
        if isinstance(payment_or_code, Payment)
        else payment_or_code
    )
    return code in WAITING_FUNDS_PROVISIONING_CODES


def _schedule_payment_retry(
    payment: Payment,
    failure_code: str,
    *,
    now: datetime | None = None,
) -> None:
    payment.provisioning_failure_code = failure_code
    if not _is_waiting_for_supplier_funds(failure_code):
        payment.next_attempt_at = None
        return
    attempt = int(payment.reconciliation_attempts or 0)
    delay = PAYMENT_RETRY_DELAYS[min(attempt, len(PAYMENT_RETRY_DELAYS) - 1)]
    payment.reconciliation_attempts = attempt + 1
    payment.next_attempt_at = (now or datetime.utcnow()) + delay


def _reset_payment_retry(payment: Payment) -> None:
    payment.reconciliation_attempts = 0
    payment.next_attempt_at = None


async def _queue_customer_payment_ack(
    bot: Bot,
    *,
    payment_id: int,
    recipient_telegram_id: int,
) -> bool:
    """Persist and attempt the neutral acknowledgement without affecting fulfillment."""
    try:
        async with async_session() as session:
            payment = await session.get(Payment, payment_id)
            if not payment:
                return False
            owner = await session.get(User, payment.user_id)
            if not owner or int(owner.telegram_id) != int(recipient_telegram_id):
                logger.error(
                    "Refusing YooKassa acknowledgement for unverified recipient: payment=%s recipient=%s owner=%s",
                    payment_id,
                    recipient_telegram_id,
                    owner.telegram_id if owner else None,
                )
                return False
            if payment.telegram_chat_id is not None and int(payment.telegram_chat_id) != int(recipient_telegram_id):
                logger.error(
                    "Refusing YooKassa acknowledgement for conflicting stored chat: payment=%s recipient=%s stored_chat=%s",
                    payment_id,
                    recipient_telegram_id,
                    payment.telegram_chat_id,
                )
                return False
            existing_outbox = await session.scalar(
                select(PaymentNotificationOutbox)
                .where(PaymentNotificationOutbox.payment_id == payment.id)
                .where(PaymentNotificationOutbox.kind == "payment_received")
                .limit(1)
            )
            if existing_outbox and int(existing_outbox.recipient_telegram_id) != int(recipient_telegram_id):
                logger.error(
                    "Refusing YooKassa acknowledgement for conflicting outbox recipient: payment=%s recipient=%s outbox=%s",
                    payment_id,
                    recipient_telegram_id,
                    existing_outbox.recipient_telegram_id,
                )
                return False
            await enqueue_payment_notification(
                session,
                payment_id=payment.id,
                kind="payment_received",
                recipient_telegram_id=int(recipient_telegram_id),
                payload={"text": YOOKASSA_PAYMENT_RECEIVED},
            )
            await session.commit()
        await process_payment_notification_outbox(
            bot,
            payment_id=payment_id,
            limit=10,
            session_factory=async_session,
        )
        return True
    except Exception:
        logger.exception(
            "Unable to enqueue/process customer payment acknowledgement for payment %s",
            payment_id,
        )
        return False


async def queue_customer_payment_ack_for_provider(
    bot: Bot,
    *,
    provider_payment_id: str,
    recipient_telegram_id: int,
) -> bool:
    """Find a persisted YooKassa payment and enqueue its neutral customer acknowledgement."""
    try:
        async with async_session() as session:
            payment = await session.scalar(
                select(Payment).where(Payment.provider_payment_id == provider_payment_id).limit(1)
            )
            payment_id = payment.id if payment else None
    except Exception:
        logger.exception(
            "Unable to locate YooKassa payment %s for customer acknowledgement",
            provider_payment_id,
        )
        return False
    if payment_id is None:
        return False
    return await _queue_customer_payment_ack(
        bot,
        payment_id=payment_id,
        recipient_telegram_id=recipient_telegram_id,
    )


def _provider_value(obj: Any, key: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _provider_amount_value(amount: Any) -> Any:
    if isinstance(amount, dict):
        return amount.get("value")
    return getattr(amount, "value", None)


def _provider_amount_currency(amount: Any) -> Any:
    if isinstance(amount, dict):
        return amount.get("currency")
    return getattr(amount, "currency", None)


async def _persist_provider_snapshot(
    provider_payment_id: str,
    *,
    status: str | None,
    paid: bool | None,
    amount_kopecks: int | None,
    currency: str | None,
) -> None:
    """Persist authenticated provider truth before any fulfillment validation."""
    async with async_session() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == provider_payment_id)
        )
        topup = await session.scalar(
            select(BalanceTopUp).where(BalanceTopUp.provider_payment_id == provider_payment_id)
        )
        verified_at = datetime.utcnow()
        if payment:
            payment.provider_status = status
            payment.provider_paid = paid
            payment.provider_amount_kopecks = amount_kopecks
            payment.provider_currency = currency
            payment.provider_verified_at = verified_at
        if topup:
            topup.provider_status = status
            topup.provider_paid = paid
            topup.provider_amount_kopecks = amount_kopecks
            topup.provider_currency = currency
            topup.provider_verified_at = verified_at
        if payment or topup:
            await session.commit()


async def _mark_provider_validation_failure(provider_payment_id: str, code: str) -> None:
    """Persist a deterministic post-paid validation failure for DB-only recovery."""
    async with async_session() as session:
        payment = await session.scalar(
            select(Payment).where(Payment.provider_payment_id == provider_payment_id)
        )
        topup = await session.scalar(
            select(BalanceTopUp).where(BalanceTopUp.provider_payment_id == provider_payment_id)
        )
        if payment:
            payment.provisioning_failure_code = code
        if topup:
            topup.provider_failure_code = code
        if payment or topup:
            await session.commit()


async def _notify_provider_issue(
    bot: Bot,
    provider_payment_id: str,
    *,
    title: str,
    details: list[str],
) -> bool:
    """Notify staff and synchronize the durable incident tracker on success."""
    delivered = await notify_admins_issue(bot, title=title, details=details)
    if delivered:
        try:
            await _record_provider_alert_delivery(provider_payment_id)
        except Exception as exc:
            logger.warning(
                "Unable to record delivered YooKassa incident %s: %s",
                provider_payment_id,
                exc,
            )
    return delivered


async def _finalize_customer_payment_notification(
    bot: Bot,
    provider_payment_id: str,
    *,
    payment_id: int | None,
    recipient_telegram_id: int | None,
    required: bool,
    recipient_conflict: bool,
) -> None:
    """Finish the one customer-facing outcome after every verified payment exit."""
    if not required:
        return
    if payment_id is not None and recipient_telegram_id is not None and not recipient_conflict:
        queued = await _queue_customer_payment_ack(
            bot,
            payment_id=payment_id,
            recipient_telegram_id=recipient_telegram_id,
        )
        if not queued:
            logger.error(
                "Could not persist customer acknowledgement for verified YooKassa payment %s",
                provider_payment_id,
            )
        return

    if recipient_conflict:
        await _notify_provider_issue(
            bot,
            provider_payment_id,
            title="Подтверждена YooKassa-оплата, получатель не подтверждён",
            details=[
                f"PayId: {provider_payment_id}",
                f"Payment: #{payment_id or '—'}",
                "Клиентское уведомление не отправлено из-за конфликта идентификаторов получателя.",
                "Требуется ручная проверка привязки платежа к пользователю.",
            ],
        )
    else:
        logger.error(
            "Verified YooKassa payment %s has no durable customer notification target",
            provider_payment_id,
        )


async def _record_provider_alert_delivery(provider_payment_id: str) -> None:
    """Synchronize the scheduler's durable incident tracker after direct delivery."""
    async with async_session() as session:
        payment_id = await session.scalar(
            select(Payment.id).where(Payment.provider_payment_id == provider_payment_id)
        )
        topup_id = await session.scalar(
            select(BalanceTopUp.id).where(BalanceTopUp.provider_payment_id == provider_payment_id)
        )
        keys: set[str] = set()
        if payment_id is not None:
            keys.add(f"payment:{payment_id}")
        if topup_id is not None:
            keys.add(f"topup:{topup_id}")
        if not keys:
            return

        state_key = "problem_payments_alert_tracker"
        row = await session.get(BotSettings, state_key)
        data: dict[str, Any] = {}
        if row and row.value:
            try:
                data = json.loads(row.value)
            except Exception:
                data = {}
        alerted = set(data.get("alerted_keys", []))
        alerted.update(keys)
        data["alerted_keys"] = sorted(alerted)
        data["last_alert_at"] = datetime.utcnow().isoformat()
        if row:
            row.value = json.dumps(data)
        else:
            session.add(BotSettings(key=state_key, value=json.dumps(data)))
        await session.commit()


async def _terminalize_recurring_operation(
    session: AsyncSession,
    payment: Payment | None,
    *,
    failure_code: str,
) -> None:
    """Close only the recurring operation identified by a terminal provider result."""
    if not payment or not payment.provider_idempotence_key:
        return
    profile = await session.scalar(
        select(RecurringPaymentProfile).where(
            RecurringPaymentProfile.recurring_operation_key == payment.provider_idempotence_key,
        ).limit(1)
    )
    if not profile:
        return
    payment.status = PaymentStatus.FAILED
    payment.provisioning_failure_code = failure_code
    profile.payment_attempt_count = int(profile.payment_attempt_count or 0) + 1
    profile.last_payment_attempt = datetime.utcnow()
    profile.recurring_operation_key = None
    profile.recurring_operation_amount_kopecks = None
    profile.recurring_operation_description = None
    profile.recurring_operation_payment_method_id = None
    profile.recurring_operation_provider_payment_id = None
    profile.recurring_operation_started_at = None


async def _try_repair_legacy_amount(
    *,
    payment: Payment,
    provider_amount_kopecks: int,
    telegram_user_id: int,
    tariff_id: int,
    platform: str,
    provider_payment_id: str,
) -> bool:
    """Repair only the provable one-kopeck float truncation legacy shape."""
    if payment.amount >= provider_amount_kopecks or provider_amount_kopecks - payment.amount != 1:
        return False
    if payment.discount_kopecks is not None:
        return False
    if payment.method != PaymentMethod.YOOKASSA or payment.currency != "RUB":
        return False

    discount_kopecks = rub_to_kopecks(payment.discount_applied or 0)
    if discount_kopecks <= 0:
        return False

    from bot.services.purchase_intent import get_purchase_price_rub

    async with async_session() as session:
        current = await session.get(Payment, payment.id)
        if not current or current.provider_payment_id != provider_payment_id:
            return False
        user = await session.get(User, current.user_id)
        tariff = await session.get(Tariff, tariff_id)
        if not user or user.telegram_id != telegram_user_id or not tariff:
            return False
        _plain_platform, action, target_id = decode_intent(platform)
        try:
            purchase_rub = await get_purchase_price_rub(
                session,
                user=user,
                tariff=tariff,
                action=action,
                target_subscription_id=target_id,
            )
        except (ValueError, TypeError, RuntimeError):
            return False
        purchase_kopecks = rub_to_kopecks(purchase_rub)
        if current.purchase_kopecks is not None and current.purchase_kopecks != purchase_kopecks:
            return False
        expected = purchase_kopecks - discount_kopecks
        if expected != provider_amount_kopecks:
            return False
        current.amount = provider_amount_kopecks
        current.purchase_kopecks = purchase_kopecks
        current.discount_kopecks = discount_kopecks
        session.add(
            AdminActionLog(
                actor_telegram_id=0,
                action="repair_yookassa_legacy_amount",
                entity_type="payment",
                entity_id=str(current.id),
                target_telegram_id=telegram_user_id,
                status="success",
                summary="Repaired provable legacy YooKassa amount truncation",
                details=(
                    f"provider_payment_id={provider_payment_id}; local_before={payment.amount}; "
                    f"purchase_kopecks={purchase_kopecks}; discount_kopecks={discount_kopecks}; "
                    f"provider_amount_kopecks={provider_amount_kopecks}"
                ),
            )
        )
        await session.commit()
    logger.warning("Repaired provable legacy YooKassa amount for payment #%s", payment.id)
    return True

PRE_MUTATION_PROVISIONING_CODES = {
    "missing_server",
    "adapt_subscription_discovery_failed",
    "adapt_ambiguous_target",
    "adapt_target_not_found",
    "adapt_status_before_purchase_failed",
    "adapt_unfreeze_failed",
    "adapt_trial_requires_upgrade",
    "adapt_renew_plan_mismatch",
    "adapt_upgrade_same_plan",
    "tariff_not_found",
    "user_not_found",
    "vhq_unsupported_operation",
    "vhq_balance",
    "invalid_renewal_target",
    "adapt_api_402",
}

MANUAL_REVIEW_PROVISIONING_CODES = {
    "ambiguous_buy_mutation_manual_review_required",
    "ambiguous_renewal_manual_review_required",
    "ambiguous_competing_orders_at_supplier",
    "historical_payment_manual_review_required",
    "renewal_evidence_missing_manual_review_required",
    "partial_admin_renewal_manual_review_required",
    "manual_review_required",
    "target_sub_user_mismatch",
    "yookassa_create_ambiguous_manual_review",
    "payment_canceled_with_financial_effects_review_needed",
}


async def fetch_yookassa_payment(payment_id: str) -> Any | None:
    """Fetch verified payment object from YooKassa authenticated API."""
    import yookassa
    from yookassa.domain.exceptions import BadRequestError, NotFoundError

    if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
        logger.warning("YooKassa shop credentials not configured; cannot fetch payment %s", payment_id)
        return None

    yookassa.Configuration.account_id = settings.yookassa_shop_id
    yookassa.Configuration.secret_key = settings.yookassa_secret_key

    def _fetch():
        return yookassa.Payment.find_one(payment_id)

    try:
        return await asyncio.to_thread(_fetch)
    except (NotFoundError, BadRequestError):
        logger.warning("Payment %s not found in YooKassa", payment_id)
        return None
    except Exception as exc:
        logger.error("Failed to fetch payment %s from YooKassa: %s", payment_id, exc)
        raise


def _is_admin_log_for_payment(log: AdminActionLog, payment_id: int | None, provider_payment_id: str | None) -> bool:
    content = f"{log.summary or ''} {log.details or ''}"
    if provider_payment_id and provider_payment_id in content:
        return True
    if payment_id is not None:
        pattern = rf"(?:^|[^\d])#{payment_id}(?:[^\d]|$)|(?:\b(?:payment|платеж|платежа|платежу)\s+#?{payment_id}\b)"
        if re.search(pattern, content, re.IGNORECASE):
            return True
    return False


async def filter_real_competing_payments(candidate_payments: Any) -> list[Payment]:
    """Filter candidate competing payments to genuine competitors using authoritative state.

    Business semantics:
    - Local COMPLETED: genuine competitor.
    - Local PENDING:
        - Query authoritative provider state from YooKassa.
        - If status in ('pending', 'waiting_for_capture') and paid=False: NOT a competitor.
        - If status == 'canceled' and paid=False: NOT a competitor.
        - If status == 'succeeded' and paid=True: genuine competitor.
        - If provider error/timeout/malformed/missing: fail-closed (treat as competitor).
    - Other local statuses (FAILED, REFUNDED): not competitors.
    """
    real_competitors: list[Payment] = []
    for cp in candidate_payments:
        if cp.status == PaymentStatus.COMPLETED:
            real_competitors.append(cp)
            continue

        if cp.status == PaymentStatus.PENDING:
            if not cp.provider_payment_id:
                logger.warning(
                    "Competing payment %s has no provider_payment_id; failing closed as competitor",
                    cp.id,
                )
                real_competitors.append(cp)
                continue

            try:
                yk_comp = await asyncio.wait_for(
                    fetch_yookassa_payment(cp.provider_payment_id),
                    timeout=7.0,
                )
            except (asyncio.TimeoutError, Exception) as exc:
                logger.warning(
                    "Error fetching competing payment %s (%s) from YooKassa: %s; failing closed as competitor",
                    cp.id, cp.provider_payment_id, exc,
                )
                real_competitors.append(cp)
                continue

            if yk_comp is None:
                logger.warning(
                    "Competing payment %s (%s) returned None from YooKassa; failing closed as competitor",
                    cp.id, cp.provider_payment_id,
                )
                real_competitors.append(cp)
                continue

            if isinstance(yk_comp, dict):
                c_status = yk_comp.get("status")
                c_paid = bool(yk_comp.get("paid", False))
            else:
                c_status = getattr(yk_comp, "status", None)
                c_paid = bool(getattr(yk_comp, "paid", False))

            if c_status in ("pending", "waiting_for_capture") and not c_paid:
                logger.info(
                    "Competing candidate payment %s is unpaid at YooKassa (status=%s, paid=%s); ignoring as competitor",
                    cp.id, c_status, c_paid,
                )
                continue
            elif c_status == "canceled" and not c_paid:
                logger.info(
                    "Competing candidate payment %s is canceled at YooKassa (status=%s, paid=%s); ignoring as competitor",
                    cp.id, c_status, c_paid,
                )
                continue
            elif c_status == "succeeded" and c_paid:
                logger.warning(
                    "Competing candidate payment %s (%s) is succeeded and paid at YooKassa; treating as genuine competitor",
                    cp.id, cp.provider_payment_id,
                )
                real_competitors.append(cp)
            else:
                logger.warning(
                    "Competing candidate payment %s (%s) has unverified/unexpected status (%s, paid=%s); failing closed as competitor",
                    cp.id, cp.provider_payment_id, c_status, c_paid,
                )
                real_competitors.append(cp)
        else:
            continue

    return real_competitors


def _ensure_naive_utc(dt: Any) -> datetime | None:
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return None
    if isinstance(dt, datetime):
        if dt.tzinfo is not None:
            return dt.astimezone(timezone.utc).replace(tzinfo=None)
        return dt
    return None


async def _reconcile_ambiguous_adapt_mutation(
    session: AsyncSession,
    payment: Payment,
    provider_payment_id: str,
    effective_platform: str,
    effective_tariff_id: int,
    user_db_id: int,
    effective_user_tg_id: int,
) -> tuple[bool, int | None, str]:
    """Autonomously reconcile ambiguous Adapt mutation states.

    Guarantees:
    - Target state proven via authoritative GET -> 0 mutations, sync local DB, finalize.
    - Baseline state proven via authoritative GET -> exactly 1 safe retry mutation -> verify GET -> finalize.
    - Transient GET error (5xx/timeout) -> 0 mutations, bounded backoff, fail safe.
    - Contradictory state (neither baseline nor target) -> 0 mutations, fail closed for manual review.
    - Genuine competing payments -> 0 mutations, fail closed with ambiguous_competing_payments_for_subscription.
    """
    _, plat_action, target_sub_id = decode_intent(effective_platform)
    if not target_sub_id and effective_platform and ("~u~" in effective_platform or "~r~" in effective_platform):
        try:
            target_sub_id = int(effective_platform.split("~")[-1])
        except (ValueError, IndexError):
            target_sub_id = None

    if not target_sub_id:
        return False, None, "unsupported_ambiguous_mutation"

    if plat_action not in ("renew", "upgrade"):
        if "~u~" in effective_platform:
            plat_action = "upgrade"
        elif "~r~" in effective_platform:
            plat_action = "renew"
        else:
            return False, None, "unsupported_ambiguous_mutation"

    target_sub = await session.get(Subscription, target_sub_id)
    if not target_sub:
        payment.provisioning_failure_code = "target_subscription_not_found"
        await session.commit()
        return False, None, "target_subscription_not_found"

    if target_sub.user_id != user_db_id:
        payment.provisioning_failure_code = "target_sub_user_mismatch"
        await session.commit()
        return False, None, "target_sub_user_mismatch"

    target_tariff = await session.get(Tariff, effective_tariff_id) if effective_tariff_id else None
    if not target_tariff:
        return False, None, "tariff_not_found"

    adapt_rec = await session.scalar(
        select(AdaptSubscription).where(AdaptSubscription.subscription_id == target_sub.id)
    )
    if not adapt_rec or not adapt_rec.adapt_uuid:
        logger.warning("Subscription %s is not an Adapt subscription; skipping Adapt recovery", target_sub.id)
        return False, None, "non_adapt_unresolved_supplier_mutation"

    now_utc = datetime.utcnow()
    p_created = payment.created_at or now_utc

    # Pre-mutation safety check: check for genuine competing payments
    candidate_competing_payments = (await session.execute(
        select(Payment).where(
            Payment.id != payment.id,
            Payment.user_id == user_db_id,
            or_(
                Payment.subscription_id == target_sub.id,
                Payment.platform.like(f"%~u~{target_sub.id}%"),
                Payment.platform.like(f"%~r~{target_sub.id}%"),
            ),
            Payment.status.in_([PaymentStatus.COMPLETED, PaymentStatus.PENDING]),
            Payment.created_at.between(p_created - timedelta(days=2), p_created + timedelta(days=2)),
        )
    )).scalars().all()
    competing_payments = await filter_real_competing_payments(candidate_competing_payments)
    if competing_payments:
        logger.warning(
            "Payment %s has %s competing payments for sub %s; refusing mutation retry",
            provider_payment_id, len(competing_payments), target_sub.id,
        )
        payment.provisioning_failure_code = "ambiguous_competing_payments_for_subscription"
        await session.commit()
        return False, None, "ambiguous_competing_payments_for_subscription"

    api = AdaptAPI()

    if plat_action == "upgrade":
        baseline_plan_uuid = str(
            getattr(payment, "provisioning_baseline_plan_uuid", None)
            or adapt_rec.adapt_plan_uuid
            or ""
        ).strip()
        target_plan_uuid = str(target_tariff.adapt_plan_uuid or "").strip()

        if not target_plan_uuid:
            logger.error("Target tariff %s missing adapt_plan_uuid for upgrade", target_tariff.id)
            return False, None, "missing_target_plan_uuid"

        if not payment.provisioning_baseline_plan_uuid and baseline_plan_uuid:
            payment.provisioning_baseline_plan_uuid = baseline_plan_uuid
            await session.commit()

        # Step A: Authoritative GET
        try:
            status = await api.get_status(adapt_rec.adapt_uuid)
        except Exception as exc:
            logger.warning(
                "Adapt get_status failed for sub %s (%s): %s; backing off without mutation",
                target_sub.id, adapt_rec.adapt_uuid, exc,
            )
            return False, None, "supplier_status_temporarily_unavailable"

        if not isinstance(status, dict) or not status.get("success", True):
            logger.warning("Adapt get_status returned unsuccessful payload for sub %s: %s", target_sub.id, status)
            return False, None, "supplier_status_temporarily_unavailable"

        status_plan_uuid = str(status.get("plan_uuid") or "").strip()
        status_end = _ensure_naive_utc(status.get("end_date"))

        # Step B: Check target state
        if status_plan_uuid == target_plan_uuid:
            logger.info("Adapt sub %s is already in target plan %s; 0 mutations needed", adapt_rec.adapt_uuid, target_plan_uuid)
            target_sub.tariff_id = target_tariff.id
            target_sub.tariff_days = target_tariff.days
            target_sub.tariff_months = target_tariff.days // 30
            target_sub.billing_mode = "tariff"
            target_sub.status = SubStatus.ACTIVE
            dev_count = getattr(target_tariff, "device_count", None) or status.get("devices")
            if dev_count:
                target_sub.device_slots = int(dev_count)
            if status_end:
                target_sub.expires_at = status_end
                adapt_rec.end_date = status_end
            adapt_rec.adapt_plan_uuid = target_plan_uuid
            user = await session.get(User, payment.user_id)
            if user:
                _disable_balance_autodebit_after_tariff_purchase(user)
            payment.subscription_id = target_sub.id
            payment.provisioning_failure_code = "financial_finalization_pending"
            await session.commit()
            return True, target_sub.id, "target_state_verified"

        # Step C: Check baseline state
        elif baseline_plan_uuid and status_plan_uuid == baseline_plan_uuid:
            logger.info(
                "Adapt sub %s is confirmed at baseline plan %s; executing exactly 1 retry upgrade to %s",
                adapt_rec.adapt_uuid, baseline_plan_uuid, target_plan_uuid,
            )
            try:
                await api.upgrade_subscription(adapt_rec.adapt_uuid, target_plan_uuid)
            except AdaptAPIError as upgrade_err:
                if upgrade_err.status == 402:
                    _schedule_payment_retry(payment, "adapt_api_402")
                    await session.commit()
                    return False, None, "adapt_api_402"
                logger.warning("Retry upgrade POST threw: %s", upgrade_err)
            except Exception as upgrade_err:
                if "Not enough days" in str(upgrade_err):
                    try:
                        await api.renew_subscription_custom(adapt_rec.adapt_uuid, ADAPT_MIN_CUSTOM_RENEW_DAYS)
                        await api.upgrade_subscription(adapt_rec.adapt_uuid, target_plan_uuid)
                    except Exception as renew_exc:
                        if isinstance(renew_exc, AdaptAPIError) and renew_exc.status == 402:
                            _schedule_payment_retry(payment, "adapt_api_402")
                            await session.commit()
                            return False, None, "adapt_api_402"
                        logger.warning("Custom renewal before upgrade failed: %s", renew_exc)
                else:
                    logger.warning("Retry upgrade POST threw: %s", upgrade_err)

            # Immediately authoritative GET after retry POST
            try:
                post_status = await api.get_status(adapt_rec.adapt_uuid)
                post_plan = str(post_status.get("plan_uuid") or "").strip()
                post_end = _ensure_naive_utc(post_status.get("end_date"))
            except Exception as post_exc:
                post_plan = None
                post_end = None
                logger.warning("Post-retry get_status failed: %s", post_exc)

            if post_plan == target_plan_uuid:
                logger.info("Retry upgrade succeeded; sub %s verified in target plan %s", adapt_rec.adapt_uuid, target_plan_uuid)
                target_sub.tariff_id = target_tariff.id
                target_sub.tariff_days = target_tariff.days
                target_sub.tariff_months = target_tariff.days // 30
                target_sub.billing_mode = "tariff"
                target_sub.status = SubStatus.ACTIVE
                post_dev = getattr(target_tariff, "device_count", None) or (post_status.get("devices") if post_status else None)
                if post_dev:
                    target_sub.device_slots = int(post_dev)
                if post_end:
                    target_sub.expires_at = post_end
                    adapt_rec.end_date = post_end
                adapt_rec.adapt_plan_uuid = target_plan_uuid
                user = await session.get(User, payment.user_id)
                if user:
                    _disable_balance_autodebit_after_tariff_purchase(user)
                payment.subscription_id = target_sub.id
                payment.provisioning_failure_code = "financial_finalization_pending"
                await session.commit()
                return True, target_sub.id, "retry_mutation_succeeded"
            else:
                logger.error("Retry upgrade did not achieve target plan (post_plan=%s)", post_plan)
                payment.provisioning_failure_code = "uncertain_supplier_mutation"
                await session.commit()
                return False, None, "retry_mutation_failed"

        # Step D: Contradictory state (neither target nor baseline)
        else:
            logger.error(
                "Adapt sub %s has contradictory plan %s (baseline=%s, target=%s); failing closed",
                adapt_rec.adapt_uuid, status_plan_uuid, baseline_plan_uuid, target_plan_uuid,
            )
            payment.provisioning_failure_code = "contradictory_supplier_state"
            await session.commit()
            return False, None, "contradictory_supplier_state"

    elif plat_action == "renew":
        expected_days = target_tariff.days if target_tariff else (target_sub.tariff_days or 30)
        required_extension = timedelta(days=expected_days) - timedelta(hours=2)
        baseline_end = _ensure_naive_utc(
            getattr(payment, "provisioning_baseline_expires_at", None)
            or adapt_rec.end_date
            or target_sub.expires_at
        )
        if not payment.provisioning_baseline_expires_at and baseline_end:
            payment.provisioning_baseline_expires_at = baseline_end
            await session.commit()

        # Step A: Authoritative GET
        try:
            status = await api.get_status(adapt_rec.adapt_uuid)
        except Exception as exc:
            logger.warning(
                "Adapt get_status failed for renew sub %s (%s): %s; backing off without mutation",
                target_sub.id, adapt_rec.adapt_uuid, exc,
            )
            return False, None, "supplier_status_temporarily_unavailable"

        if not isinstance(status, dict) or not status.get("success", True):
            logger.warning("Adapt get_status returned unsuccessful payload for renew sub %s: %s", target_sub.id, status)
            return False, None, "supplier_status_temporarily_unavailable"

        status_end = _ensure_naive_utc(status.get("end_date"))

        # Step B: Check target state
        if baseline_end and status_end and status_end >= baseline_end + required_extension:
            logger.info("Adapt sub %s is already extended to %s; 0 mutations needed", adapt_rec.adapt_uuid, status_end)
            target_sub.status = SubStatus.ACTIVE
            target_sub.expires_at = status_end
            adapt_rec.end_date = status_end
            user = await session.get(User, payment.user_id)
            if user:
                _disable_balance_autodebit_after_tariff_purchase(user)
            payment.subscription_id = target_sub.id
            payment.provisioning_failure_code = "financial_finalization_pending"
            await session.commit()
            return True, target_sub.id, "target_state_verified"

        # Step C: Check baseline state
        elif baseline_end and status_end and (status_end < baseline_end + timedelta(hours=2)) and (status_end >= baseline_end - timedelta(hours=2)):
            logger.info("Adapt sub %s is confirmed at baseline expiry %s; executing exactly 1 retry renewal", adapt_rec.adapt_uuid, status_end)
            try:
                await renew_adapt_subscription(
                    session, adapt_record=adapt_rec, tariff_days=expected_days, manage_freeze=False,
                )
            except AdaptAPIError as renew_exc:
                if renew_exc.status == 402:
                    _schedule_payment_retry(payment, "adapt_api_402")
                    await session.commit()
                    return False, None, "adapt_api_402"
                logger.warning("Retry renewal POST threw: %s", renew_exc)
            except Exception as renew_exc:
                logger.warning("Retry renewal POST threw: %s", renew_exc)

            # Immediately authoritative GET after retry POST
            try:
                post_status = await api.get_status(adapt_rec.adapt_uuid)
                post_end = _ensure_naive_utc(post_status.get("end_date"))
            except Exception as post_exc:
                post_end = None
                logger.warning("Post-retry renew get_status failed: %s", post_exc)

            if baseline_end and post_end and post_end >= baseline_end + required_extension:
                logger.info("Retry renew succeeded; sub %s verified extended to %s", adapt_rec.adapt_uuid, post_end)
                target_sub.status = SubStatus.ACTIVE
                target_sub.expires_at = post_end
                adapt_rec.end_date = post_end
                user = await session.get(User, payment.user_id)
                if user:
                    _disable_balance_autodebit_after_tariff_purchase(user)
                payment.subscription_id = target_sub.id
                payment.provisioning_failure_code = "financial_finalization_pending"
                await session.commit()
                return True, target_sub.id, "retry_mutation_succeeded"
            else:
                logger.error("Retry renew did not achieve target extension (post_end=%s, baseline=%s)", post_end, baseline_end)
                payment.provisioning_failure_code = "uncertain_supplier_mutation"
                await session.commit()
                return False, None, "retry_mutation_failed"

        # Step D: Contradictory state
        else:
            logger.error(
                "Adapt sub %s has contradictory expiry %s (baseline=%s); failing closed",
                adapt_rec.adapt_uuid, status_end, baseline_end,
            )
            payment.provisioning_failure_code = "contradictory_supplier_state"
            await session.commit()
            return False, None, "contradictory_supplier_state"

    return False, None, "unsupported_ambiguous_mutation"


async def _reconcile_ambiguous_vhq_mutation(
    session: AsyncSession,
    payment: Payment,
    provider_payment_id: str,
    effective_platform: str,
    effective_tariff_id: int,
    user_db_id: int,
    effective_user_tg_id: int,
    api: Any = None,
) -> tuple[bool, int | None, str]:
    """Autonomously reconcile ambiguous VHQ mutation states.

    Guarantees:
    - Authoritative GET /orders confirms renewal was applied -> sync local DB, finalize.
    - Authoritative GET /orders confirms zero mutation occurred (no order created after payment timestamp)
      -> check balance:
         - If balance < required cost -> mark vhq_balance cleanly.
         - If balance >= required cost -> mark vhq_balance and allow clean retry.
    - Genuine competing payments/orders -> fail closed for manual review.
    """
    from bot.services.vhq_partner_api import VHQPartnerAPI
    from bot.services.vhq_routing import get_vhq_spec_for_tariff
    from bot.services.subscription_service import _match_supplier_order_by_key

    target_tariff = await session.get(Tariff, effective_tariff_id) if effective_tariff_id else None
    if not target_tariff:
        return False, None, "tariff_not_found"

    if api is None:
        api = VHQPartnerAPI()
    if not api.enabled:
        return False, None, "vhq_api_not_configured"

    try:
        orders_resp = await api.get_orders()
        orders_list = (
            orders_resp
            if isinstance(orders_resp, list)
            else (orders_resp.get("orders", []) if isinstance(orders_resp, dict) else [])
        )
    except Exception as exc:
        logger.warning("VHQ get_orders failed during reconciliation for payment %s: %s", provider_payment_id, exc)
        return False, None, "vhq_get_orders_failed"

    current_balance = None
    bal_resp = None
    try:
        bal_resp = await api.get_balance()
        if isinstance(bal_resp, dict) and "balance" in bal_resp:
            current_balance = float(bal_resp["balance"])
    except Exception as exc:
        logger.warning("VHQ get_balance failed during reconciliation for payment %s: %s", provider_payment_id, exc)

    from bot.services.problem_payments import get_vhq_wholesale_cost
    vhq_spec = get_vhq_spec_for_tariff(target_tariff)
    tier = str(vhq_spec.get("tier", "basic")) if vhq_spec else "basic"
    required_cost = get_vhq_wholesale_cost(target_tariff)
    if isinstance(bal_resp, dict) and "pricing" in bal_resp:
        pricing = bal_resp.get("pricing", {}).get(tier, [])
        days = int(vhq_spec.get("days", 30)) if vhq_spec else 30
        for item in pricing:
            if isinstance(item, dict) and item.get("days") == days and "price" in item:
                required_cost = float(item["price"])
                break

    _, plat_action, target_sub_id = decode_intent(effective_platform)
    if not target_sub_id and effective_platform:
        for code_char, act in (("~r~", "renew"), ("~u~", "upgrade"), ("~n~", "new")):
            if code_char in effective_platform:
                try:
                    target_sub_id = int(effective_platform.split("~")[-1])
                    plat_action = act
                    break
                except (ValueError, IndexError):
                    pass

    # Case A: Renewal of existing subscription
    if plat_action in ("renew", "upgrade") and target_sub_id:
        target_sub = await session.get(Subscription, target_sub_id)
        if not target_sub:
            payment.provisioning_failure_code = "target_subscription_not_found"
            await session.commit()
            return False, None, "target_subscription_not_found"
        if target_sub.user_id != user_db_id:
            payment.provisioning_failure_code = "target_sub_user_mismatch"
            await session.commit()
            return False, None, "target_sub_user_mismatch"

        target_order_id = target_sub.vhq_order_id
        matched = None
        if target_order_id:
            matched = next(
                (o for o in orders_list if str(o.get("order_id") or o.get("id") or "").strip() == str(target_order_id).strip()),
                None,
            )
        if not matched and target_sub.vpn_key:
            matched = _match_supplier_order_by_key(orders_list, target_sub.vpn_key)

        if matched:
            sup_active = _ensure_naive_utc(matched.get("active_until"))
            baseline_exp = payment.provisioning_baseline_expires_at or target_sub.expires_at
            expected_days = target_tariff.days if target_tariff else 30
            required_extension = timedelta(days=expected_days) - timedelta(hours=2)
            if sup_active and baseline_exp and sup_active >= baseline_exp + required_extension:
                target_sub.vhq_active_until = sup_active
                target_sub.expires_at = max(target_sub.expires_at, sup_active) if target_sub.expires_at else sup_active
                target_sub.status = SubStatus.ACTIVE
                payment.subscription_id = target_sub.id
                payment.provisioning_failure_code = "financial_finalization_pending"
                await session.commit()
                return True, target_sub.id, "vhq_renewal_proven_applied"

        payment.provisioning_failure_code = "ambiguous_renewal_manual_review_required"
        await session.commit()
        return False, None, "ambiguous_renewal_manual_review_required"

    # Case B: Buy flow (new subscription)
    p_created = payment.created_at or datetime.utcnow()
    recent_orders = []
    for o in orders_list:
        c_dt = _ensure_naive_utc(o.get("created_at"))
        if c_dt and c_dt >= p_created - timedelta(minutes=1):
            recent_orders.append(o)

    if not recent_orders:
        # We CANNOT prove absence of order merely from get_orders + balance!
        # An unknown operation must remain locked until authoritative proof.
        payment.provisioning_failure_code = "ambiguous_buy_mutation_manual_review_required"
        await session.commit()
        return False, None, "ambiguous_buy_mutation_manual_review_required"
    else:
        payment.provisioning_failure_code = "ambiguous_competing_orders_at_supplier"
        await session.commit()
        return False, None, "ambiguous_competing_orders_at_supplier"


async def verify_and_fulfill_yookassa_payment(
    bot_or_app: Any,
    provider_payment_id: str,
    source: str = "webhook",
    unverified_webhook_payload: dict | None = None,
    *,
    existing_lease: LeaseManager | None = None,
    allow_manual_review_override: bool = False,
) -> tuple[bool, str]:
    """Verify YooKassa payment via authenticated API and fulfill purchased service.

    Returns (success: bool, status_code_or_message: str).
    """
    if not provider_payment_id:
        return False, "missing_provider_payment_id"

    # Resolve bot and app handles
    if hasattr(bot_or_app, "__getitem__") and "bot" in bot_or_app:
        bot = bot_or_app["bot"]
        app = bot_or_app
    else:
        bot = bot_or_app
        app = {"bot": bot}

    # 1. Acquire execution-unique lease or reuse parent execution lease
    lock_name = f"payment:yookassa:{provider_payment_id}"
    ttl = getattr(settings, "webhook_lock_ttl_seconds", 120) or 120

    if existing_lease is not None:
        lease_manager = existing_lease
        heartbeat = None
    else:
        owner_token = f"{settings.instance_id}:{source}:{uuid4().hex[:8]}"
        lease_manager = LeaseManager(owner_id=owner_token)
        acquired = await lease_manager.acquire_or_renew(lock_name, ttl_seconds=ttl)
        if not acquired:
            if source == "webhook":
                from bot.webhooks import _wait_for_completed_payment
                wait_secs = getattr(settings, "webhook_lock_wait_seconds", 10) or 10
                if await _wait_for_completed_payment(provider_payment_id, wait_secs):
                    logger.info("YooKassa payment %s completed by concurrent worker", provider_payment_id)
                    return True, "already_completed"
            logger.info("YooKassa payment %s locked by another worker (%s)", provider_payment_id, source)
            return False, "locked_by_another_execution"

        heartbeat = LeaseHeartbeat(lease_manager, lock_name, ttl)
        heartbeat.start()

    customer_ack_required = False
    customer_ack_payment_id: int | None = None
    customer_ack_recipient_id: int | None = None
    customer_ack_recipient_conflict = False

    try:
        # 2. Check local DB state for already-completed fulfillment
        async with async_session() as session:
            existing_payment = await session.scalar(
                select(Payment).where(Payment.provider_payment_id == provider_payment_id)
            )
            existing_topup = await session.scalar(
                select(BalanceTopUp).where(BalanceTopUp.provider_payment_id == provider_payment_id)
            )
            if existing_payment and existing_payment.status == PaymentStatus.COMPLETED and (
                existing_payment.subscription_id is not None or existing_payment.platform == "device"
            ):
                if existing_payment.provisioning_failure_code == "financial_finalization_pending":
                    logger.info("Payment %s has pending financial finalization; resuming", provider_payment_id)
                else:
                    logger.info("Payment %s already completed in DB", provider_payment_id)
                    return True, "already_completed"

            if existing_topup and existing_topup.status == "completed":
                logger.info("BalanceTopUp %s already completed in DB", provider_payment_id)
                return True, "already_completed"

        # 3. Authenticated YooKassa verification
        try:
            yk_payment = await asyncio.wait_for(
                fetch_yookassa_payment(provider_payment_id),
                timeout=7.0,
            )
        except (asyncio.TimeoutError, Exception) as exc:
            logger.error("Transient error fetching payment %s: %s", provider_payment_id, exc)
            return False, "provider_unavailable"

        if not yk_payment:
            logger.warning("Payment %s not found in YooKassa", provider_payment_id)
            return False, "payment_not_found"

        if isinstance(yk_payment, dict):
            yk_status = yk_payment.get("status")
            yk_paid = yk_payment.get("paid")
            yk_amount = yk_payment.get("amount", {})
            raw_amount_value = _provider_amount_value(yk_amount)
            raw_currency = _provider_amount_currency(yk_amount)
            meta = yk_payment.get("metadata", {}) or {}
            payment_method_data = yk_payment.get("payment_method", {}) or {}
        else:
            yk_status = getattr(yk_payment, "status", None)
            yk_paid = getattr(yk_payment, "paid", None)
            yk_amount = getattr(yk_payment, "amount", None)
            raw_amount_value = _provider_amount_value(yk_amount) if yk_amount else None
            raw_currency = _provider_amount_currency(yk_amount) if yk_amount else None
            meta = getattr(yk_payment, "metadata", {}) or {}
            if hasattr(meta, "to_dict"):
                meta = meta.to_dict()
            elif not isinstance(meta, dict):
                meta = dict(meta) if meta else {}
            pm = getattr(yk_payment, "payment_method", None)
            if hasattr(pm, "to_dict"):
                payment_method_data = pm.to_dict()
            elif isinstance(pm, dict):
                payment_method_data = pm
            else:
                payment_method_data = {}

        try:
            provider_amount_kopecks = (
                rub_to_kopecks(raw_amount_value) if raw_amount_value is not None else None
            )
        except ValueError:
            provider_amount_kopecks = None
        amount_val = kopecks_to_rub_float(provider_amount_kopecks or 0)
        currency_val = str(raw_currency).strip().upper() if raw_currency else None
        await _persist_provider_snapshot(
            provider_payment_id,
            status=yk_status,
            paid=bool(yk_paid) if yk_paid is not None else None,
            amount_kopecks=provider_amount_kopecks,
            currency=currency_val,
        )

        if not raw_currency:
            logger.error("Payment %s missing currency in provider amount", provider_payment_id)
            if yk_status == "succeeded" and yk_paid:
                await _mark_provider_validation_failure(provider_payment_id, "yookassa_currency_missing")
            return False, "currency_missing"

        if provider_amount_kopecks is None:
            logger.error("Payment %s has invalid provider amount", provider_payment_id)
            if yk_status == "succeeded" and yk_paid:
                await _mark_provider_validation_failure(provider_payment_id, "yookassa_amount_invalid")
            return False, "amount_invalid"

        if yk_status == "canceled":
            async with async_session() as session:
                payment_row = await session.scalar(
                    select(Payment).where(Payment.provider_payment_id == provider_payment_id)
                )
                topup_row = await session.scalar(
                    select(BalanceTopUp).where(BalanceTopUp.provider_payment_id == provider_payment_id)
                )
                if topup_row and topup_row.status in ("pending", "completed"):
                    topup_row.status = "canceled"

                if payment_row and (payment_row.status == PaymentStatus.PENDING or not payment_row.subscription_id):
                    has_ref_log = await session.scalar(
                        select(ReferralPaymentLog.id).where(ReferralPaymentLog.payment_id == payment_row.id).limit(1)
                    )
                    has_ref_earning = await session.scalar(
                        select(ReferralEarning.id).where(ReferralEarning.payment_id == payment_row.id).limit(1)
                    )
                    has_partner_earning = await session.scalar(
                        select(PartnerEarning.id).where(PartnerEarning.payment_id == payment_row.id).limit(1)
                    )

                    if has_ref_log or has_ref_earning or has_partner_earning:
                        # Existing financial side effects are present in the ledger.
                        # Retain historical records to preserve audit trail; flag for manual admin review.
                        payment_row.status = PaymentStatus.FAILED
                        payment_row.provisioning_failure_code = "payment_canceled_with_financial_effects_review_needed"
                        await _terminalize_recurring_operation(
                            session,
                            payment_row,
                            failure_code="payment_canceled_with_financial_effects_review_needed",
                        )
                        await session.commit()
                        logger.warning(
                            "Payment %s is canceled at YooKassa but has existing financial ledger side-effects; "
                            "preserving records for administrator review",
                            provider_payment_id,
                        )
                        return False, "payment_canceled_with_financial_effects_review_needed"

                    payment_row.status = PaymentStatus.FAILED
                    payment_row.provisioning_failure_code = "payment_canceled_at_provider"

                    # Refund debited discount if any was deducted
                    discount_tx = await session.scalar(
                        select(BalanceTransaction).where(
                            BalanceTransaction.user_id == payment_row.user_id,
                            BalanceTransaction.source_type == "payment_discount",
                            BalanceTransaction.source_id == provider_payment_id,
                        ).limit(1)
                    )
                    if discount_tx and discount_tx.amount_rub > 0:
                        refund_exists = await session.scalar(
                            select(BalanceTransaction).where(
                                BalanceTransaction.user_id == payment_row.user_id,
                                BalanceTransaction.source_type == "payment_discount_refund",
                                BalanceTransaction.source_id == provider_payment_id,
                            ).limit(1)
                        )
                        if not refund_exists:
                            u = await session.get(User, payment_row.user_id)
                            if u:
                                credit_user_balance(
                                    session,
                                    u,
                                    discount_tx.amount_rub,
                                    BalanceTransactionKind.REFUND,
                                    f"Возврат списания по отмененному платежу {provider_payment_id}",
                                    source_type="payment_discount_refund",
                                    source_id=provider_payment_id,
                                )

                    await _terminalize_recurring_operation(
                        session,
                        payment_row,
                        failure_code="payment_canceled_at_provider",
                    )

                await session.commit()
            logger.info("Payment %s canceled at YooKassa; updated local status to FAILED/canceled", provider_payment_id)
            return False, "payment_canceled"

        if yk_status in ("pending", "waiting_for_capture"):
            logger.info("Payment %s is still pending at YooKassa (%s)", provider_payment_id, yk_status)
            return False, "payment_still_pending"

        if yk_status in ("failed", "expired"):
            async with async_session() as session:
                payment_row = await session.scalar(
                    select(Payment).where(Payment.provider_payment_id == provider_payment_id)
                )
                await _terminalize_recurring_operation(
                    session,
                    payment_row,
                    failure_code="recurring_provider_failed",
                )
                await session.commit()
            logger.warning("Payment %s reached terminal YooKassa status %s", provider_payment_id, yk_status)
            return False, f"payment_not_succeeded:{yk_status}"

        if yk_status != "succeeded" or not yk_paid:
            logger.warning("Payment %s not succeeded at YooKassa (status=%s, paid=%s)", provider_payment_id, yk_status, yk_paid)
            return False, f"payment_not_succeeded:{yk_status}"

        if currency_val != "RUB":
            err_msg = f"Payment {provider_payment_id} currency mismatch: {currency_val} (expected RUB)"
            logger.error(err_msg)
            await _notify_provider_issue(
                bot,
                provider_payment_id,
                title="Несовпадение валюты платежа YooKassa",
                details=[f"PayId: {provider_payment_id}", f"Currency: {currency_val}", f"Amount: {amount_val}"],
            )
            await _mark_provider_validation_failure(provider_payment_id, "yookassa_currency_mismatch")
            return False, "currency_mismatch"

        # 4. Parse metadata
        try:
            telegram_user_id = int(meta.get("user_id", 0)) if meta.get("user_id") else None
            chat_id = int(meta.get("chat_id", 0)) if meta.get("chat_id") else telegram_user_id
            purpose = str(meta.get("purpose", "")).strip()
            is_device = str(meta.get("is_device", "")).strip() == "1"
            sub_id = int(meta.get("sub_id", 0)) if meta.get("sub_id") else None
            tariff_id = int(meta.get("tariff_id", 0)) if meta.get("tariff_id") else None
            platform_str = str(meta.get("platform", "")).strip() or None
            is_recurring = str(meta.get("recurring", "")).strip().lower() == "true"
            recurring_profile_id = int(meta.get("recurring_profile_id", 0)) if meta.get("recurring_profile_id") else None
            recurring_operation_key = str(meta.get("operation_key", "")).strip() or None
        except (ValueError, TypeError) as exc:
            logger.error("Invalid YooKassa metadata for %s: %s (meta=%s)", provider_payment_id, exc, meta)
            await _mark_provider_validation_failure(provider_payment_id, "yookassa_invalid_metadata")
            return False, "invalid_metadata"

        if telegram_user_id and chat_id and telegram_user_id != chat_id:
            customer_ack_recipient_conflict = True
            await _mark_provider_validation_failure(provider_payment_id, "yookassa_recipient_mismatch")
            await _notify_provider_issue(
                bot,
                provider_payment_id,
                title="Несовпадение получателя YooKassa",
                details=[
                    f"PayId: {provider_payment_id}",
                    f"user_id: {telegram_user_id}",
                    f"chat_id: {chat_id}",
                    "Клиентское уведомление заблокировано до проверки привязки получателя.",
                ],
            )
            return False, "user_mismatch"

        if not is_recurring:
            async with async_session() as session:
                recurring_profile = None
                if existing_payment and existing_payment.provider_idempotence_key:
                    recurring_profile = await session.scalar(
                        select(RecurringPaymentProfile).where(
                            RecurringPaymentProfile.recurring_operation_key == existing_payment.provider_idempotence_key,
                        ).limit(1)
                    )
                if not recurring_profile:
                    recurring_profile = await session.scalar(
                        select(RecurringPaymentProfile).where(
                            RecurringPaymentProfile.recurring_operation_provider_payment_id == provider_payment_id,
                        ).limit(1)
                    )
            if recurring_profile:
                await _mark_provider_validation_failure(
                    provider_payment_id,
                    "yookassa_missing_recurring_metadata",
                )
                return False, "missing_recurring_metadata"

        # Recurring metadata is only a routing hint.  Resolve it against the
        # durable local profile/operation before accepting a provider payment;
        # arbitrary provider-supplied IDs must never create a new entitlement.
        if is_recurring:
            recurring_payment = existing_payment
            async with async_session() as session:
                recurring_profile = None
                if recurring_profile_id:
                    recurring_profile = await session.get(RecurringPaymentProfile, recurring_profile_id)
                if not recurring_profile and recurring_operation_key:
                    recurring_profile = await session.scalar(
                        select(RecurringPaymentProfile).where(
                            RecurringPaymentProfile.recurring_operation_key == recurring_operation_key,
                        ).limit(1)
                    )
                recurring_user = await session.get(User, recurring_profile.user_id) if recurring_profile else None
                recurring_target_id = recurring_profile.subscription_id if recurring_profile else None
                valid_recurring = bool(
                    recurring_profile
                    and recurring_user
                    and telegram_user_id == recurring_user.telegram_id
                    and tariff_id == recurring_profile.tariff_id
                    and recurring_target_id
                    and platform_str
                    and platform_str.endswith(f"~r~{recurring_target_id}")
                    and recurring_operation_key == recurring_profile.recurring_operation_key
                    and (
                        existing_payment is None
                        or (
                            existing_payment.user_id == recurring_profile.user_id
                            and existing_payment.tariff_id == recurring_profile.tariff_id
                            and existing_payment.provider_idempotence_key == recurring_profile.recurring_operation_key
                            and existing_payment.provider_payment_id in (None, provider_payment_id)
                        )
                    )
                    and (
                        recurring_profile.recurring_operation_provider_payment_id in (None, provider_payment_id)
                    )
                )
                if not valid_recurring:
                    await _mark_provider_validation_failure(
                        provider_payment_id,
                        "yookassa_intent_mismatch",
                    )
                    return False, "recurring_identity_mismatch"

                if recurring_payment is None:
                    recurring_payment = await session.scalar(
                        select(Payment).where(
                            Payment.provider_idempotence_key == recurring_profile.recurring_operation_key,
                            Payment.method == PaymentMethod.YOOKASSA,
                        ).limit(1)
                    )
                if recurring_payment is None:
                    recurring_payment = Payment(
                        user_id=recurring_profile.user_id,
                        subscription_id=None,
                        amount=recurring_profile.recurring_operation_amount_kopecks or provider_amount_kopecks,
                        currency="RUB",
                        method=PaymentMethod.YOOKASSA,
                        status=PaymentStatus.PENDING,
                        provider_payment_id=provider_payment_id,
                        provider_idempotence_key=recurring_profile.recurring_operation_key,
                        telegram_chat_id=recurring_user.telegram_id,
                        tariff_id=recurring_profile.tariff_id,
                        platform=platform_str,
                        purchase_kopecks=recurring_profile.recurring_operation_amount_kopecks or provider_amount_kopecks,
                        discount_kopecks=0,
                        provisioning_failure_code="recurring_fulfillment_pending",
                    )
                    session.add(recurring_payment)
                recurring_payment.provider_payment_id = provider_payment_id
                recurring_payment.provider_idempotence_key = recurring_profile.recurring_operation_key
                recurring_payment.user_id = recurring_profile.user_id
                recurring_payment.tariff_id = recurring_profile.tariff_id
                recurring_payment.platform = platform_str
                recurring_payment.subscription_id = None
                recurring_payment.status = PaymentStatus.PENDING
                recurring_payment.provisioning_failure_code = "recurring_fulfillment_pending"
                await session.commit()
                existing_payment = recurring_payment
            if existing_payment is None:
                await _mark_provider_validation_failure(provider_payment_id, "yookassa_missing_local_payment")
                return False, "missing_local_payment"
            await _persist_provider_snapshot(
                provider_payment_id,
                status=yk_status,
                paid=bool(yk_paid),
                amount_kopecks=provider_amount_kopecks,
                currency=currency_val,
            )

        # 4.1. Recover crash gap: link provider_payment_id to local pending record via metadata if process crashed before saving
        if not existing_payment and meta.get("payment_id"):
            try:
                meta_p_id = int(meta["payment_id"])
            except (ValueError, TypeError):
                meta_p_id = None
            if meta_p_id:
                async with async_session() as session:
                    cand_p = await session.get(Payment, meta_p_id)
                    if (
                        cand_p
                        and cand_p.status == PaymentStatus.PENDING
                        and cand_p.method == PaymentMethod.YOOKASSA
                        and (cand_p.provider_payment_id is None or cand_p.provider_payment_id == provider_payment_id)
                    ):
                        user_matches = False
                        if telegram_user_id:
                            p_user = await session.get(User, cand_p.user_id)
                            user_matches = bool(p_user and p_user.telegram_id == telegram_user_id)
                        amount_matches = cand_p.amount == provider_amount_kopecks
                        currency_matches = (currency_val == "RUB" and cand_p.currency == "RUB")
                        purpose_matches = True
                        if is_device:
                            purpose_matches = (cand_p.platform == "device" and (sub_id is None or cand_p.subscription_id == sub_id))
                        elif tariff_id:
                            purpose_matches = (cand_p.tariff_id == tariff_id)

                        if user_matches and currency_matches and purpose_matches and not amount_matches:
                            cand_p.provider_status = yk_status
                            cand_p.provider_paid = bool(yk_paid)
                            cand_p.provider_amount_kopecks = provider_amount_kopecks
                            cand_p.provider_currency = currency_val
                            cand_p.provider_verified_at = datetime.utcnow()
                            cand_p.provisioning_failure_code = "yookassa_amount_mismatch"
                            await session.commit()
                            await _notify_provider_issue(
                                bot,
                                provider_payment_id,
                                title="Несовпадение суммы платежа YooKassa",
                                details=[
                                    f"PayId: {provider_payment_id}",
                                    f"Payment #{cand_p.id}: local={cand_p.amount} kop, yookassa={provider_amount_kopecks} kop",
                                ],
                            )
                            return False, "amount_mismatch"

                        if user_matches and amount_matches and currency_matches and purpose_matches:
                            cand_p.provider_payment_id = provider_payment_id
                            cand_p.provider_status = yk_status
                            cand_p.provider_paid = bool(yk_paid)
                            cand_p.provider_amount_kopecks = provider_amount_kopecks
                            cand_p.provider_currency = currency_val
                            cand_p.provider_verified_at = datetime.utcnow()
                            await session.commit()
                            existing_payment = cand_p
                            logger.info(
                                "Safely linked unpersisted provider_payment_id %s to local pending Payment #%s via metadata",
                                provider_payment_id,
                                cand_p.id,
                            )

                        else:
                            if not user_matches:
                                cand_p.provisioning_failure_code = "yookassa_user_mismatch"
                            elif not currency_matches:
                                cand_p.provisioning_failure_code = "yookassa_currency_mismatch"
                            else:
                                cand_p.provisioning_failure_code = "yookassa_intent_mismatch"
                            await session.commit()
                            await _mark_provider_validation_failure(
                                provider_payment_id,
                                cand_p.provisioning_failure_code,
                            )
                            if is_device and not user_matches:
                                return False, "missing_local_payment"
                            return False, "user_mismatch" if not user_matches else "intent_mismatch"

        if not existing_topup and meta.get("topup_id"):
            try:
                meta_t_id = int(meta["topup_id"])
            except (ValueError, TypeError):
                meta_t_id = None
            if meta_t_id:
                async with async_session() as session:
                    cand_t = await session.get(BalanceTopUp, meta_t_id)
                    if (
                        cand_t
                        and cand_t.status == "pending"
                        and cand_t.provider == "yookassa"
                        and (cand_t.provider_payment_id is None or cand_t.provider_payment_id == provider_payment_id or cand_t.provider_payment_id == str(meta_t_id))
                    ):
                        local_topup_kopecks = cand_t.amount_kopecks or rub_to_kopecks(cand_t.amount_rub)
                        if not telegram_user_id or cand_t.telegram_id == telegram_user_id:
                            if local_topup_kopecks != provider_amount_kopecks:
                                cand_t.provider_status = yk_status
                                cand_t.provider_paid = bool(yk_paid)
                                cand_t.provider_amount_kopecks = provider_amount_kopecks
                                cand_t.provider_currency = currency_val
                                cand_t.provider_verified_at = datetime.utcnow()
                                cand_t.provider_failure_code = "yookassa_amount_mismatch"
                                await session.commit()
                                await _notify_provider_issue(
                                    bot,
                                    provider_payment_id,
                                    title="Несовпадение суммы пополнения баланса",
                                    details=[
                                        f"PayId: {provider_payment_id}",
                                        f"TopUp #{cand_t.id}: local={local_topup_kopecks} kop, yookassa={provider_amount_kopecks} kop",
                                    ],
                                )
                                return False, "amount_mismatch"
                            cand_t.provider_payment_id = provider_payment_id
                            cand_t.amount_kopecks = local_topup_kopecks
                            cand_t.provider_status = yk_status
                            cand_t.provider_paid = bool(yk_paid)
                            cand_t.provider_amount_kopecks = provider_amount_kopecks
                            cand_t.provider_currency = currency_val
                            cand_t.provider_verified_at = datetime.utcnow()
                            await session.commit()
                            existing_topup = cand_t
                            logger.info("Safely linked provider_payment_id %s to BalanceTopUp #%s", provider_payment_id, cand_t.id)
                        elif telegram_user_id:
                            cand_t.provider_failure_code = "yookassa_user_mismatch"
                            await session.commit()
                            await _mark_provider_validation_failure(provider_payment_id, "yookassa_user_mismatch")
                            return False, "user_mismatch"

        # 5. Routing & local record verification
        from bot.webhooks import (
            _complete_balance_topup,
            _ensure_paid_webhook_payment,
            _process_and_deliver,
            _process_device_deliver,
        )

        # ── Case A: Balance TopUp ──────────────────────────
        if purpose == "balance_topup" or (existing_topup is not None):
            kopecks = provider_amount_kopecks
            if existing_topup:
                local_kopecks = existing_topup.amount_kopecks or rub_to_kopecks(existing_topup.amount_rub)
                if local_kopecks != kopecks:
                    err = f"Topup amount mismatch: local={local_kopecks} kop, yookassa={kopecks} kop"
                    logger.error(err)
                    await _notify_provider_issue(bot, provider_payment_id, title="Несовпадение суммы пополнения баланса", details=[f"PayId: {provider_payment_id}", err])
                    await _mark_provider_validation_failure(provider_payment_id, "yookassa_amount_mismatch")
                    return False, "amount_mismatch"
                if not telegram_user_id:
                    logger.error("Topup %s missing user_id in metadata", provider_payment_id)
                    await _mark_provider_validation_failure(
                        provider_payment_id,
                        "yookassa_missing_user_metadata",
                    )
                    return False, "missing_user_id"
                if existing_topup.telegram_id != telegram_user_id:
                    err = f"Topup user mismatch: local={existing_topup.telegram_id}, yookassa={telegram_user_id}"
                    logger.error(err)
                    await _notify_provider_issue(bot, provider_payment_id, title="Несовпадение пользователя пополнения баланса", details=[f"PayId: {provider_payment_id}", err])
                    await _mark_provider_validation_failure(provider_payment_id, "yookassa_user_mismatch")
                    return False, "user_mismatch"
                target_tg_id = existing_topup.telegram_id
            else:
                if not telegram_user_id:
                    logger.error("Topup %s missing user_id in metadata", provider_payment_id)
                    await _mark_provider_validation_failure(
                        provider_payment_id,
                        "yookassa_missing_user_metadata",
                    )
                    return False, "missing_user_id"
                target_tg_id = telegram_user_id

            user_db_id, new_balance = await _complete_balance_topup(
                telegram_user_id=target_tg_id,
                amount_rub=amount_val,
                provider="yookassa",
                provider_payment_id=provider_payment_id,
            )
            if user_db_id is not None:
                async with async_session() as session:
                    user_rec = await session.get(User, user_db_id)
                try:
                    from bot.keyboards.inline import back_to_menu_kb
                    await bot.send_message(
                        chat_id or target_tg_id,
                        f"✅ <b>Баланс пополнен</b>\n\n"
                        f"Зачислено: <b>{amount_val:.2f} ₽</b>\n"
                        f"Сейчас на балансе: <b>{new_balance:.2f} ₽</b>",
                        parse_mode="HTML",
                        reply_markup=back_to_menu_kb(),
                    )
                except Exception:
                    pass
                if user_rec:
                    await notify_admins_payment(
                        bot,
                        telegram_id=target_tg_id,
                        full_name=user_rec.full_name or "",
                        username=user_rec.username,
                        amount_rub=amount_val,
                        tariff_label="Пополнение баланса",
                        method="💳 YooKassa",
                        platform="Баланс",
                    )
                return True, "balance_topup_completed"
            await _mark_provider_validation_failure(
                provider_payment_id,
                "yookassa_topup_credit_failed",
            )
            return False, "user_not_found"

        # ── Case B: Extra Device Slot ──────────────────────
        if is_device or (existing_payment and existing_payment.platform == "device"):
            if not existing_payment:
                err = f"Device payment {provider_payment_id} rejected: no durable local Payment record exists."
                logger.error(err)
                await _notify_provider_issue(bot, provider_payment_id, title="Оплата устройства без локального платежа", details=[f"PayId: {provider_payment_id}", err])
                await _mark_provider_validation_failure(provider_payment_id, "yookassa_missing_local_payment")
                return False, "missing_local_payment"

            if not sub_id:
                logger.error("Device payment %s missing sub_id in metadata", provider_payment_id)
                await _mark_provider_validation_failure(provider_payment_id, "yookassa_missing_sub_id")
                return False, "missing_sub_id"

            if not telegram_user_id:
                logger.error("Device payment %s missing user_id in metadata", provider_payment_id)
                await _mark_provider_validation_failure(provider_payment_id, "yookassa_missing_user_metadata")
                return False, "missing_user_id"

            if existing_payment.subscription_id and existing_payment.subscription_id != sub_id:
                err = f"Device payment subscription mismatch: local={existing_payment.subscription_id}, yookassa={sub_id}"
                logger.error(err)
                await _mark_provider_validation_failure(provider_payment_id, "yookassa_intent_mismatch")
                return False, "subscription_mismatch"

            target_sub_id = sub_id
            if existing_payment.amount != provider_amount_kopecks:
                err = f"Device payment amount mismatch: local={existing_payment.amount} kop, yookassa={provider_amount_kopecks} kop"
                logger.error(err)
                await _notify_provider_issue(bot, provider_payment_id, title="Несовпадение суммы покупки устройства", details=[f"PayId: {provider_payment_id}", err])
                await _mark_provider_validation_failure(provider_payment_id, "yookassa_amount_mismatch")
                return False, "amount_mismatch"

            async with async_session() as session:
                user_row = await session.get(User, existing_payment.user_id)
                if not user_row or user_row.telegram_id != telegram_user_id:
                    err = f"Device payment user mismatch: local={user_row.telegram_id if user_row else 'none'}, yookassa={telegram_user_id}"
                    logger.error(err)
                    await _notify_provider_issue(bot, provider_payment_id, title="Несовпадение пользователя покупки устройства", details=[f"PayId: {provider_payment_id}", err])
                    await _mark_provider_validation_failure(provider_payment_id, "yookassa_user_mismatch")
                    return False, "user_mismatch"

            customer_ack_required = True
            customer_ack_payment_id = existing_payment.id
            customer_ack_recipient_id = telegram_user_id

            user_db_id, delivered_sub_id = await _process_device_deliver(
                app,
                telegram_user_id=telegram_user_id,
                chat_id=chat_id or telegram_user_id,
                sub_id=target_sub_id,
                payment_id=existing_payment.id,
                provider_payment_id=provider_payment_id,
            )
            if user_db_id is not None:
                customer_ack_required = False
                return True, "device_slot_completed"
            await _mark_provider_validation_failure(
                provider_payment_id,
                "yookassa_device_delivery_failed",
            )
            return False, "device_delivery_failed"

        # ── Case C: Subscription Purchase / Upgrade / Renewal ──
        if not telegram_user_id:
            logger.error("Subscription payment %s missing user_id in provider metadata", provider_payment_id)
            await _mark_provider_validation_failure(provider_payment_id, "yookassa_missing_user_metadata")
            return False, "missing_metadata:user_id"
        if not tariff_id:
            logger.error("Subscription payment %s missing tariff_id in provider metadata", provider_payment_id)
            await _mark_provider_validation_failure(provider_payment_id, "yookassa_missing_tariff_metadata")
            return False, "missing_metadata:tariff_id"
        if not platform_str:
            logger.error("Subscription payment %s missing platform in provider metadata", provider_payment_id)
            await _mark_provider_validation_failure(provider_payment_id, "yookassa_missing_platform_metadata")
            return False, "missing_metadata:platform"

        kopecks = provider_amount_kopecks
        async with async_session() as session:
            payment_row = await session.scalar(
                select(Payment).where(Payment.provider_payment_id == provider_payment_id)
            )
            if payment_row:
                user_row = await session.get(User, payment_row.user_id)
                if not user_row or user_row.telegram_id != telegram_user_id:
                    err = f"Payment user mismatch: local={user_row.telegram_id if user_row else 'none'}, yookassa={telegram_user_id}"
                    logger.error(err)
                    await _notify_provider_issue(bot, provider_payment_id, title="Несовпадение пользователя YooKassa", details=[f"PayId: {provider_payment_id}", err])
                    payment_row.provisioning_failure_code = "yookassa_user_mismatch"
                    await session.commit()
                    return False, "user_mismatch"

                if payment_row.telegram_chat_id is not None and payment_row.telegram_chat_id != telegram_user_id:
                    customer_ack_recipient_conflict = True
                    err = f"Payment chat mismatch: local={payment_row.telegram_chat_id}, yookassa={telegram_user_id}"
                    logger.error(err)
                    await _notify_provider_issue(
                        bot,
                        provider_payment_id,
                        title="Несовпадение чата получателя YooKassa",
                        details=[f"PayId: {provider_payment_id}", err],
                    )
                    payment_row.provisioning_failure_code = "yookassa_recipient_mismatch"
                    await session.commit()
                    return False, "user_mismatch"

                customer_ack_required = True
                customer_ack_payment_id = payment_row.id
                customer_ack_recipient_id = telegram_user_id

                if payment_row.tariff_id and payment_row.tariff_id != tariff_id:
                    err = f"Payment tariff mismatch: local={payment_row.tariff_id}, yookassa={tariff_id}"
                    logger.error(err)
                    await _notify_provider_issue(bot, provider_payment_id, title="Несовпадение тарифа YooKassa", details=[f"PayId: {provider_payment_id}", err])
                    payment_row.provisioning_failure_code = "yookassa_tariff_mismatch"
                    await session.commit()
                    return False, "tariff_mismatch"

                if payment_row.platform and payment_row.platform != platform_str:
                    err = f"Payment intent mismatch: local={payment_row.platform}, yookassa={platform_str}"
                    logger.error(err)
                    await _notify_provider_issue(bot, provider_payment_id, title="Несовпадение намерения покупки YooKassa", details=[f"PayId: {provider_payment_id}", err])
                    payment_row.provisioning_failure_code = "yookassa_intent_mismatch"
                    await session.commit()
                    return False, "intent_mismatch"

                if payment_row.amount != kopecks:
                    repaired = await _try_repair_legacy_amount(
                        payment=payment_row,
                        provider_amount_kopecks=kopecks,
                        telegram_user_id=telegram_user_id,
                        tariff_id=tariff_id,
                        platform=platform_str,
                        provider_payment_id=provider_payment_id,
                    )
                    if repaired:
                        payment_row.amount = kopecks
                        payment_row.purchase_kopecks = payment_row.purchase_kopecks or (
                            kopecks + rub_to_kopecks(payment_row.discount_applied or 0)
                        )
                    else:
                        err = f"Payment amount mismatch: local={payment_row.amount} kop, yookassa={kopecks} kop"
                        logger.error(err)
                        payment_row.provisioning_failure_code = "yookassa_amount_mismatch"
                        await session.commit()
                        await _notify_provider_issue(
                            bot,
                            provider_payment_id,
                            title="Несовпадение суммы платежа YooKassa",
                            details=[f"PayId: {provider_payment_id}", err],
                        )
                        return False, "amount_mismatch"

                effective_user_tg_id = telegram_user_id
                effective_tariff_id = tariff_id
                effective_platform = platform_str
                db_payment_id = payment_row.id
                user_db_id = payment_row.user_id
            else:
                user_row = await session.scalar(
                    select(User).where(User.telegram_id == telegram_user_id)
                )
                if not user_row:
                    logger.error("User %s not found for YooKassa payment %s", telegram_user_id, provider_payment_id)
                    return False, "user_not_found"
                effective_user_tg_id = telegram_user_id
                effective_tariff_id = tariff_id
                effective_platform = platform_str
                user_db_id = user_row.id
                db_payment_id = None
                customer_ack_required = True
                customer_ack_recipient_id = telegram_user_id

        if not effective_user_tg_id or not effective_tariff_id or not effective_platform:
            logger.error("Cannot resolve subscription fulfillment parameters for %s", provider_payment_id)
            return False, "missing_subscription_parameters"

        if db_payment_id is None:
            db_payment_id = await _ensure_paid_webhook_payment(
                user_id=user_db_id,
                provider_payment_id=provider_payment_id,
                amount_rub=amount_val,
                method=PaymentMethod.YOOKASSA,
                tariff_id=effective_tariff_id,
                platform=effective_platform,
            )
        if customer_ack_required:
            customer_ack_payment_id = db_payment_id

        # Check lease validity before calling supplier. The paid local record is
        # already durable, so a lost lease still leaves a recoverable operation.
        if (heartbeat is not None and heartbeat.lease_lost) or not await lease_manager.is_owned_by_me(lock_name):
            logger.error("Lease %s expired before supplier provisioning for %s", lock_name, provider_payment_id)
            return False, "lease_expired"

        # A verified payment must have a durable customer acknowledgement before
        # any VHQ/Adapt read or mutation. The outbox worker may deliver it now or
        # after a restart; supplier execution never depends on a direct send.
        if customer_ack_required:
            if (
                customer_ack_payment_id is None
                or customer_ack_recipient_id is None
                or not await _queue_customer_payment_ack(
                    bot,
                    payment_id=customer_ack_payment_id,
                    recipient_telegram_id=customer_ack_recipient_id,
                )
            ):
                logger.error(
                    "Refusing supplier execution because customer acknowledgement was not durable: payment=%s",
                    provider_payment_id,
                )
                return False, "customer_ack_persist_failed"
            customer_ack_required = False

        await _persist_provider_snapshot(
            provider_payment_id,
            status=yk_status,
            paid=bool(yk_paid),
            amount_kopecks=provider_amount_kopecks,
            currency=currency_val,
        )

        subscription_id = None
        delivered_user_id = None
        is_prior_fulfilled = False

        # Check if an earlier mutation is already in-flight or failed with uncertain state
        async with async_session() as session:
            curr_payment = await session.get(Payment, db_payment_id)
            if (
                curr_payment
                and curr_payment.status == PaymentStatus.COMPLETED
                and (curr_payment.subscription_id or curr_payment.platform == "device")
                and curr_payment.provisioning_failure_code != "financial_finalization_pending"
            ):
                logger.info("Payment %s already completed in DB", provider_payment_id)
                customer_ack_required = False
                return True, "already_completed"

            if curr_payment and (
                curr_payment.provisioning_failure_code in MANUAL_REVIEW_PROVISIONING_CODES
                or (curr_payment.provisioning_failure_code and "manual_review" in curr_payment.provisioning_failure_code)
            ):
                if not allow_manual_review_override and source != "admin_authorized_recovery":
                    logger.warning(
                        "Payment %s is locked in manual review state (%s). Blocking automated fulfillment.",
                        provider_payment_id,
                        curr_payment.provisioning_failure_code,
                    )
                    return False, curr_payment.provisioning_failure_code

            if (
                curr_payment
                and _is_waiting_for_supplier_funds(curr_payment)
                and curr_payment.next_attempt_at
                and curr_payment.next_attempt_at > datetime.utcnow()
                and not (
                    allow_manual_review_override
                    and source == "admin_authorized_recovery"
                )
            ):
                logger.info(
                    "Payment %s is in supplier-funds backoff until %s; skipping duplicate supplier attempt",
                    provider_payment_id,
                    curr_payment.next_attempt_at,
                )
                return False, f"delivery_failed:{curr_payment.provisioning_failure_code}"

            if curr_payment and curr_payment.subscription_id:
                # Subscription was already linked (either completed earlier or during local fulfillment commit);
                # use it to complete/finalize without supplier re-mutation
                delivered_user_id = curr_payment.user_id
                subscription_id = curr_payment.subscription_id
                is_prior_fulfilled = bool(
                    curr_payment.status == PaymentStatus.COMPLETED
                    and curr_payment.provisioning_failure_code != "financial_finalization_pending"
                )
                logger.info(
                    "Recovering payment %s using existing subscription %s (state=%s) without supplier re-mutation",
                    provider_payment_id,
                    subscription_id,
                    curr_payment.provisioning_failure_code,
                )
            elif curr_payment and curr_payment.provisioning_failure_code in (
                "mutation_in_flight",
                "uncertain_supplier_mutation",
                "adapt_upgrade_failed",
                "adapt_renew_failed",
            ):
                tariff_rec = await session.get(Tariff, effective_tariff_id) if effective_tariff_id else None
                from bot.services.vhq_routing import is_vhq_tariff
                is_vhq = tariff_rec and is_vhq_tariff(tariff_rec)
                if is_vhq:
                    logger.info(
                        "Attempting autonomous VHQ mutation recovery for payment %s (state=%s)",
                        provider_payment_id,
                        curr_payment.provisioning_failure_code,
                    )
                    rec_ok, rec_sub_id, rec_code = await _reconcile_ambiguous_vhq_mutation(
                        session=session,
                        payment=curr_payment,
                        provider_payment_id=provider_payment_id,
                        effective_platform=effective_platform,
                        effective_tariff_id=effective_tariff_id,
                        user_db_id=user_db_id,
                        effective_user_tg_id=effective_user_tg_id,
                    )
                else:
                    logger.info(
                        "Attempting autonomous Adapt mutation recovery for payment %s (state=%s)",
                        provider_payment_id,
                        curr_payment.provisioning_failure_code,
                    )
                    rec_ok, rec_sub_id, rec_code = await _reconcile_ambiguous_adapt_mutation(
                        session=session,
                        payment=curr_payment,
                        provider_payment_id=provider_payment_id,
                        effective_platform=effective_platform,
                        effective_tariff_id=effective_tariff_id,
                        user_db_id=user_db_id,
                        effective_user_tg_id=effective_user_tg_id,
                    )
                if rec_ok and rec_sub_id:
                    delivered_user_id = curr_payment.user_id
                    subscription_id = rec_sub_id
                    is_prior_fulfilled = False
                    logger.info(
                        "Autonomous mutation recovery succeeded for payment %s on sub %s (%s)",
                        provider_payment_id,
                        rec_sub_id,
                        rec_code,
                    )
                elif rec_code == "vhq_reconciled_ready_for_retry":
                    logger.info(
                        "VHQ ambiguous mutation resolved as clean pre-mutation for payment %s; proceeding to safe delivery",
                        provider_payment_id,
                    )
                elif rec_code == "adapt_api_402":
                    return False, "delivery_failed:adapt_api_402"
                else:
                    logger.warning(
                        "Payment %s unresolved supplier mutation (%s): %s; refusing duplicate mutation",
                        provider_payment_id,
                        curr_payment.provisioning_failure_code,
                        rec_code,
                    )
                    fail_return_code = "supplier_mutation_in_flight_or_uncertain" if rec_code in ("unsupported_ambiguous_mutation", "non_adapt_unresolved_supplier_mutation") else rec_code
                    return False, fail_return_code

        if subscription_id is None:
            # Check for existing manual or active fulfillment before attempting automated provisioning
            async with async_session() as session:
                now_utc = datetime.utcnow()
                p_row = await session.get(Payment, db_payment_id)
                p_created = p_row.created_at if p_row else now_utc

                # Case 1: Target subscription renewal/upgrade
                if effective_platform and ("~u~" in effective_platform or "~r~" in effective_platform):
                    try:
                        target_sub_id = int(effective_platform.split("~")[-1])
                    except (ValueError, IndexError):
                        target_sub_id = None

                    if target_sub_id:
                        target_sub = await session.get(Subscription, target_sub_id)
                        if not target_sub:
                            logger.error("Target subscription %s not found for payment %s", target_sub_id, provider_payment_id)
                            if p_row:
                                p_row.provisioning_failure_code = "target_subscription_not_found"
                                await session.commit()
                            return False, "target_subscription_not_found"

                        # Validate target_sub.user_id matches the verified payment owner!
                        if target_sub.user_id != user_db_id:
                            customer_ack_recipient_conflict = True
                            customer_ack_recipient_id = None
                            logger.error(
                                "Target subscription %s user mismatch: sub.user_id=%s, payment.user_id=%s",
                                target_sub_id, target_sub.user_id, user_db_id,
                            )
                            await _notify_provider_issue(
                                bot,
                                provider_payment_id,
                                title="Конфликт получателя при оплате YooKassa",
                                details=[
                                    f"PayId: {provider_payment_id}",
                                    f"Target subscription: #{target_sub_id}",
                                    f"Payment owner: #{user_db_id}",
                                    "Клиентское уведомление заблокировано до ручной проверки.",
                                ],
                            )
                            if p_row:
                                p_row.provisioning_failure_code = "target_sub_user_mismatch"
                                await session.commit()
                            customer_ack_required = False
                            return False, "target_sub_user_mismatch"

                        # Require trustworthy evidence of the intended renewal/upgrade already being applied:
                        tariff_rec = await session.get(Tariff, effective_tariff_id) if effective_tariff_id else None
                        expected_days = tariff_rec.days if tariff_rec else (target_sub.tariff_days if target_sub else None)
                        baseline_exp = p_row.provisioning_baseline_expires_at if p_row else None
                        required_extension = (timedelta(days=expected_days) - timedelta(hours=2)) if expected_days else None

                        # Check for competing payments for this subscription around this time
                        candidate_competing_payments = (await session.execute(
                            select(Payment).where(
                                Payment.id != (p_row.id if p_row else -1),
                                Payment.user_id == user_db_id,
                                or_(
                                    Payment.subscription_id == target_sub.id,
                                    Payment.platform.like(f"%~u~{target_sub.id}%"),
                                    Payment.platform.like(f"%~r~{target_sub.id}%"),
                                ),
                                Payment.status.in_([PaymentStatus.COMPLETED, PaymentStatus.PENDING]),
                                Payment.created_at.between(p_created - timedelta(days=2), p_created + timedelta(days=2)),
                            )
                        )).scalars().all()
                        competing_payments = await filter_real_competing_payments(candidate_competing_payments)

                        candidate_admin_logs = (await session.execute(
                            select(AdminActionLog).where(
                                AdminActionLog.action.in_(["renew_subscription", "update_subscription_days", "grant_access"]),
                                AdminActionLog.status == "success",
                                AdminActionLog.entity_id == str(target_sub.id),
                                AdminActionLog.target_telegram_id == effective_user_tg_id,
                                AdminActionLog.created_at.between(p_created - timedelta(hours=1), p_created + timedelta(days=2)),
                            )
                        )).scalars().all()

                        unique_admin_log = next(
                            (l for l in candidate_admin_logs if _is_admin_log_for_payment(l, p_row.id if p_row else None, provider_payment_id)),
                            None,
                        )

                        # If there are competing payments and no unique administrative log explicitly linking this payment:
                        # Check if subscription extension covers ALL competing payments combined
                        has_competing_conflict = False
                        if competing_payments and not unique_admin_log:
                            total_competing_days = expected_days or 30
                            for cp in competing_payments:
                                cp_tariff = await session.get(Tariff, cp.tariff_id) if cp.tariff_id else None
                                total_competing_days += (cp_tariff.days if cp_tariff else 30)
                            cum_ext = timedelta(days=total_competing_days) - timedelta(hours=2)
                            if not (baseline_exp and target_sub.expires_at and target_sub.expires_at >= baseline_exp + cum_ext):
                                has_competing_conflict = True

                        if has_competing_conflict:
                            logger.warning(
                                "Payment %s has %s competing payments for sub %s; attribution is ambiguous",
                                provider_payment_id, len(competing_payments), target_sub.id
                            )
                            if p_row:
                                p_row.provisioning_failure_code = "ambiguous_competing_payments_for_subscription"
                                await session.commit()
                            return False, "ambiguous_competing_payments_for_subscription"

                        has_baseline_evidence = bool(
                            not competing_payments
                            and baseline_exp
                            and required_extension
                            and target_sub.expires_at
                            and target_sub.expires_at >= baseline_exp + required_extension
                        )

                        manual_op = None
                        has_op_evidence = False
                        if not competing_payments:
                            manual_op = await session.scalar(
                                select(ManualSubscriptionOperation).where(
                                    ManualSubscriptionOperation.target_user_id == user_db_id,
                                    ManualSubscriptionOperation.subscription_id == target_sub.id,
                                    ManualSubscriptionOperation.status == "completed",
                                    ManualSubscriptionOperation.action.in_(["renew", "upgrade", "manual_fulfillment"]),
                                    ManualSubscriptionOperation.created_at.between(p_created - timedelta(hours=1), p_created + timedelta(days=2)),
                                ).limit(1)
                            )
                            if manual_op:
                                tariff_match = (manual_op.tariff_id == effective_tariff_id)
                                extension_match = bool(
                                    baseline_exp
                                    and required_extension
                                    and target_sub.expires_at
                                    and target_sub.expires_at >= baseline_exp + required_extension
                                )
                                has_op_evidence = bool(tariff_match and extension_match)

                        has_admin_evidence = False
                        if unique_admin_log:
                            extension_match = bool(
                                baseline_exp
                                and required_extension
                                and target_sub.expires_at
                                and target_sub.expires_at >= baseline_exp + required_extension
                            )
                            if extension_match:
                                has_admin_evidence = True
                            else:
                                logger.warning(
                                    "Payment %s has unique admin log %s but required extension not met; failing closed",
                                    provider_payment_id, unique_admin_log.id,
                                )
                                if p_row:
                                    p_row.provisioning_failure_code = "partial_admin_renewal_manual_review_required"
                                    await session.commit()
                                return False, "partial_admin_renewal_manual_review_required"
                        elif not competing_payments:
                            admin_log = await session.scalar(
                                select(AdminActionLog).where(
                                    AdminActionLog.action.in_(["renew_subscription", "update_subscription_days", "grant_access"]),
                                    AdminActionLog.status == "success",
                                    AdminActionLog.entity_id == str(target_sub.id),
                                    AdminActionLog.target_telegram_id == effective_user_tg_id,
                                    AdminActionLog.created_at.between(p_created - timedelta(hours=1), p_created + timedelta(days=2)),
                                ).limit(1)
                            )
                            if admin_log:
                                extension_match = bool(
                                    baseline_exp
                                    and required_extension
                                    and target_sub.expires_at
                                    and target_sub.expires_at >= baseline_exp + required_extension
                                )
                                has_admin_evidence = bool(extension_match)

                        if has_baseline_evidence or has_op_evidence or has_admin_evidence:
                            logger.info(
                                "Payment %s has verified durable evidence of renewal fulfillment on sub %s (baseline=%s, op=%s, admin=%s)",
                                provider_payment_id, target_sub.id, has_baseline_evidence, has_op_evidence, has_admin_evidence,
                            )
                            # Link and route into financial finalization without supplier re-mutation!
                            subscription_id = target_sub.id
                            delivered_user_id = user_db_id
                            is_prior_fulfilled = True
                            if p_row:
                                p_row.subscription_id = target_sub.id
                                p_row.provisioning_failure_code = "financial_finalization_pending"
                                await session.commit()
                        else:
                            # Never mark completed based only on age and active sub.
                            # If payment is historical (>24h old), fail closed for manual review.
                            if (
                                now_utc - p_created > timedelta(hours=24)
                                and not _is_waiting_for_supplier_funds(p_row)
                            ):
                                logger.warning(
                                    "Payment %s is historical renewal (%s) but lacks trustworthy evidence of fulfillment; failing closed for manual review",
                                    provider_payment_id, p_created,
                                )
                                if p_row:
                                    p_row.provisioning_failure_code = "renewal_evidence_missing_manual_review_required"
                                    await session.commit()
                                return False, "renewal_evidence_missing_manual_review_required"

                # Case 2: Historical payment (>24h old) - fail closed!
                if (
                    subscription_id is None
                    and now_utc - p_created > timedelta(hours=24)
                    and not _is_waiting_for_supplier_funds(p_row)
                ):
                    logger.warning(
                        "Payment %s is historical (%s); refusing automated unverified provisioning",
                        provider_payment_id, p_created,
                    )
                    if p_row:
                        p_row.provisioning_failure_code = "historical_payment_manual_review_required"
                        await session.commit()
                    return False, "historical_payment_manual_review_required"

        if subscription_id is None:
            # Pre-flight check for waiting funds (vhq_balance / adapt_api_402): abort early if balance is still insufficient
            async with async_session() as session:
                p_check = await session.get(Payment, db_payment_id)
                if p_check and (p_check.provisioning_failure_code in ("vhq_balance", "adapt_api_402") or (allow_manual_review_override and source == "admin_authorized_recovery")):
                    tariff_rec = await session.get(Tariff, effective_tariff_id) if effective_tariff_id else None
                    from bot.services.vhq_routing import is_vhq_tariff
                    if tariff_rec and is_vhq_tariff(tariff_rec):
                        from bot.services.vhq_partner_api import VHQPartnerAPI
                        api_chk = VHQPartnerAPI()
                        if api_chk.enabled:
                            try:
                                bal_data = await api_chk.get_balance()
                                if isinstance(bal_data, dict) and "balance" in bal_data:
                                    c_bal = float(bal_data["balance"])
                                    from bot.services.problem_payments import get_vhq_wholesale_cost
                                    req_cost = get_vhq_wholesale_cost(tariff_rec)
                                    if c_bal < req_cost:
                                        logger.warning(
                                            "Payment %s delivery retry aborted: VHQ balance still insufficient (%.2f < %.2f)",
                                            provider_payment_id, c_bal, req_cost,
                                        )
                                        _schedule_payment_retry(
                                            p_check,
                                            "vhq_balance",
                                            now=datetime.utcnow(),
                                        )
                                        await session.commit()
                                        return False, "delivery_failed:vhq_balance"
                            except Exception as chk_err:
                                logger.warning("Pre-flight VHQ balance check failed for %s: %s", provider_payment_id, chk_err)
                    elif p_check.provisioning_failure_code == "adapt_api_402":
                        from bot.services.adapt_api import AdaptAPI
                        api_adapt = AdaptAPI()
                        if api_adapt.enabled:
                            try:
                                bal_adapt = await api_adapt.get_balance()
                                c_adapt = float(bal_adapt.get("balance", 0.0))
                                if c_adapt <= 0:
                                    logger.warning(
                                        "Payment %s delivery retry aborted: Adapt balance still insufficient (%.2f)",
                                        provider_payment_id, c_adapt,
                                    )
                                    _schedule_payment_retry(
                                        p_check,
                                        "adapt_api_402",
                                        now=datetime.utcnow(),
                                    )
                                    await session.commit()
                                    return False, "delivery_failed:adapt_api_402"
                            except Exception as chk_adapt_err:
                                logger.warning("Pre-flight Adapt balance check failed for %s: %s", provider_payment_id, chk_adapt_err)

            # Persist durable in-flight marker BEFORE supplier mutation
            async with async_session() as session:
                p_mark = await session.get(Payment, db_payment_id)
                if p_mark:
                    p_mark.provisioning_failure_code = "mutation_in_flight"
                    await session.commit()

            from bot.services.provisioning_issues import AccessProvisionError

            delivery_issue_code: str | None = None
            try:
                delivered_user_id, subscription_id = await _process_and_deliver(
                    app,
                    telegram_user_id=effective_user_tg_id,
                    chat_id=chat_id or effective_user_tg_id,
                    tariff_id=effective_tariff_id,
                    platform_str=effective_platform,
                    provisioning_payment_id=db_payment_id,
                    paid_amount_rub=amount_val,
                    notify_admin=False,
                    deliver_customer_messages=False,
                )
            except AccessProvisionError as issue:
                if issue.provider == "vhq" and issue.code == "supplier_fulfillment_pending":
                    async with async_session() as session:
                        p = await session.get(Payment, db_payment_id)
                        if p:
                            p.status = PaymentStatus.PENDING
                            p.provisioning_failure_code = "supplier_fulfillment_pending"
                        await session.commit()
                    logger.info("VHQ fulfillment pending for YooKassa payment %s", provider_payment_id)
                    return True, "supplier_fulfillment_pending"
                delivery_issue_code = issue.code or "delivery_failed"
                logger.error("Supplier provisioning error for payment %s: %s", provider_payment_id, issue.admin_message)
            except Exception as exc:
                delivery_issue_code = "delivery_failed"
                logger.error("Unexpected error in _process_and_deliver for payment %s: %s", provider_payment_id, exc)

            # Fencing check after supplier provisioning: if lease expired while supplier call was in flight, do NOT proceed as exclusive winner
            lease_is_held = (heartbeat is None or not heartbeat.lease_lost) and await lease_manager.is_owned_by_me(lock_name)
            if not lease_is_held:
                logger.error("Lease %s lost/expired during supplier provisioning for %s", lock_name, provider_payment_id)
                if db_payment_id:
                    async with async_session() as session:
                        p = await session.get(Payment, db_payment_id)
                        if p and p.status != PaymentStatus.COMPLETED:
                            p.provisioning_failure_code = "uncertain_supplier_mutation"
                            if subscription_id and not p.subscription_id:
                                p.subscription_id = subscription_id
                            await session.commit()
                return False, "lease_expired"

            # If lease is held, check if delivery failed or was incomplete
            if delivery_issue_code or delivered_user_id is None or subscription_id is None:
                fail_code = "uncertain_supplier_mutation"
                if db_payment_id:
                    async with async_session() as session:
                        p = await session.get(Payment, db_payment_id)
                        if p and not p.subscription_id:
                            # Only clear the in-flight marker for failures proven to have occurred BEFORE any supplier mutation.
                            # Ambiguous or post-mutation failures must retain durable uncertainty to prevent duplicate creations.
                            current_code = p.provisioning_failure_code
                            if delivery_issue_code in PRE_MUTATION_PROVISIONING_CODES or current_code in PRE_MUTATION_PROVISIONING_CODES:
                                fail_code = delivery_issue_code or current_code or "pre_mutation_failure"
                                p.provisioning_failure_code = fail_code
                            elif delivery_issue_code in ("vhq_request_invalid", "vhq_auth"):
                                fail_code = delivery_issue_code
                                p.provisioning_failure_code = fail_code
                            else:
                                fail_code = "uncertain_supplier_mutation"
                            _schedule_payment_retry(p, fail_code, now=datetime.utcnow())
                            await session.commit()
                return False, f"delivery_failed:{fail_code}"

        if delivered_user_id is not None and subscription_id is not None:
            cashback_result: dict[str, Any] = {}
            try:
                async with async_session() as session:
                    payment = await session.get(Payment, db_payment_id)
                    if not payment:
                        payment = await session.scalar(
                            select(Payment).where(Payment.provider_payment_id == provider_payment_id)
                        )
                    if payment:
                        payment.status = PaymentStatus.COMPLETED
                        payment.subscription_id = subscription_id
                        payment.tariff_id = effective_tariff_id
                        payment.platform = effective_platform
                        payment.provisioning_failure_code = None
                        _reset_payment_retry(payment)
                        payment.amount = provider_amount_kopecks
                        discount_kopecks = payment.discount_kopecks
                        if discount_kopecks is None:
                            discount_kopecks = rub_to_kopecks(payment.discount_applied or 0)
                        payment.discount_kopecks = discount_kopecks
                        if payment.purchase_kopecks is None:
                            payment.purchase_kopecks = provider_amount_kopecks + discount_kopecks
                        payment.discount_applied = kopecks_to_rub_float(discount_kopecks)

                        if is_recurring and recurring_profile_id:
                            recurring_profile = await session.get(
                                RecurringPaymentProfile,
                                recurring_profile_id,
                            )
                            if (
                                recurring_profile
                                and recurring_profile.user_id == payment.user_id
                                and recurring_profile.tariff_id == effective_tariff_id
                            ):
                                recurring_profile.payment_attempt_count = 0
                                recurring_profile.last_payment_attempt = None
                                recurring_profile.last_charge_at = datetime.utcnow()
                                fulfilled_sub = await session.get(Subscription, subscription_id)
                                if fulfilled_sub:
                                    recurring_profile.subscription_id = fulfilled_sub.id
                                    recurring_profile.next_charge_at = fulfilled_sub.expires_at
                                recurring_profile.recurring_operation_key = None
                                recurring_profile.recurring_operation_amount_kopecks = None
                                recurring_profile.recurring_operation_description = None
                                recurring_profile.recurring_operation_payment_method_id = None
                                recurring_profile.recurring_operation_provider_payment_id = None
                                recurring_profile.recurring_operation_started_at = None
                    else:
                        payment = Payment(
                            user_id=delivered_user_id,
                            subscription_id=subscription_id,
                            amount=provider_amount_kopecks,
                            currency="RUB",
                            method=PaymentMethod.YOOKASSA,
                            status=PaymentStatus.COMPLETED,
                            provider_payment_id=provider_payment_id,
                            tariff_id=effective_tariff_id,
                            platform=effective_platform,
                            purchase_kopecks=provider_amount_kopecks,
                            discount_kopecks=0,
                        )
                        session.add(payment)
                        discount_kopecks = 0

                    if discount_kopecks > 0:
                        user_rec = await session.get(User, delivered_user_id)
                        if user_rec:
                            already_debited = await session.scalar(
                                select(BalanceTransaction).where(
                                    BalanceTransaction.user_id == user_rec.id,
                                    BalanceTransaction.source_type == "payment_discount",
                                    BalanceTransaction.source_id == provider_payment_id,
                                ).limit(1)
                            )
                            if not already_debited:
                                debit_user_balance(
                                    session,
                                    user_rec,
                                    kopecks_to_rub_float(discount_kopecks),
                                    BalanceTransactionKind.TOPUP,
                                    "Списание с баланса при оплате тарифа",
                                    source_type="payment_discount",
                                    source_id=provider_payment_id,
                                )

                    await session.flush()
                    plog(
                        "ОПЛАТА",
                        provider="Yookassa",
                        user_id=effective_user_tg_id,
                        amount=f"{amount_val:.2f}",
                        tariff_id=effective_tariff_id or "",
                        PayId=provider_payment_id,
                    )

                    # Financial side effects (idempotent; bot=None prevents early uncommitted notification)
                    await credit_referral(session, delivered_user_id, payment.id, amount_val, bot=None)
                    await log_referral_payment(session, delivered_user_id, amount_val, bot=None, payment_id=payment.id)
                    await credit_partner(session, delivered_user_id, payment.id, amount_val, bot=None)
                    cashback_result = await credit_cashback(session, delivered_user_id, payment.id, amount_val, bot=None)

                    # Recurring profile
                    pm = payment_method_data
                    if settings.recurring_payments_enabled and pm.get("saved") and pm.get("id") and subscription_id and effective_tariff_id:
                        pm_id = pm["id"]
                        card_info = pm.get("card", {})
                        last4 = card_info.get("last4", "")
                        card_type = card_info.get("card_type", pm.get("type", "card"))
                        label = f"{card_type} *{last4}" if last4 else card_type

                        existing_profile = await session.scalar(
                            select(RecurringPaymentProfile).where(
                                RecurringPaymentProfile.user_id == delivered_user_id,
                            ).order_by(RecurringPaymentProfile.id.desc()).limit(1)
                        )

                        sub_for_date = await session.get(Subscription, subscription_id)
                        next_charge = sub_for_date.expires_at if sub_for_date else None

                        if existing_profile:
                            existing_profile.provider_payment_method_id = pm_id
                            existing_profile.payment_method_label = label
                            existing_profile.subscription_id = subscription_id
                            existing_profile.tariff_id = effective_tariff_id
                            existing_profile.is_active = True
                            existing_profile.consent_granted = True
                            existing_profile.next_charge_at = next_charge
                            existing_profile.payment_attempt_count = 0
                            existing_profile.last_payment_attempt = None
                        else:
                            session.add(RecurringPaymentProfile(
                                user_id=delivered_user_id,
                                subscription_id=subscription_id,
                                tariff_id=effective_tariff_id,
                                provider="yookassa",
                                provider_payment_method_id=pm_id,
                                payment_method_label=label,
                                is_active=True,
                                consent_granted=True,
                                next_charge_at=next_charge,
                            ))

                    if payment and not is_prior_fulfilled:
                        delivery_sub = await session.get(Subscription, subscription_id)
                        delivery_tariff = await session.get(Tariff, effective_tariff_id)
                        if delivery_sub:
                            _, plat_action, _ = decode_intent(effective_platform)
                            from bot.webhooks import _is_deferred_platform, _platform_from_str

                            if plat_action in {"renew", "upgrade"} and _is_deferred_platform(effective_platform):
                                if delivery_sub.platform:
                                    delivery_plat = (
                                        delivery_sub.platform
                                        if isinstance(delivery_sub.platform, Platform)
                                        else _platform_from_str(str(delivery_sub.platform))
                                    )
                                else:
                                    delivery_plat = _platform_from_str(effective_platform)
                            else:
                                delivery_plat = _platform_from_str(effective_platform)

                            needs_choice = bool(
                                _is_deferred_platform(effective_platform)
                                and plat_action not in {"renew", "upgrade"}
                                and delivery_tariff
                                and delivery_tariff.tariff_type != TariffType.TG_PROXY
                            )
                            delivery_platform_value = (
                                delivery_plat.value
                                if isinstance(delivery_plat, Platform)
                                else str(delivery_plat)
                            )
                            await enqueue_subscription_delivery(
                                session,
                                payment_id=payment.id,
                                kind="subscription_delivery",
                                recipient_telegram_id=int(chat_id or effective_user_tg_id),
                                telegram_user_id=int(effective_user_tg_id),
                                subscription_id=delivery_sub.id,
                                delivery_platform=delivery_platform_value,
                                needs_platform_choice=needs_choice,
                            )

                    await session.commit()
            except Exception as exc:
                logger.error("Financial finalization failed for payment %s: %s", provider_payment_id, exc)
                return False, f"financial_finalization_failed:{exc}"

            # Post-commit notifications ONLY after financial commit is fully successful
            recurring_profile_rec = None
            customer_delivery_sent = bool(is_prior_fulfilled)
            async with async_session() as session:
                sub_rec = await session.get(Subscription, subscription_id)
                user_rec = await session.get(User, delivered_user_id)
                tariff_rec = await session.get(Tariff, effective_tariff_id)
                if is_recurring and recurring_profile_id:
                    recurring_profile_rec = await session.get(
                        RecurringPaymentProfile,
                        recurring_profile_id,
                    )

            if sub_rec and not is_prior_fulfilled and db_payment_id:
                customer_delivery_sent = False
                try:
                    delivery_stats = await process_payment_notification_outbox(
                        bot,
                        payment_id=db_payment_id,
                        limit=10,
                        session_factory=async_session,
                    )
                    customer_delivery_sent = delivery_stats.get("sent", 0) > 0
                except Exception as exc:
                    logger.error(
                        "Failed to process subscription delivery outbox for user %s: %s",
                        effective_user_tg_id,
                        exc,
                    )

            if not customer_delivery_sent:
                customer_ack_required = True
                customer_ack_payment_id = db_payment_id
                customer_ack_recipient_id = effective_user_tg_id
            else:
                customer_ack_required = False

            if not is_prior_fulfilled and cashback_result.get("status") == "credited" and cashback_result.get("notify_text"):
                try:
                    await bot.send_message(effective_user_tg_id, cashback_result["notify_text"], parse_mode="HTML")
                except Exception as e:
                    logger.warning("Failed to notify user %s about cashback: %s", effective_user_tg_id, e)

            if not is_prior_fulfilled and user_rec and tariff_rec:
                if not is_recurring:
                    await notify_admins_payment(
                        bot,
                        telegram_id=effective_user_tg_id,
                        full_name=user_rec.full_name or "",
                        username=user_rec.username,
                        amount_rub=amount_val,
                        tariff_label=tariff_rec.label,
                        method="💳 YooKassa",
                        platform=effective_platform or "unknown",
                    )

            if is_recurring and recurring_profile_rec and user_rec and tariff_rec:
                try:
                    from bot.utils.texts import RECURRING_CHARGE_SUCCESS

                    expires_str = sub_rec.expires_at.strftime("%d.%m.%Y") if sub_rec and sub_rec.expires_at else "—"
                    payment_label = recurring_profile_rec.payment_method_label or recurring_profile_rec.provider
                    await bot.send_message(
                        user_rec.telegram_id,
                        RECURRING_CHARGE_SUCCESS.format(
                            expires=expires_str,
                            payment_method=payment_label,
                            amount=tariff_rec.price_rub,
                        ),
                        parse_mode="HTML",
                    )
                except Exception as exc:
                    logger.warning(
                        "Failed to send recurring success notification for payment %s: %s",
                        provider_payment_id,
                        exc,
                    )

                await notify_admins_payment(
                    bot,
                    telegram_id=effective_user_tg_id,
                    full_name=user_rec.full_name or "",
                    username=user_rec.username,
                    amount_rub=amount_val,
                    tariff_label=tariff_rec.label,
                    method="💳 YooKassa",
                    platform=effective_platform or "unknown",
                )

            if is_prior_fulfilled:
                return True, "already_manually_or_previously_fulfilled"
            return True, "subscription_completed"

        return False, "delivery_incomplete"

    finally:
        if heartbeat is not None:
            await heartbeat.stop()
            await lease_manager.release(lock_name)
        try:
            await _finalize_customer_payment_notification(
                bot,
                provider_payment_id,
                payment_id=customer_ack_payment_id,
                recipient_telegram_id=customer_ack_recipient_id,
                required=customer_ack_required,
                recipient_conflict=customer_ack_recipient_conflict,
            )
        except Exception:
            logger.exception(
                "Failed to finalize customer notification for YooKassa payment %s",
                provider_payment_id,
            )


async def reconcile_pending_yookassa_payments(bot: Bot) -> dict[str, int]:
    """Reconcile lost webhooks and every durable paid-but-unfinished operation.

    New pending operations are bounded to the normal 24-hour retry window.  A
    provider-paid operation is kept in the scan until its local fulfillment is
    terminal, even when it is older than that window.
    """
    now = datetime.utcnow()
    window_start = now - timedelta(hours=24)
    window_end = now - timedelta(minutes=2)

    stats = {
        "scanned": 0,
        "fulfilled": 0,
        "still_pending": 0,
        "canceled": 0,
        "locked": 0,
        "failed": 0,
    }

    async with async_session() as session:
        # Count locked payments that require manual review (to monitor locked count without queue starvation)
        cnt_locked = await session.scalar(
            select(func.count(Payment.id))
            .where(
                Payment.provider_status == "succeeded",
                Payment.provider_paid.is_(True),
                Payment.status != PaymentStatus.COMPLETED,
                or_(
                    Payment.provisioning_failure_code.in_(MANUAL_REVIEW_PROVISIONING_CODES),
                    Payment.provisioning_failure_code.like("%manual_review%"),
                ),
            )
        )
        stats["locked"] = cnt_locked or 0

        # Keep due retries separate from never-attempted work. A retry slice
        # cannot consume the fresh-work slice, so a large funds-wait backlog
        # cannot starve newly paid orders.
        payment_common = and_(
            Payment.method == PaymentMethod.YOOKASSA,
            Payment.provider_payment_id.is_not(None),
            or_(
                Payment.provisioning_failure_code.is_(None),
                and_(
                    ~Payment.provisioning_failure_code.in_(MANUAL_REVIEW_PROVISIONING_CODES),
                    ~Payment.provisioning_failure_code.like("%manual_review%"),
                ),
            ),
        )
        payment_operation = or_(
            and_(
                or_(
                    Payment.status == PaymentStatus.PENDING,
                    Payment.provisioning_failure_code == "financial_finalization_pending",
                ),
                Payment.created_at >= window_start,
                Payment.created_at <= window_end,
            ),
            and_(
                Payment.provider_status == "succeeded",
                Payment.provider_paid.is_(True),
                or_(
                    Payment.status != PaymentStatus.COMPLETED,
                    Payment.subscription_id.is_(None),
                    Payment.provisioning_failure_code == "financial_finalization_pending",
                ),
                Payment.created_at <= window_end,
            ),
        )

        retry_payments = await session.scalars(
            select(Payment.provider_payment_id)
            .where(payment_common, payment_operation)
            .where(Payment.next_attempt_at.is_not(None))
            .where(Payment.next_attempt_at <= now)
            .order_by(Payment.next_attempt_at.asc(), Payment.created_at.asc(), Payment.id.asc())
            .limit(25)
        )
        fresh_oldest_payments = await session.scalars(
            select(Payment.provider_payment_id)
            .where(payment_common, payment_operation)
            .where(Payment.next_attempt_at.is_(None))
            .order_by(Payment.created_at.asc(), Payment.id.asc())
            .limit(20)
        )
        fresh_newest_payments = await session.scalars(
            select(Payment.provider_payment_id)
            .where(payment_common, payment_operation)
            .where(Payment.next_attempt_at.is_(None))
            .order_by(Payment.created_at.desc(), Payment.id.desc())
            .limit(5)
        )
        payment_provider_ids = [
            provider_id
            for provider_id in [
                *retry_payments.all(),
                *fresh_oldest_payments.all(),
                *fresh_newest_payments.all(),
            ]
            if provider_id
        ]

        topup_common = and_(
            BalanceTopUp.provider == "yookassa",
            BalanceTopUp.provider_payment_id.is_not(None),
        )
        topup_operation = or_(
            and_(
                BalanceTopUp.status == "pending",
                BalanceTopUp.created_at >= window_start,
                BalanceTopUp.created_at <= window_end,
            ),
            and_(
                BalanceTopUp.provider_status == "succeeded",
                BalanceTopUp.provider_paid.is_(True),
                BalanceTopUp.status != "completed",
                BalanceTopUp.created_at <= window_end,
            ),
        )
        retry_topups = await session.scalars(
            select(BalanceTopUp.provider_payment_id)
            .where(topup_common, topup_operation)
            .where(BalanceTopUp.next_attempt_at.is_not(None))
            .where(BalanceTopUp.next_attempt_at <= now)
            .order_by(BalanceTopUp.next_attempt_at.asc(), BalanceTopUp.created_at.asc(), BalanceTopUp.id.asc())
            .limit(25)
        )
        fresh_oldest_topups = await session.scalars(
            select(BalanceTopUp.provider_payment_id)
            .where(topup_common, topup_operation)
            .where(BalanceTopUp.next_attempt_at.is_(None))
            .order_by(BalanceTopUp.created_at.asc(), BalanceTopUp.id.asc())
            .limit(20)
        )
        fresh_newest_topups = await session.scalars(
            select(BalanceTopUp.provider_payment_id)
            .where(topup_common, topup_operation)
            .where(BalanceTopUp.next_attempt_at.is_(None))
            .order_by(BalanceTopUp.created_at.desc(), BalanceTopUp.id.desc())
            .limit(5)
        )
        topup_provider_ids = [
            provider_id
            for provider_id in [
                *retry_topups.all(),
                *fresh_oldest_topups.all(),
                *fresh_newest_topups.all(),
            ]
            if provider_id
        ]

    all_provider_ids = list(dict.fromkeys(payment_provider_ids + topup_provider_ids))
    stats["scanned"] = len(all_provider_ids)

    if not all_provider_ids:
        return stats

    logger.info("YooKassa reconciliation scanning %d pending payment(s)", len(all_provider_ids))

    for provider_payment_id in all_provider_ids:
        try:
            success, code = await verify_and_fulfill_yookassa_payment(
                bot,
                provider_payment_id,
                source="scheduler",
            )
            if success:
                stats["fulfilled"] += 1
                logger.info("Reconciled and fulfilled YooKassa payment: %s (%s)", provider_payment_id, code)
            elif code == "payment_still_pending":
                stats["still_pending"] += 1
            elif code in ("payment_canceled", "payment_canceled_at_provider", "payment_canceled_with_financial_effects_review_needed"):
                stats["canceled"] += 1
            elif code == "locked_by_another_execution":
                stats["locked"] += 1
            else:
                stats["failed"] += 1
                logger.warning("Reconciliation skipped/failed for %s: %s", provider_payment_id, code)
        except Exception as exc:
            stats["failed"] += 1
            logger.exception("Unexpected error reconciling payment %s: %s", provider_payment_id, exc)

    logger.info("YooKassa reconciliation finished: %s", stats)
    return stats
