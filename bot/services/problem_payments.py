"""Problem Payments service and administrator diagnostics.

Categorizes and manages problem payments:
- Confirmed paid but not fulfilled (urgent incident).
- Payment still pending at YooKassa (not yet a delivery incident).
- Captured payment with failed or uncertain supplier fulfillment.
- Completed payment with unresolved subscription delivery.
- Records missing provider_payment_id (manual investigation needed).

Excludes legitimate asynchronous VHQ provisioning while operating normally.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import enum
import html
import json
import logging
import re
from typing import Any

from aiogram import Bot
from sqlalchemy import and_, cast, distinct, exists, func, literal, or_, select, String

from bot.database import async_session
from bot.models import (
    AdminActionLog,
    BalanceDirection,
    BalanceTopUp,
    BalanceTransaction,
    BalanceTransactionKind,
    BotSettings,
    ManualSubscriptionOperation,
    Payment,
    PaymentMethod,
    PaymentNotificationOutbox,
    PaymentStatus,
    SubStatus,
    Subscription,
    Tariff,
    User,
    VhqSubscriptionFulfillment,
)
from bot.services.notifications import notify_admins_issue, notify_staff_text
from bot.services.money import kopecks_to_rub_float, rub_to_kopecks
from bot.services.yookassa_reconciliation import (
    fetch_yookassa_payment,
    filter_real_competing_payments,
    verify_and_fulfill_yookassa_payment,
)
from bot.services.vhq_routing import is_vhq_tariff

logger = logging.getLogger(__name__)


class ProblemCategory(str, enum.Enum):
    CONFIRMED_UNFULFILLED = "confirmed_unfulfilled"       # Urgent: paid at YooKassa, not delivered locally
    MANUALLY_FULFILLED = "manually_fulfilled"             # Paid at YooKassa, verified granted manually
    VERIFIED_MANUALLY_FULFILLED = "manually_fulfilled"    # Explicit alias matching VERIFIED_MANUALLY_FULFILLED
    DELIVERY_UNCERTAIN = "delivery_uncertain"             # Paid or completed, but fulfillment status ambiguous (manual review required)
    TELEGRAM_DELIVERY_FAILED = "telegram_delivery_failed" # Sub created, but Telegram delivery failed
    PROVIDER_PENDING = "provider_pending"                 # Normal: pending at YooKassa (not yet a delivery incident)
    FAILED_SUPPLIER = "failed_supplier"                   # Captured/paid, but upstream supplier mutation failed/stalled
    UNRESOLVED_SUBSCRIPTION = "unresolved_subscription"   # Completed payment without linked subscription
    MISSING_PROVIDER_ID = "missing_provider_id"           # Local pending record missing provider_payment_id (>15m)
    PROCESSED = "processed"                               # Normal completed payment or topup (no problem)


class ProblemScope(str, enum.Enum):
    ACTIVE = "active"     # Last 7 days
    ARCHIVE = "archive"   # Older than 7 days


@dataclass
class FastDashboardSummary:
    urgent_unfulfilled: int
    tg_delivery_failed: int
    supplier_failed: int
    needs_review: int
    total_active: int
    total_archive: int
    urgent_items: list[ProblemPaymentItem]


@dataclass
class CachedLiveStatus:
    status: str
    paid: bool
    status_label: str
    checked_at: datetime

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


# Short in-memory cache for on-demand live YooKassa checks
_live_yk_cache: dict[str, CachedLiveStatus] = {}


def get_cached_live_status(item_type: str, item_id: int) -> CachedLiveStatus | None:
    key = f"{item_type}:{item_id}"
    if key in _live_yk_cache:
        cached = _live_yk_cache[key]
        if datetime.utcnow() - cached.checked_at < timedelta(minutes=5):
            return cached
    return None


def set_cached_live_status(item_type: str, item_id: int, data: dict[str, Any] | CachedLiveStatus) -> CachedLiveStatus:
    if isinstance(data, CachedLiveStatus):
        cached = data
    else:
        cached = CachedLiveStatus(
            status=data.get("status", "unknown"),
            paid=data.get("paid", False),
            status_label=data.get("status_label", ""),
            checked_at=datetime.utcnow(),
        )
    _live_yk_cache[f"{item_type}:{item_id}"] = cached
    return cached


@dataclass
class ProblemPaymentDiagnosis:
    supplier_name: str | None
    summary_title: str
    client_status: str
    fulfillment_status: str
    root_cause: str
    recommended_action: str
    safe_retry_eligible: bool
    diagnostic_code: str


@dataclass
class ProblemPaymentItem:
    item_type: str  # "payment" | "topup"
    item_id: int
    user_id: int | None
    telegram_id: int | None
    username: str | None
    full_name: str | None
    amount_rub: float
    created_at: datetime
    age_str: str
    provider_payment_id: str | None
    local_status: str
    verified_provider_status: str | None
    category: ProblemCategory
    fulfillment_state: str
    details: str
    supplier_name: str | None = None
    root_cause: str | None = None
    recommended_action: str | None = None
    safe_retry_eligible: bool = False
    diagnostic_code: str | None = None


def format_reconciliation_result(res_code: str) -> tuple[str, str]:
    """Convert technical reconciliation result code into a human-friendly Russian description and recommended action."""
    clean_code = str(res_code or "").strip()
    if clean_code.startswith("delivery_failed:"):
        sub_code = clean_code.split(":", 1)[1]
    else:
        sub_code = clean_code

    if sub_code in ("subscription_completed", "already_completed", "already_manually_or_previously_fulfilled"):
        return (
            "Услуга успешно выдана клиенту, платёж финализирован.",
            "Дополнительных действий не требуется.",
        )
    if sub_code == "balance_topup_completed":
        return (
            "Баланс пользователя успешно пополнен.",
            "Дополнительных действий не требуется.",
        )
    if sub_code == "device_slot_completed":
        return (
            "Слот устройства успешно добавлен клиенту.",
            "Дополнительных действий не требуется.",
        )
    if sub_code == "vhq_balance":
        return (
            "VHQ — недостаточно средств на партнёрском балансе.",
            "Пополните баланс в кабинете VHQ, затем повторите выдачу через сверку.",
        )
    if sub_code == "vhq_auth":
        return (
            "VHQ — ошибка аутентификации API партнёра.",
            "Проверьте настройки VHQ_PARTNER_API_KEY в конфигурации сервера.",
        )
    if sub_code == "vhq_request_invalid":
        return (
            "VHQ — неверные параметры запроса.",
            "Проверьте настройки тарифа и обратитесь в поддержку VHQ.",
        )
    if sub_code in ("uncertain_supplier_mutation", "supplier_mutation_in_flight_or_uncertain", "mutation_in_flight"):
        return (
            "Неопределённое состояние у поставщика (сетевой сбой или таймаут).",
            "Проверьте кабинет поставщика: если заказ не создан, выполните сверку повторно. Не выдавайте ключ вручную без проверки.",
        )
    if sub_code == "locked_by_another_execution":
        return (
            "Операция уже выполняется параллельным процессом (active lease lock).",
            "Подождите 30 секунд и повторите попытку.",
        )
    if sub_code == "payment_still_pending":
        return (
            "Платёж в YooKassa ещё не завершён клиентом.",
            "Дождитесь завершения оплаты клиентом или автоматической отмены шлюзом.",
        )
    if sub_code == "payment_canceled":
        return (
            "Платёж отменён в YooKassa клиентом или банком.",
            "Выдача услуги не требуется.",
        )
    if sub_code == "payment_not_found":
        return (
            "Платёж не найден в YooKassa.",
            "Проверьте ID платежа в личном кабинете платёжного шлюза.",
        )
    if sub_code == "provider_unavailable":
        return (
            "Шлюз YooKassa временно недоступен.",
            "Повторите попытку через 1-2 минуты.",
        )
    if sub_code == "adapt_api_402":
        return (
            "Adapt — недостаточно средств на балансе поставщика.",
            "Пополните баланс в кабинете Adapt Group и повторите сверку.",
        )
    if sub_code == "ambiguous_buy_mutation_manual_review_required":
        return (
            "VHQ — неопределённое состояние закупки (требуется ручная проверка).",
            "Проверьте историю списаний и заказов в кабинете VHQ перед повторной выдачей во избежание двойной оплаты.",
        )
    if sub_code == "ambiguous_renewal_manual_review_required":
        return (
            "VHQ — неопределённое состояние продления (требуется ручная проверка).",
            "Проверьте срок действия подписки в кабинете VHQ перед повторным продлением во избежание двойного списания.",
        )
    if sub_code == "ambiguous_competing_orders_at_supplier":
        return (
            "У поставщика обнаружены конкурирующие заказы в этом временном окне.",
            "Требуется ручная сверка заказов в кабинете поставщика во избежание двойной оплаты.",
        )
    if sub_code == "ambiguous_competing_payments_for_subscription":
        return (
            "Обнаружено несколько платежей для одной подписки в близкое время.",
            "Проверьте историю платежей пользователя вручную перед повторной выдачей.",
        )
    if sub_code == "historical_payment_manual_review_required":
        return (
            "Исторический платёж (старше 7 дней).",
            "Проверьте историю пользователя и выполните ручную выдачу при необходимости.",
        )
    if sub_code == "renewal_evidence_missing_manual_review_required":
        return (
            "Отсутствуют однозначные данные о продлении подписки.",
            "Проверьте статус подписки в кабинете поставщика.",
        )
    if sub_code == "target_subscription_not_found":
        return (
            "Целевая подписка для продления не найдена в базе данных.",
            "Проверьте данные подписки пользователя.",
        )
    if sub_code == "target_sub_user_mismatch":
        return (
            "Целевая подписка принадлежит другому пользователю.",
            "Проверьте привязку пользователя к подписке.",
        )
    if sub_code in ("amount_mismatch", "currency_mismatch", "user_mismatch"):
        return (
            f"Несовпадение параметров платежа ({sub_code}).",
            "Сверьте сумму и валюту в YooKassa с параметрами тарифа.",
        )
    if clean_code.startswith("delivery_failed:"):
        return (
            f"Сбой выдачи у поставщика ({sub_code}).",
            "Проверьте статус в кабинете поставщика и логи сервера.",
        )
    return (
        f"Результат сверки: {clean_code}",
        "Проверьте статус платежа и подписки в панели администратора.",
    )


VHQ_EXACT_PRICING: dict[str, dict[int, float]] = {
    "lite": {
        1: 1.0,
        7: 59.0,
        30: 149.0,
        90: 359.0,
        365: 1199.0,
    },
    "basic": {
        30: 249.0,
        180: 1379.0,
        365: 2219.0,
    },
}


def get_vhq_wholesale_cost(tariff: Any) -> float:
    """Calculate wholesale VHQ procurement cost in RUB based on tier and duration according to official VHQ API pricing."""
    if not tariff:
        return 249.0
    from bot.services.vhq_routing import get_vhq_spec_for_tariff
    v_spec = get_vhq_spec_for_tariff(tariff)
    tier = str((v_spec.get("tier") if v_spec else getattr(tariff, "vhq_tier", None)) or "basic").strip().lower()
    days = int((v_spec.get("days") if v_spec else getattr(tariff, "days", None)) or 30)

    pricing_map = VHQ_EXACT_PRICING.get(tier, VHQ_EXACT_PRICING["basic"])
    if days in pricing_map:
        return pricing_map[days]

    # For non-standard day counts, find the exact or closest supported plan without arbitrary multiplication
    sorted_days = sorted(pricing_map.keys())
    for d in sorted_days:
        if days <= d:
            return pricing_map[d]
    return pricing_map[sorted_days[-1]]


def _format_vhq_cost_hint(tariff: Any) -> str:
    if not tariff:
        return ""
    wholesale_cost = get_vhq_wholesale_cost(tariff)
    return f" (закупка VHQ: {wholesale_cost:.0f} ₽)"


def diagnose_payment_problem(
    payment: Payment,
    tariff: Tariff | None = None,
    vhq_row: VhqSubscriptionFulfillment | None = None,
) -> ProblemPaymentDiagnosis:
    """Analyze a problem payment and return unified, structured diagnostic details."""
    supplier_name: str | None = None
    if vhq_row is not None or (payment.provisioning_failure_code and "vhq" in payment.provisioning_failure_code):
        supplier_name = "VHQ"
    elif tariff is not None:
        if is_vhq_tariff(tariff):
            supplier_name = "VHQ"
        elif getattr(tariff, "adapt_plan_uuid", None):
            supplier_name = "Adapt"
        else:
            supplier_name = "Локально"

    if payment.provider_status == "succeeded" or payment.provider_paid:
        client_status = "YooKassa: оплачено"
    elif payment.provider_status == "canceled":
        client_status = "YooKassa: отменено"
    elif payment.status == PaymentStatus.PENDING:
        client_status = "YooKassa: ожидает оплаты"
    else:
        client_status = f"Статус: {payment.status.value}"

    if payment.status == PaymentStatus.COMPLETED and getattr(payment, "telegram_delivery_error", None):
        return ProblemPaymentDiagnosis(
            supplier_name=supplier_name or "Локально",
            summary_title="Telegram — сбой доставки клиенту",
            client_status="Оплачено и создано в БД",
            fulfillment_status="VPN: создан в БД, не доставлен в TG",
            root_cause=f"Подписка #{payment.subscription_id} создана в БД, но сообщение клиенту не дошло: {payment.telegram_delivery_error}",
            recommended_action="Нажмите «Повторить отправку в TG» (безопасно, без повторной закупки).",
            safe_retry_eligible=True,
            diagnostic_code="telegram_delivery_failed",
        )

    if not payment.provider_payment_id and payment.status == PaymentStatus.PENDING:
        return ProblemPaymentDiagnosis(
            supplier_name="YooKassa",
            summary_title="YooKassa — платёж без ID провайдера",
            client_status="Не оплачен (нет ID)",
            fulfillment_status="VPN: не выдан",
            root_cause=f"Платёж #{payment.id} создан без ID провайдера (окно оплаты не было открыто).",
            recommended_action="Действий не требуется. Если клиент обратится, проверьте ЛК YooKassa.",
            safe_retry_eligible=False,
            diagnostic_code="missing_provider_id",
        )

    if vhq_row:
        if vhq_row.status in (
            "pending", "processing", "retry_wait", "uncertain", "buy_inflight", "renew_inflight"
        ):
            is_balance = (
                vhq_row.last_error_code == "insufficient_balance"
                or "insufficient balance" in str(vhq_row.last_error_message or "").lower()
            )
            if is_balance:
                cost_hint = _format_vhq_cost_hint(tariff)
                return ProblemPaymentDiagnosis(
                    supplier_name="VHQ",
                    summary_title="VHQ — недостаточно средств на партнёрском балансе",
                    client_status=client_status,
                    fulfillment_status="VPN: не выдан (ожидает пополнения VHQ)",
                    root_cause=f"Недостаточно средств на партнёрском балансе VHQ{cost_hint}: {vhq_row.last_error_message or 'баланс исчерпан'}.",
                    recommended_action="Пополнить баланс VHQ и повторить выдачу через сверку.",
                    safe_retry_eligible=True,
                    diagnostic_code="vhq_balance",
                )
            return ProblemPaymentDiagnosis(
                supplier_name="VHQ",
                summary_title="VHQ — фоновая выдача зависла",
                client_status=client_status,
                fulfillment_status="VPN: не выдан (в очереди)",
                root_cause=f"Очередь выдачи VHQ зависла ({vhq_row.attempts}/{vhq_row.max_attempts} поп.): {vhq_row.last_error_message or vhq_row.status}.",
                recommended_action="Проверить статус заказа в кабинете VHQ перед повторной выдачей.",
                safe_retry_eligible=False,
                diagnostic_code=vhq_row.last_error_code or "vhq_stalled",
            )
        elif vhq_row.status == "failed":
            is_balance = (
                vhq_row.last_error_code == "insufficient_balance"
                or "insufficient balance" in str(vhq_row.last_error_message or "").lower()
            )
            if is_balance:
                cost_hint = _format_vhq_cost_hint(tariff)
                return ProblemPaymentDiagnosis(
                    supplier_name="VHQ",
                    summary_title="VHQ — недостаточно средств на партнёрском балансе",
                    client_status=client_status,
                    fulfillment_status="VPN: не выдан",
                    root_cause=f"Недостаточно средств на партнёрском балансе VHQ{cost_hint}: {vhq_row.last_error_message or 'баланс исчерпан'}.",
                    recommended_action="Пополнить баланс VHQ и повторить выдачу через сверку.",
                    safe_retry_eligible=True,
                    diagnostic_code="vhq_balance",
                )
            return ProblemPaymentDiagnosis(
                supplier_name="VHQ",
                summary_title="VHQ — сбой поставщика",
                client_status=client_status,
                fulfillment_status="VPN: не выдан",
                root_cause=f"Поставщик VHQ вернул ошибку: {vhq_row.last_error_message or vhq_row.last_error_code or 'неизвестный сбой'}.",
                recommended_action="Проверить статус заказа в кабинете VHQ.",
                safe_retry_eligible=False,
                diagnostic_code=vhq_row.last_error_code or "vhq_failed",
            )

    fail_code = payment.provisioning_failure_code
    if fail_code == "vhq_balance":
        cost_hint = _format_vhq_cost_hint(tariff)
        return ProblemPaymentDiagnosis(
            supplier_name="VHQ",
            summary_title="VHQ — недостаточно средств на партнёрском балансе",
            client_status=client_status,
            fulfillment_status="VPN: не выдан",
            root_cause=f"Недостаточно средств на партнёрском балансе VHQ{cost_hint}.",
            recommended_action="Пополнить баланс VHQ и повторить выдачу через сверку.",
            safe_retry_eligible=True,
            diagnostic_code="vhq_balance",
        )
    if fail_code == "vhq_auth":
        return ProblemPaymentDiagnosis(
            supplier_name="VHQ",
            summary_title="VHQ — ошибка авторизации API",
            client_status=client_status,
            fulfillment_status="VPN: не выдан",
            root_cause="Ошибка аутентификации API VHQ (проверьте VHQ_PARTNER_API_KEY в .env).",
            recommended_action="Проверить VHQ_PARTNER_API_KEY в конфигурации сервера.",
            safe_retry_eligible=False,
            diagnostic_code="vhq_auth",
        )
    if fail_code == "vhq_request_invalid":
        return ProblemPaymentDiagnosis(
            supplier_name="VHQ",
            summary_title="VHQ — неверные параметры запроса",
            client_status=client_status,
            fulfillment_status="VPN: не выдан",
            root_cause="Поставщик VHQ отклонил параметры заказа (400 Bad Request).",
            recommended_action="Проверить конфигурацию тарифа в базе данных.",
            safe_retry_eligible=False,
            diagnostic_code="vhq_request_invalid",
        )
    if fail_code in ("uncertain_supplier_mutation", "mutation_in_flight"):
        supp = supplier_name or "Поставщик"
        return ProblemPaymentDiagnosis(
            supplier_name=supplier_name,
            summary_title=f"{supp} — неопределённое состояние мутации",
            client_status=client_status,
            fulfillment_status="VPN: не выдан (требует подтверждения)",
            root_cause="Сетевой таймаут или сбой при обращении к поставщику. Создание заказа не подтверждено.",
            recommended_action="Проверить кабинет поставщика перед повторной выдачей. Не выдавать доступ вручную без проверки.",
            safe_retry_eligible=False,
            diagnostic_code=fail_code,
        )
    if fail_code == "ambiguous_buy_mutation_manual_review_required":
        return ProblemPaymentDiagnosis(
            supplier_name="VHQ",
            summary_title="VHQ — закупка требует ручной проверки",
            client_status=client_status,
            fulfillment_status="VPN: заблокирован (ручная проверка)",
            root_cause="Неопределённое состояние закупки у поставщика VHQ. Автоматический повтор запрещён во избежание двойного списания.",
            recommended_action="Проверить историю заказов и списаний в кабинете VHQ перед повторной выдачей.",
            safe_retry_eligible=False,
            diagnostic_code="ambiguous_buy_mutation_manual_review_required",
        )
    if fail_code == "ambiguous_renewal_manual_review_required":
        return ProblemPaymentDiagnosis(
            supplier_name="VHQ",
            summary_title="VHQ — продление требует ручной проверки",
            client_status=client_status,
            fulfillment_status="VPN: заблокирован (ручная проверка)",
            root_cause="Неопределённое состояние продления в VHQ. Автоматический повтор запрещён во избежание двойного продления.",
            recommended_action="Проверить срок действия подписки в кабинете VHQ перед повторным продлением.",
            safe_retry_eligible=False,
            diagnostic_code="ambiguous_renewal_manual_review_required",
        )
    if fail_code == "ambiguous_competing_orders_at_supplier":
        return ProblemPaymentDiagnosis(
            supplier_name="VHQ",
            summary_title="VHQ — конкурирующие заказы у поставщика",
            client_status=client_status,
            fulfillment_status="VPN: заблокирован (ручная проверка)",
            root_cause="Обнаружены сторонние или конкурирующие заказы у поставщика в окне оплаты.",
            recommended_action="Проверить заказы в кабинете VHQ вручную во избежание двойной оплаты.",
            safe_retry_eligible=False,
            diagnostic_code="ambiguous_competing_orders_at_supplier",
        )
    if fail_code == "adapt_api_402":
        return ProblemPaymentDiagnosis(
            supplier_name="Adapt",
            summary_title="Adapt — недостаточно средств на балансе",
            client_status=client_status,
            fulfillment_status="VPN: не выдан",
            root_cause="Недостаточно средств на балансе Adapt Group.",
            recommended_action="Пополнить баланс Adapt и повторить выдачу через сверку.",
            safe_retry_eligible=True,
            diagnostic_code="adapt_api_402",
        )
    if fail_code == "payment_canceled_at_provider":
        return ProblemPaymentDiagnosis(
            supplier_name="YooKassa",
            summary_title="YooKassa — платёж отменён",
            client_status="YooKassa: отменено",
            fulfillment_status="VPN: не требуется",
            root_cause="Платёж был отменён клиентом или банком.",
            recommended_action="Действий не требуется.",
            safe_retry_eligible=False,
            diagnostic_code="payment_canceled_at_provider",
        )
    if fail_code == "payment_canceled_with_financial_effects_review_needed":
        return ProblemPaymentDiagnosis(
            supplier_name="YooKassa",
            summary_title="YooKassa — отменён, требуется фин. проверка",
            client_status="YooKassa: отменено",
            fulfillment_status="VPN: не выдан",
            root_cause="Платёж отменён в YooKassa, но имеет финансовые начисления (бонусы/кэшбэк).",
            recommended_action="Проверить баланс клиента и финансовые транзакции вручную.",
            safe_retry_eligible=False,
            diagnostic_code="payment_canceled_with_financial_effects_review_needed",
        )

    if fail_code:
        supp = supplier_name or "Поставщик"
        return ProblemPaymentDiagnosis(
            supplier_name=supplier_name,
            summary_title=f"{supp} — сбой: {fail_code}",
            client_status=client_status,
            fulfillment_status=f"Сбой: {fail_code}",
            root_cause=f"Сбой операции у поставщика или шлюза: {fail_code}.",
            recommended_action="Проверить логи сервера и статус заказа у поставщика.",
            safe_retry_eligible=False,
            diagnostic_code=fail_code,
        )

    if payment.status == PaymentStatus.COMPLETED and payment.subscription_id is None and payment.platform != "device":
        return ProblemPaymentDiagnosis(
            supplier_name=supplier_name or "Локально",
            summary_title="Оплата завершена, подписка не привязана",
            client_status=client_status,
            fulfillment_status="VPN: не привязан",
            root_cause="Платёж в статусе COMPLETED, но subscription_id отсутствует в записи.",
            recommended_action="Проверить подписки пользователя и привязать вручную.",
            safe_retry_eligible=False,
            diagnostic_code="unresolved_subscription",
        )

    if payment.provider_status == "succeeded" and payment.provider_paid and payment.status != PaymentStatus.COMPLETED:
        supp = supplier_name or "Поставщик"
        return ProblemPaymentDiagnosis(
            supplier_name=supplier_name,
            summary_title=f"{supp} — оплачено, выдача не завершена",
            client_status="YooKassa: оплачено",
            fulfillment_status="VPN: не выдан",
            root_cause=f"Провайдер подтвердил оплату, локальная выдача не завершена ({fail_code or 'неизвестно'}).",
            recommended_action="Запустить сверку с YooKassa для завершения выдачи.",
            safe_retry_eligible=(fail_code in ("vhq_balance", "vhq_auth", "adapt_api_402")),
            diagnostic_code=fail_code or "provider_paid_unfulfilled",
        )

    return ProblemPaymentDiagnosis(
        supplier_name=supplier_name,
        summary_title=f"Платёж #{payment.id} ({payment.status.value})",
        client_status=client_status,
        fulfillment_status=f"Статус: {payment.status.value}",
        root_cause=f"Локальный статус: {payment.status.value}, failure_code={fail_code or '—'}.",
        recommended_action="Проверить статус платежа при необходимости.",
        safe_retry_eligible=False,
        diagnostic_code=fail_code or payment.status.value,
    )


@dataclass
class ProblemPaymentsReport:
    items: list[ProblemPaymentItem]
    counts: dict[ProblemCategory, int]
    total_problems: int
    total_db_problems: int = 0

    @property
    def urgent_count(self) -> int:
        return (
            self.counts.get(ProblemCategory.CONFIRMED_UNFULFILLED, 0)
            + self.counts.get(ProblemCategory.TELEGRAM_DELIVERY_FAILED, 0)
            + self.counts.get(ProblemCategory.FAILED_SUPPLIER, 0)
        )


def format_age(dt: datetime, now: datetime | None = None) -> str:
    """Format timedelta in human-readable Russian abbreviation."""
    now = now or datetime.utcnow()
    diff = now - dt
    total_seconds = max(0, int(diff.total_seconds()))
    if total_seconds < 60:
        return f"{total_seconds} сек"
    minutes = total_seconds // 60
    if minutes < 60:
        return f"{minutes} мин"
    hours = minutes // 60
    if hours < 24:
        return f"{hours} ч"
    days = hours // 24
    return f"{days} дн"


def _is_admin_log_for_payment(log: AdminActionLog, payment_id: int | None, provider_payment_id: str | None) -> bool:
    content = f"{log.summary or ''} {log.details or ''}"
    if provider_payment_id and provider_payment_id in content:
        return True
    if payment_id is not None:
        pattern = rf"(?:^|[^\d])#{payment_id}(?:[^\d]|$)|(?:\b(?:payment|платеж|платежа|платежу)\s+#?{payment_id}\b)"
        if re.search(pattern, content, re.IGNORECASE):
            return True
    return False


async def get_candidate_items(
    limit: int = 60,
    include_pending_provider: bool = False,
) -> list[dict[str, Any]]:
    """Query bounded local candidate records from Payment and BalanceTopUp.

    Prioritizes urgent delivery/supplier incidents first, and orders pending/unresolved
    records oldest-first (ASC) so that long-lived items (>30 days) are never pushed
    off by newer records.
    """
    now = datetime.utcnow()
    cutoff_fresh = now - timedelta(minutes=5)
    candidates: list[dict[str, Any]] = []

    async with async_session() as session:
        # 1. Urgent: Completed without subscription_id (oldest first)
        completed_unlinked = await session.execute(
            select(Payment, User)
            .join(User, User.id == Payment.user_id)
            .where(Payment.status == PaymentStatus.COMPLETED)
            .where(Payment.subscription_id.is_(None))
            .where(or_(Payment.platform.is_(None), Payment.platform != "device"))
            .order_by(Payment.created_at.asc())
            .limit(limit)
        )
        for payment, user in completed_unlinked.all():
            if payment.provider_payment_id:
                has_topup = await session.scalar(
                    select(BalanceTopUp.id).where(
                        BalanceTopUp.provider_payment_id == payment.provider_payment_id,
                        BalanceTopUp.status == "completed",
                    ).limit(1)
                )
                if has_topup:
                    continue
            candidates.append({
                "type": "payment",
                "obj": payment,
                "user": user,
            })

        # 2. Urgent: Payments with provisioning_failure_code (oldest first)
        failed_provisioning = await session.execute(
            select(Payment, User)
            .join(User, User.id == Payment.user_id)
            .where(
                Payment.provisioning_failure_code.is_not(None),
                Payment.provisioning_failure_code != "payment_canceled_at_provider",
            )
            .order_by(Payment.created_at.asc())
            .limit(limit)
        )
        for payment, user in failed_provisioning.all():
            if not any(c["type"] == "payment" and c["obj"].id == payment.id for c in candidates):
                candidates.append({
                    "type": "payment",
                    "obj": payment,
                    "user": user,
                })

        # 3. Urgent: Payments with stalled or failed VHQ fulfillment (oldest first)
        vhq_problems = await session.execute(
            select(Payment, User)
            .join(VhqSubscriptionFulfillment, VhqSubscriptionFulfillment.payment_id == Payment.id)
            .outerjoin(User, User.id == Payment.user_id)
            .where(
                or_(
                    VhqSubscriptionFulfillment.status == "failed",
                    and_(
                        VhqSubscriptionFulfillment.status == "pending",
                        or_(
                            VhqSubscriptionFulfillment.attempts >= VhqSubscriptionFulfillment.max_attempts,
                            VhqSubscriptionFulfillment.created_at <= now - timedelta(minutes=30),
                        ),
                    ),
                )
            )
            .order_by(Payment.created_at.asc())
            .limit(limit)
        )
        for payment, user in vhq_problems.all():
            if not any(c["type"] == "payment" and c["obj"].id == payment.id for c in candidates):
                candidates.append({
                    "type": "payment",
                    "obj": payment,
                    "user": user,
                })

        # 4. Pending YooKassa payments:
        # Fetch both recent (newest first, to catch fresh paid-but-unfulfilled payments)
        # and oldest (to ensure >30d items remain discoverable)
        recent_pending = await session.execute(
            select(Payment, User)
            .join(User, User.id == Payment.user_id)
            .where(Payment.status == PaymentStatus.PENDING)
            .where(Payment.method == PaymentMethod.YOOKASSA)
            .where(Payment.created_at <= cutoff_fresh)
            .order_by(Payment.created_at.desc())
            .limit(limit)
        )
        for payment, user in recent_pending.all():
            if not any(c["type"] == "payment" and c["obj"].id == payment.id for c in candidates):
                candidates.append({
                    "type": "payment",
                    "obj": payment,
                    "user": user,
                })

        oldest_pending = await session.execute(
            select(Payment, User)
            .join(User, User.id == Payment.user_id)
            .where(Payment.status == PaymentStatus.PENDING)
            .where(Payment.method == PaymentMethod.YOOKASSA)
            .where(Payment.created_at <= cutoff_fresh)
            .order_by(Payment.created_at.asc())
            .limit(limit)
        )
        for payment, user in oldest_pending.all():
            if not any(c["type"] == "payment" and c["obj"].id == payment.id for c in candidates):
                candidates.append({
                    "type": "payment",
                    "obj": payment,
                    "user": user,
                })

        # 4c. Payments with Telegram delivery failure
        tg_delivery_failures = await session.execute(
            select(Payment, User)
            .outerjoin(User, User.id == Payment.user_id)
            .where(Payment.telegram_delivery_error.isnot(None))
            .order_by(Payment.created_at.desc())
            .limit(limit)
        )
        for payment, user in tg_delivery_failures.all():
            if not any(c["type"] == "payment" and c["obj"].id == payment.id for c in candidates):
                candidates.append({
                    "type": "payment",
                    "obj": payment,
                    "user": user,
                })

        # 5. Pending YooKassa BalanceTopUps (both recent and oldest)
        recent_topups = await session.execute(
            select(BalanceTopUp)
            .where(BalanceTopUp.status == "pending")
            .where(BalanceTopUp.provider == "yookassa")
            .where(BalanceTopUp.created_at <= cutoff_fresh)
            .order_by(BalanceTopUp.created_at.desc())
            .limit(limit)
        )
        for topup in recent_topups.scalars().all():
            if not any(c["type"] == "topup" and c["obj"].id == topup.id for c in candidates):
                user = await session.scalar(select(User).where(User.telegram_id == topup.telegram_id))
                candidates.append({
                    "type": "topup",
                    "obj": topup,
                    "user": user,
                })

        oldest_topups = await session.execute(
            select(BalanceTopUp)
            .where(BalanceTopUp.status == "pending")
            .where(BalanceTopUp.provider == "yookassa")
            .where(BalanceTopUp.created_at <= cutoff_fresh)
            .order_by(BalanceTopUp.created_at.asc())
            .limit(limit)
        )
        for topup in oldest_topups.scalars().all():
            if not any(c["type"] == "topup" and c["obj"].id == topup.id for c in candidates):
                user = await session.scalar(select(User).where(User.telegram_id == topup.telegram_id))
                candidates.append({
                    "type": "topup",
                    "obj": topup,
                    "user": user,
                })

    return candidates


async def _check_manual_fulfillment_evidence(
    payment: Payment,
    user: User | None,
    now: datetime,
) -> tuple[ProblemCategory, str, str]:
    """Check database records for reliable correlation that access was granted for this payment."""
    if not user:
        return (
            ProblemCategory.CONFIRMED_UNFULFILLED,
            "Не выдана (требует сверки)",
            "Оплата проведена, но пользователь не найден в базе данных.",
        )

    provider_id = payment.provider_payment_id
    created_at = payment.created_at or now

    async with async_session() as session:
        user_subs = (await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user.id)
            .order_by(Subscription.expires_at.desc())
        )).scalars().all()

        now_utc = now or datetime.utcnow()
        active_subs = [s for s in user_subs if s.status == SubStatus.ACTIVE and s.expires_at and s.expires_at > now_utc]

        tariff_days = 30
        if payment.tariff_id:
            tf = await session.get(Tariff, payment.tariff_id)
            if tf:
                tariff_days = getattr(tf, "days", None) or getattr(tf, "duration_days", None) or 30

        target_sub_id = None
        if payment.platform and ("~u~" in payment.platform or "~r~" in payment.platform):
            try:
                target_sub_id = int(payment.platform.split("~")[-1])
            except (ValueError, IndexError):
                target_sub_id = None

        if target_sub_id:
            t_sub = next((s for s in user_subs if s.id == target_sub_id), None)
            if t_sub:
                baseline = payment.provisioning_baseline_expires_at
                tariff_id = payment.tariff_id
                t_rec = await session.get(Tariff, tariff_id) if tariff_id else None
                expected_days = t_rec.days if t_rec else t_sub.tariff_days
                required_extension = (timedelta(days=expected_days) - timedelta(hours=2)) if expected_days else None

                candidate_competing_payments = (await session.execute(
                    select(Payment).where(
                        Payment.id != payment.id,
                        Payment.user_id == user.id,
                        or_(
                            Payment.subscription_id == t_sub.id,
                            Payment.platform.like(f"%~u~{t_sub.id}%"),
                            Payment.platform.like(f"%~r~{t_sub.id}%"),
                        ),
                        Payment.status.in_([PaymentStatus.COMPLETED, PaymentStatus.PENDING]),
                        Payment.created_at.between(created_at - timedelta(days=2), created_at + timedelta(days=2)),
                    )
                )).scalars().all()
                competing_payments = await filter_real_competing_payments(candidate_competing_payments)

                candidate_admin_logs = (await session.execute(
                    select(AdminActionLog).where(
                        AdminActionLog.action.in_(["renew_subscription", "update_subscription_days", "grant_access"]),
                        AdminActionLog.status == "success",
                        AdminActionLog.entity_id == str(t_sub.id),
                        AdminActionLog.target_telegram_id == user.telegram_id,
                        AdminActionLog.created_at.between(created_at - timedelta(hours=1), created_at + timedelta(days=2)),
                    )
                )).scalars().all()

                unique_admin_log = next(
                    (l for l in candidate_admin_logs if _is_admin_log_for_payment(l, payment.id, provider_id)),
                    None,
                )

                has_competing_conflict = False
                if competing_payments and not unique_admin_log:
                    total_competing_days = expected_days or 30
                    for cp in competing_payments:
                        cp_tariff = await session.get(Tariff, cp.tariff_id) if cp.tariff_id else None
                        total_competing_days += (cp_tariff.days if cp_tariff else 30)
                    cum_ext = timedelta(days=total_competing_days) - timedelta(hours=2)
                    if not (baseline and t_sub.expires_at and t_sub.expires_at >= baseline + cum_ext):
                        has_competing_conflict = True

                if has_competing_conflict:
                    return (
                        ProblemCategory.DELIVERY_UNCERTAIN,
                        "Статус неясен (конкурирующие платежи)",
                        (
                            f"Для целевой подписки #{t_sub.id} обнаружено несколько платежей в одном временном окне. "
                            f"Атрибуция продления неоднозначна, требуется ручная проверка."
                        ),
                    )

                has_baseline = bool(
                    not competing_payments
                    and baseline
                    and required_extension
                    and t_sub.expires_at
                    and t_sub.expires_at >= baseline + required_extension
                )

                has_op = False
                if not competing_payments:
                    manual_op = await session.scalar(
                        select(ManualSubscriptionOperation).where(
                            ManualSubscriptionOperation.target_user_id == user.id,
                            ManualSubscriptionOperation.subscription_id == t_sub.id,
                            ManualSubscriptionOperation.status == "completed",
                            ManualSubscriptionOperation.action.in_(["renew", "upgrade", "manual_fulfillment"]),
                            ManualSubscriptionOperation.created_at.between(created_at - timedelta(hours=1), created_at + timedelta(days=2)),
                        ).limit(1)
                    )
                    if manual_op:
                        tariff_match = (manual_op.tariff_id == tariff_id)
                        extension_match = bool(
                            baseline
                            and required_extension
                            and t_sub.expires_at
                            and t_sub.expires_at >= baseline + required_extension
                        )
                        has_op = bool(tariff_match and extension_match)

                has_admin = False
                if unique_admin_log:
                    extension_match = bool(
                        baseline
                        and required_extension
                        and t_sub.expires_at
                        and t_sub.expires_at >= baseline + required_extension
                    )
                    if extension_match:
                        has_admin = True
                elif not competing_payments:
                    admin_log = await session.scalar(
                        select(AdminActionLog).where(
                            AdminActionLog.action.in_(["renew_subscription", "update_subscription_days", "grant_access"]),
                            AdminActionLog.status == "success",
                            AdminActionLog.entity_id == str(t_sub.id),
                            AdminActionLog.target_telegram_id == user.telegram_id,
                            AdminActionLog.created_at.between(created_at - timedelta(hours=1), created_at + timedelta(days=2)),
                        ).limit(1)
                    )
                    if admin_log:
                        extension_match = bool(
                            baseline
                            and required_extension
                            and t_sub.expires_at
                            and t_sub.expires_at >= baseline + required_extension
                        )
                        has_admin = bool(extension_match)

                if has_baseline or has_op or has_admin:
                    return (
                        ProblemCategory.VERIFIED_MANUALLY_FULFILLED,
                        f"Выдана вручную (подписка #{t_sub.id})",
                        (
                            f"Оплата подтверждена. Целевая подписка #{t_sub.id} продлена "
                            f"до {t_sub.expires_at.strftime('%d.%m.%Y')}. Подтверждено записями операций."
                        ),
                    )

                if t_sub.expires_at and t_sub.expires_at <= created_at:
                    return (
                        ProblemCategory.CONFIRMED_UNFULFILLED,
                        "Не выдана (целевая подписка не продлена)",
                        (
                            f"Оплата подтверждена, но целевая подписка #{t_sub.id} истекла "
                            f"{t_sub.expires_at.strftime('%d.%m.%Y')} и не была продлена."
                        ),
                    )
                elif baseline and required_extension and t_sub.expires_at and t_sub.expires_at < baseline + required_extension:
                    return (
                        ProblemCategory.DELIVERY_UNCERTAIN,
                        "Статус неясен (частичное продление)",
                        (
                            f"Подписка #{t_sub.id} продлена меньше чем на полный оплаченный срок "
                            f"(требуется {expected_days} дн.). Требуется ручная проверка."
                        ),
                    )
                else:
                    return (
                        ProblemCategory.DELIVERY_UNCERTAIN,
                        "Статус неясен (нет подтверждения продления)",
                        (
                            f"Целевая подписка #{t_sub.id} активна, но отсутствуют надежные данные о том, "
                            f"что именно этот платеж был применен. Требуется ручная проверка."
                        ),
                    )
            else:
                t_sub_other = await session.get(Subscription, target_sub_id)
                if t_sub_other and t_sub_other.user_id != user.id:
                    return (
                        ProblemCategory.DELIVERY_UNCERTAIN,
                        "Несовпадение владельца подписки",
                        (
                            f"Целевая подписка #{target_sub_id} принадлежит другому пользователю (ID {t_sub_other.user_id}). "
                            f"Требуется ручная проверка."
                        ),
                    )
                else:
                    return (
                        ProblemCategory.DELIVERY_UNCERTAIN,
                        "Целевая подписка не найдена",
                        f"Целевая подписка #{target_sub_id} не найдена в базе данных.",
                    )

        elif payment.platform == "topup":
            amount_rub = (
                kopecks_to_rub_float(payment.amount)
                if payment.currency == "RUB"
                else float(payment.amount)
            )
            valid_source_types = [
                "yookassa_topup",
                "telegram_topup",
                "robokassa_topup",
                "balance_topup",
                "manual_topup",
                "topup",
                "web_balance_migration",
            ]
            tx = await session.scalar(
                select(BalanceTransaction).where(
                    BalanceTransaction.user_id == user.id,
                    BalanceTransaction.kind.in_([BalanceTransactionKind.TOPUP, "TOPUP", "topup"]),
                    BalanceTransaction.direction.in_([BalanceDirection.CREDIT, "CREDIT", "credit"]),
                    func.abs(BalanceTransaction.amount_rub - amount_rub) < 0.01,
                    BalanceTransaction.source_id == provider_id,
                    or_(
                        BalanceTransaction.source_type.is_(None),
                        BalanceTransaction.source_type.in_(valid_source_types),
                    ),
                ).order_by(BalanceTransaction.id.desc()).limit(1)
            )
            if tx:
                return (
                    ProblemCategory.VERIFIED_MANUALLY_FULFILLED,
                    "Баланс зачислен ранее",
                    f"Пополнение подтверждено, баланс был начислен (транзакция #{tx.id}).",
                )
            else:
                return (
                    ProblemCategory.CONFIRMED_UNFULFILLED,
                    "Баланс не начислен",
                    "Оплата подтверждена, но баланс не пополнен!",
                )

        else:
            manual_op = await session.scalar(
                select(ManualSubscriptionOperation).where(
                    ManualSubscriptionOperation.target_user_id == user.id,
                    ManualSubscriptionOperation.status == "completed",
                    ManualSubscriptionOperation.action.in_(["grant_access", "create_subscription", "renew", "buy"]),
                    ManualSubscriptionOperation.created_at.between(created_at - timedelta(hours=1), created_at + timedelta(days=2)),
                ).limit(1)
            )
            admin_log = await session.scalar(
                select(AdminActionLog).where(
                    AdminActionLog.action.in_(["grant_access", "create_subscription", "renew_subscription"]),
                    AdminActionLog.status == "success",
                    AdminActionLog.target_telegram_id == user.telegram_id,
                    AdminActionLog.created_at.between(created_at - timedelta(hours=1), created_at + timedelta(days=2)),
                ).limit(1)
            )

            if manual_op or admin_log:
                matched_id = manual_op.subscription_id if manual_op and manual_op.subscription_id else (user_subs[0].id if user_subs else None)
                state_label = f"Выдана вручную (подписка #{matched_id})" if matched_id else "Выдана вручную"
                return (
                    ProblemCategory.VERIFIED_MANUALLY_FULFILLED,
                    state_label,
                    "Оплата подтверждена. Обнаружена надежная корреляция ручной выдачи доступа.",
                )
            elif (now_utc - created_at) > timedelta(days=7):
                return (
                    ProblemCategory.DELIVERY_UNCERTAIN,
                    "Архив: статус неясен (нет надежных данных)",
                    (
                        f"Исторический платеж (создан {created_at.strftime('%d.%m.%Y')}). "
                        f"Отсутствует подтверждение выдачи доступа именно по этому платежу. Требуется ручная проверка."
                    ),
                )
            else:
                return (
                    ProblemCategory.CONFIRMED_UNFULFILLED,
                    "Не выдана (требует сверки)",
                    "Оплата подтверждена, но услуга не выдана в боте!",
                )


async def _check_manual_topup_evidence(
    topup: BalanceTopUp,
    user: User | None,
    now: datetime,
    live_confirmed: bool = False,
) -> tuple[ProblemCategory, str, str]:
    """Check database records for correlation of balance topup in balance transactions ledger."""
    async with async_session() as session:
        user_id = user.id if user else (topup.user_id if getattr(topup, "user_id", None) else None)
        if not user_id and topup.telegram_id:
            user_id = await session.scalar(select(User.id).where(User.telegram_id == topup.telegram_id))

        if not user_id:
            return (
                ProblemCategory.DELIVERY_UNCERTAIN,
                "Требует ручной проверки",
                f"Пополнение баланса #{topup.id}: пользователь не найден в БД. Требуется ручная проверка.",
            )

        amount_rub = kopecks_to_rub_float(
            topup.amount_kopecks if topup.amount_kopecks is not None else rub_to_kopecks(topup.amount_rub or 0.0)
        )
        provider_id = (topup.provider_payment_id or "").strip()
        topup_id_str = str(topup.id)
        provider = (topup.provider or "").strip()

        source_id_conds = []
        if provider_id:
            source_id_conds.append(BalanceTransaction.source_id == provider_id)
        source_id_conds.append(BalanceTransaction.source_id == topup_id_str)

        valid_source_types = {
            "yookassa_topup",
            "telegram_topup",
            "robokassa_topup",
            "balance_topup",
            "manual_topup",
            "topup",
            "web_balance_migration",
        }
        if provider:
            valid_source_types.add(f"{provider}_topup")
            valid_source_types.add(provider)

        # Strict balance ledger correlation:
        # 1. Matching customer/user_id
        # 2. kind=TOPUP and direction=CREDIT
        # 3. Matching credited amount
        # 4. Topup-specific source correlation (provider_payment_id or topup.id)
        # 5. Correct source_type where available
        tx = await session.scalar(
            select(BalanceTransaction).where(
                BalanceTransaction.user_id == user_id,
                BalanceTransaction.kind.in_([BalanceTransactionKind.TOPUP, "TOPUP", "topup"]),
                BalanceTransaction.direction.in_([BalanceDirection.CREDIT, "CREDIT", "credit"]),
                func.abs(BalanceTransaction.amount_rub - amount_rub) < 0.01,
                or_(*source_id_conds),
                or_(
                    BalanceTransaction.source_type.is_(None),
                    BalanceTransaction.source_type.in_(list(valid_source_types)),
                ),
            ).order_by(BalanceTransaction.id.desc()).limit(1)
        )
        if tx:
            return (
                ProblemCategory.PROCESSED,
                "✅ Баланс пополнен",
                f"Пополнение баланса #{topup.id} на {amount_rub:.2f} ₽ успешно зачислено (транзакция #{tx.id}).",
            )
        if live_confirmed:
            return (
                ProblemCategory.CONFIRMED_UNFULFILLED,
                "Баланс не начислен",
                f"Оплата {amount_rub:.2f} ₽ подтверждена в YooKassa, но баланс не пополнен!",
            )
        return (
            ProblemCategory.DELIVERY_UNCERTAIN,
            "Требует ручной проверки",
            f"Пополнение баланса #{topup.id} (статус {topup.status}), но подтверждение зачисления в BalanceTransaction не найдено. Требуется ручная проверка.",
        )


async def classify_candidate_db_only(
    candidate: dict[str, Any],
    now: datetime | None = None,
) -> ProblemPaymentItem | None:
    """Fast, DB-only classification of a single candidate record without external network calls."""
    now = now or datetime.utcnow()
    c_type = candidate["type"]
    obj = candidate["obj"]
    user = candidate.get("user")

    if c_type == "payment":
        payment: Payment = obj
        amount_rub = (
            kopecks_to_rub_float(payment.amount)
            if payment.currency == "RUB"
            else float(payment.amount)
        )
        provider_id = payment.provider_payment_id
        created_at = payment.created_at or now
        age_str = format_age(created_at, now)
        local_status_str = f"Payment.{payment.status.value}"

        # 1. Telegram delivery failure on completed payment
        if payment.status == PaymentStatus.COMPLETED and getattr(payment, "telegram_delivery_error", None):
            return ProblemPaymentItem(
                item_type="payment",
                item_id=payment.id,
                user_id=user.id if user else None,
                telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=provider_id,
                local_status=local_status_str,
                verified_provider_status="Завершен (сбой отправки TG)",
                category=ProblemCategory.TELEGRAM_DELIVERY_FAILED,
                fulfillment_state="Доступ создан, сбой отправки в TG",
                details=f"Подписка #{payment.subscription_id} создана в БД, но сообщение клиенту не дошло: {payment.telegram_delivery_error}",
            )

        # 2. Missing provider_payment_id
        if not provider_id and payment.status == PaymentStatus.PENDING:
            if now - created_at > timedelta(minutes=15):
                return ProblemPaymentItem(
                    item_type="payment",
                    item_id=payment.id,
                    user_id=user.id if user else None,
                    telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                    username=user.username if user else None,
                    full_name=user.full_name if user else None,
                    amount_rub=amount_rub,
                    created_at=created_at,
                    age_str=age_str,
                    provider_payment_id=None,
                    local_status=local_status_str,
                    verified_provider_status="нет provider_id",
                    category=ProblemCategory.MISSING_PROVIDER_ID,
                    fulfillment_state="Требует расследования",
                    details=f"Локальный платеж #{payment.id} создан {age_str} назад без ID провайдера",
                )
            return ProblemPaymentItem(
                item_type="payment",
                item_id=payment.id,
                user_id=user.id if user else None,
                telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=None,
                local_status=local_status_str,
                verified_provider_status="нет provider_id",
                category=ProblemCategory.PROVIDER_PENDING,
                fulfillment_state="Создан недавно, ожидает ID",
                details=f"Локальный платеж #{payment.id} создан {age_str} назад, ожидает перехода в платёжную систему.",
            )

        # 3. Check VHQ background fulfillment state
        async with async_session() as session:
            vhq_row = await session.scalar(
                select(VhqSubscriptionFulfillment).where(
                    VhqSubscriptionFulfillment.payment_id == payment.id
                ).order_by(VhqSubscriptionFulfillment.id.desc()).limit(1)
            )
            tariff = await session.get(Tariff, payment.tariff_id) if payment.tariff_id else None

        if vhq_row:
            if vhq_row.status == "completed":
                pass
            elif vhq_row.status in (
                "pending", "processing", "retry_wait", "uncertain", "buy_inflight", "renew_inflight"
            ):
                if vhq_row.attempts >= vhq_row.max_attempts or (now - vhq_row.created_at >= timedelta(minutes=30)):
                    diag = diagnose_payment_problem(payment, tariff=tariff, vhq_row=vhq_row)
                    return ProblemPaymentItem(
                        item_type="payment",
                        item_id=payment.id,
                        user_id=user.id if user else None,
                        telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                        username=user.username if user else None,
                        full_name=user.full_name if user else None,
                        amount_rub=amount_rub,
                        created_at=created_at,
                        age_str=age_str,
                        provider_payment_id=provider_id,
                        local_status=local_status_str,
                        verified_provider_status="VHQ: stalled",
                        category=ProblemCategory.FAILED_SUPPLIER,
                        fulfillment_state=diag.summary_title,
                        details=diag.root_cause,
                        supplier_name=diag.supplier_name,
                        root_cause=diag.root_cause,
                        recommended_action=diag.recommended_action,
                        safe_retry_eligible=diag.safe_retry_eligible,
                        diagnostic_code=diag.diagnostic_code,
                    )
            elif vhq_row.status == "failed":
                diag = diagnose_payment_problem(payment, tariff=tariff, vhq_row=vhq_row)
                return ProblemPaymentItem(
                    item_type="payment",
                    item_id=payment.id,
                    user_id=user.id if user else None,
                    telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                    username=user.username if user else None,
                    full_name=user.full_name if user else None,
                    amount_rub=amount_rub,
                    created_at=created_at,
                    age_str=age_str,
                    provider_payment_id=provider_id,
                    local_status=local_status_str,
                    verified_provider_status="VHQ: failed",
                    category=ProblemCategory.FAILED_SUPPLIER,
                    fulfillment_state="Требует ручной проверки",
                    details=diag.root_cause,
                    supplier_name=diag.supplier_name,
                    root_cause=diag.root_cause,
                    recommended_action=diag.recommended_action,
                    safe_retry_eligible=diag.safe_retry_eligible,
                    diagnostic_code=diag.diagnostic_code,
                )

        # A paid VHQ renewal that is inside the bounded autonomous recovery
        # window is intentionally quiet.  The provider-paid snapshot remains
        # durable, while the fulfillment worker owns recovery; only a stalled
        # or exhausted fulfillment is an actionable incident.
        if (
            vhq_row
            and vhq_row.status in (
                "pending", "processing", "retry_wait", "uncertain", "buy_inflight", "renew_inflight"
            )
            and vhq_row.attempts < vhq_row.max_attempts
            and now - vhq_row.created_at < timedelta(minutes=30)
            and not str(payment.provisioning_failure_code or "").startswith("yookassa_")
        ):
            return None

        # Authenticated provider truth is durable and must stay visible after a
        # restart. A completed local payment is the only clean terminal state.
        provider_paid_unresolved = (
            payment.provider_status == "succeeded"
            and payment.provider_paid is True
            and (
                payment.status != PaymentStatus.COMPLETED
                or str(payment.provisioning_failure_code or "").startswith("yookassa_")
            )
        )
        if provider_paid_unresolved:
            failure_code = payment.provisioning_failure_code or "provider_paid_unfulfilled"
            if "manual_review" in failure_code or failure_code in {
                "ambiguous_buy_mutation_manual_review_required",
                "ambiguous_renewal_manual_review_required",
                "ambiguous_competing_orders_at_supplier",
                "historical_payment_manual_review_required",
                "renewal_evidence_missing_manual_review_required",
                "partial_admin_renewal_manual_review_required",
                "target_sub_user_mismatch",
            }:
                category = ProblemCategory.DELIVERY_UNCERTAIN
            elif failure_code in {
                "uncertain_supplier_mutation",
                "mutation_in_flight",
                "vhq_balance",
                "vhq_auth",
                "vhq_request_invalid",
                "vhq_supplier",
                "adapt_api_402",
                "adapt_api_429",
            }:
                category = ProblemCategory.FAILED_SUPPLIER
            else:
                category = ProblemCategory.CONFIRMED_UNFULFILLED
            diag = diagnose_payment_problem(payment, tariff=tariff, vhq_row=vhq_row)
            return ProblemPaymentItem(
                item_type="payment",
                item_id=payment.id,
                user_id=user.id if user else None,
                telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=provider_id,
                local_status=local_status_str,
                verified_provider_status="YooKassa: succeeded (оплачен)",
                category=category,
                fulfillment_state=diag.summary_title,
                details=diag.root_cause,
                supplier_name=diag.supplier_name,
                root_cause=diag.root_cause,
                recommended_action=diag.recommended_action,
                safe_retry_eligible=diag.safe_retry_eligible,
                diagnostic_code=diag.diagnostic_code,
            )

        # 4. Check provisioning failure code
        if payment.provisioning_failure_code:
            diag = diagnose_payment_problem(payment, tariff=tariff, vhq_row=vhq_row)
            if payment.provisioning_failure_code == "payment_canceled_at_provider":
                return ProblemPaymentItem(
                    item_type="payment",
                    item_id=payment.id,
                    user_id=user.id if user else None,
                    telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                    username=user.username if user else None,
                    full_name=user.full_name if user else None,
                    amount_rub=amount_rub,
                    created_at=created_at,
                    age_str=age_str,
                    provider_payment_id=provider_id,
                    local_status=local_status_str,
                    verified_provider_status="YooKassa: canceled",
                    category=ProblemCategory.PROVIDER_PENDING,
                    fulfillment_state=diag.summary_title,
                    details=diag.root_cause,
                    supplier_name=diag.supplier_name,
                    root_cause=diag.root_cause,
                    recommended_action=diag.recommended_action,
                    safe_retry_eligible=diag.safe_retry_eligible,
                    diagnostic_code=diag.diagnostic_code,
                )
            if payment.provisioning_failure_code == "payment_canceled_with_financial_effects_review_needed":
                return ProblemPaymentItem(
                    item_type="payment",
                    item_id=payment.id,
                    user_id=user.id if user else None,
                    telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                    username=user.username if user else None,
                    full_name=user.full_name if user else None,
                    amount_rub=amount_rub,
                    created_at=created_at,
                    age_str=age_str,
                    provider_payment_id=provider_id,
                    local_status=local_status_str,
                    verified_provider_status="Отменен (фин. проверка)",
                    category=ProblemCategory.DELIVERY_UNCERTAIN,
                    fulfillment_state="Требует ручной проверки",
                    details=diag.root_cause,
                    supplier_name=diag.supplier_name,
                    root_cause=diag.root_cause,
                    recommended_action=diag.recommended_action,
                    safe_retry_eligible=diag.safe_retry_eligible,
                    diagnostic_code=diag.diagnostic_code,
                )
            return ProblemPaymentItem(
                item_type="payment",
                item_id=payment.id,
                user_id=user.id if user else None,
                telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=provider_id,
                local_status=local_status_str,
                verified_provider_status=f"Сбой: {payment.provisioning_failure_code}",
                category=ProblemCategory.FAILED_SUPPLIER,
                fulfillment_state=diag.summary_title,
                details=diag.root_cause,
                supplier_name=diag.supplier_name,
                root_cause=diag.root_cause,
                recommended_action=diag.recommended_action,
                safe_retry_eligible=diag.safe_retry_eligible,
                diagnostic_code=diag.diagnostic_code,
            )

        # 4b. Clean successfully completed payment with linked subscription or device
        cached_live = get_cached_live_status("payment", payment.id) if provider_id else None
        live_status_label = cached_live.get("status_label") if cached_live else None

        if payment.status == PaymentStatus.COMPLETED and payment.subscription_id is not None:
            return ProblemPaymentItem(
                item_type="payment",
                item_id=payment.id,
                user_id=user.id if user else None,
                telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=provider_id,
                local_status=local_status_str,
                verified_provider_status=live_status_label or "Успешно завершен",
                category=ProblemCategory.PROCESSED,
                fulfillment_state=f"✅ Обработан (подписка #{payment.subscription_id})",
                details=f"Оплата проведена успешно. Услуга привязана к подписке #{payment.subscription_id}.",
            )

        if payment.status == PaymentStatus.COMPLETED and payment.platform == "device":
            return ProblemPaymentItem(
                item_type="payment",
                item_id=payment.id,
                user_id=user.id if user else None,
                telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=provider_id,
                local_status=local_status_str,
                verified_provider_status=live_status_label or "Успешно завершен",
                category=ProblemCategory.PROCESSED,
                fulfillment_state="✅ Обработан (устройство)",
                details="Оплата устройства успешно завершена.",
            )

        # 5. Completed without subscription delivery
        if payment.status == PaymentStatus.COMPLETED and payment.subscription_id is None and payment.platform != "device":
            if provider_id:
                async with async_session() as session:
                    topup_obj = await session.scalar(
                        select(BalanceTopUp).where(
                            BalanceTopUp.provider_payment_id == provider_id,
                            BalanceTopUp.status == "completed",
                        ).limit(1)
                    )
                    if topup_obj:
                        t_cat, t_state, t_det = await _check_manual_topup_evidence(topup_obj, user, now)
                        if t_cat == ProblemCategory.PROCESSED:
                            return ProblemPaymentItem(
                                item_type="payment",
                                item_id=payment.id,
                                user_id=user.id if user else None,
                                telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                                username=user.username if user else None,
                                full_name=user.full_name if user else None,
                                amount_rub=amount_rub,
                                created_at=created_at,
                                age_str=age_str,
                                provider_payment_id=provider_id,
                                local_status=local_status_str,
                                verified_provider_status=live_status_label or "Баланс пополнен",
                                category=ProblemCategory.PROCESSED,
                                fulfillment_state="✅ Баланс пополнен (через TopUp)",
                                details="Платеж соответствует пополнению баланса, средства зачислены.",
                            )

            cat, state_label, det_label = await _check_manual_fulfillment_evidence(payment, user, now)
            if cat == ProblemCategory.CONFIRMED_UNFULFILLED:
                det_label = "Оплата проведена, но subscription_id отсутствует"

            return ProblemPaymentItem(
                item_type="payment",
                item_id=payment.id,
                user_id=user.id if user else None,
                telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=provider_id,
                local_status=local_status_str,
                verified_provider_status=live_status_label or "completed_unlinked",
                category=cat,
                fulfillment_state=state_label,
                details=det_label,
            )

        # 6. Pending payment
        if payment.status == PaymentStatus.PENDING:
            if cached_live:
                status = cached_live.get("status")
                paid = cached_live.get("paid", False)
                if status == "canceled":
                    return ProblemPaymentItem(
                        item_type="payment",
                        item_id=payment.id,
                        user_id=user.id if user else None,
                        telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                        username=user.username if user else None,
                        full_name=user.full_name if user else None,
                        amount_rub=amount_rub,
                        created_at=created_at,
                        age_str=age_str,
                        provider_payment_id=provider_id,
                        local_status=local_status_str,
                        verified_provider_status="YooKassa: canceled",
                        category=ProblemCategory.PROVIDER_PENDING,
                        fulfillment_state="Отменен в YooKassa",
                        details="Платёж был отменен клиентом или банком в YooKassa",
                    )
                if status == "succeeded" and paid:
                    cat, state_label, det_label = await _check_manual_fulfillment_evidence(payment, user, now)
                    return ProblemPaymentItem(
                        item_type="payment",
                        item_id=payment.id,
                        user_id=user.id if user else None,
                        telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                        username=user.username if user else None,
                        full_name=user.full_name if user else None,
                        amount_rub=amount_rub,
                        created_at=created_at,
                        age_str=age_str,
                        provider_payment_id=provider_id,
                        local_status=local_status_str,
                        verified_provider_status="YooKassa: succeeded (оплачен)",
                        category=cat,
                        fulfillment_state=state_label,
                        details=det_label,
                    )

            return ProblemPaymentItem(
                item_type="payment",
                item_id=payment.id,
                user_id=user.id if user else None,
                telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=provider_id,
                local_status=local_status_str,
                verified_provider_status=live_status_label or "Не проверялся (нажмите «Проверить»)",
                category=ProblemCategory.PROVIDER_PENDING,
                fulfillment_state="Ожидает оплаты",
                details="Локальный статус PENDING. Ожидает завершения оплаты клиентом или сверки.",
            )

        return ProblemPaymentItem(
            item_type="payment",
            item_id=payment.id,
            user_id=user.id if user else None,
            telegram_id=user.telegram_id if user else payment.telegram_chat_id,
            username=user.username if user else None,
            full_name=user.full_name if user else None,
            amount_rub=amount_rub,
            created_at=created_at,
            age_str=age_str,
            provider_payment_id=provider_id,
            local_status=local_status_str,
            verified_provider_status=live_status_label or f"Статус: {payment.status.value}",
            category=ProblemCategory.PROVIDER_PENDING,
            fulfillment_state=f"Статус: {payment.status.value}",
            details=f"Локальный платеж #{payment.id} имеет статус {payment.status.value}.",
        )

    elif c_type == "topup":
        topup: BalanceTopUp = obj
        amount_rub = kopecks_to_rub_float(
            topup.amount_kopecks if topup.amount_kopecks is not None else rub_to_kopecks(topup.amount_rub)
        )
        provider_id = topup.provider_payment_id
        created_at = topup.created_at or now
        age_str = format_age(created_at, now)
        local_status_str = f"TopUp.{topup.status}"

        provider_paid_unresolved = (
            topup.provider_status == "succeeded"
            and topup.provider_paid is True
            and (
                topup.status != "completed"
                or str(topup.provider_failure_code or "").startswith("yookassa_")
            )
        )
        if provider_paid_unresolved:
            failure_code = topup.provider_failure_code or "provider_paid_unfulfilled"
            return ProblemPaymentItem(
                item_type="topup",
                item_id=topup.id,
                user_id=user.id if user else topup.user_id,
                telegram_id=topup.telegram_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=provider_id,
                local_status=local_status_str,
                verified_provider_status="YooKassa: succeeded (оплачен)",
                category=ProblemCategory.CONFIRMED_UNFULFILLED,
                fulfillment_state=f"Оплачено, баланс не начислен ({failure_code})",
                details="Провайдер подтвердил оплату, но зачисление на баланс не завершено.",
            )

        cached_live = get_cached_live_status("topup", topup.id) if provider_id else None
        live_status_label = cached_live.get("status_label") if cached_live else None

        if topup.status == "completed":
            cat, state_label, det_label = await _check_manual_topup_evidence(topup, user, now)
            return ProblemPaymentItem(
                item_type="topup",
                item_id=topup.id,
                user_id=user.id if user else None,
                telegram_id=topup.telegram_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=provider_id,
                local_status=local_status_str,
                verified_provider_status=live_status_label or (
                    "Успешно завершен" if cat == ProblemCategory.PROCESSED else "Требует ручной проверки"
                ),
                category=cat,
                fulfillment_state=state_label,
                details=det_label,
            )

        if not provider_id and topup.status == "pending":
            if now - created_at > timedelta(minutes=15):
                return ProblemPaymentItem(
                    item_type="topup",
                    item_id=topup.id,
                    user_id=user.id if user else None,
                    telegram_id=topup.telegram_id,
                    username=user.username if user else None,
                    full_name=user.full_name if user else None,
                    amount_rub=amount_rub,
                    created_at=created_at,
                    age_str=age_str,
                    provider_payment_id=None,
                    local_status=local_status_str,
                    verified_provider_status="нет provider_id",
                    category=ProblemCategory.MISSING_PROVIDER_ID,
                    fulfillment_state="Пополнение без provider_id",
                    details=f"Пополнение баланса #{topup.id} создано без ID провайдера",
                )
            return ProblemPaymentItem(
                item_type="topup",
                item_id=topup.id,
                user_id=user.id if user else None,
                telegram_id=topup.telegram_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=None,
                local_status=local_status_str,
                verified_provider_status="нет provider_id",
                category=ProblemCategory.PROVIDER_PENDING,
                fulfillment_state="Создан недавно, ожидает ID",
                details=f"Пополнение баланса #{topup.id} создано {age_str} назад, ожидает перехода к оплате.",
            )

        if cached_live and cached_live.get("status") == "succeeded" and cached_live.get("paid"):
            cat, state_label, det_label = await _check_manual_topup_evidence(topup, user, now, live_confirmed=True)
            return ProblemPaymentItem(
                item_type="topup",
                item_id=topup.id,
                user_id=user.id if user else None,
                telegram_id=topup.telegram_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=provider_id,
                local_status=local_status_str,
                verified_provider_status="YooKassa: succeeded (оплачен)",
                category=cat,
                fulfillment_state=state_label,
                details=det_label,
            )
        elif cached_live and cached_live.get("status") == "canceled":
            return ProblemPaymentItem(
                item_type="topup",
                item_id=topup.id,
                user_id=user.id if user else None,
                telegram_id=topup.telegram_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=provider_id,
                local_status=local_status_str,
                verified_provider_status="YooKassa: canceled",
                category=ProblemCategory.PROVIDER_PENDING,
                fulfillment_state="Отменен в YooKassa",
                details="Пополнение баланса отменено клиентом или банком в YooKassa",
            )

        return ProblemPaymentItem(
            item_type="topup",
            item_id=topup.id,
            user_id=user.id if user else None,
            telegram_id=topup.telegram_id,
            username=user.username if user else None,
            full_name=user.full_name if user else None,
            amount_rub=amount_rub,
            created_at=created_at,
            age_str=age_str,
            provider_payment_id=provider_id,
            local_status=local_status_str,
            verified_provider_status=live_status_label or "Не проверялся (нажмите «Проверить»)",
            category=ProblemCategory.PROVIDER_PENDING,
            fulfillment_state="Ожидает оплаты",
            details="Пополнение баланса ожидает оплаты клиентом или проверки",
        )

    return None

    return None


async def classify_and_verify_candidate(
    candidate: dict[str, Any],
    now: datetime | None = None,
) -> ProblemPaymentItem | None:
    """Classify a single candidate and verify with YooKassa if needed."""
    now = now or datetime.utcnow()
    c_type = candidate["type"]
    obj = candidate["obj"]
    user = candidate["user"]

    if c_type == "payment":
        payment: Payment = obj
        amount_rub = (
            kopecks_to_rub_float(payment.amount)
            if payment.currency == "RUB"
            else float(payment.amount)
        )
        provider_id = payment.provider_payment_id
        created_at = payment.created_at or now
        age_str = format_age(created_at, now)
        local_status_str = f"Payment.{payment.status.value}"

        # Check Telegram delivery failure on completed payment
        if payment.status == PaymentStatus.COMPLETED and getattr(payment, "telegram_delivery_error", None):
            return ProblemPaymentItem(
                item_type="payment",
                item_id=payment.id,
                user_id=user.id if user else None,
                telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=provider_id,
                local_status=local_status_str,
                verified_provider_status="Завершен (сбой отправки TG)",
                category=ProblemCategory.TELEGRAM_DELIVERY_FAILED,
                fulfillment_state="Доступ создан, сбой отправки в TG",
                details=f"Подписка #{payment.subscription_id} создана в БД, но сообщение клиенту не дошло: {payment.telegram_delivery_error}",
            )

        # Missing provider_payment_id
        if not provider_id:
            if now - created_at > timedelta(minutes=15):
                return ProblemPaymentItem(
                    item_type="payment",
                    item_id=payment.id,
                    user_id=user.id if user else None,
                    telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                    username=user.username if user else None,
                    full_name=user.full_name if user else None,
                    amount_rub=amount_rub,
                    created_at=created_at,
                    age_str=age_str,
                    provider_payment_id=None,
                    local_status=local_status_str,
                    verified_provider_status="нет provider_id",
                    category=ProblemCategory.MISSING_PROVIDER_ID,
                    fulfillment_state="Требует расследования",
                    details=f"Локальный платеж #{payment.id} создан {age_str} назад без ID провайдера",
                )
            return None

        # Check VHQ background fulfillment state
        async with async_session() as session:
            vhq_row = await session.scalar(
                select(VhqSubscriptionFulfillment).where(
                    VhqSubscriptionFulfillment.payment_id == payment.id
                ).order_by(VhqSubscriptionFulfillment.id.desc()).limit(1)
            )
            tariff = await session.get(Tariff, payment.tariff_id) if payment.tariff_id else None

        if vhq_row:
            if vhq_row.status == "completed":
                pass
            elif vhq_row.status in (
                "pending", "processing", "retry_wait", "uncertain", "buy_inflight", "renew_inflight"
            ):
                # Legitimate normal VHQ provisioning in progress: exclude if attempts < max and not overdue (>30m)
                if vhq_row.attempts < vhq_row.max_attempts and (now - vhq_row.created_at < timedelta(minutes=30)):
                    return None
                # Stalled VHQ provisioning
                diag = diagnose_payment_problem(payment, tariff=tariff, vhq_row=vhq_row)
                return ProblemPaymentItem(
                    item_type="payment",
                    item_id=payment.id,
                    user_id=user.id if user else None,
                    telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                    username=user.username if user else None,
                    full_name=user.full_name if user else None,
                    amount_rub=amount_rub,
                    created_at=created_at,
                    age_str=age_str,
                    provider_payment_id=provider_id,
                    local_status=local_status_str,
                    verified_provider_status="VHQ: stalled",
                    category=ProblemCategory.FAILED_SUPPLIER,
                    fulfillment_state="Требует ручной проверки",
                    details=diag.root_cause,
                    supplier_name=diag.supplier_name,
                    root_cause=diag.root_cause,
                    recommended_action=diag.recommended_action,
                    safe_retry_eligible=diag.safe_retry_eligible,
                    diagnostic_code=diag.diagnostic_code,
                )
            elif vhq_row.status == "failed":
                diag = diagnose_payment_problem(payment, tariff=tariff, vhq_row=vhq_row)
                return ProblemPaymentItem(
                    item_type="payment",
                    item_id=payment.id,
                    user_id=user.id if user else None,
                    telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                    username=user.username if user else None,
                    full_name=user.full_name if user else None,
                    amount_rub=amount_rub,
                    created_at=created_at,
                    age_str=age_str,
                    provider_payment_id=provider_id,
                    local_status=local_status_str,
                    verified_provider_status="VHQ: failed",
                    category=ProblemCategory.FAILED_SUPPLIER,
                    fulfillment_state="Требует ручной проверки",
                    details=diag.root_cause,
                    supplier_name=diag.supplier_name,
                    root_cause=diag.root_cause,
                    recommended_action=diag.recommended_action,
                    safe_retry_eligible=diag.safe_retry_eligible,
                    diagnostic_code=diag.diagnostic_code,
                )

        # Check provisioning failure code
        if payment.provisioning_failure_code:
            diag = diagnose_payment_problem(payment, tariff=tariff, vhq_row=vhq_row)
            if payment.provisioning_failure_code == "payment_canceled_at_provider":
                return None
            if payment.provisioning_failure_code == "payment_canceled_with_financial_effects_review_needed":
                return ProblemPaymentItem(
                    item_type="payment",
                    item_id=payment.id,
                    user_id=user.id if user else None,
                    telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                    username=user.username if user else None,
                    full_name=user.full_name if user else None,
                    amount_rub=amount_rub,
                    created_at=created_at,
                    age_str=age_str,
                    provider_payment_id=provider_id,
                    local_status=local_status_str,
                    verified_provider_status="Отменен (фин. проверка)",
                    category=ProblemCategory.FAILED_SUPPLIER,
                    fulfillment_state="Требует ручной проверки",
                    details=diag.root_cause,
                    supplier_name=diag.supplier_name,
                    root_cause=diag.root_cause,
                    recommended_action=diag.recommended_action,
                    safe_retry_eligible=diag.safe_retry_eligible,
                    diagnostic_code=diag.diagnostic_code,
                )
            return ProblemPaymentItem(
                item_type="payment",
                item_id=payment.id,
                user_id=user.id if user else None,
                telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=provider_id,
                local_status=local_status_str,
                verified_provider_status=f"Сбой: {payment.provisioning_failure_code}",
                category=ProblemCategory.FAILED_SUPPLIER,
                fulfillment_state=diag.summary_title,
                details=diag.root_cause,
                supplier_name=diag.supplier_name,
                root_cause=diag.root_cause,
                recommended_action=diag.recommended_action,
                safe_retry_eligible=diag.safe_retry_eligible,
                diagnostic_code=diag.diagnostic_code,
            )

        # Completed without subscription delivery
        if payment.status == PaymentStatus.COMPLETED and payment.subscription_id is None and payment.platform != "device":
            return ProblemPaymentItem(
                item_type="payment",
                item_id=payment.id,
                user_id=user.id if user else None,
                telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                username=user.username if user else None,
                full_name=user.full_name if user else None,
                amount_rub=amount_rub,
                created_at=created_at,
                age_str=age_str,
                provider_payment_id=provider_id,
                local_status=local_status_str,
                verified_provider_status="completed_unlinked",
                category=ProblemCategory.UNRESOLVED_SUBSCRIPTION,
                fulfillment_state="Подписка не привязана",
                details=f"Оплата проведена, но subscription_id отсутствует",
            )

        # Pending payment: verify with YooKassa
        if payment.method == PaymentMethod.YOOKASSA and provider_id:
            try:
                yk_info = await fetch_yookassa_payment(provider_id)
            except Exception as exc:
                logger.warning("Error fetching YooKassa payment %s: %s", provider_id, exc)
                yk_info = None

            if yk_info:
                status = getattr(yk_info, "status", None) or (yk_info.get("status") if isinstance(yk_info, dict) else None)
                paid = getattr(yk_info, "paid", False) if hasattr(yk_info, "paid") else (yk_info.get("paid", False) if isinstance(yk_info, dict) else False)

                if status == "canceled":
                    # Read-only inspection: do not mutate database state
                    return None

                if status in ("pending", "waiting_for_capture"):
                    return ProblemPaymentItem(
                        item_type="payment",
                        item_id=payment.id,
                        user_id=user.id if user else None,
                        telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                        username=user.username if user else None,
                        full_name=user.full_name if user else None,
                        amount_rub=amount_rub,
                        created_at=created_at,
                        age_str=age_str,
                        provider_payment_id=provider_id,
                        local_status=local_status_str,
                        verified_provider_status=f"YooKassa: {status}",
                        category=ProblemCategory.PROVIDER_PENDING,
                        fulfillment_state="Ожидает оплаты",
                        details="Платеж открыт клиентом в YooKassa, но ещё не оплачен",
                    )

                if status == "succeeded" and paid:
                    fulfillment_cat, state_label, det_label = await _check_manual_fulfillment_evidence(payment, user, now)

                    return ProblemPaymentItem(
                        item_type="payment",
                        item_id=payment.id,
                        user_id=user.id if user else None,
                        telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                        username=user.username if user else None,
                        full_name=user.full_name if user else None,
                        amount_rub=amount_rub,
                        created_at=created_at,
                        age_str=age_str,
                        provider_payment_id=provider_id,
                        local_status=local_status_str,
                        verified_provider_status="YooKassa: succeeded (оплачен)",
                        category=fulfillment_cat,
                        fulfillment_state=state_label,
                        details=det_label,
                    )
            else:
                return ProblemPaymentItem(
                    item_type="payment",
                    item_id=payment.id,
                    user_id=user.id if user else None,
                    telegram_id=user.telegram_id if user else payment.telegram_chat_id,
                    username=user.username if user else None,
                    full_name=user.full_name if user else None,
                    amount_rub=amount_rub,
                    created_at=created_at,
                    age_str=age_str,
                    provider_payment_id=provider_id,
                    local_status=local_status_str,
                    verified_provider_status="не удалось проверить",
                    category=ProblemCategory.PROVIDER_PENDING,
                    fulfillment_state="Не проверен (ошибка API)",
                    details="Не удалось получить статус от YooKassa API",
                )

    elif c_type == "topup":
        topup: BalanceTopUp = obj
        amount_rub = kopecks_to_rub_float(
            topup.amount_kopecks if topup.amount_kopecks is not None else rub_to_kopecks(topup.amount_rub)
        )
        provider_id = topup.provider_payment_id
        created_at = topup.created_at or now
        age_str = format_age(created_at, now)
        local_status_str = f"TopUp.{topup.status}"

        if not provider_id:
            if now - created_at > timedelta(minutes=15):
                return ProblemPaymentItem(
                    item_type="topup",
                    item_id=topup.id,
                    user_id=user.id if user else None,
                    telegram_id=topup.telegram_id,
                    username=user.username if user else None,
                    full_name=user.full_name if user else None,
                    amount_rub=amount_rub,
                    created_at=created_at,
                    age_str=age_str,
                    provider_payment_id=None,
                    local_status=local_status_str,
                    verified_provider_status="нет provider_id",
                    category=ProblemCategory.MISSING_PROVIDER_ID,
                    fulfillment_state="Пополнение без provider_id",
                    details=f"Пополнение баланса #{topup.id} создано без ID провайдера",
                )
            return None

        if topup.provider == "yookassa" and provider_id:
            try:
                yk_info = await fetch_yookassa_payment(provider_id)
            except Exception as exc:
                logger.warning("Error fetching YooKassa topup %s: %s", provider_id, exc)
                yk_info = None

            if yk_info:
                status = getattr(yk_info, "status", None) or (yk_info.get("status") if isinstance(yk_info, dict) else None)
                paid = getattr(yk_info, "paid", False) if hasattr(yk_info, "paid") else (yk_info.get("paid", False) if isinstance(yk_info, dict) else False)

                if status == "canceled":
                    # Read-only inspection: do not mutate database state
                    return None

                if status in ("pending", "waiting_for_capture"):
                    return ProblemPaymentItem(
                        item_type="topup",
                        item_id=topup.id,
                        user_id=user.id if user else None,
                        telegram_id=topup.telegram_id,
                        username=user.username if user else None,
                        full_name=user.full_name if user else None,
                        amount_rub=amount_rub,
                        created_at=created_at,
                        age_str=age_str,
                        provider_payment_id=provider_id,
                        local_status=local_status_str,
                        verified_provider_status=f"YooKassa: {status}",
                        category=ProblemCategory.PROVIDER_PENDING,
                        fulfillment_state="Ожидает оплаты",
                        details="Пополнение баланса ожидает оплаты клиентом",
                    )

                if status == "succeeded" and paid:
                    topup_cat, topup_state, topup_det = await _check_manual_topup_evidence(topup, user, now, live_confirmed=True)

                    return ProblemPaymentItem(
                        item_type="topup",
                        item_id=topup.id,
                        user_id=user.id if user else None,
                        telegram_id=topup.telegram_id,
                        username=user.username if user else None,
                        full_name=user.full_name if user else None,
                        amount_rub=amount_rub,
                        created_at=created_at,
                        age_str=age_str,
                        provider_payment_id=provider_id,
                        local_status=local_status_str,
                        verified_provider_status="YooKassa: succeeded (оплачен)",
                        category=topup_cat,
                        fulfillment_state=topup_state,
                        details=topup_det,
                    )
            else:
                return ProblemPaymentItem(
                    item_type="topup",
                    item_id=topup.id,
                    user_id=user.id if user else None,
                    telegram_id=topup.telegram_id,
                    username=user.username if user else None,
                    full_name=user.full_name if user else None,
                    amount_rub=amount_rub,
                    created_at=created_at,
                    age_str=age_str,
                    provider_payment_id=provider_id,
                    local_status=local_status_str,
                    verified_provider_status="не удалось проверить",
                    category=ProblemCategory.PROVIDER_PENDING,
                    fulfillment_state="Не проверен (ошибка API)",
                    details="Не удалось получить статус от YooKassa API",
                )

    return None


async def build_problem_payments_report(limit: int = 60) -> ProblemPaymentsReport:
    """Build a complete diagnostics report of problem payments with database-wide totals."""
    candidates = await get_candidate_items(limit=limit)
    items: list[ProblemPaymentItem] = []
    sample_counts: dict[ProblemCategory, int] = {cat: 0 for cat in ProblemCategory}

    now = datetime.utcnow()
    cutoff_fresh = now - timedelta(minutes=5)
    for cand in candidates:
        item = await classify_and_verify_candidate(cand, now=now)
        if item:
            items.append(item)
            sample_counts[item.category] = sample_counts.get(item.category, 0) + 1

    category_priority = {
        ProblemCategory.CONFIRMED_UNFULFILLED: 0,
        ProblemCategory.TELEGRAM_DELIVERY_FAILED: 1,
        ProblemCategory.FAILED_SUPPLIER: 2,
        ProblemCategory.UNRESOLVED_SUBSCRIPTION: 3,
        ProblemCategory.DELIVERY_UNCERTAIN: 4,
        ProblemCategory.MISSING_PROVIDER_ID: 5,
        ProblemCategory.VERIFIED_MANUALLY_FULFILLED: 6,
        ProblemCategory.MANUALLY_FULFILLED: 6,
        ProblemCategory.PROVIDER_PENDING: 7,
    }
    # Sort items: highest urgency first (0 < 1 < 2...), and within urgency tier newest first (created_at DESC)
    items.sort(
        key=lambda it: (
            category_priority.get(it.category, 99),
            -it.created_at.timestamp() if it.created_at else 0,
        )
    )

    # Database-level exact total counts across all records (mutually exclusive)
    async with async_session() as session:
        vhq_failed_subq = (
            select(VhqSubscriptionFulfillment.payment_id)
            .where(
                or_(
                    VhqSubscriptionFulfillment.status == "failed",
                    and_(
                        VhqSubscriptionFulfillment.status == "pending",
                        or_(
                            VhqSubscriptionFulfillment.attempts >= VhqSubscriptionFulfillment.max_attempts,
                            VhqSubscriptionFulfillment.created_at <= now - timedelta(minutes=30),
                        ),
                    ),
                )
            )
        )

        cnt_failed_supplier = (await session.scalar(
            select(func.count(distinct(Payment.id)))
            .where(
                or_(
                    Payment.provisioning_failure_code.is_not(None),
                    Payment.id.in_(vhq_failed_subq),
                )
            )
        )) or 0

        cnt_unlinked = (await session.scalar(
            select(func.count(distinct(Payment.id)))
            .where(Payment.status == PaymentStatus.COMPLETED)
            .where(Payment.subscription_id.is_(None))
            .where(or_(Payment.platform.is_(None), Payment.platform != "device"))
            .where(Payment.provisioning_failure_code.is_(None))
            .where(~Payment.id.in_(vhq_failed_subq))
        )) or 0

        cnt_missing_id_payment = (await session.scalar(
            select(func.count(Payment.id))
            .where(Payment.status == PaymentStatus.PENDING)
            .where(Payment.provider_payment_id.is_(None))
            .where(Payment.created_at <= now - timedelta(minutes=15))
        )) or 0

        cnt_missing_id_topup = (await session.scalar(
            select(func.count(BalanceTopUp.id))
            .where(BalanceTopUp.status == "pending")
            .where(BalanceTopUp.provider_payment_id.is_(None))
            .where(BalanceTopUp.created_at <= now - timedelta(minutes=15))
        )) or 0

        cnt_pending_with_id_payment = (await session.scalar(
            select(func.count(Payment.id))
            .where(Payment.status == PaymentStatus.PENDING)
            .where(Payment.method == PaymentMethod.YOOKASSA)
            .where(Payment.provider_payment_id.is_not(None))
            .where(Payment.created_at <= cutoff_fresh)
        )) or 0

        cnt_pending_with_id_topup = (await session.scalar(
            select(func.count(BalanceTopUp.id))
            .where(BalanceTopUp.status == "pending")
            .where(BalanceTopUp.provider == "yookassa")
            .where(BalanceTopUp.provider_payment_id.is_not(None))
            .where(BalanceTopUp.created_at <= cutoff_fresh)
        )) or 0

    confirmed_count = sample_counts.get(ProblemCategory.CONFIRMED_UNFULFILLED, 0)
    tg_delivery_failed_count = sample_counts.get(ProblemCategory.TELEGRAM_DELIVERY_FAILED, 0)
    manual_count = sample_counts.get(ProblemCategory.VERIFIED_MANUALLY_FULFILLED, 0) or sample_counts.get(ProblemCategory.MANUALLY_FULFILLED, 0)
    uncertain_count = sample_counts.get(ProblemCategory.DELIVERY_UNCERTAIN, 0)
    counts = {
        ProblemCategory.CONFIRMED_UNFULFILLED: confirmed_count,
        ProblemCategory.TELEGRAM_DELIVERY_FAILED: tg_delivery_failed_count,
        ProblemCategory.VERIFIED_MANUALLY_FULFILLED: manual_count,
        ProblemCategory.MANUALLY_FULFILLED: manual_count,
        ProblemCategory.DELIVERY_UNCERTAIN: uncertain_count,
        ProblemCategory.FAILED_SUPPLIER: cnt_failed_supplier,
        ProblemCategory.UNRESOLVED_SUBSCRIPTION: cnt_unlinked,
        ProblemCategory.MISSING_PROVIDER_ID: cnt_missing_id_payment + cnt_missing_id_topup,
        ProblemCategory.PROVIDER_PENDING: max(
            sample_counts.get(ProblemCategory.PROVIDER_PENDING, 0),
            max(0, (cnt_pending_with_id_payment + cnt_pending_with_id_topup) - confirmed_count - manual_count - uncertain_count),
        ),
    }
    total_db_problems = (
        counts[ProblemCategory.CONFIRMED_UNFULFILLED]
        + counts[ProblemCategory.MANUALLY_FULFILLED]
        + counts[ProblemCategory.DELIVERY_UNCERTAIN]
        + counts[ProblemCategory.FAILED_SUPPLIER]
        + counts[ProblemCategory.UNRESOLVED_SUBSCRIPTION]
        + counts[ProblemCategory.MISSING_PROVIDER_ID]
        + counts[ProblemCategory.PROVIDER_PENDING]
    )

    return ProblemPaymentsReport(
        items=items,
        counts=counts,
        total_problems=len(items),
        total_db_problems=total_db_problems,
    )


async def alert_admins_if_problem_payments_detected(bot: Bot) -> None:
    """Alert administrators when urgent unfulfilled payments are detected or resolved, without spam."""
    # Alerts consume the same durable DB-only classifier as the fast dashboard.
    # No live YooKassa call is needed after the authenticated snapshot is saved.
    category_map = await _get_active_problem_categories(days=7)
    now = datetime.utcnow()
    historical_paid_items = await _get_historical_paid_problem_items(now)
    actionable_review = [
        it for it in category_map["review"]
        if it.category == ProblemCategory.DELIVERY_UNCERTAIN
        or (it.diagnostic_code and "manual_review" in it.diagnostic_code)
    ]
    urgent_items = (
        list(category_map["urgent"])
        + list(category_map["tg_delivery"])
        + list(category_map["supplier"])
        + actionable_review
    )
    known_keys = {f"{item.item_type}:{item.item_id}" for item in urgent_items}
    for item in historical_paid_items:
        key = f"{item.item_type}:{item.item_id}"
        if key not in known_keys:
            urgent_items.append(item)
            known_keys.add(key)

    # Track alert state in BotSettings to prevent spam and allow sensible re-escalation
    state_key = "problem_payments_alert_tracker"

    filtered_urgent_items: list[ProblemPaymentItem] = []
    for item in urgent_items:
        if item.category == ProblemCategory.FAILED_SUPPLIER and item.item_type == "payment":
            if (
                item.diagnostic_code in ("uncertain_supplier_mutation", "mutation_in_flight")
                or item.fulfillment_state in (
                    "Сбой: uncertain_supplier_mutation",
                    "Сбой: mutation_in_flight",
                )
                or "неопределённое состояние" in (item.fulfillment_state or "").lower()
            ):
                if item.created_at and (now - item.created_at < timedelta(minutes=5)):
                    logger.info("Payment #%s is in transient recovery backoff (<5m); suppressing urgent alert", item.item_id)
                    continue
        filtered_urgent_items.append(item)
    urgent_items = filtered_urgent_items

    async with async_session() as session:
        tracker_row = await session.get(BotSettings, state_key)
        tracker_data: dict[str, Any] = {}
        if tracker_row and tracker_row.value:
            try:
                tracker_data = json.loads(tracker_row.value)
            except Exception:
                tracker_data = {}

        last_alert_str = tracker_data.get("last_alert_at")
        last_alert_dt = datetime.fromisoformat(last_alert_str) if last_alert_str else None
        previously_alerted_keys = set(tracker_data.get("alerted_keys", []))

        current_keys = {f"{item.item_type}:{item.item_id}" for item in urgent_items}
        resolved_candidates = previously_alerted_keys - current_keys
        new_keys = current_keys - previously_alerted_keys

        actually_resolved_keys: set[str] = set()

        # If payment/topup was previously alerted as urgent and is candidate for resolution,
        # verify actual entity state from DB. Do NOT declare resolved merely because it left urgent categories.
        for res_key in sorted(resolved_candidates):
            try:
                parts = res_key.split(":", 1)
                if len(parts) != 2:
                    continue
                item_type, item_id_str = parts
                item_id = int(item_id_str)

                is_clean = False
                res_text = None

                if item_type == "payment":
                    p_obj = await session.get(Payment, item_id)
                    if not p_obj:
                        continue

                    # Resolution alert is only sent when entity is genuinely clean:
                    # - status == COMPLETED
                    # - provisioning_failure_code is None
                    # - telegram_delivery_error is None
                    # - subscription_id is linked (for subscription platforms)
                    # - classify_candidate_db_only confirms ProblemCategory == PROCESSED (or None)
                    if (
                        p_obj.status == PaymentStatus.COMPLETED
                        and not p_obj.provisioning_failure_code
                        and not getattr(p_obj, "telegram_delivery_error", None)
                    ):
                        if p_obj.platform not in ("topup", "device") and not p_obj.subscription_id:
                            continue

                        u_obj = await session.get(User, p_obj.user_id) if p_obj.user_id else None
                        cand = {"type": "payment", "obj": p_obj, "user": u_obj}
                        prob = await classify_candidate_db_only(cand, now=now)
                        if prob is None or prob.category == ProblemCategory.PROCESSED:
                            is_clean = True

                    if not is_clean:
                        # Payment transitioned to a non-urgent problem (e.g. TELEGRAM_DELIVERY_FAILED, DELIVERY_UNCERTAIN)
                        # or is still unfulfilled. Suppress resolution alert and retain in alerted keys.
                        continue

                    u_obj = await session.get(User, p_obj.user_id) if (p_obj and p_obj.user_id) else None
                    sub_obj = await session.get(Subscription, p_obj.subscription_id) if (p_obj and p_obj.subscription_id) else None
                    amt_rub = (
                        kopecks_to_rub_float(p_obj.amount)
                        if p_obj and p_obj.currency == "RUB"
                        else (float(p_obj.amount) if p_obj else 0.0)
                    )
                    tg_id = (u_obj.telegram_id if u_obj else (p_obj.telegram_chat_id if p_obj else None))
                    user_str = f"@{u_obj.username}" if (u_obj and u_obj.username) else f"id={tg_id or '—'}"

                    sub_text = ""
                    if sub_obj and sub_obj.status == SubStatus.ACTIVE:
                        sub_text = f"\n✅ Подписка #{sub_obj.id} активна"
                    elif p_obj and p_obj.subscription_id:
                        sub_text = f"\n✅ Подписка #{p_obj.subscription_id} привязана"

                    status_text = "Оплата завершена" if (p_obj and p_obj.status == PaymentStatus.COMPLETED) else (f"Статус: {p_obj.status.value}" if p_obj else "Обработано")

                    res_text = (
                        f"✅ <b>ПРОБЛЕМНАЯ ОПЛАТА ОБРАБОТАНА</b>\n\n"
                        f"payment #{item_id}\n"
                        f"{amt_rub:.2f} ₽\n"
                        f"Пользователь: {user_str}\n\n"
                        f"Итог:\n"
                        f"✅ {status_text}"
                        f"{sub_text}\n"
                        f"✅ Финансовая обработка завершена\n"
                        f"✅ Дополнительных действий не требуется"
                    )

                elif item_type == "topup":
                    top_obj = await session.get(BalanceTopUp, item_id)
                    if not top_obj:
                        continue

                    if top_obj.status == "completed":
                        u_obj = await session.get(User, top_obj.user_id) if top_obj.user_id else None
                        cand = {"type": "topup", "obj": top_obj, "user": u_obj}
                        prob = await classify_candidate_db_only(cand, now=now)
                        if prob is None or prob.category == ProblemCategory.PROCESSED:
                            is_clean = True

                    if not is_clean:
                        continue

                    u_obj = await session.get(User, top_obj.user_id) if (top_obj and top_obj.user_id) else None
                    amt_rub = (
                        kopecks_to_rub_float(
                            top_obj.amount_kopecks
                            if top_obj and top_obj.amount_kopecks is not None
                            else rub_to_kopecks(top_obj.amount_rub)
                        )
                        if top_obj
                        else 0.0
                    )
                    tg_id = u_obj.telegram_id if u_obj else (top_obj.telegram_id if top_obj else None)
                    user_str = f"@{u_obj.username}" if (u_obj and u_obj.username) else f"id={tg_id or '—'}"

                    res_text = (
                        f"✅ <b>ПРОБЛЕМНОЕ ПОПОЛНЕНИЕ ОБРАБОТАНО</b>\n\n"
                        f"topup #{item_id}\n"
                        f"{amt_rub:.2f} ₽\n"
                        f"Пользователь: {user_str}\n\n"
                        f"Итог:\n"
                        f"✅ Пополнение баланса завершено\n"
                        f"✅ Баланс успешно зачислен\n"
                        f"✅ Дополнительных действий не требуется"
                    )

                if is_clean and res_text:
                    if await notify_staff_text(bot, res_text):
                        actually_resolved_keys.add(res_key)
            except Exception as res_exc:
                logger.error("Failed to send resolution alert for %s: %s", res_key, res_exc)

        # Determine if we should send an urgent alert:
        # 1. Any brand-new urgent payment detected
        # 2. Or re-escalate if unfulfilled for > 6 hours since last alert
        should_alert = bool(urgent_items) and (
            bool(new_keys) or (last_alert_dt and (now - last_alert_dt > timedelta(hours=6)))
        )

        alerted_current_keys: set[str] = set()
        if should_alert:
            details = []
            for item in urgent_items:
                tg = item.telegram_id or "—"
                user_str = f"@{item.username}" if item.username else f"id={tg}"
                supp_line = f"\n  Поставщик: {item.supplier_name}" if item.supplier_name else ""
                reason_line = f"\n  Причина: {item.root_cause}" if item.root_cause else ""
                action_line = f"\n  Действие: {item.recommended_action}" if item.recommended_action else ""
                safe_tag = " | Безопасный повтор: Да" if item.safe_retry_eligible else ""
                code_line = f"\n  [Код: {item.diagnostic_code or item.category.value}{safe_tag}]"
                details.append(
                    f"• {item.item_type} #{item.item_id}: {item.amount_rub:.2f} ₽ ({item.age_str} назад), {user_str}"
                    f"{supp_line}\n"
                    f"  Статус: {item.fulfillment_state}{reason_line}{action_line}\n"
                    f"  Провайдер: <code>{item.provider_payment_id or '—'}</code>{code_line}"
                )

            title = f"⚠️ ПРОБЛЕМНЫЕ ОПЛАТЫ ({len(urgent_items)} шт.)"
            alert_delivered = await notify_admins_issue(
                bot,
                title=title,
                details=details,
            )
            if alert_delivered:
                alerted_current_keys = current_keys
                tracker_data["last_alert_at"] = now.isoformat()

        # Update tracker state:
        # Urgent alert tracker drops the key ONLY upon genuine resolution.
        # Keep current urgent items + any previously alerted items that haven't genuinely resolved yet.
        next_alerted_keys = (
            (previously_alerted_keys - actually_resolved_keys)
            | alerted_current_keys
            | (current_keys & previously_alerted_keys)
        )

        if actually_resolved_keys or new_keys or should_alert or (next_alerted_keys != previously_alerted_keys):
            tracker_data["alerted_keys"] = sorted(next_alerted_keys)
            new_json = json.dumps(tracker_data)
            if tracker_row:
                tracker_row.value = new_json
            else:
                session.add(BotSettings(key=state_key, value=new_json))
            await session.commit()

async def verify_single_payment_live(
    item_type: str,
    item_id: int,
    force: bool = False,
) -> tuple[ProblemPaymentItem | None, str]:
    """Perform on-demand live verification with YooKassa API for a single record.

    - If force is False: checks 5-minute TTL cache first.
    - If force is True: rate-limited to avoid external request flooding (minimum 3 seconds between live checks).
    - Preserves 7.0s timeout and authenticated fetch_yookassa_payment call.
    - Never uses cache for payment provisioning or fulfillment.
    """
    async with async_session() as session:
        if item_type == "payment":
            obj = await session.get(Payment, item_id)
            user = await session.get(User, obj.user_id) if (obj and obj.user_id) else None
        else:
            obj = await session.get(BalanceTopUp, item_id)
            user = await session.get(User, obj.user_id) if (obj and getattr(obj, "user_id", None)) else None
            if not user and obj and obj.telegram_id:
                user = await session.scalar(select(User).where(User.telegram_id == obj.telegram_id))

    if not obj:
        return None, "Запись не найдена в базе данных"

    provider_id = getattr(obj, "provider_payment_id", None)
    if not provider_id:
        item = await classify_candidate_db_only({"type": item_type, "obj": obj, "user": user})
        return item, "У записи отсутствует provider_payment_id"

    now = datetime.utcnow()
    cached = get_cached_live_status(item_type, item_id)

    # If not forcing refresh, return valid cached entry
    if not force and cached:
        item = await classify_candidate_db_only({"type": item_type, "obj": obj, "user": user})
        age_str = format_age(cached.checked_at, now)
        msg = f"Статус в YooKassa (из кэша, {age_str} назад): {cached.status} ({'оплачен' if cached.paid else 'не оплачен'})"
        return item, msg

    # Bounded rate limiting on forced refresh: at least 3s between external network calls
    if cached and (now - cached.checked_at < timedelta(seconds=3)):
        item = await classify_candidate_db_only({"type": item_type, "obj": obj, "user": user})
        msg = f"Статус в YooKassa (проверено только что): {cached.status} ({'оплачен' if cached.paid else 'не оплачен'})"
        return item, msg

    try:
        yk_info = await asyncio.wait_for(fetch_yookassa_payment(provider_id), timeout=7.0)
    except asyncio.TimeoutError:
        item = await classify_candidate_db_only({"type": item_type, "obj": obj, "user": user})
        return item, "Таймаут обращения к YooKassa API (>7 сек)"
    except Exception as exc:
        logger.warning("Error fetching YooKassa payment %s: %s", provider_id, exc)
        item = await classify_candidate_db_only({"type": item_type, "obj": obj, "user": user})
        return item, f"Ошибка API YooKassa: {exc}"

    if not yk_info:
        item = await classify_candidate_db_only({"type": item_type, "obj": obj, "user": user})
        return item, "Платёж не найден в YooKassa"

    status = getattr(yk_info, "status", None) or (yk_info.get("status") if isinstance(yk_info, dict) else "unknown")
    paid = getattr(yk_info, "paid", False) if hasattr(yk_info, "paid") else (yk_info.get("paid", False) if isinstance(yk_info, dict) else False)

    live_label = f"YooKassa: {status}{' (оплачен)' if paid else ''}"
    set_cached_live_status(item_type, item_id, {
        "status": status,
        "paid": paid,
        "status_label": live_label,
    })

    item = await classify_candidate_db_only({"type": item_type, "obj": obj, "user": user})
    msg = f"Статус в YooKassa (проверено только что): {status} ({'оплачен' if paid else 'не оплачен'})"
    return item, msg


def _build_stale_telegram_outbox_item(
    payment: Payment,
    user: User | None,
    outbox: PaymentNotificationOutbox,
    now: datetime,
) -> ProblemPaymentItem:
    created_at = payment.created_at or outbox.created_at or now
    amount_rub = (
        kopecks_to_rub_float(payment.amount)
        if payment.currency == "RUB"
        else float(payment.amount)
    )
    recipient = user.telegram_id if user else payment.telegram_chat_id
    error_text = outbox.last_error or "нет зафиксированной ошибки"
    return ProblemPaymentItem(
        item_type="payment",
        item_id=payment.id,
        user_id=user.id if user else payment.user_id,
        telegram_id=recipient,
        username=user.username if user else None,
        full_name=user.full_name if user else None,
        amount_rub=amount_rub,
        created_at=created_at,
        age_str=format_age(created_at, now),
        provider_payment_id=payment.provider_payment_id,
        local_status=f"Payment.{payment.status.value}",
        verified_provider_status="Оплата подтверждена" if payment.provider_paid else "Доставка уведомления не завершена",
        category=ProblemCategory.TELEGRAM_DELIVERY_FAILED,
        fulfillment_state=f"Клиентское сообщение {outbox.kind} не доставлено (outbox: {outbox.status})",
        details=(
            f"Долговечная запись Telegram outbox #{outbox.id} остаётся в статусе {outbox.status}; "
            f"попыток: {outbox.attempts}; последняя ошибка: {error_text}"
        ),
        root_cause="Клиентское сообщение не подтверждено как доставленное Telegram.",
        recommended_action="Проверить Telegram и повторить доставку из панели. Повторная закупка не требуется.",
        safe_retry_eligible=False,
        diagnostic_code="telegram_delivery_failed",
    )


async def _get_historical_paid_problem_items(now: datetime) -> list[ProblemPaymentItem]:
    """Return unresolved provider-paid payments regardless of payment age for alerting."""
    async with async_session() as session:
        rows = (await session.execute(
            select(Payment, User)
            .outerjoin(User, User.id == Payment.user_id)
            .where(
                or_(
                    Payment.telegram_delivery_error.is_not(None),
                    and_(
                        Payment.provider_status == "succeeded",
                        Payment.provider_paid.is_(True),
                        or_(
                            Payment.status != PaymentStatus.COMPLETED,
                            Payment.provisioning_failure_code.is_not(None),
                        ),
                    ),
                )
            )
            .order_by(Payment.created_at.asc())
        )).all()

    items: list[ProblemPaymentItem] = []
    for payment, user in rows:
        item = await classify_candidate_db_only({"type": "payment", "obj": payment, "user": user}, now=now)
        if item and item.category not in (ProblemCategory.PROCESSED, ProblemCategory.PROVIDER_PENDING):
            items.append(item)
    return items


async def _get_active_problem_categories(days: int = 7, now: datetime | None = None) -> dict[str, list[ProblemPaymentItem]]:
    """Group all active problem payments by category strictly based on actual classification."""
    now = now or datetime.utcnow()
    cutoff_7d = now - timedelta(days=days)

    category_map: dict[str, list[ProblemPaymentItem]] = {
        "urgent": [],
        "tg_delivery": [],
        "supplier": [],
        "review": [],
    }
    seen_keys: set[str] = set()

    async with async_session() as session:
        vhq_fail_subq = (
            select(VhqSubscriptionFulfillment.payment_id)
            .where(
                or_(
                    VhqSubscriptionFulfillment.status == "failed",
                    and_(
                        VhqSubscriptionFulfillment.status == "pending",
                        or_(
                            VhqSubscriptionFulfillment.attempts >= VhqSubscriptionFulfillment.max_attempts,
                            VhqSubscriptionFulfillment.created_at <= now - timedelta(minutes=30),
                        ),
                    ),
                ),
                VhqSubscriptionFulfillment.created_at >= cutoff_7d,
            )
        )

        stale_outbox_cutoff = now - timedelta(minutes=30)
        stale_customer_outbox = (await session.execute(
            select(PaymentNotificationOutbox, Payment, User)
            .join(Payment, Payment.id == PaymentNotificationOutbox.payment_id)
            .outerjoin(User, User.id == Payment.user_id)
            .where(
                PaymentNotificationOutbox.status.in_(["pending", "sending", "unknown"]),
                func.coalesce(
                    PaymentNotificationOutbox.last_attempt_at,
                    PaymentNotificationOutbox.created_at,
                ) <= stale_outbox_cutoff,
            )
            .order_by(
                func.coalesce(
                    PaymentNotificationOutbox.last_attempt_at,
                    PaymentNotificationOutbox.created_at,
                ).asc()
            )
        )).all()

        for outbox, payment, user in stale_customer_outbox:
            key = f"payment:{payment.id}"
            if key in seen_keys:
                continue
            category_map["tg_delivery"].append(
                _build_stale_telegram_outbox_item(payment, user, outbox, now)
            )
            seen_keys.add(key)

        # 1. Completed unlinked payments
        completed_unlinked = (await session.execute(
            select(Payment, User)
            .outerjoin(User, User.id == Payment.user_id)
            .where(
                Payment.status == PaymentStatus.COMPLETED,
                Payment.subscription_id.is_(None),
                or_(Payment.platform.is_(None), Payment.platform != "device"),
                Payment.created_at >= cutoff_7d,
            )
            .order_by(Payment.created_at.desc())
        )).all()

        for p, u in completed_unlinked:
            key = f"payment:{p.id}"
            if key in seen_keys:
                continue
            item = await classify_candidate_db_only({"type": "payment", "obj": p, "user": u}, now=now)
            if item:
                if item.category == ProblemCategory.CONFIRMED_UNFULFILLED:
                    category_map["urgent"].append(item)
                    seen_keys.add(key)
                elif item.category == ProblemCategory.UNRESOLVED_SUBSCRIPTION:
                    category_map["urgent"].append(item)
                    seen_keys.add(key)
                elif item.category in (ProblemCategory.DELIVERY_UNCERTAIN, ProblemCategory.MISSING_PROVIDER_ID):
                    category_map["review"].append(item)
                    seen_keys.add(key)

        # 2. Telegram delivery errors
        tg_errs = (await session.execute(
            select(Payment, User)
            .outerjoin(User, User.id == Payment.user_id)
            .where(
                Payment.telegram_delivery_error.is_not(None),
                Payment.created_at >= cutoff_7d,
            )
            .order_by(Payment.created_at.desc())
        )).all()

        for p, u in tg_errs:
            key = f"payment:{p.id}"
            if key in seen_keys:
                continue
            item = await classify_candidate_db_only({"type": "payment", "obj": p, "user": u}, now=now)
            if item and item.category == ProblemCategory.TELEGRAM_DELIVERY_FAILED:
                category_map["tg_delivery"].append(item)
                seen_keys.add(key)

        # 3. Supplier errors
        supp_errs = (await session.execute(
            select(Payment, User)
            .outerjoin(User, User.id == Payment.user_id)
            .where(
                or_(
                    and_(
                        Payment.provisioning_failure_code.is_not(None),
                        Payment.provisioning_failure_code != "payment_canceled_at_provider",
                        Payment.provisioning_failure_code != "payment_canceled_with_financial_effects_review_needed",
                    ),
                    Payment.id.in_(vhq_fail_subq),
                ),
                Payment.created_at >= cutoff_7d,
            )
            .order_by(Payment.created_at.desc())
        )).all()

        for p, u in supp_errs:
            key = f"payment:{p.id}"
            if key in seen_keys:
                continue
            item = await classify_candidate_db_only({"type": "payment", "obj": p, "user": u}, now=now)
            if item:
                if item.category == ProblemCategory.FAILED_SUPPLIER:
                    category_map["supplier"].append(item)
                    seen_keys.add(key)
                elif item.category in (ProblemCategory.DELIVERY_UNCERTAIN, ProblemCategory.MISSING_PROVIDER_ID):
                    category_map["review"].append(item)
                    seen_keys.add(key)
                elif item.category == ProblemCategory.CONFIRMED_UNFULFILLED:
                    category_map["urgent"].append(item)
                    seen_keys.add(key)

        # 3b. Provider-confirmed payments remain urgent without a live API call.
        paid_unresolved = (await session.execute(
            select(Payment, User)
            .outerjoin(User, User.id == Payment.user_id)
            .where(
                Payment.provider_status == "succeeded",
                Payment.provider_paid.is_(True),
                or_(
                    Payment.status != PaymentStatus.COMPLETED,
                    Payment.provisioning_failure_code.like("yookassa_%"),
                ),
                Payment.created_at >= cutoff_7d,
            )
            .order_by(Payment.created_at.desc())
        )).all()

        for p, u in paid_unresolved:
            key = f"payment:{p.id}"
            if key in seen_keys:
                continue
            item = await classify_candidate_db_only({"type": "payment", "obj": p, "user": u}, now=now)
            if item:
                if item.category == ProblemCategory.CONFIRMED_UNFULFILLED:
                    category_map["urgent"].append(item)
                    seen_keys.add(key)
                elif item.category == ProblemCategory.FAILED_SUPPLIER:
                    category_map["supplier"].append(item)
                    seen_keys.add(key)
                elif item.category in (ProblemCategory.DELIVERY_UNCERTAIN, ProblemCategory.MISSING_PROVIDER_ID):
                    category_map["review"].append(item)
                    seen_keys.add(key)

        paid_topups = (await session.execute(
            select(BalanceTopUp, User)
            .outerjoin(User, User.id == BalanceTopUp.user_id)
            .where(
                BalanceTopUp.provider == "yookassa",
                BalanceTopUp.provider_status == "succeeded",
                BalanceTopUp.provider_paid.is_(True),
                or_(
                    BalanceTopUp.status != "completed",
                    BalanceTopUp.provider_failure_code.like("yookassa_%"),
                ),
                BalanceTopUp.created_at >= cutoff_7d,
            )
            .order_by(BalanceTopUp.created_at.desc())
        )).all()

        for topup, u in paid_topups:
            key = f"topup:{topup.id}"
            if key in seen_keys:
                continue
            item = await classify_candidate_db_only({"type": "topup", "obj": topup, "user": u}, now=now)
            if item and item.category == ProblemCategory.CONFIRMED_UNFULFILLED:
                category_map["urgent"].append(item)
                seen_keys.add(key)

        # 4. Review:
        # a) financial review needed
        # b) manual review needed (ambiguous mutations, competing orders, unproven renewals)
        # c) pending payments without provider_payment_id after 15m
        review_payments = (await session.execute(
            select(Payment, User)
            .outerjoin(User, User.id == Payment.user_id)
            .where(
                or_(
                    Payment.provisioning_failure_code == "payment_canceled_with_financial_effects_review_needed",
                    Payment.provisioning_failure_code.like("%manual_review%"),
                    and_(
                        Payment.status == PaymentStatus.PENDING,
                        Payment.provider_payment_id.is_(None),
                        Payment.created_at <= now - timedelta(minutes=15),
                    ),
                ),
                Payment.created_at >= cutoff_7d,
            )
            .order_by(Payment.created_at.desc())
        )).all()

        for p, u in review_payments:
            key = f"payment:{p.id}"
            if key in seen_keys:
                continue
            item = await classify_candidate_db_only({"type": "payment", "obj": p, "user": u}, now=now)
            if item and item.category in (ProblemCategory.DELIVERY_UNCERTAIN, ProblemCategory.MISSING_PROVIDER_ID):
                category_map["review"].append(item)
                seen_keys.add(key)

        # 5. BalanceTopUp issues in active window:
        # a) Pending without provider ID after 15m -> review
        # b) Succeeded/paid via live cache but not credited -> urgent
        topup_review = (await session.execute(
            select(BalanceTopUp)
            .where(
                BalanceTopUp.status == "pending",
                BalanceTopUp.provider_payment_id.is_(None),
                BalanceTopUp.created_at <= now - timedelta(minutes=15),
                BalanceTopUp.created_at >= cutoff_7d,
            )
            .order_by(BalanceTopUp.created_at.desc())
        )).scalars().all()

        for t in topup_review:
            key = f"topup:{t.id}"
            if key in seen_keys:
                continue
            u = await session.get(User, t.user_id) if getattr(t, "user_id", None) else None
            if not u and t.telegram_id:
                u = await session.scalar(select(User).where(User.telegram_id == t.telegram_id))
            item = await classify_candidate_db_only({"type": "topup", "obj": t, "user": u}, now=now)
            if item and item.category in (ProblemCategory.DELIVERY_UNCERTAIN, ProblemCategory.MISSING_PROVIDER_ID):
                category_map["review"].append(item)
                seen_keys.add(key)

        # Check for completed or confirmed topups that were not credited
        topup_active = (await session.execute(
            select(BalanceTopUp)
            .where(
                BalanceTopUp.created_at >= cutoff_7d,
                or_(
                    BalanceTopUp.status == "completed",
                    BalanceTopUp.provider_payment_id.is_not(None),
                ),
            )
            .order_by(BalanceTopUp.created_at.desc())
        )).scalars().all()

        for t in topup_active:
            key = f"topup:{t.id}"
            if key in seen_keys:
                continue
            u = await session.get(User, t.user_id) if getattr(t, "user_id", None) else None
            if not u and t.telegram_id:
                u = await session.scalar(select(User).where(User.telegram_id == t.telegram_id))
            item = await classify_candidate_db_only({"type": "topup", "obj": t, "user": u}, now=now)
            if item:
                if item.category in (ProblemCategory.DELIVERY_UNCERTAIN, ProblemCategory.MISSING_PROVIDER_ID):
                    category_map["review"].append(item)
                    seen_keys.add(key)
                elif item.category == ProblemCategory.CONFIRMED_UNFULFILLED:
                    category_map["urgent"].append(item)
                    seen_keys.add(key)

    return category_map


async def refresh_unsnapshotted_yookassa_state(days: int = 30) -> None:
    """Persist authenticated provider snapshots for old pending records before alerting."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    async with async_session() as session:
        payment_ids = (
            await session.scalars(
                select(Payment.provider_payment_id)
                .where(
                    Payment.method == PaymentMethod.YOOKASSA,
                    Payment.provider_payment_id.is_not(None),
                    Payment.created_at >= cutoff,
                    Payment.provider_status.is_(None),
                )
            )
        ).all()
        topup_ids = (
            await session.scalars(
                select(BalanceTopUp.provider_payment_id)
                .where(
                    BalanceTopUp.provider == "yookassa",
                    BalanceTopUp.provider_payment_id.is_not(None),
                    BalanceTopUp.created_at >= cutoff,
                    BalanceTopUp.provider_status.is_(None),
                )
            )
        ).all()

    provider_ids = {str(value) for value in [*payment_ids, *topup_ids] if value}
    for provider_id in provider_ids:
        try:
            snapshot = await asyncio.wait_for(fetch_yookassa_payment(provider_id), timeout=7.0)
        except Exception as exc:
            logger.warning("Unable to refresh YooKassa snapshot %s: %s", provider_id, exc)
            continue
        if not snapshot:
            continue
        if isinstance(snapshot, dict):
            status = snapshot.get("status")
            paid = snapshot.get("paid")
            amount = snapshot.get("amount") or {}
        else:
            status = getattr(snapshot, "status", None)
            paid = getattr(snapshot, "paid", None)
            amount = getattr(snapshot, "amount", None)
        raw_amount = amount.get("value") if isinstance(amount, dict) else getattr(amount, "value", None)
        raw_currency = amount.get("currency") if isinstance(amount, dict) else getattr(amount, "currency", None)
        try:
            amount_kopecks = rub_to_kopecks(raw_amount) if raw_amount is not None else None
        except ValueError:
            amount_kopecks = None
        currency = str(raw_currency).strip().upper() if raw_currency else None

        async with async_session() as session:
            payment = await session.scalar(select(Payment).where(Payment.provider_payment_id == provider_id))
            topup = await session.scalar(select(BalanceTopUp).where(BalanceTopUp.provider_payment_id == provider_id))
            verified_at = datetime.utcnow()
            if payment:
                payment.provider_status = status
                payment.provider_paid = bool(paid) if paid is not None else None
                payment.provider_amount_kopecks = amount_kopecks
                payment.provider_currency = currency
                payment.provider_verified_at = verified_at
            if topup:
                topup.provider_status = status
                topup.provider_paid = bool(paid) if paid is not None else None
                topup.provider_amount_kopecks = amount_kopecks
                topup.provider_currency = currency
                topup.provider_verified_at = verified_at
            if payment or topup:
                await session.commit()


def _build_topup_ledger_tx_subquery():
    """Strict subquery verifying actual BalanceTransaction credit evidence for BalanceTopUp.

    Validates:
    1. Matching customer/user_id (coalescing user_id or looking up by telegram_id).
    2. kind=TOPUP and direction=CREDIT.
    3. Matching credited amount (within 0.01 RUB).
    4. Trustworthy provider_payment_id or topup-specific source correlation.
    5. Correct source_type where available.
    """
    valid_topup_source_types = [
        "yookassa_topup",
        "telegram_topup",
        "robokassa_topup",
        "balance_topup",
        "manual_topup",
        "topup",
        "web_balance_migration",
    ]
    return (
        select(BalanceTransaction.id)
        .where(
            BalanceTransaction.user_id == func.coalesce(
                BalanceTopUp.user_id,
                select(User.id).where(User.telegram_id == BalanceTopUp.telegram_id).scalar_subquery(),
            ),
            BalanceTransaction.kind.in_([BalanceTransactionKind.TOPUP, "TOPUP", "topup"]),
            BalanceTransaction.direction.in_([BalanceDirection.CREDIT, "CREDIT", "credit"]),
            func.abs(BalanceTransaction.amount_rub - BalanceTopUp.amount_rub) < 0.01,
            or_(
                and_(
                    BalanceTopUp.provider_payment_id.is_not(None),
                    BalanceTopUp.provider_payment_id != "",
                    BalanceTransaction.source_id == BalanceTopUp.provider_payment_id,
                ),
                BalanceTransaction.source_id == cast(BalanceTopUp.id, String),
            ),
            or_(
                BalanceTransaction.source_type.is_(None),
                BalanceTransaction.source_type.in_(valid_topup_source_types),
                BalanceTransaction.source_type == func.coalesce(BalanceTopUp.provider, "") + "_topup",
                BalanceTransaction.source_type == BalanceTopUp.provider,
            ),
        )
    )


async def get_fast_dashboard_summary(days: int = 7) -> FastDashboardSummary:
    """Fast, DB-only summary counters and recent problem cases without external API calls."""
    now = datetime.utcnow()
    cutoff_7d = now - timedelta(days=days)

    category_map = await _get_active_problem_categories(days=days, now=now)
    cnt_urgent = len(category_map["urgent"])
    cnt_tg = len(category_map["tg_delivery"])
    cnt_supp = len(category_map["supplier"])
    cnt_rev = len(category_map["review"])
    total_active = cnt_urgent + cnt_tg + cnt_supp + cnt_rev

    # Prioritize items for the main screen: urgent first, then tg, supplier, review
    category_priority = {
        ProblemCategory.CONFIRMED_UNFULFILLED: 0,
        ProblemCategory.TELEGRAM_DELIVERY_FAILED: 1,
        ProblemCategory.FAILED_SUPPLIER: 2,
        ProblemCategory.DELIVERY_UNCERTAIN: 3,
        ProblemCategory.MISSING_PROVIDER_ID: 4,
    }
    all_urgent: list[ProblemPaymentItem] = []
    for cat_items in category_map.values():
        all_urgent.extend(cat_items)
    all_urgent.sort(
        key=lambda it: (
            category_priority.get(it.category, 99),
            -it.created_at.timestamp() if it.created_at else 0,
        )
    )

    # Compute archive count across both Payment and BalanceTopUp (>7 days)
    async with async_session() as session:
        vhq_fail_subq = (
            select(VhqSubscriptionFulfillment.payment_id)
            .where(
                or_(
                    VhqSubscriptionFulfillment.status == "failed",
                    and_(
                        VhqSubscriptionFulfillment.status == "pending",
                        or_(
                            VhqSubscriptionFulfillment.attempts >= VhqSubscriptionFulfillment.max_attempts,
                            VhqSubscriptionFulfillment.created_at <= now - timedelta(minutes=30),
                        ),
                    ),
                )
            )
        )

        qp = select(
            literal("payment").label("type"),
            Payment.id.label("id"),
            Payment.created_at.label("created_at")
        ).where(
            Payment.created_at < cutoff_7d,
            or_(
                and_(
                    Payment.status == PaymentStatus.COMPLETED,
                    Payment.subscription_id.is_(None),
                    or_(Payment.platform.is_(None), Payment.platform != "device"),
                ),
                Payment.telegram_delivery_error.is_not(None),
                and_(
                    Payment.provisioning_failure_code.is_not(None),
                    Payment.provisioning_failure_code != "payment_canceled_at_provider",
                ),
                Payment.status == PaymentStatus.PENDING,
                Payment.id.in_(vhq_fail_subq),
            )
        )

        tx_topup_subq = _build_topup_ledger_tx_subquery()
        qt = select(
            literal("topup").label("type"),
            BalanceTopUp.id.label("id"),
            BalanceTopUp.created_at.label("created_at")
        ).where(
            BalanceTopUp.created_at < cutoff_7d,
            or_(
                BalanceTopUp.status == "pending",
                and_(
                    BalanceTopUp.status == "completed",
                    ~exists(tx_topup_subq),
                ),
            ),
        )

        u = qp.union_all(qt).subquery()
        total_archive = (await session.scalar(select(func.count()).select_from(u))) or 0

    return FastDashboardSummary(
        urgent_unfulfilled=cnt_urgent,
        tg_delivery_failed=cnt_tg,
        supplier_failed=cnt_supp,
        needs_review=cnt_rev,
        total_active=total_active,
        total_archive=total_archive,
        urgent_items=all_urgent[:6],
    )


async def get_problem_payments_page(
    category: str | None = None,
    scope: ProblemScope = ProblemScope.ACTIVE,
    page: int = 1,
    per_page: int = 6,
) -> tuple[list[ProblemPaymentItem], int, int]:
    """Get paginated problem payments list for a category or archive view without live API calls."""
    now = datetime.utcnow()
    cutoff_7d = now - timedelta(days=7)

    if scope == ProblemScope.ACTIVE:
        category_map = await _get_active_problem_categories(days=7, now=now)
        if category and category in category_map:
            filtered_items = category_map[category]
        else:
            all_active = []
            for cat_list in category_map.values():
                all_active.extend(cat_list)
            category_priority = {
                ProblemCategory.CONFIRMED_UNFULFILLED: 0,
                ProblemCategory.TELEGRAM_DELIVERY_FAILED: 1,
                ProblemCategory.FAILED_SUPPLIER: 2,
                ProblemCategory.DELIVERY_UNCERTAIN: 3,
                ProblemCategory.MISSING_PROVIDER_ID: 4,
            }
            all_active.sort(
                key=lambda it: (
                    category_priority.get(it.category, 99),
                    -it.created_at.timestamp() if it.created_at else 0,
                )
            )
            filtered_items = all_active

        total_count = len(filtered_items)
        total_pages = max(1, (total_count + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * per_page
        items = filtered_items[offset : offset + per_page]
        return items, total_count, total_pages

    # ARCHIVE VIEW (>7 days)
    async with async_session() as session:
        vhq_fail_subq = (
            select(VhqSubscriptionFulfillment.payment_id)
            .where(
                or_(
                    VhqSubscriptionFulfillment.status == "failed",
                    and_(
                        VhqSubscriptionFulfillment.status == "pending",
                        or_(
                            VhqSubscriptionFulfillment.attempts >= VhqSubscriptionFulfillment.max_attempts,
                            VhqSubscriptionFulfillment.created_at <= now - timedelta(minutes=30),
                        ),
                    ),
                )
            )
        )

        qp = select(
            literal("payment").label("type"),
            Payment.id.label("id"),
            Payment.created_at.label("created_at")
        ).where(
            Payment.created_at < cutoff_7d,
            or_(
                and_(
                    Payment.status == PaymentStatus.COMPLETED,
                    Payment.subscription_id.is_(None),
                    or_(Payment.platform.is_(None), Payment.platform != "device"),
                ),
                Payment.telegram_delivery_error.is_not(None),
                and_(
                    Payment.provisioning_failure_code.is_not(None),
                    Payment.provisioning_failure_code != "payment_canceled_at_provider",
                ),
                Payment.status == PaymentStatus.PENDING,
                Payment.id.in_(vhq_fail_subq),
            )
        )

        tx_topup_subq = _build_topup_ledger_tx_subquery()
        qt = select(
            literal("topup").label("type"),
            BalanceTopUp.id.label("id"),
            BalanceTopUp.created_at.label("created_at")
        ).where(
            BalanceTopUp.created_at < cutoff_7d,
            or_(
                BalanceTopUp.status == "pending",
                and_(
                    BalanceTopUp.status == "completed",
                    ~exists(tx_topup_subq),
                ),
            ),
        )

        u = qp.union_all(qt).subquery()
        total_count = (await session.scalar(select(func.count()).select_from(u))) or 0
        total_pages = max(1, (total_count + per_page - 1) // per_page)
        page = max(1, min(page, total_pages))
        offset = (page - 1) * per_page

        page_rows = (await session.execute(
            select(u.c.type, u.c.id)
            .order_by(u.c.created_at.desc())
            .offset(offset)
            .limit(per_page)
        )).all()

        items = []
        for it_type, it_id in page_rows:
            if it_type == "payment":
                p = await session.get(Payment, it_id)
                if p:
                    user = await session.get(User, p.user_id) if p.user_id else None
                    item = await classify_candidate_db_only({"type": "payment", "obj": p, "user": user}, now=now)
                    if item:
                        items.append(item)
            else:
                t = await session.get(BalanceTopUp, it_id)
                if t:
                    user = await session.get(User, t.user_id) if getattr(t, "user_id", None) else None
                    if not user and t.telegram_id:
                        user = await session.scalar(select(User).where(User.telegram_id == t.telegram_id))
                    item = await classify_candidate_db_only({"type": "topup", "obj": t, "user": user}, now=now)
                    if item:
                        items.append(item)

        return items, total_count, total_pages


async def get_problem_payment_by_id(
    item_id: int,
    item_type: str = "payment",
) -> ProblemPaymentItem | None:
    """Fast DB-only lookup of any payment or balance topup by ID."""
    async with async_session() as session:
        if item_type == "payment":
            p = await session.get(Payment, item_id)
            if not p:
                return None
            u = await session.get(User, p.user_id) if p.user_id else None
            return await classify_candidate_db_only({"type": "payment", "obj": p, "user": u})
        else:
            t = await session.get(BalanceTopUp, item_id)
            if not t:
                return None
            u = await session.get(User, t.user_id) if getattr(t, "user_id", None) else None
            if not u and t.telegram_id:
                u = await session.scalar(select(User).where(User.telegram_id == t.telegram_id))
            return await classify_candidate_db_only({"type": "topup", "obj": t, "user": u})


async def find_problem_payment_by_query(query_str: str) -> ProblemPaymentItem | None:
    """Find any payment or balance topup by local ID (#306 or 306) or provider UUID."""
    clean_q = query_str.strip().lstrip("#")
    if not clean_q:
        return None

    async with async_session() as session:
        if clean_q.isdigit():
            num_id = int(clean_q)
            p = await session.get(Payment, num_id)
            if p:
                u = await session.get(User, p.user_id) if p.user_id else None
                return await classify_candidate_db_only({"type": "payment", "obj": p, "user": u})

            t = await session.get(BalanceTopUp, num_id)
            if t:
                u = await session.get(User, t.user_id) if getattr(t, "user_id", None) else None
                if not u and t.telegram_id:
                    u = await session.scalar(select(User).where(User.telegram_id == t.telegram_id))
                return await classify_candidate_db_only({"type": "topup", "obj": t, "user": u})

        # By provider_payment_id
        p = await session.scalar(select(Payment).where(Payment.provider_payment_id == clean_q).limit(1))
        if p:
            u = await session.get(User, p.user_id) if p.user_id else None
            return await classify_candidate_db_only({"type": "payment", "obj": p, "user": u})

        t = await session.scalar(select(BalanceTopUp).where(BalanceTopUp.provider_payment_id == clean_q).limit(1))
        if t:
            u = await session.get(User, t.user_id) if getattr(t, "user_id", None) else None
            if not u and t.telegram_id:
                u = await session.scalar(select(User).where(User.telegram_id == t.telegram_id))
            return await classify_candidate_db_only({"type": "topup", "obj": t, "user": u})

    return None
