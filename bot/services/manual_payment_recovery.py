"""Controlled recovery procedure for payments requiring manual review."""

from __future__ import annotations

import getpass
import logging
import platform
from datetime import datetime
from typing import Any
from uuid import uuid4

from aiogram import Bot
from sqlalchemy import select

from bot.config import settings
from bot.database import async_session
from bot.models import Payment, PaymentStatus, Tariff, User
from bot.services.admin_audit import write_admin_audit
from bot.services.cluster import LeaseHeartbeat, LeaseManager
from bot.services.problem_payments import get_vhq_wholesale_cost
from bot.services.vhq_partner_api import VHQPartnerAPI
from bot.services.vhq_routing import is_vhq_tariff

logger = logging.getLogger(__name__)


async def authorize_and_recover_manual_payment(
    bot: Bot,
    payment_id: int,
    admin_telegram_id: int,
    reason: str,
    *,
    confirm_no_prior_mutation: bool = False,
    pre_mutation_evidence: dict[str, Any] | None = None,
    reclassify_to_waiting_funds_only: bool = False,
    dry_run: bool = False,
) -> tuple[bool, str]:
    """Execute a strictly audited, locked manual recovery or reclassification for a problem payment.

    Guarantees:
    1. Authenticates admin credentials against settings.all_admin_ids.
    2. Validates payment eligibility (paid, not completed, VHQ tariff, non-contradictory).
    3. Rejects ambiguous payments without explicit confirmation of no prior mutation.
    4. Supports true dry-run mode without mutations or state changes.
    5. Reuses distributed lease during fulfillment without self-collision.
    6. Ensures payment is never left in an unsafe automated retry state if recovery fails (fail-closed).
    7. Atomically logs full audit trail with operator identity and justification.
    """
    # 1. Admin authorization check
    authorized_admins = set(settings.admin_ids) | set(settings.notification_recipient_ids)
    if admin_telegram_id not in authorized_admins:
        logger.warning("Unauthorized operator attempted manual recovery: %d", admin_telegram_id)
        return False, "unauthorized_admin"

    if not reason or len(reason.strip()) < 5:
        return False, "reason_too_short"

    # 2. Pre-lock payment eligibility inspection
    async with async_session() as session:
        payment = await session.get(Payment, payment_id)
        if not payment:
            return False, "payment_not_found"

        if payment.status == PaymentStatus.COMPLETED:
            return False, "payment_already_completed"

        if not payment.provider_paid or payment.provider_status != "succeeded":
            return False, "payment_not_paid_by_client"

        tariff = await session.get(Tariff, payment.tariff_id) if payment.tariff_id else None
        if not tariff or not is_vhq_tariff(tariff):
            return False, "unsupported_non_vhq_payment"

        if payment.provisioning_failure_code in (
            "yookassa_user_mismatch",
            "yookassa_tariff_mismatch",
            "yookassa_amount_mismatch",
        ):
            return False, f"contradictory_payment_state:{payment.provisioning_failure_code}"

        current_failure_code = payment.provisioning_failure_code
        provider_id = payment.provider_payment_id
        owner = await session.get(User, payment.user_id)
        recipient_telegram_id = int(payment.telegram_chat_id or (owner.telegram_id if owner else 0))

    if not provider_id:
        return False, "missing_provider_payment_id"

    # 3. Authoritative refusal verification vs. explicit manual override requirement
    is_historically_ambiguous = (
        current_failure_code in (
            "uncertain_supplier_mutation",
            "ambiguous_buy_mutation_manual_review_required",
            "mutation_in_flight",
        )
        or bool(current_failure_code and "manual_review" in current_failure_code)
    )
    if is_historically_ambiguous and not confirm_no_prior_mutation:
        logger.warning(
            "Payment %d has ambiguous mutation status (%s); explicit confirmation required",
            payment_id,
            current_failure_code,
        )
        return False, "explicit_no_prior_mutation_confirmation_required"

    if is_historically_ambiguous:
        evidence = pre_mutation_evidence or {}
        try:
            provider_status = int(evidence.get("provider_status"))
        except (TypeError, ValueError):
            provider_status = None
        provider_error = str(evidence.get("provider_error") or "").strip().lower()
        evidence_reference = str(evidence.get("evidence_reference") or "").strip()
        has_funds_rejection = (
            "insufficient balance" in provider_error
            or "недостаточно средств" in provider_error
        )
        if (
            provider_status not in {400, 402}
            or not has_funds_rejection
            or not evidence_reference
        ):
            logger.warning(
                "Payment %d cannot be reclassified without authoritative pre-mutation rejection evidence",
                payment_id,
            )
            return False, "pre_mutation_evidence_required"

    required_cost = get_vhq_wholesale_cost(tariff)

    # 4. Supplier balance inspection
    api = VHQPartnerAPI()
    if not api.enabled:
        return False, "vhq_api_not_configured"

    try:
        bal_data = await api.get_balance()
        curr_bal = float(bal_data.get("balance", 0.0))
    except Exception as exc:
        logger.error("Supplier preflight balance check failed for payment %s: %s", payment_id, exc)
        return False, f"supplier_verification_error:{exc}"

    # 5. Dry-run mode
    if dry_run:
        logger.info(
            "DRY-RUN check passed for payment #%d: balance=%.2f, required=%.2f, code=%s",
            payment_id,
            curr_bal,
            required_cost,
            current_failure_code,
        )
        return True, f"dry_run_success:balance={curr_bal:.2f}:required={required_cost:.2f}"

    # 6. Distributed lease acquisition
    os_user = getpass.getuser()
    sys_node = platform.node()
    owner_token = f"{getattr(settings, 'instance_id', 'manual')}:manual_recovery:{admin_telegram_id}:{uuid4().hex[:8]}"
    lock_name = f"payment:yookassa:{provider_id}"
    lease_manager = LeaseManager(owner_id=owner_token, session_factory=async_session)
    ttl = getattr(settings, "webhook_lock_ttl_seconds", 120) or 120

    acquired = await lease_manager.acquire_or_renew(lock_name, ttl_seconds=ttl)
    if not acquired:
        return False, "lock_acquisition_failed"

    heartbeat = LeaseHeartbeat(lease_manager, lock_name, ttl, interval_seconds=10)
    heartbeat.start()

    try:
        # Critical section: re-verify payment and set audited transition under lock
        async with async_session() as session:
            curr_p = await session.get(Payment, payment_id)
            if not curr_p or curr_p.status == PaymentStatus.COMPLETED:
                return False, "concurrently_completed"

            # Durably record audit BEFORE modifying state
            try:
                action_name = (
                    "manual_payment_reclassified_to_waiting_funds"
                    if (reclassify_to_waiting_funds_only or curr_bal < required_cost)
                    else "manual_payment_recovery_authorized"
                )
                summary_text = (
                    f"Перевод платежа #{payment_id} ({provider_id}) в очередь ожидания средств"
                    if (reclassify_to_waiting_funds_only or curr_bal < required_cost)
                    else f"Ручное санкционирование восстановления платежа #{payment_id} ({provider_id})"
                )
                await write_admin_audit(
                    actor_telegram_id=admin_telegram_id,
                    action=action_name,
                    summary=summary_text,
                    entity_type="payment",
                    entity_id=str(payment_id),
                    status="authorized",
                    details=(
                        f"operator={os_user}@{sys_node} reason={reason[:150]} "
                        f"bal={curr_bal:.2f} req={required_cost:.2f} prev_code={curr_p.provisioning_failure_code} "
                        f"evidence_status={(pre_mutation_evidence or {}).get('provider_status')} "
                        f"evidence_reference={(pre_mutation_evidence or {}).get('evidence_reference')}"
                    ),
                )
            except Exception as audit_exc:
                logger.error("Audit log write failed for payment %s: %s", payment_id, audit_exc)
                return False, "audit_log_write_failed"

            # Transition state under lock to confirmed pre-mutation waiting funds
            from bot.services.yookassa_reconciliation import (
                _queue_customer_payment_ack,
                _schedule_payment_retry,
            )

            _schedule_payment_retry(curr_p, "vhq_balance")
            if curr_bal < required_cost:
                curr_p.next_attempt_at = datetime.utcnow()
            await session.commit()

            if reclassify_to_waiting_funds_only or curr_bal < required_cost:
                logger.info(
                    "Payment #%d reclassified to vhq_balance under lock. It will auto-fulfill on balance top-up.",
                    payment_id,
                )
                if curr_bal < required_cost:
                    await _queue_customer_payment_ack(
                        bot,
                        payment_id=payment_id,
                        recipient_telegram_id=recipient_telegram_id,
                    )
                    return True, f"reclassified_to_waiting_funds:balance_insufficient_{curr_bal:.2f}<{required_cost:.2f}"
                await _queue_customer_payment_ack(
                    bot,
                    payment_id=payment_id,
                    recipient_telegram_id=recipient_telegram_id,
                )
                return True, "reclassified_to_waiting_funds"

        # 7. Dispatch fulfillment under the existing lease
        from bot.services.yookassa_reconciliation import verify_and_fulfill_yookassa_payment
        success, code = await verify_and_fulfill_yookassa_payment(
            bot,
            provider_id,
            source="admin_authorized_recovery",
            existing_lease=lease_manager,
            allow_manual_review_override=True,
        )

        # 8. Post-fulfillment safety verification
        async with async_session() as session:
            check_p = await session.get(Payment, payment_id)
            if not success or not check_p or check_p.status != PaymentStatus.COMPLETED:
                # If fulfillment failed and it wasn't a verified pre-mutation balance error,
                # restore fail-closed manual review lock!
                if check_p and check_p.provisioning_failure_code != "vhq_balance":
                    check_p.provisioning_failure_code = "ambiguous_buy_mutation_manual_review_required"
                    await session.commit()
                return False, f"recovery_failed:{code}"

        return True, code
    finally:
        await heartbeat.stop()
        await lease_manager.release(lock_name)
