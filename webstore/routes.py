"""HTTP route handlers for the web store."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import contextvars
import fcntl
import os
import html
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import secrets
import tempfile
import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import WSCloseCode, web
from sqlalchemy import delete, func, insert, literal, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

from bot.services.payment_logger import plog
from bot.services.provisioning_issues import (
    AccessProvisionError,
    build_internal_access_error,
    build_vhq_access_error,
)
from bot.services.vhq_partner_api import VHQPartnerAPI, VHQPartnerAPIError, _ALLOWED_DAYS
from bot.services.vhq_routing import get_vhq_spec_for_store_tariff
from bot.services.adapt_api import (
    ADAPT_MIN_CUSTOM_RENEW_DAYS,
    AdaptAPI,
    AdaptAPIError,
)
from bot.services.purchase_intent import (
    MIN_PURCHASE_PRICE_RUB,
    calculate_upgrade_price_rub,
    effective_expired_adapt_action,
)
from bot.utils.device_info import adapt_device_details
try:
    from bot.services.adapt_subscription_proxy import build_adapt_mirror_url
except (ImportError, Exception):
    async def build_adapt_mirror_url(*args, **kwargs):  # type: ignore[misc]
        raise RuntimeError("Adapt subscription proxy not available in this environment")
from webstore.config import (
    get_adapt_plan_metadata_by_key,
    get_adapt_plan_metadata_by_uuid,
    get_store_tariffs,
    get_store_tariffs_by_key,
    settings,
)
from webstore.database import async_session
from webstore import otp_auth
from webstore.email_auth import send_email
from webstore.marzban import MarzbanClient
from webstore.models import (
    SupportAgentSession,
    SupportMessage,
    SupportRecoveryAudit,
    SupportRecoveryRequest,
    SupportTicket,
    WebBalanceAccount,
    WebBalanceTopUp,
    WebBalanceTransaction,
    WebBalanceTransfer,
    WebAccount,
    WebSession,
    WebEmail,
    WebEmailToken,
    WebAuthLimit,
    WebOrder,
    WebProfileLink,
    WebProfileLinkCode,
    WebTelegramAuthCode,
    WebTelegramItem,
)
from webstore.vhq_proxy import build_vhq_mirror_url, build_vhq_web_order_ref_url
from webstore.telegram_notify import send_telegram_notifications, send_telegram_photo
from webstore.notification_outbox import (
    all_staff_recipient_ids,
    enqueue_notifications,
)
from webstore.yookassa_verify import (
    YooKassaPaymentSnapshot,
    YooKassaVerificationError,
    fetch_yookassa_payment,
)

logger = logging.getLogger(__name__)
_MSK = timezone(timedelta(hours=3))


def _web_plog(event: str, **fields: object) -> None:
    plog(event, _instance_hint=f"webstore_{settings.port}", **fields)


def _support_hint_text() -> str:
    if settings.support_url:
        return f" Если вопрос срочный, напишите в поддержку: {settings.support_url}"
    return ""


_held_profile_locks = contextvars.ContextVar("_held_profile_locks", default={})


def _assert_profile_lock(profile_token: str) -> None:
    held = _held_profile_locks.get().get(profile_token)
    if not held or held[0] is not asyncio.current_task():
        raise RuntimeError("Profile lock ownership lost")
    _, lock_file, lock_path = held
    if os.fstat(lock_file.fileno()).st_ino != lock_path.stat().st_ino:
        raise RuntimeError("Profile lock file replaced")


@contextlib.asynccontextmanager
async def _acquire_profile_lock(profile_token: str, timeout: float = 8.0, lease: float = 30.0):
    """Hold a kernel lock beside the shared SQLite file; never unlink lock files.

    No lease expires during HTTP or event-loop stalls. Process exit closes the
    descriptor. All workers must share the database and its local lock directory.
    """
    current_held = _held_profile_locks.get()
    held = current_held.get(profile_token)
    if held and held[0] is asyncio.current_task():
        _assert_profile_lock(profile_token)
        yield
        return

    database = async_session.kw["bind"].url.database
    lock_root = Path(tempfile.gettempdir()) if database in (None, "", ":memory:") else Path(database).resolve().parent
    lock_dir = lock_root / ".webstore-profile-locks"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    lock_path = lock_dir / hashlib.sha256(profile_token.encode()).hexdigest()
    lock_file = lock_path.open("a+b")
    deadline = asyncio.get_running_loop().time() + timeout
    try:
        while True:
            try:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if asyncio.get_running_loop().time() >= deadline:
                    raise TimeoutError("Could not acquire profile lock")
                await asyncio.sleep(0.05)
        token = _held_profile_locks.set({
            **current_held, profile_token: (asyncio.current_task(), lock_file, lock_path),
        })
        try:
            _assert_profile_lock(profile_token)
            yield
        finally:
            try:
                _held_profile_locks.reset(token)
            except ValueError:
                pass
    finally:
        lock_file.close()



_FULFILLMENT_RETRY_DELAYS_MINUTES = (1, 5, 20, 60)
_FULFILLMENT_MAX_ATTEMPTS = 5
_ADAPT_MUTATION_INITIAL_INFLIGHT = "initial_inflight"
_ADAPT_MUTATION_RECOVERY_INFLIGHT = "recovery_inflight"
_ADAPT_MUTATION_CONFIRMED = "confirmed"
_ADAPT_MUTATION_FAILED_CLOSED = "failed_closed"
_ADAPT_MUTATION_STATES = {
    _ADAPT_MUTATION_INITIAL_INFLIGHT,
    _ADAPT_MUTATION_RECOVERY_INFLIGHT,
    _ADAPT_MUTATION_CONFIRMED,
    _ADAPT_MUTATION_FAILED_CLOSED,
}
_ADAPT_MUTATION_POST_STATES = {
    _ADAPT_MUTATION_INITIAL_INFLIGHT,
    _ADAPT_MUTATION_RECOVERY_INFLIGHT,
    _ADAPT_MUTATION_CONFIRMED,
}


def _is_retryable_provision_issue(issue: AccessProvisionError) -> bool:
    permanent_codes = {
        "adapt_target_missing",
        "adapt_target_not_found",
        "adapt_renew_plan_mismatch",
        "adapt_result_mismatch",
        "adapt_baseline_missing",
        "adapt_upgrade_same_plan",
        "adapt_upgrade_same_plan_unextended",
        "adapt_upgrade_plan_contradictory",
        "adapt_upgrade_reactivation_required",
        "adapt_renew_recovery_exhausted",
        "adapt_upgrade_recovery_exhausted",
        "device_requires_plan_upgrade",
        "vhq_request_invalid",
        "ambiguous_buy_network_error",
        "renew_409_wrong_product",
        "buy_fallback_failed",
        "fallback_buy_missing_url",
    }
    if issue.code in permanent_codes:
        return False
    if issue.provider == "vhq":
        # VHQ buy has no lookup/idempotency key: an automatic repeat after a
        # lost response could charge twice and create a second subscription.
        return False
    if issue.status in {400, 401, 403, 404}:
        return False
    return True


def _legacy_adapt_mutation_state(order: WebOrder, action: str) -> str | None:
    raw_code = getattr(order, "failure_code", None)
    code = raw_code.strip() if isinstance(raw_code, str) else ""
    if code and ("preflight" in code or "get_status" in code):
        # Before adapt_mutation_state existed, a later preflight outage could
        # overwrite the failure code from an earlier ambiguous POST. Treat a
        # multi-attempt legacy row conservatively so it can only take the one
        # safe reconciliation path after a fresh provider GET.
        return (
            _ADAPT_MUTATION_INITIAL_INFLIGHT
            if int(order.fulfillment_attempts or 0) > 1
            else None
        )
    if code in {
        "adapt_target_missing",
        "adapt_target_not_found",
        "adapt_renew_plan_mismatch",
        "adapt_upgrade_same_plan",
        "adapt_upgrade_same_plan_unextended",
        "adapt_upgrade_plan_contradictory",
        "adapt_baseline_missing",
        "adapt_upgrade_reactivation_required",
    }:
        return None
    if code.startswith((f"adapt_{action}_", "adapt_renew_", "adapt_upgrade_")):
        return _ADAPT_MUTATION_INITIAL_INFLIGHT
    if code in {"adapt_result_mismatch", "adapt_reactivation_mismatch", "adapt_refreeze_failed"}:
        return _ADAPT_MUTATION_INITIAL_INFLIGHT
    if not code and int(order.fulfillment_attempts or 0) > 1:
        return _ADAPT_MUTATION_INITIAL_INFLIGHT
    return None


def _adapt_mutation_state(order: WebOrder, action: str) -> str | None:
    if getattr(order, "fulfillment_freeze_state", None) in {"mutation_applied", "freeze_restored"}:
        return _ADAPT_MUTATION_CONFIRMED
    raw_stored = getattr(order, "adapt_mutation_state", None)
    stored = raw_stored.strip() if isinstance(raw_stored, str) else ""
    if stored:
        return stored if stored in _ADAPT_MUTATION_STATES else _ADAPT_MUTATION_FAILED_CLOSED
    return _legacy_adapt_mutation_state(order, action)


def _is_adapt_mutation_attempted(order: WebOrder, action: str) -> bool:
    if getattr(order, "fulfillment_freeze_state", None) in {"mutation_applied", "freeze_restored"}:
        return True
    return _adapt_mutation_state(order, action) in _ADAPT_MUTATION_POST_STATES


_VHQ_NON_RETRYABLE_BUY_FAILURE_CODES = {
    "buy_inflight",
    "buy_fallback_inflight",
    "stale_buy_inflight",
    "vhq_buy_outcome_unknown",
    "ambiguous_buy_network_error",
    "buy_missing_data",
    "fallback_buy_missing_data",
    "fallback_buy_missing_url",
    "buy_fallback_failed",
    "vhq_response_runtime",
    "missing_baseline_already_requested",
}


_INTERNAL_SECRET_HEADER = "X-Internal-Secret"


def _verify_internal_secret(request: web.Request) -> bool:
    expected = settings.bridge_shared_secret.strip()
    actual = request.headers.get(_INTERNAL_SECRET_HEADER, "").strip()
    return bool(expected and actual and hmac.compare_digest(expected, actual))


def _bridge_headers() -> dict[str, str]:
    return {_INTERNAL_SECRET_HEADER: settings.bridge_shared_secret}


def _extract_adapt_uuid(subscription_url: str | None, external_id: str | None = None) -> str | None:
    for candidate in (external_id, subscription_url):
        raw = str(candidate or "").strip()
        if not raw:
            continue
        for prefix in ("uuid:", "adapt:", "tg:", "sub:", "adapt_"):
            if raw.lower().startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        for marker in ("/adapt-sub/", "/sub/"):
            if marker in raw:
                raw = raw.rsplit(marker, 1)[-1].split("?", 1)[0].split("#", 1)[0].strip()
                break
        raw = raw.split("?", 1)[0].split("#", 1)[0].strip("/ ")
        try:
            return str(uuid.UUID(raw))
        except (ValueError, AttributeError):
            continue
    return None


def _get_order_adapt_uuid(order: WebOrder) -> str | None:
    """Extract Adapt UUID from an order if present."""
    if order.marzban_username and order.marzban_username.startswith("adapt_"):
        candidate = order.marzban_username.removeprefix("adapt_").strip()
        extracted = _extract_adapt_uuid(None, candidate)
        if extracted:
            return extracted
    if order.target_order_id:
        extracted = _extract_adapt_uuid(None, order.target_order_id)
        if extracted:
            return extracted
    if order.subscription_url:
        extracted = _extract_adapt_uuid(order.subscription_url)
        if extracted:
            return extracted
    return None


def _resolve_telegram_user_locally(
    adapt_uuid: str | None = None,
    marzban_username: str | None = None,
    target_order_id: str | None = None,
    telegram_id: int | None = None,
) -> dict | None:
    """Fast local lookup in bot DB if file is accessible."""
    from webstore.config import _bot_db_path
    import sqlite3
    path = _bot_db_path()
    if not path or not path.exists():
        return None

    try:
        with sqlite3.connect(str(path), timeout=5.0) as con:
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA busy_timeout=3000")

            if adapt_uuid:
                try:
                    row = con.execute(
                        """
                        SELECT u.telegram_id, u.username, u.full_name, s.id AS sub_id
                        FROM adapt_subscriptions a
                        JOIN subscriptions s ON s.id = a.subscription_id
                        JOIN users u ON u.id = s.user_id
                        WHERE LOWER(a.adapt_uuid) = LOWER(?)
                        LIMIT 1
                        """,
                        (adapt_uuid,),
                    ).fetchone()
                    if row:
                        return {
                            "telegram_id": row["telegram_id"],
                            "username": row["username"],
                            "full_name": row["full_name"],
                            "subscription_id": row["sub_id"],
                        }
                except sqlite3.Error:
                    pass

                try:
                    row = con.execute(
                        """
                        SELECT u.telegram_id, u.username, u.full_name, s.id AS sub_id
                        FROM subscriptions s
                        JOIN users u ON u.id = s.user_id
                        WHERE LOWER(s.client_name) = LOWER(?) OR s.vpn_key LIKE ?
                        ORDER BY s.id DESC LIMIT 1
                        """,
                        (f"adapt_{adapt_uuid}", f"%{adapt_uuid}%"),
                    ).fetchone()
                    if row:
                        return {
                            "telegram_id": row["telegram_id"],
                            "username": row["username"],
                            "full_name": row["full_name"],
                            "subscription_id": row["sub_id"],
                        }
                except sqlite3.Error:
                    pass

            if target_order_id and target_order_id.startswith("sub_"):
                try:
                    sub_id = int(target_order_id.removeprefix("sub_"))
                    row = con.execute(
                        """
                        SELECT u.telegram_id, u.username, u.full_name, s.id AS sub_id
                        FROM subscriptions s
                        JOIN users u ON u.id = s.user_id
                        WHERE s.id = ?
                        LIMIT 1
                        """,
                        (sub_id,),
                    ).fetchone()
                    if row:
                        return {
                            "telegram_id": row["telegram_id"],
                            "username": row["username"],
                            "full_name": row["full_name"],
                            "subscription_id": row["sub_id"],
                        }
                except (ValueError, sqlite3.Error):
                    pass

            if marzban_username:
                try:
                    row = con.execute(
                        """
                        SELECT u.telegram_id, u.username, u.full_name, s.id AS sub_id
                        FROM subscriptions s
                        JOIN users u ON u.id = s.user_id
                        WHERE s.client_name = ?
                        ORDER BY s.id DESC LIMIT 1
                        """,
                        (marzban_username,),
                    ).fetchone()
                    if row:
                        return {
                            "telegram_id": row["telegram_id"],
                            "username": row["username"],
                            "full_name": row["full_name"],
                            "subscription_id": row["sub_id"],
                        }
                except sqlite3.Error:
                    pass

            if telegram_id:
                row = con.execute(
                    "SELECT telegram_id, username, full_name FROM users WHERE telegram_id = ? LIMIT 1",
                    (telegram_id,),
                ).fetchone()
                if row:
                    return {
                        "telegram_id": row["telegram_id"],
                        "username": row["username"],
                        "full_name": row["full_name"],
                    }
    except Exception as exc:
        logger.debug("Failed local bot DB user resolution: %s", exc)

    return None


async def _resolve_telegram_user_for_order(order: WebOrder) -> dict | None:
    """Resolve Telegram user info for an order (id, username, full_name)."""
    adapt_uuid = _get_order_adapt_uuid(order)
    link_tg_id = None
    if order.profile_token:
        async with async_session() as session:
            link = await session.get(WebProfileLink, order.profile_token)
            if link and link.telegram_id:
                link_tg_id = link.telegram_id

    # 1. Try local bot DB lookup
    local_info = _resolve_telegram_user_locally(
        adapt_uuid=adapt_uuid,
        marzban_username=order.marzban_username,
        target_order_id=order.target_order_id,
        telegram_id=link_tg_id,
    )
    if local_info:
        return local_info

    if link_tg_id:
        return {"telegram_id": link_tg_id, "username": None, "full_name": None}

    # 2. Try HTTP bridge lookup if remote
    if settings.referral_bridge_url and settings.bridge_shared_secret:
        url = f"{settings.referral_bridge_url.rstrip('/')}/internal/web-order/resolve-user"
        params = {}
        if adapt_uuid:
            params["adapt_uuid"] = adapt_uuid
        if order.marzban_username:
            params["marzban_username"] = order.marzban_username
        if order.target_order_id:
            params["target_order_id"] = order.target_order_id
        if order.subscription_url:
            params["subscription_url"] = order.subscription_url
        if params:
            try:
                timeout = aiohttp.ClientTimeout(total=4)
                async with aiohttp.ClientSession(timeout=timeout) as http:
                    async with http.get(url, params=params, headers=_bridge_headers()) as resp:
                        if resp.status == 200 and resp.content_type == "application/json":
                            data = await resp.json()
                            if data.get("ok") and data.get("user"):
                                return data["user"]
            except Exception as exc:
                logger.debug("Bridge user resolution failed for %s: %s", order.order_id, exc)

    return None


def _format_telegram_user_line(user_info: dict | None) -> str:
    if not user_info or not user_info.get("telegram_id"):
        return ""
    tg_id = user_info["telegram_id"]
    username = user_info.get("username")
    full_name = user_info.get("full_name")

    if username and full_name:
        return f"Клиент в боте: {html.escape(full_name)} (@{username}, ID: {tg_id})\n"
    elif username:
        return f"Клиент в боте: @{username} (ID: {tg_id})\n"
    elif full_name:
        return f"Клиент в боте: {html.escape(full_name)} (ID: {tg_id})\n"
    else:
        return f"Клиент в боте: ID: {tg_id}\n"


async def _notify_admins_webstore(
    order: WebOrder,
    event: str = "paid",
    *,
    issue: AccessProvisionError | None = None,
    user_info: dict | None = None,
) -> None:
    """Send Telegram notification to admins about webstore payment."""
    admin_ids = await _get_all_webstore_admin_ids()
    if not admin_ids:
        logger.warning("Webstore payment notification skipped: no administrators configured")
        return

    if user_info is None:
        try:
            user_info = await _resolve_telegram_user_for_order(order)
        except Exception as exc:
            logger.debug("Failed to resolve telegram user for admin notify: %s", exc)
            user_info = None

    client_line = _format_telegram_user_line(user_info)

    if event == "paid":
        # Build entry source line
        entry_parts = []
        if order.entry_referrer:
            entry_parts.append(f"реф: {html.escape(order.entry_referrer)}")
        if order.entry_url and "utm_" in order.entry_url:
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(order.entry_url).query)
            utm_bits = " / ".join(
                qs.get(k, [""])[0]
                for k in ("utm_source", "utm_medium", "utm_campaign")
                if qs.get(k, [""])[0]
            )
            if utm_bits:
                entry_parts.append(f"utm: {html.escape(utm_bits)}")
        elif order.entry_url:
            entry_parts.append(f"страница: {html.escape(order.entry_url)}")
        source_line = "; ".join(entry_parts) if entry_parts else (order.ref_source or "прямой заход")
        text = (
            f"🛒 <b>Оплата через веб-магазин</b>\n\n"
            f"Тариф: {order.tariff_label}\n"
            f"Стоимость: {order.original_amount_rub or order.amount_rub}₽\n"
            f"Оплачено: {order.amount_rub}₽\n"
            f"Контакт: {html.escape(order.contact or order.email or '—')}\n"
            f"{client_line}"
            f"Заказ: <code>{order.order_id}</code>\n"
            f"Источник: {source_line}"
        )
    elif event == "delivered":
        target_info = []
        adapt_uuid = _get_order_adapt_uuid(order)
        if adapt_uuid:
            target_info.append(f"Adapt: <code>{html.escape(adapt_uuid)}</code>")
        elif order.marzban_username:
            target_info.append(f"Marzban: <code>{html.escape(order.marzban_username)}</code>")
        elif order.subscription_url:
            target_info.append(f"Ссылка: <code>{html.escape(order.subscription_url)}</code>")
        target_str = ("\n" + "\n".join(target_info)) if target_info else ""
        text = (
            f"✅ <b>Ключ выдан (веб-магазин)</b>\n\n"
            f"Тариф: {order.tariff_label}\n"
            f"Заказ: <code>{order.order_id}</code>\n"
            f"{client_line}"
            f"{target_str}".strip()
        )
    elif event in {"failed", "retrying"} and issue is not None:
        heading = "Ошибка выдачи" if event == "failed" else "Выдача задержана — будет повтор"
        text = (
            f"🚨 <b>{heading} (веб-магазин)</b>\n\n"
            f"Тариф: {html.escape(order.tariff_label)}\n"
            f"Стоимость: {order.original_amount_rub or order.amount_rub}₽\n"
            f"Оплачено: {order.amount_rub}₽\n"
            f"Контакт: {html.escape(order.contact or order.email or '—')}\n"
            f"{client_line}"
            f"Заказ: <code>{order.order_id}</code>\n"
            f"Провайдер: {html.escape(issue.provider)}\n"
            f"Код: {html.escape(issue.code)}\n"
            f"HTTP статус: {html.escape(str(issue.status or '—'))}\n"
            f"Причина: {html.escape(issue.admin_message)}"
        )
    else:
        return

    await enqueue_notifications(
        event=f"order_{event}",
        dedupe_prefix=f"web-order:{order.order_id}:{event}",
        recipient_ids=admin_ids,
        text=text,
    )


async def _notify_bot_order_delivered(
    order: WebOrder,
    *,
    force: bool = False,
    cashback_only: bool = False,
    session=None,
) -> dict:
    """Notify the Telegram bot that a web order was delivered.

    If the order corresponds to a bot user subscription, the bot will sync
    the subscription status and deliver the native renewal messages and platform guide
    to the user in Telegram.
    """
    adapt_uuid = _get_order_adapt_uuid(order)

    try:
        raw_amt = getattr(order, "amount_rub", None)
        if isinstance(raw_amt, (int, float)):
            amount_val = float(raw_amt)
        elif isinstance(raw_amt, str) and raw_amt.strip():
            amount_val = float(raw_amt)
        else:
            amount_val = 0.0
    except (TypeError, ValueError):
        amount_val = 0.0

    cashback_eligible = True
    if amount_val <= 0:
        cashback_eligible = False
    elif getattr(order, "cashback_status", None) == "web_credited":
        cashback_eligible = False
    else:
        async def _check_existing_web_cashback(s):
            return await s.scalar(
                select(WebBalanceTransaction).where(
                    WebBalanceTransaction.kind == "cashback",
                    WebBalanceTransaction.source_id == order.order_id,
                ).limit(1)
            )

        try:
            if session is not None:
                existing_web_cb = await _check_existing_web_cashback(session)
            else:
                async with async_session() as lookup_session:
                    existing_web_cb = await _check_existing_web_cashback(lookup_session)

            if existing_web_cb:
                cashback_eligible = False
        except Exception as exc:
            logger.warning("Failed checking existing web cashback for %s: %s; failing closed", getattr(order, "order_id", None), exc)
            return {"ok": False, "delivered": False, "cashback_status": "pending", "error": "web_cashback_check_failed"}

    payload = {
        "order_id": order.order_id,
        "adapt_uuid": adapt_uuid or "",
        "marzban_username": order.marzban_username or "",
        "target_order_id": order.target_order_id or "",
        "subscription_url": order.subscription_url or "",
        "access_expires_at": order.access_expires_at.isoformat() if order.access_expires_at else None,
        "tariff_label": order.tariff_label or "",
        "days": order.days,
        "force": force,
        "cashback_only": cashback_only,
        "amount_rub": amount_val,
        "cashback_eligible": bool(cashback_eligible),
    }

    if not settings.referral_bridge_url or not settings.bridge_shared_secret:
        return {"ok": False, "delivered": False, "reason": "bridge_not_configured"}

    url = f"{settings.referral_bridge_url.rstrip('/')}/internal/web-order/delivered"
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(url, json=payload, headers=_bridge_headers()) as resp:
                if resp.status == 200 and resp.content_type == "application/json":
                    data = await resp.json()
                    logger.info(
                        "Bot web-order delivered response for %s: matched=%s delivered=%s tg_id=%s",
                        order.order_id,
                        data.get("matched"),
                        data.get("delivered"),
                        data.get("telegram_id"),
                    )
                    return data
                else:
                    text = await resp.text()
                    logger.warning("Bot web-order delivered returned status %s: %s", resp.status, text[:200])
                    return {"ok": False, "delivered": False, "status": resp.status}
    except Exception as exc:
        logger.warning("Failed to notify bot of delivered web order %s: %s", order.order_id, exc)
        return {"ok": False, "delivered": False, "error": str(exc)}


async def _notify_linked_webstore_user(
    order: WebOrder,
    event: str,
    *,
    dedupe_suffix: str = "",
) -> bool:
    """Keep a linked Telegram customer informed even when provisioning fails."""
    if not order.profile_token:
        return False
    async with async_session() as session:
        link = await session.get(WebProfileLink, order.profile_token)
    if not link:
        return False

    support = settings.support_url or f"{settings.subscription_base_url.rstrip('/')}/support"
    if event == "delayed":
        text = (
            "⏳ <b>Оплата получена</b>\n\n"
            f"Заказ <code>{order.order_id}</code> оплачен. Доступ пока не выдан, "
            "система уже повторяет попытку автоматически.\n"
            f"Если доступ нужен срочно, напишите в поддержку: {html.escape(support)}"
        )
    elif event == "delivered":
        text = (
            "✅ <b>Доступ готов</b>\n\n"
            f"Заказ <code>{order.order_id}</code> успешно обработан. "
            f"Ключ доступен в профиле: {html.escape(settings.subscription_base_url.rstrip('/') + '/profile')}"
        )
    elif event == "failed":
        text = (
            "🚨 <b>Оплата получена, нужна помощь с выдачей</b>\n\n"
            f"По заказу <code>{order.order_id}</code> автоматические попытки не завершились. "
            "Оплата не потеряна.\n"
            f"Напишите в поддержку: {html.escape(support)}"
        )
    else:
        return False
    await enqueue_notifications(
        event=f"customer_{event}",
        dedupe_prefix=f"web-order:{order.order_id}:customer:{event}{dedupe_suffix}",
        recipient_ids=[link.telegram_id],
        text=text,
    )
    return True


async def _mark_order_failed(order: WebOrder, issue: AccessProvisionError) -> None:
    attempts = int(order.fulfillment_attempts or 0)
    retryable = _is_retryable_provision_issue(issue) and attempts < _FULFILLMENT_MAX_ATTEMPTS
    if retryable:
        delay_index = min(max(attempts - 1, 0), len(_FULFILLMENT_RETRY_DELAYS_MINUTES) - 1)
        retry_minutes = _FULFILLMENT_RETRY_DELAYS_MINUTES[delay_index]
        order.status = "paid"
        order.next_fulfillment_retry_at = datetime.utcnow() + timedelta(minutes=retry_minutes)
        order.failure_message = (
            "Оплата получена. Автоматическая выдача задержалась, мы уже повторяем попытку."
            f"{_support_hint_text()}"
        ).strip()
    else:
        order.status = "failed"
        order.next_fulfillment_retry_at = None
        order.failure_message = f"{issue.client_message}{_support_hint_text()}".strip()
    order.failure_reason = issue.admin_message
    order.failure_code = issue.code
    _web_plog(
        "WEB_ОШИБКА_ВЫДАЧИ",
        order_id=order.order_id,
        tariff=order.tariff_label,
        provider=issue.provider,
        code=issue.code,
        status=issue.status or "",
    )
    suppress_adapt_retry_alert = (
        retryable
        and issue.provider == "adapt"
        and order.status == "paid"
        and order.next_fulfillment_retry_at is not None
    )
    if not suppress_adapt_retry_alert:
        await _notify_admins_webstore(order, "retrying" if retryable else "failed", issue=issue)
    await _notify_linked_webstore_user(order, "delayed" if retryable else "failed")

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# YooKassa trusted IP ranges
_YOOKASSA_NETWORKS = [
    ipaddress.ip_network("185.71.76.0/27"),
    ipaddress.ip_network("185.71.77.0/27"),
    ipaddress.ip_network("77.75.153.0/25"),
    ipaddress.ip_network("77.75.154.128/25"),
    ipaddress.ip_network("77.75.156.11/32"),
    ipaddress.ip_network("77.75.156.35/32"),
]


def _is_yookassa_ip(request: web.Request) -> bool:
    """Check if the webhook comes from YooKassa trusted IPs.

    Behind nginx, request.remote is 127.0.0.1. The real sender IP
    is in X-Real-IP / X-Forwarded-For set by nginx.
    """
    candidates = set()
    candidates.add(request.remote or "")
    candidates.add(request.headers.get("X-Real-IP", ""))
    for part in request.headers.get("X-Forwarded-For", "").split(","):
        candidates.add(part.strip())
    candidates.discard("")

    for ip_str in candidates:
        try:
            addr = ipaddress.ip_address(ip_str)
            if any(addr in net for net in _YOOKASSA_NETWORKS):
                return True
        except ValueError:
            continue
    return False


def _get_client_ip(request: web.Request) -> str:
    headers = getattr(request, "headers", {})
    real_ip = headers.get("X-Real-IP", "") if hasattr(headers, "get") else ""
    if real_ip:
        return real_ip.strip()
    forwarded = headers.get("X-Forwarded-For", "") if hasattr(headers, "get") else ""
    if forwarded:
        return forwarded.split(",")[0].strip()
    return getattr(request, "remote", "unknown") or "unknown"


_PROFILE_SECRET = settings.yookassa_secret_key or "webstore-profile-secret"
_INTERNAL_SECRET_HEADER = "X-Internal-Secret"
_AUTH_COOKIE = "webstore_session"
_AUTH_COOKIE_MAX_AGE = 60 * 60 * 24 * 30


def _normalize_contact(contact: str) -> str:
    return contact.strip().lower()


def _hash_password(password: str, *, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 180_000)
    return "pbkdf2_sha256$180000$" + base64.urlsafe_b64encode(salt).decode().rstrip("=") + "$" + base64.urlsafe_b64encode(digest).decode().rstrip("=")


def _verify_password(password: str, stored: str) -> bool:
    try:
        scheme, rounds_raw, salt_raw, digest_raw = stored.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        rounds = int(rounds_raw)
        salt = base64.urlsafe_b64decode(salt_raw + "=" * (-len(salt_raw) % 4))
        expected = base64.urlsafe_b64decode(digest_raw + "=" * (-len(digest_raw) % 4))
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


_DUMMY_PASSWORD_HASH = _hash_password(secrets.token_urlsafe(32))


def _set_profile_cookie(response: web.StreamResponse, token: str) -> None:
    response.set_cookie(
        _AUTH_COOKIE,
        token,
        max_age=_AUTH_COOKIE_MAX_AGE,
        path="/",
        secure=True,
        httponly=True,
        samesite="Lax",
    )


def _clear_profile_cookie(response: web.StreamResponse) -> None:
    response.del_cookie(_AUTH_COOKIE, path="/")
    response.del_cookie("webstore_profile_token", path="/")


def _has_raw_auth_cookie(request: web.Request) -> bool:
    cookies = getattr(request, "cookies", None)
    if not hasattr(cookies, "get"):
        return False
    raw = cookies.get(_AUTH_COOKIE, "")
    return isinstance(raw, str) and bool(raw.strip())


async def _authenticated_profile(request: web.Request) -> str:
    cookies = getattr(request, "cookies", None)
    if not hasattr(cookies, "get"):
        return ""
    raw_token = cookies.get(_AUTH_COOKIE, "")
    token = raw_token.strip() if isinstance(raw_token, str) else ""
    if not token:
        return ""
    async with async_session() as session:
        record = await session.get(WebSession, hashlib.sha256(token.encode()).hexdigest())
        if record and record.expires_at > datetime.utcnow():
            return record.profile_token
    return ""


async def _auth_response(payload: dict, profile_token: str, method: str = "password", expected_password_hash: str | None = None) -> web.Response:
    token = secrets.token_urlsafe(32)
    async with async_session() as session:
        digest = hashlib.sha256(token.encode()).hexdigest()
        expires = datetime.utcnow() + timedelta(seconds=_AUTH_COOKIE_MAX_AGE)
        if expected_password_hash is not None:
            result = await session.execute(insert(WebSession).from_select(
                ["token_hash", "profile_token", "expires_at", "method", "created_at"],
                select(literal(digest), WebAccount.profile_token, literal(expires), literal(method), literal(datetime.utcnow()))
                .where(WebAccount.profile_token == profile_token, WebAccount.password_hash == expected_password_hash)))
            if result.rowcount != 1:
                await session.rollback()
                return web.json_response({"error": "Неверный контакт или пароль"}, status=401)
        else:
            session.add(WebSession(token_hash=digest, profile_token=profile_token, method=method, expires_at=expires))
        await session.commit()
    response = web.json_response(payload)
    _set_profile_cookie(response, token)
    return response


def _extract_receipt_email(contact: str) -> str | None:
    normalized = _normalize_contact(contact)
    if "@" in normalized and "." in normalized.split("@")[-1]:
        return normalized
    return None


def _make_profile_token(contact: str) -> str:
    """Deterministic token for a contact value."""
    return hmac.new(
        _PROFILE_SECRET.encode(), _normalize_contact(contact).encode(), hashlib.sha256
    ).hexdigest()[:32]


def _make_telegram_profile_token(telegram_id: int) -> str:
    return hmac.new(
        _PROFILE_SECRET.encode(),
        f"tg:{telegram_id}".encode(),
        hashlib.sha256,
    ).hexdigest()[:32]


def _make_web_ref_code(profile_token: str) -> str:
    return hmac.new(
        _PROFILE_SECRET.encode(),
        f"ref:{profile_token}".encode(),
        hashlib.sha256,
    ).hexdigest()[:16]


def _display_order_subscription_url(order: WebOrder) -> str | None:
    if not order or order.status not in ("delivered", "demo"):
        return None
    is_vhq = bool(
        getattr(order, "vhq_order_id", None)
        or str(order.marzban_username or "").startswith("vhq_")
        or "/proxy-subscription/" in str(order.subscription_url or "")
        or "vhq-connect" in str(order.subscription_url or "")
        or "/vhq-sub/" in str(order.subscription_url or "")
    )
    if not is_vhq:
        adapt_uuid = _get_order_adapt_uuid(order)
        if adapt_uuid:
            return build_adapt_mirror_url(adapt_uuid)
    raw_url = str(order.subscription_url or "").strip()
    if not raw_url:
        return None
    if "/proxy-subscription/" not in raw_url and "vhq-connect.xyz" not in raw_url:
        return raw_url
    return build_vhq_mirror_url(
        raw_url,
        public_base_url=settings.vhq_subscription_proxy_base_url,
        path_prefix=settings.bot_webhook_path_prefix,
        secret=settings.vhq_subscription_proxy_secret,
        order_id=order.order_id,
    )


def _is_intro_basic_store_tariff(tariff_key: str) -> bool:
    return tariff_key == "basic_1"


def _is_adapt_trial_store_tariff(tariff: dict | None) -> bool:
    return bool(
        tariff
        and str(tariff.get("adapt_plan_uuid") or "").strip() == settings.adapt_demo_plan_uuid
    )


def _has_adapt_upgrade_option(tariff: dict | None, *, expires_at: datetime | None = None) -> bool:
    if not tariff or not str(tariff.get("adapt_plan_uuid") or "").strip():
        return False
    from bot.services.subscription_semantics import is_adapt_provider_upgrade_eligible
    return any(
        is_adapt_provider_upgrade_eligible(tariff, candidate, expires_at=expires_at)
        for candidate in get_store_tariffs()
    )


def _web_order_adapt_metadata(order: WebOrder, tariffs_by_key: dict[str, dict]) -> dict | None:
    if order.status == "demo" or order.tariff_key == "demo":
        snapshot_uuid = str(getattr(order, "target_snapshot_plan_uuid", None) or "").strip()
        if snapshot_uuid:
            meta = get_adapt_plan_metadata_by_uuid(snapshot_uuid)
            if meta:
                return meta
        return get_adapt_plan_metadata_by_uuid(settings.adapt_demo_plan_uuid) if settings.adapt_demo_plan_uuid else None
    metadata = get_adapt_plan_metadata_by_key(order.tariff_key)
    if metadata or tariffs_by_key.get(order.tariff_key):
        return metadata or tariffs_by_key.get(order.tariff_key)
    snapshot_uuid = str(getattr(order, "target_snapshot_plan_uuid", None) or "").strip()
    if snapshot_uuid:
        return get_adapt_plan_metadata_by_uuid(snapshot_uuid)
    return None


def _web_order_current_adapt_plan_uuid(order: WebOrder, tariffs_by_key: dict[str, dict] | None = None) -> str:
    if order.status == "demo" or order.tariff_key == "demo":
        snapshot_uuid = str(getattr(order, "target_snapshot_plan_uuid", None) or "").strip()
        if snapshot_uuid:
            return snapshot_uuid
        return str(settings.adapt_demo_plan_uuid or "").strip()
    tariffs_by_key = tariffs_by_key or get_store_tariffs_by_key()
    metadata = get_adapt_plan_metadata_by_key(order.tariff_key)
    plan_from_tariff = str((metadata or tariffs_by_key.get(order.tariff_key) or {}).get("adapt_plan_uuid") or "").strip()
    if plan_from_tariff:
        return plan_from_tariff
    snapshot_uuid = str(getattr(order, "target_snapshot_plan_uuid", None) or "").strip()
    if snapshot_uuid:
        return snapshot_uuid
    return ""


def _effective_adapt_purchase_action(
    action: str,
    old_tariff: dict | None,
    new_plan_uuid: str,
    old_plan_uuid: str = "",
) -> str:
    """A trial is shown as renewal in UI, but Adapt must execute it via upgrade."""
    actual_old_plan = old_plan_uuid or str((old_tariff or {}).get("adapt_plan_uuid") or "").strip()
    is_trial = bool(actual_old_plan and actual_old_plan == settings.adapt_demo_plan_uuid)
    if not is_trial:
        return action
    if not actual_old_plan or actual_old_plan == str(new_plan_uuid or "").strip():
        raise ValueError("Для продления тестовой подписки выберите платный тариф")
    return "upgrade"


def _next_charge_datetime(now: datetime | None = None) -> datetime:
    current = now or datetime.utcnow().replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(_MSK)
    candidate = local.replace(hour=5, minute=0, second=0, microsecond=0)
    if local >= candidate:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)


def _serialize_orders(
    orders: list[WebOrder],
    telegram_items: list[WebTelegramItem] | None = None,
) -> list[dict[str, str | int | None]]:
    tariffs_by_key = get_store_tariffs_by_key()
    owned_adapt_uuids: set[str] = set()

    for item in (telegram_items or []):
        if item.provider == "adapt" or _get_access_provider(item.key_value, item.external_id) == "adapt":
            u = _extract_adapt_uuid(item.key_value, item.external_id)
            if u:
                owned_adapt_uuids.add(u)

    for o in orders:
        if _get_order_provider(o) == "adapt" and o.status in ("delivered", "demo"):
            is_root = (
                o.status == "demo"
                or o.tariff_key == "demo"
                or o.purchase_action == "new"
                or not o.target_order_id
            )
            if is_root:
                u = _extract_adapt_uuid_from_web_order(o)
                if u:
                    owned_adapt_uuids.add(u)

    serialized = []
    for o in orders:
        current_meta = _web_order_adapt_metadata(o, tariffs_by_key)
        current_uuid = _web_order_current_adapt_plan_uuid(o, tariffs_by_key)
        is_demo = bool(
            (o.status == "demo" or o.tariff_key == "demo")
            or (current_uuid and current_uuid == settings.adapt_demo_plan_uuid)
        )
        provider = _get_order_provider(o)
        serialized.append(
            {
                "order_id": o.order_id,
                "tariff_label": o.tariff_label,
                "tariff_key": o.tariff_key,
                "days": o.days,
                "amount_rub": o.amount_rub,
                "original_amount_rub": o.original_amount_rub or o.amount_rub,
                "bonus_applied_rub": o.bonus_applied_rub or 0,
                "purchase_action": o.purchase_action or "new",
                "target_order_id": o.target_order_id,
                "status": o.status,
                "provider": provider,
                "device_target": (
                    f"order:{o.id}"
                    if provider == "adapt" and _extract_adapt_uuid_from_web_order(o) in owned_adapt_uuids
                    else None
                ),
                "is_trial": is_demo,
                "can_upgrade": _has_adapt_upgrade_option(
                    current_meta,
                    expires_at=o.access_expires_at,
                ) if provider == "adapt" else False,
                "adapt_plan_uuid": (
                    settings.adapt_demo_plan_uuid
                    if is_demo
                    else current_uuid
                ),
                "adapt_uuid": _extract_adapt_uuid_from_web_order(o),
                "subscription_url": _display_order_subscription_url(o),
                "raw_subscription_url": o.subscription_url or "",
                "failure_message": o.failure_message if o.status in {"failed", "paid"} else None,
                "entry_referrer": o.entry_referrer or "",
                "entry_url": o.entry_url or "",
                "ref_source": o.ref_source or "",
                "referrer_telegram_id": o.referrer_telegram_id,
                "created_at": (o.created_at.isoformat() + "Z") if o.created_at else None,
                "paid_at": (o.paid_at.isoformat() + "Z") if o.paid_at else None,
                "delivered_at": (o.delivered_at.isoformat() + "Z") if o.delivered_at else None,
                "access_expires_at": (o.access_expires_at.isoformat() + "Z") if o.access_expires_at else None,
            }
        )
    return serialized


def _serialize_telegram_items(items: list[WebTelegramItem]) -> list[dict[str, str | int | bool | None]]:
    tariffs_by_plan = {
        str(tariff.get("adapt_plan_uuid") or "").strip(): tariff
        for tariff in get_store_tariffs()
        if str(tariff.get("adapt_plan_uuid") or "").strip()
    }
    serialized = []
    for item in items:
        current_uuid = str(item.adapt_plan_uuid or "").strip()
        current_meta = get_adapt_plan_metadata_by_uuid(current_uuid) if current_uuid else None
        if not current_meta and not getattr(item, "target_snapshot_plan_uuid", None):
            current_meta = tariffs_by_plan.get(current_uuid)
        provider = item.provider or _get_access_provider(item.key_value, item.external_id)
        display_key = item.key_value
        if provider == "adapt":
            adapt_uuid = _extract_adapt_uuid(item.key_value, item.external_id)
            if adapt_uuid:
                display_key = build_adapt_mirror_url(adapt_uuid)
        serialized.append(
            {
                "item_type": item.item_type,
                "title": item.title,
                "subtitle": item.subtitle,
                "key_value": display_key,
                "provider": provider,
                "device_target": (
                    f"tgitem:{item.id}"
                    if (item.provider == "adapt" or _get_access_provider(item.key_value, item.external_id) == "adapt")
                    else None
                ),
                "adapt_plan_uuid": item.adapt_plan_uuid or "",
                "is_trial": bool(
                    str(item.adapt_plan_uuid or "").strip() == settings.adapt_demo_plan_uuid
                ),
                "can_upgrade": _has_adapt_upgrade_option(
                    current_meta,
                    expires_at=item.expires_at,
                ) if provider == "adapt" else False,
                "target_order_id": (
                    f"tg:{_extract_adapt_uuid(item.key_value, item.external_id)}"
                    if _extract_adapt_uuid(item.key_value, item.external_id)
                    else None
                ),
                "status": item.status,
                "device_slots": item.device_slots,
                "expires_at": item.expires_at.isoformat() if item.expires_at else None,
            }
        )
    return serialized


def _get_access_provider(subscription_url: str | None, external_id: str | None = None) -> str:
    sub_url = str(subscription_url or "").lower()
    identifier = str(external_id or "").lower()
    if identifier.startswith("adapt_") or "/adapt-sub/" in sub_url or "adaptgroup" in sub_url:
        return "adapt"
    if "/proxy-subscription/" in sub_url or "vhq-connect" in sub_url or "/vhq-sub/" in sub_url:
        return "vhq"
    return "marzban" if sub_url or identifier else ""


def _get_order_provider(order: WebOrder) -> str:
    tariff = get_store_tariffs_by_key().get(order.tariff_key)
    if tariff and tariff.get("provider"):
        return str(tariff["provider"])
    return _get_access_provider(order.subscription_url, order.marzban_username)


async def _migrate_web_balance_to_telegram(
    session, profile_token: str, telegram_id: int, *, reconcile_existing_only: bool = False
) -> WebBalanceTransfer | None:
    """Transfer web-only balance to Telegram user via bridge on profile link (durable & idempotent per transfer)."""
    if not settings.bridge_shared_secret or not settings.referral_bridge_url:
        return None

    async with _acquire_profile_lock(profile_token):
        ledger_rows = (await session.execute(select(WebBalanceTransaction).where(
            WebBalanceTransaction.profile_token == profile_token,
            WebBalanceTransaction.kind == "balance_migration",
        ))).scalars().all()
        for tx in ledger_rows:
            if not (tx.source_id or "").startswith("wbtr_"):
                continue
            durable = await session.scalar(select(WebBalanceTransfer).where(
                WebBalanceTransfer.transfer_id == tx.source_id,
            ))
            if (
                not durable or durable.profile_token != profile_token
                or durable.status != "completed" or not durable.completed_at
                or durable.telegram_id <= 0 or durable.amount_rub != tx.amount_rub
                or tx.direction != "debit" or tx.amount_rub <= 0
            ):
                return None

        has_historical_ledger = any(
            not (tx.source_id or "").startswith("wbtr_") for tx in ledger_rows
        )

        # 1. Check for an existing uncompleted / pending transfer for this profile
        # Must search by profile_token only (recipient is immutable once created)
        existing_result = await session.execute(
            select(WebBalanceTransfer)
            .where(
                WebBalanceTransfer.profile_token == profile_token,
                WebBalanceTransfer.status == "pending",
            )
            .order_by(WebBalanceTransfer.id.asc())
            .limit(1)
        )
        transfer = existing_result.scalar_one_or_none()

        if not transfer:
            if reconcile_existing_only:
                return None
            account = await session.get(WebBalanceAccount, profile_token)
            if not account or not account.balance_rub or account.balance_rub <= 0:
                return None

            # Check for legacy pre-PR migration evidence before creating a new transfer intent
            legacy_source_id = f"web_migrate_{profile_token[:16]}"
            status_url = f"{settings.referral_bridge_url.rstrip('/')}/internal/balance-credit-status"
            st_data = None
            timeout = aiohttp.ClientTimeout(total=8)
            try:
                # Ensure no pending transaction holds SQLite locks during HTTP probe
                await session.rollback()
                async with aiohttp.ClientSession(timeout=timeout) as http:
                    async with http.get(
                        status_url,
                        params={"source_id": legacy_source_id},
                        headers=_bridge_headers(),
                    ) as resp:
                        if resp.status == 200 and resp.content_type == "application/json":
                            st_data = await resp.json()
            except Exception as exc:
                logger.warning("Failed to check legacy credit status for %s: %s", profile_token, exc)
                return None

            if st_data is None or not st_data.get("ok"):
                logger.error("Legacy balance probe returned error for %s: %s", profile_token, st_data)
                return None

            if st_data.get("ambiguous"):
                logger.error("Ambiguous legacy balance status for %s: %s. Refusing automatic actions.", profile_token, st_data)
                return None

            if not st_data.get("found") and has_historical_ledger:
                return None

            if st_data.get("found"):
                # Strict validation of legacy status evidence
                is_valid_completed = (
                    st_data.get("completed") is True
                    and st_data.get("status") == "completed"
                    and st_data.get("provider") == "web_balance_migration"
                )
                if not is_valid_completed:
                    logger.error(
                        "Legacy transfer %s in bot is not a completed web_balance_migration: %s. Fail closed.",
                        legacy_source_id,
                        st_data,
                    )
                    return None

                raw_amount = st_data.get("amount_rub")
                raw_tg = st_data.get("telegram_id")
                try:
                    evidence_amount_float = float(raw_amount)
                    evidence_tg_id = int(raw_tg)
                except (TypeError, ValueError):
                    logger.error(
                        "Invalid legacy transfer data for %s: tg=%s amt=%s. Fail closed.",
                        legacy_source_id,
                        raw_tg,
                        raw_amount,
                    )
                    return None

                # Non-fractional integer ruble check (do not use round() to convert fractional amounts into whole rubles)
                if evidence_amount_float <= 0 or not evidence_amount_float.is_integer() or evidence_tg_id <= 0:
                    logger.error(
                        "Invalid legacy transfer data for %s: tg=%s amt=%s. Fail closed.",
                        legacy_source_id,
                        evidence_tg_id,
                        raw_amount,
                    )
                    return None

                evidence_amount = int(evidence_amount_float)

                # Check historical local migration rows for this profile
                local_legacy_res = await session.execute(
                    select(WebBalanceTransaction).where(
                        WebBalanceTransaction.profile_token == profile_token,
                        WebBalanceTransaction.kind == "balance_migration",
                    )
                )
                local_txs = [
                    tx for tx in local_legacy_res.scalars().all()
                    if not (tx.source_id or "").startswith("wbtr_")
                ]

                # Strict ledger validation:
                # 1. kind == "balance_migration" (already filtered in query)
                # 2. direction == "debit"
                # 3. source_id in (tg:<evidence_tg_id>, legacy_source_id)
                # Any contradiction -> fail closed
                contradictory_txs = [
                    tx for tx in local_txs
                    if tx.direction != "debit" or tx.source_id not in (f"tg:{evidence_tg_id}", legacy_source_id)
                ]
                if contradictory_txs:
                    logger.error(
                        "Contradictory historical migration rows for %s: %s. Fail closed.",
                        profile_token,
                        [(tx.id, tx.direction, tx.source_id, tx.amount_rub) for tx in contradictory_txs],
                    )
                    return None

                if len(local_txs) > 1:
                    return None
                if local_txs:
                    matched_amount = sum(int(tx.amount_rub) for tx in local_txs)
                    if matched_amount == evidence_amount:
                        # Fully reconciled! The legacy debit was already deducted locally historically.
                        logger.info(
                            "Legacy transfer %s for %s was already reconciled locally (historical debit %d rub).",
                            legacy_source_id,
                            profile_token,
                            matched_amount,
                        )
                    else:
                        # Ambiguous/conflicting amounts in local ledger
                        logger.error(
                            "Legacy transfer %s evidence amount %d != local matching debit %d for %s. Fail closed.",
                            legacy_source_id,
                            evidence_amount,
                            matched_amount,
                            profile_token,
                        )
                        return None
                else:
                    # No matching debit found. Execute atomic conditional debit (Задача 3)
                    _assert_profile_lock(profile_token)
                    debit_res = await session.execute(
                        update(WebBalanceAccount)
                        .where(
                            WebBalanceAccount.profile_token == profile_token,
                            WebBalanceAccount.balance_rub >= evidence_amount,
                        )
                        .values(
                            balance_rub=WebBalanceAccount.balance_rub - evidence_amount,
                            total_spent_rub=func.coalesce(WebBalanceAccount.total_spent_rub, 0) + evidence_amount,
                            updated_at=datetime.utcnow(),
                        )
                    )
                    if debit_res.rowcount != 1:
                        logger.error(
                            "Failed atomic legacy debit for %s: balance insufficient (%d needed). Fail closed.",
                            profile_token,
                            evidence_amount,
                        )
                        await session.rollback()
                        return None

                    account = await session.get(WebBalanceAccount, profile_token)
                    session.add(
                        WebBalanceTransaction(
                            profile_token=profile_token,
                            amount_rub=evidence_amount,
                            direction="debit",
                            kind="balance_migration",
                            balance_after_rub=account.balance_rub,
                            description="Сверка переноса баланса в Telegram (legacy)",
                            source_id=legacy_source_id,
                        )
                    )
                    await session.commit()
                    logger.info(
                        "Reconciled missing legacy balance transfer %s for %s (%d rub debited locally)",
                        legacy_source_id,
                        profile_token,
                        evidence_amount,
                    )
                    if account.balance_rub <= 0:
                        return None

            # Re-read fresh balance before reserving
            account = await session.get(WebBalanceAccount, profile_token)
            if not account or not account.balance_rub or account.balance_rub <= 0:
                return None

            amount = int(account.balance_rub)
            if amount <= 0:
                return None

            # Atomically reserve balance via conditional update in the intent transaction
            _assert_profile_lock(profile_token)
            reserve_res = await session.execute(
                update(WebBalanceAccount)
                .where(
                    WebBalanceAccount.profile_token == profile_token,
                    WebBalanceAccount.balance_rub == amount,
                    WebBalanceAccount.balance_rub > 0,
                )
                .values(
                    balance_rub=0,
                    updated_at=datetime.utcnow(),
                )
            )
            if reserve_res.rowcount != 1:
                # Another concurrent request already reserved/spent the balance
                await session.rollback()
                existing_result = await session.execute(
                    select(WebBalanceTransfer)
                    .where(
                        WebBalanceTransfer.profile_token == profile_token,
                        WebBalanceTransfer.status == "pending",
                    )
                    .order_by(WebBalanceTransfer.id.asc())
                    .limit(1)
                )
                transfer = existing_result.scalar_one_or_none()
                if not transfer:
                    completed_res = await session.execute(
                        select(WebBalanceTransfer)
                        .where(WebBalanceTransfer.profile_token == profile_token)
                        .order_by(WebBalanceTransfer.id.desc())
                        .limit(1)
                    )
                    t_comp = completed_res.scalar_one_or_none()
                    if t_comp:
                        try:
                            session.expunge(t_comp)
                        except Exception:
                            pass
                    return t_comp
            else:
                # Create new durable transfer intent and commit in the same transaction
                transfer_id = f"wbtr_{profile_token[:8]}_{secrets.token_hex(8)}"
                transfer = WebBalanceTransfer(
                    transfer_id=transfer_id,
                    profile_token=profile_token,
                    telegram_id=telegram_id,
                    amount_rub=amount,
                    status="pending",
                    created_at=datetime.utcnow(),
                )
                session.add(transfer)
                try:
                    await session.commit()
                except IntegrityError:
                    # Concurrent insertion: another worker created pending transfer
                    await session.rollback()
                    existing_result = await session.execute(
                        select(WebBalanceTransfer)
                        .where(
                            WebBalanceTransfer.profile_token == profile_token,
                            WebBalanceTransfer.status == "pending",
                        )
                        .order_by(WebBalanceTransfer.id.asc())
                        .limit(1)
                    )
                    transfer = existing_result.scalar_one_or_none()
                    if not transfer:
                        completed_res = await session.execute(
                            select(WebBalanceTransfer)
                            .where(WebBalanceTransfer.profile_token == profile_token)
                            .order_by(WebBalanceTransfer.id.desc())
                            .limit(1)
                        )
                        t_comp = completed_res.scalar_one_or_none()
                        if t_comp:
                            try:
                                session.expunge(t_comp)
                            except Exception:
                                pass
                        return t_comp

        if transfer.status == "completed":
            try:
                session.expunge(transfer)
            except Exception:
                pass
            return transfer

        target_transfer_id = int(transfer.id)
        transfer_amount = int(transfer.amount_rub)
        transfer_source_id = str(transfer.transfer_id)
        transfer_recipient_id = int(transfer.telegram_id)

        _assert_profile_lock(profile_token)
        # Track attempt via atomic SQL update
        await session.execute(
            update(WebBalanceTransfer)
            .where(WebBalanceTransfer.id == target_transfer_id)
            .values(
                attempts=WebBalanceTransfer.attempts + 1,
                last_attempt_at=datetime.utcnow(),
            )
        )
        await session.commit()

        url = f"{settings.referral_bridge_url.rstrip('/')}/internal/balance-credit"
        # STRICTLY use transfer.telegram_id (immutable recipient!)
        payload = {
            "telegram_id": transfer_recipient_id,
            "amount_rub": transfer_amount,
            "source_type": "web_balance_migration",
            "source_id": transfer_source_id,
            "description": "Перенос баланса с сайта",
        }
        timeout = aiohttp.ClientTimeout(total=8)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as http:
                async with http.post(url, json=payload, headers=_bridge_headers()) as resp:
                    data = await resp.json() if resp.content_type == "application/json" else {}
                    resp_status = resp.status
        except Exception as exc:
            _assert_profile_lock(profile_token)
            logger.warning("Web balance migration network error for %s: %s", profile_token, exc)
            await session.execute(
                update(WebBalanceTransfer)
                .where(WebBalanceTransfer.id == target_transfer_id)
                .values(error_message=str(exc)[:255])
            )
            await session.commit()
            transfer.status = "pending"
            transfer.error_message = str(exc)[:255]
            try:
                t_fresh = await session.get(WebBalanceTransfer, target_transfer_id)
                if t_fresh:
                    await session.refresh(t_fresh)
                    session.expunge(t_fresh)
                    transfer = t_fresh
            except Exception:
                pass
            return transfer

        _assert_profile_lock(profile_token)
        if resp_status == 200 and data.get("status") in ("credited", "already_credited"):
            now = datetime.utcnow()
            # Atomic Compare-And-Set finalization
            cas_res = await session.execute(
                update(WebBalanceTransfer)
                .where(
                    WebBalanceTransfer.id == target_transfer_id,
                    WebBalanceTransfer.status == "pending",
                )
                .values(
                    status="completed",
                    completed_at=now,
                    error_message=None,
                )
            )
            if cas_res.rowcount == 1:
                account = await session.get(WebBalanceAccount, profile_token)
                if account:
                    account.total_spent_rub = (account.total_spent_rub or 0) + transfer_amount
                    account.updated_at = now
                    bal_after = account.balance_rub or 0
                else:
                    bal_after = 0

                session.add(
                    WebBalanceTransaction(
                        profile_token=profile_token,
                        amount_rub=transfer_amount,
                        direction="debit",
                        kind="balance_migration",
                        balance_after_rub=bal_after,
                        description="Перенос баланса в Telegram",
                        source_id=transfer_source_id,
                    )
                )
                commit_ok = False
                try:
                    await session.commit()
                    commit_ok = True
                except Exception as exc:
                    logger.error("Commit failed during finalization of transfer %s: %s", target_transfer_id, exc)
                    await session.rollback()

                if commit_ok:
                    transfer.status = "completed"
                    transfer.completed_at = now
                    transfer.error_message = None
                    logger.info(
                        "Migrated %d₽ web balance to Telegram %d (transfer %s, status=%s)",
                        transfer_amount,
                        transfer_recipient_id,
                        transfer_source_id,
                        data.get("status"),
                    )
                else:
                    # Commit failed: DB rolled back. Do NOT mark completed in memory!
                    t_check = await session.execute(
                        select(WebBalanceTransfer).where(WebBalanceTransfer.id == target_transfer_id).limit(1)
                    )
                    t_db = t_check.scalar_one_or_none()
                    if t_db:
                        transfer.status = t_db.status
                        transfer.completed_at = t_db.completed_at
                        transfer.error_message = t_db.error_message or "Commit failed during finalization"
                    else:
                        transfer.status = "pending"
                        transfer.error_message = "Commit failed during finalization"
            else:
                await session.rollback()
                t_res = await session.execute(
                    select(WebBalanceTransfer).where(WebBalanceTransfer.id == target_transfer_id).limit(1)
                )
                t_row = t_res.scalar_one_or_none()
                if t_row:
                    transfer.status = t_row.status
                    transfer.completed_at = t_row.completed_at
        elif resp_status == 404 and (data or {}).get("error") == "User not found":
            # Definite permanent failure: user does not exist in bot. Release reservation.
            cas_res = await session.execute(
                update(WebBalanceTransfer)
                .where(
                    WebBalanceTransfer.id == target_transfer_id,
                    WebBalanceTransfer.status == "pending",
                )
                .values(
                    status="failed",
                    error_message="User not found in Telegram bot",
                )
            )
            if cas_res.rowcount == 1:
                account = await session.get(WebBalanceAccount, profile_token)
                if account:
                    account.balance_rub = (account.balance_rub or 0) + transfer_amount
                    account.updated_at = datetime.utcnow()
                await session.commit()
                transfer.status = "failed"
                transfer.error_message = "User not found in Telegram bot"
        else:
            err = (data or {}).get("error") or f"HTTP {resp_status}"
            logger.warning(
                "Web balance migration rejected/ambiguous for %s (transfer %s): %s",
                profile_token,
                transfer_source_id,
                err,
            )
            await session.execute(
                update(WebBalanceTransfer)
                .where(WebBalanceTransfer.id == target_transfer_id)
                .values(error_message=str(err)[:255])
            )
            await session.commit()
            transfer.status = "pending"
            transfer.error_message = str(err)[:255]

        try:
            t_fresh = await session.get(WebBalanceTransfer, target_transfer_id)
            if t_fresh:
                await session.refresh(t_fresh)
                session.expunge(t_fresh)
                transfer = t_fresh
        except Exception:
            pass

        return transfer


async def _get_or_create_web_balance_account(
    session,
    profile_token: str,
    contact: str | None = None,
) -> WebBalanceAccount:
    account = await session.get(WebBalanceAccount, profile_token)
    if not account:
        account = WebBalanceAccount(
            profile_token=profile_token,
            contact=contact,
            ref_code=_make_web_ref_code(profile_token),
            updated_at=datetime.utcnow(),
        )
        session.add(account)
    elif contact and not account.contact:
        account.contact = contact
        account.updated_at = datetime.utcnow()
    if account.balance_rub is None:
        account.balance_rub = 0
    if account.total_earned_rub is None:
        account.total_earned_rub = 0
    if account.total_spent_rub is None:
        account.total_spent_rub = 0
    return account


async def _maybe_issue_web_demo_key(session, account: WebBalanceAccount) -> str | None:
    """Issue a free demo Marzban or Adapt key for a new web user if demo is enabled.

    Returns the subscription URL, or None if demo is disabled / already issued / failed.
    """
    if not settings.demo_key_enabled:
        return None

    # Check if this profile token is linked to a telegram user who already got a demo subscription in the bot DB.
    linked_profile = await session.get(WebProfileLink, account.profile_token)
    if linked_profile and linked_profile.telegram_id:
        from webstore.config import _bot_db_path
        path = _bot_db_path()
        if path and path.exists():
            try:
                import sqlite3
                with sqlite3.connect(str(path)) as con:
                    row = con.execute(
                        """
                        SELECT 1 FROM subscriptions s
                        JOIN users u ON s.user_id = u.id
                        WHERE u.telegram_id = ? AND s.billing_mode = 'demo'
                        LIMIT 1
                        """,
                        (linked_profile.telegram_id,)
                    ).fetchone()
                    if row:
                        logger.info("Demo key already issued in bot for telegram_id %s, refusing web demo key", linked_profile.telegram_id)
                        return None
            except Exception as e:
                logger.error("Failed to check bot demo status for telegram_id %s: %s", linked_profile.telegram_id, e)

    existing = await session.execute(
        select(WebOrder).where(
            WebOrder.profile_token == account.profile_token,
            WebOrder.tariff_key == "demo",
        ).limit(1)
    )
    if existing.scalar_one_or_none():
        return None

    demo_days = settings.demo_key_days

    if settings.adapt_demo_enabled:
        try:
            api = AdaptAPI()
            resp = await api.create_subscription(
                plan_uuid=settings.adapt_demo_plan_uuid,
                external_user_id=f"web_demo_{account.profile_token[:16]}",
            )
            adapt_uuid = resp.get("uuid") or resp.get("subscription_uuid") or resp.get("id")
            if not adapt_uuid:
                logger.error("Adapt demo created but no UUID returned for profile %s: %s", account.profile_token[:8], resp)
                return None
            
            branded_url = build_adapt_mirror_url(adapt_uuid)
            sub_url = branded_url or resp.get("subscription_url")
            if not sub_url:
                logger.warning("No subscription URL for demo user adapt_%s", adapt_uuid)
                return None

            expires_at = _adapt_expires_at_from_response(resp, demo_days)

            demo_order = WebOrder(
                order_id=f"demo_{account.profile_token[:16]}",
                contact=account.contact,
                tariff_key="demo",
                tariff_label=f"Демо-доступ {demo_days} дн.",
                days=demo_days,
                amount_rub=0,
                original_amount_rub=0,
                status="demo",
                marzban_username=f"adapt_{adapt_uuid}",
                subscription_url=sub_url,
                profile_token=account.profile_token,
                delivered_at=datetime.utcnow(),
                access_expires_at=expires_at,
            )
            session.add(demo_order)
            logger.info("Web Adapt demo key issued for profile %s", account.profile_token[:8])
            return sub_url
        except Exception as e:
            logger.error("Error issuing Adapt web demo key for profile %s: %s", account.profile_token[:8], e)
            return None
    else:
        # Fallback to Marzban
        username = f"wdemo_{account.profile_token[:16]}"
        expire_ts = int((datetime.utcnow() + timedelta(days=demo_days)).timestamp())

        try:
            async with MarzbanClient() as marzban:
                user_data = await marzban.create_user(
                    username=username,
                    expire=expire_ts,
                    note=f"web demo | {account.contact or account.profile_token[:8]}",
                )
                if not user_data:
                    logger.warning("Failed to create demo Marzban user for profile %s", account.profile_token[:8])
                    return None

                sub_url = await marzban.get_subscription_url(username)
                if not sub_url:
                    logger.warning("No subscription URL for demo user %s", username)
                    return None

            demo_order = WebOrder(
                order_id=f"demo_{account.profile_token[:16]}",
                contact=account.contact,
                tariff_key="demo",
                tariff_label=f"Демо-доступ {demo_days} дн.",
                days=demo_days,
                amount_rub=0,
                original_amount_rub=0,
                status="demo",
                marzban_username=username,
                subscription_url=sub_url,
                profile_token=account.profile_token,
                delivered_at=datetime.utcnow(),
                access_expires_at=datetime.utcnow() + timedelta(days=demo_days),
            )
            session.add(demo_order)
            logger.info("Web demo key issued for profile %s", account.profile_token[:8])
            return sub_url

        except Exception as e:
            logger.error("Error issuing web demo key for profile %s: %s", account.profile_token[:8], e)
            return None


def _add_web_balance_transaction(
    session,
    account: WebBalanceAccount,
    *,
    amount_rub: int,
    direction: str,
    kind: str,
    description: str,
    source_id: str | None,
) -> None:
    if direction == "credit":
        account.balance_rub += amount_rub
        account.total_earned_rub += amount_rub
        account.balance_warning_for_charge_at = None
    else:
        account.balance_rub -= amount_rub
        account.total_spent_rub += amount_rub
    account.updated_at = datetime.utcnow()
    session.add(
        WebBalanceTransaction(
            profile_token=account.profile_token,
            amount_rub=amount_rub,
            direction=direction,
            kind=kind,
            balance_after_rub=account.balance_rub,
            description=description,
            source_id=source_id,
        )
    )


async def _fetch_web_balance_history(session, profile_token: str, limit: int = 20) -> list[WebBalanceTransaction]:
    result = await session.execute(
        select(WebBalanceTransaction)
        .where(WebBalanceTransaction.profile_token == profile_token)
        .order_by(WebBalanceTransaction.created_at.desc(), WebBalanceTransaction.id.desc())
        .limit(limit)
    )
    return result.scalars().all()


def _serialize_web_balance_history(items: list[WebBalanceTransaction]) -> list[dict[str, str | int | None]]:
    return [
        {
            "amount_rub": item.amount_rub,
            "direction": item.direction,
            "kind": item.kind,
            "balance_after_rub": item.balance_after_rub,
            "description": item.description,
            "created_at": item.created_at.isoformat() if item.created_at else None,
        }
        for item in items
    ]


async def _get_latest_web_access_order(session, profile_token: str) -> WebOrder | None:
    result = await session.execute(
        select(WebOrder)
        .where(WebOrder.profile_token == profile_token)
        .where(WebOrder.status.in_(["delivered", "demo"]))
        .where(WebOrder.tariff_key != "device_slot")
        .where(WebOrder.marzban_username.is_not(None))
        .order_by(WebOrder.access_expires_at.desc(), WebOrder.delivered_at.desc(), WebOrder.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _ensure_web_access_until(order: WebOrder, expires_at: datetime) -> bool:
    if not order.marzban_username:
        return False
    try:
        async with MarzbanClient() as marzban:
            updated = await marzban.update_user(
                order.marzban_username,
                expire=int(expires_at.timestamp()),
                status="active",
            )
            if not updated:
                return False
            sub_url = await marzban.get_subscription_url(order.marzban_username)
            if sub_url:
                order.subscription_url = sub_url
            order.access_expires_at = expires_at
            return True
    except Exception as exc:
        logger.error("Failed to extend web access for %s: %s", order.order_id, exc)
        return False


def _build_local_balance_status(account: WebBalanceAccount, daily_rate: float) -> dict[str, str | int | float | bool | None]:
    enabled = bool(account.balance_mode_enabled and account.balance_autodebit_enabled)
    status_text = "Ежедневные списания включены" if enabled else "Ежедневные списания выключены"
    if account.balance_grace_until:
        status_text = "Баланс ушёл в минус. Пополните его до следующего списания."
    elif not enabled and account.next_daily_charge_at:
        status_text = "Новые списания выключены. Доступ останется до указанной даты."
    return {
        "balance_rub": round(float(account.balance_rub or 0), 2),
        "balance_mode_enabled": bool(account.balance_mode_enabled),
        "balance_autodebit_enabled": bool(account.balance_autodebit_enabled),
        "next_daily_charge_at": account.next_daily_charge_at.isoformat() if account.next_daily_charge_at else None,
        "balance_grace_until": account.balance_grace_until.isoformat() if account.balance_grace_until else None,
        "daily_charge_rub": daily_rate,
        "status_text": status_text,
    }


async def _enable_local_balance_mode(session, account: WebBalanceAccount) -> tuple[bool, str | None]:
    now = datetime.utcnow()
    order = await _get_latest_web_access_order(session, account.profile_token)
    if not order:
        return False, "Сначала нужен хотя бы один оплаченный ключ"

    account.balance_mode_enabled = 1
    account.balance_autodebit_enabled = 1

    if order.access_expires_at and order.access_expires_at > now:
        account.next_daily_charge_at = order.access_expires_at
        account.balance_grace_until = None
        account.balance_warning_for_charge_at = None
        account.updated_at = now
        return True, None

    daily_rate = await _get_web_daily_charge_rub()
    current_balance = round(float(account.balance_rub or 0), 2)
    if current_balance < daily_rate:
        account.balance_mode_enabled = 0
        account.balance_autodebit_enabled = 0
        return False, "Недостаточно средств для запуска режима"

    expires_at = _next_charge_datetime(now)
    _add_web_balance_transaction(
        session,
        account,
        amount_rub=int(round(daily_rate)),
        direction="debit",
        kind="daily_charge",
        description="Ежедневное списание",
        source_id=expires_at.isoformat(),
    )
    ok = await _ensure_web_access_until(order, expires_at)
    if not ok:
        _add_web_balance_transaction(
            session,
            account,
            amount_rub=int(round(daily_rate)),
            direction="credit",
            kind="refund",
            description="Возврат: не удалось продлить доступ",
            source_id=expires_at.isoformat(),
        )
        account.balance_mode_enabled = 0
        account.balance_autodebit_enabled = 0
        return False, "Не удалось продлить доступ"

    account.next_daily_charge_at = expires_at
    account.last_daily_charge_at = now
    account.balance_grace_until = None
    account.balance_warning_for_charge_at = None
    account.updated_at = now
    return True, None


async def _disable_local_balance_mode(session, account: WebBalanceAccount) -> tuple[bool, datetime | None]:
    now = datetime.utcnow()
    order = await _get_latest_web_access_order(session, account.profile_token)
    access_until = order.access_expires_at if order and order.access_expires_at and order.access_expires_at > now else None
    account.balance_mode_enabled = 0
    account.balance_autodebit_enabled = 0
    account.balance_grace_until = None
    account.next_daily_charge_at = access_until
    account.balance_warning_for_charge_at = None
    account.updated_at = now
    return True, access_until


async def _disable_web_balance_autodebit_after_tariff_purchase(session, order: WebOrder) -> None:
    """A fixed webstore tariff pays the period; daily balance debits must stop."""
    if order.tariff_key == "device_slot" or not order.profile_token:
        return
    account = await session.get(WebBalanceAccount, order.profile_token)
    if account:
        account.balance_mode_enabled = 0
        account.balance_autodebit_enabled = 0
        account.balance_grace_until = None
        account.next_daily_charge_at = None
        account.balance_warning_for_charge_at = None
        account.updated_at = datetime.utcnow()

    link = await session.get(WebProfileLink, order.profile_token)
    if not link or not link.telegram_id:
        return

    _, status = await _toggle_balance_mode(int(link.telegram_id), False)
    if status != 200:
        logger.warning(
            "Failed to disable linked bot balance mode after webstore tariff purchase: order_id=%s telegram_id=%s status=%s",
            order.order_id,
            link.telegram_id,
            status,
        )


async def _credit_web_referral(order: WebOrder) -> dict[str, str | int | float | None]:
    if not order.ref_source or not settings.bridge_shared_secret or not settings.referral_bridge_url:
        return {"status": "skipped"}

    payload = {
        "order_id": order.order_id,
        "ref_code": order.ref_source,
        "amount_rub": order.amount_rub,
        "buyer_contact": order.contact or order.email,
        "tariff_label": order.tariff_label,
    }
    url = f"{settings.referral_bridge_url.rstrip('/')}/internal/web-referral/credit"
    timeout = aiohttp.ClientTimeout(total=8)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(url, json=payload, headers=_bridge_headers()) as resp:
                if resp.content_type == "application/json":
                    data = await resp.json()
                else:
                    data = {"status": "failed"}
                if resp.status in {200, 404, 409}:
                    return data
                logger.warning("Unexpected web referral credit status for %s: %s", order.order_id, resp.status)
                return {"status": "failed"}
    except Exception as exc:
        logger.warning("Failed to credit web referral for %s: %s", order.order_id, exc)
        return {"status": "failed"}


async def _credit_local_web_referral(order_id: str) -> dict[str, str | int | None]:
    async with async_session() as session:
        profile_token = await session.scalar(
            select(WebBalanceAccount.profile_token)
            .join(WebOrder, WebOrder.ref_source == WebBalanceAccount.ref_code)
            .where(WebOrder.order_id == order_id)
        )
    if not profile_token:
        return {"status": "skipped"}
    async with _acquire_profile_lock(profile_token):
        return await _credit_local_web_referral_locked(order_id)


async def _credit_local_web_referral_locked(order_id: str) -> dict[str, str | int | None]:
    async with async_session() as session:
        result = await session.execute(
            select(WebOrder).where(WebOrder.order_id == order_id)
        )
        order = result.scalar_one_or_none()
        if not order or not order.ref_source:
            return {"status": "skipped"}
        if order.referral_status == "credited":
            return {"status": "already_credited"}

        result = await session.execute(
            select(WebBalanceAccount).where(WebBalanceAccount.ref_code == order.ref_source)
        )
        account = result.scalar_one_or_none()
        if not account:
            return {"status": "skipped"}
        if account.profile_token == order.profile_token:
            return {"status": "invalid"}

        existing = await session.execute(
            select(WebBalanceTransaction).where(
                WebBalanceTransaction.profile_token == account.profile_token,
                WebBalanceTransaction.kind == "referral_bonus",
                WebBalanceTransaction.source_id == order.order_id,
            )
        )
        if existing.scalar_one_or_none():
            return {"status": "already_credited"}

        config = await _fetch_public_balance_config()
        commission_percent = float((config or {}).get("referral_commission_percent", settings.referral_commission_percent))
        earning_rub = max(1, int(round((order.original_amount_rub or order.amount_rub) * commission_percent / 100)))
        _add_web_balance_transaction(
            session,
            account,
            amount_rub=earning_rub,
            direction="credit",
            kind="referral_bonus",
            description="Бонус за оплату друга через сайт",
            source_id=order.order_id,
        )
        await session.commit()
        return {
            "status": "credited",
            "earning_rub": earning_rub,
            "profile_token": account.profile_token,
        }


async def _apply_order_bonus_spend(session, order: WebOrder) -> None:
    bonus_amount = int(order.bonus_applied_rub or 0)
    if bonus_amount <= 0 or order.bonus_spent_at:
        return

    account = await _get_or_create_web_balance_account(session, order.profile_token, order.contact or order.email)
    existing = await session.execute(
        select(WebBalanceTransaction).where(
            WebBalanceTransaction.profile_token == account.profile_token,
            WebBalanceTransaction.kind == "order_payment",
            WebBalanceTransaction.source_id == order.order_id,
        )
    )
    if existing.scalar_one_or_none():
        order.bonus_spent_at = datetime.utcnow()
        return

    if account.balance_rub < bonus_amount:
        bonus_amount = max(0, account.balance_rub)
    if bonus_amount <= 0:
        order.bonus_applied_rub = 0
        order.bonus_spent_at = datetime.utcnow()
        return

    _add_web_balance_transaction(
        session,
        account,
        amount_rub=bonus_amount,
        direction="debit",
        kind="order_payment",
        description=f"Списано на оплату заказа {order.order_id}",
        source_id=order.order_id,
    )
    order.bonus_applied_rub = bonus_amount
    order.bonus_spent_at = datetime.utcnow()


async def _apply_order_cashback(session, order: WebOrder) -> int:
    """Credit cashback to buyer's web balance account for real money payments."""
    if not order.profile_token:
        return 0

    real_amount = order.amount_rub
    if real_amount is None or real_amount <= 0:
        return 0

    existing = await session.execute(
        select(WebBalanceTransaction).where(
            WebBalanceTransaction.profile_token == order.profile_token,
            WebBalanceTransaction.kind == "cashback",
            WebBalanceTransaction.source_id == order.order_id,
        )
    )
    if existing.scalar_one_or_none():
        return 0

    config = await _fetch_public_balance_config()
    is_enabled = (config or {}).get("cashback_enabled", settings.cashback_enabled)
    if not is_enabled:
        return 0

    pct = float((config or {}).get("cashback_percent", settings.cashback_percent))
    if pct <= 0:
        return 0

    cashback_rub = max(1, int(round(real_amount * pct / 100)))
    if cashback_rub <= 0:
        return 0

    account = await _get_or_create_web_balance_account(session, order.profile_token, order.contact or order.email)
    pct_fmt = int(pct) if pct.is_integer() else pct
    _add_web_balance_transaction(
        session,
        account,
        amount_rub=cashback_rub,
        direction="credit",
        kind="cashback",
        description=f"Кешбэк {pct_fmt}% за оплату заказа {order.order_id}",
        source_id=order.order_id,
    )
    logger.info(
        "Cashback credited for web order %s: profile=%s amount=%s руб (%s%%)",
        order.order_id,
        order.profile_token,
        cashback_rub,
        pct,
    )
    return cashback_rub


async def _finalize_order_cashback(
    session,
    order: WebOrder,
    bot_result: dict | None,
) -> str:
    """Coordinate cashback for a delivered web order.

    Ensures cashback is credited to either Telegram or Web balance, never both.
    """
    now = datetime.utcnow()

    # Rule A: Existing web cashback transaction for order_id
    try:
        existing_web_tx = await session.scalar(
            select(WebBalanceTransaction).where(
                WebBalanceTransaction.kind == "cashback",
                WebBalanceTransaction.source_id == order.order_id,
            ).limit(1)
        )
    except Exception as exc:
        logger.warning("Failed querying existing web cashback for %s: %s; failing closed", getattr(order, "order_id", None), exc)
        order.cashback_status = "pending"
        return "pending"

    if existing_web_tx:
        order.cashback_status = "web_credited"
        order.cashback_amount_rub = int(existing_web_tx.amount_rub)
        order.cashback_processed_at = order.cashback_processed_at or now
        return "web_credited"

    if order.cashback_status in ("telegram_credited", "web_credited", "disabled", "not_eligible", "legacy_skipped"):
        return str(order.cashback_status)

    try:
        real_amount = float(order.amount_rub) if order.amount_rub is not None else 0.0
    except (TypeError, ValueError):
        real_amount = 0.0

    if real_amount <= 0:
        order.cashback_status = "not_eligible"
        order.cashback_amount_rub = 0
        order.cashback_processed_at = now
        return "not_eligible"

    if not bot_result or not isinstance(bot_result, dict):
        order.cashback_status = "pending"
        return "pending"

    if not bot_result.get("ok"):
        order.cashback_status = "pending"
        return "pending"

    # Rule B: bot_result positively says matched=True
    if bot_result.get("matched") is True:
        bot_status = str(bot_result.get("cashback_status") or "")
        cb_rub = float(bot_result.get("cashback_rub") or 0.0)

        if bot_status in ("credited", "already_credited"):
            order.cashback_status = "telegram_credited"
            order.cashback_amount_rub = int(round(cb_rub))
            order.cashback_processed_at = now
            return "telegram_credited"
        elif bot_status == "disabled":
            order.cashback_status = "disabled"
            order.cashback_amount_rub = 0
            order.cashback_processed_at = now
            return "disabled"
        elif bot_status == "not_eligible":
            order.cashback_status = "not_eligible"
            order.cashback_amount_rub = 0
            order.cashback_processed_at = now
            return "not_eligible"
        elif bot_status == "web_already_credited":
            order.cashback_status = "web_credited"
            order.cashback_processed_at = now
            return "web_credited"
        else:
            # Bot failure: keep pending, NEVER credit web balance
            order.cashback_status = "pending"
            return "pending"

    # Rule C: bot_result positively says matched=False
    if bot_result.get("matched") is False:
        credited_rub = await _apply_order_cashback(session, order)
        if credited_rub > 0:
            order.cashback_status = "web_credited"
            order.cashback_amount_rub = credited_rub
            order.cashback_processed_at = now
            return "web_credited"
        else:
            config = await _fetch_public_balance_config()
            is_enabled = (config or {}).get("cashback_enabled", settings.cashback_enabled)
            pct = float((config or {}).get("cashback_percent", settings.cashback_percent))
            if not is_enabled or pct <= 0:
                order.cashback_status = "disabled"
            else:
                order.cashback_status = "not_eligible"
            order.cashback_amount_rub = 0
            order.cashback_processed_at = now
            return str(order.cashback_status)

    # Rule D: Indeterminate
    order.cashback_status = "pending"
    return "pending"


async def _sync_referral_credit_for_order(order_id: str) -> None:
    async with async_session() as session:
        result = await session.execute(
            select(WebOrder).where(WebOrder.order_id == order_id)
        )
        order = result.scalar_one_or_none()
        if not order or order.status != "delivered" or not order.ref_source:
            return
        if order.referral_status in {"credited", "invalid"}:
            return

    credit_result = await _credit_local_web_referral(order_id)
    if str(credit_result.get("status")) == "skipped":
        credit_result = await _credit_web_referral(order)
    status = str(credit_result.get("status") or "")
    referrer_tg = credit_result.get("telegram_id")

    async with async_session() as session:
        result = await session.execute(
            select(WebOrder).where(WebOrder.order_id == order_id)
        )
        order = result.scalar_one_or_none()
        if not order:
            return

        if referrer_tg:
            try:
                order.referrer_telegram_id = int(str(referrer_tg))
            except (TypeError, ValueError):
                pass

        if status in {"credited", "already_credited"}:
            order.referral_status = "credited"
            if not order.referral_credited_at:
                order.referral_credited_at = datetime.utcnow()
        elif status in {"invalid", "disabled"}:
            order.referral_status = "invalid"
        elif status != "skipped":
            order.referral_status = "failed"

        await session.commit()


async def _fetch_balance_profile(telegram_id: int) -> dict[str, str | int | float | bool | None] | None:
    if not telegram_id or not settings.bridge_shared_secret or not settings.referral_bridge_url:
        return None

    url = f"{settings.referral_bridge_url.rstrip('/')}/internal/balance-profile?telegram_id={telegram_id}"
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(url, headers=_bridge_headers()) as resp:
                if resp.status == 404:
                    return None
                if resp.status != 200:
                    logger.warning("Unexpected balance profile status for telegram_id=%s: %s", telegram_id, resp.status)
                    return None
                return await resp.json()
    except Exception as exc:
        logger.warning("Failed to fetch balance profile for telegram_id=%s: %s", telegram_id, exc)
        return None


async def _fetch_balance_history(telegram_id: int, limit: int = 20) -> list[dict] | None:
    if not telegram_id or not settings.bridge_shared_secret or not settings.referral_bridge_url:
        return None

    url = f"{settings.referral_bridge_url.rstrip('/')}/internal/balance-history?telegram_id={telegram_id}&limit={limit}"
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(url, headers=_bridge_headers()) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                return data.get("items") or []
    except Exception as exc:
        logger.warning("Failed to fetch balance history for telegram_id=%s: %s", telegram_id, exc)
        return None


async def _fetch_public_balance_config() -> dict[str, float | bool] | None:
    if not settings.bridge_shared_secret or not settings.referral_bridge_url:
        return None

    url = f"{settings.referral_bridge_url.rstrip('/')}/internal/balance-config"
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.get(url, headers=_bridge_headers()) as resp:
                if resp.status != 200:
                    logger.warning("Unexpected balance config status: %s", resp.status)
                    return None
                data = await resp.json()
                daily_value = data.get("daily_charge_rub")
                referral_value = data.get("referral_commission_percent")
                cb_enabled = data.get("cashback_enabled")
                cb_pct = data.get("cashback_percent")
                result: dict[str, float | bool] = {}
                if daily_value is not None:
                    result["daily_charge_rub"] = round(float(daily_value), 2)
                if referral_value is not None:
                    result["referral_commission_percent"] = round(float(referral_value), 2)
                if cb_enabled is not None:
                    result["cashback_enabled"] = bool(cb_enabled)
                if cb_pct is not None:
                    result["cashback_percent"] = round(float(cb_pct), 2)
                return result
    except Exception as exc:
        logger.warning("Failed to fetch public balance config: %s", exc)
        return None


async def _get_web_daily_charge_rub() -> float:
    config = await _fetch_public_balance_config()
    value = (config or {}).get("daily_charge_rub")
    if value and value > 0:
        return round(float(value), 2)
    return 3.17


async def _toggle_balance_mode(telegram_id: int, enabled: bool) -> tuple[dict | None, int]:
    if not telegram_id or not settings.bridge_shared_secret or not settings.referral_bridge_url:
        return None, 500

    url = f"{settings.referral_bridge_url.rstrip('/')}/internal/balance-toggle"
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(url, json={"telegram_id": telegram_id, "enabled": enabled}, headers=_bridge_headers()) as resp:
                data = await resp.json()
                return data, resp.status
    except Exception as exc:
        logger.warning("Failed to toggle balance mode for telegram_id=%s: %s", telegram_id, exc)
        return None, 500


async def _credit_web_balance_topup(topup: WebBalanceTopUp) -> dict[str, str | float | None]:
    if not topup.telegram_id:
        return {"status": "skipped"}
    if not settings.bridge_shared_secret or not settings.referral_bridge_url:
        return {"status": "failed"}

    payload = {
        "telegram_id": int(topup.telegram_id),
        "amount_rub": float(topup.amount_rub),
        "source_type": "webstore_topup",
        "source_id": topup.topup_id,
        "description": "Пополнение баланса через сайт",
    }
    url = f"{settings.referral_bridge_url.rstrip('/')}/internal/balance-credit"
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(url, json=payload, headers=_bridge_headers()) as resp:
                if resp.content_type == "application/json":
                    return await resp.json()
                return {"status": "failed"}
    except Exception as exc:
        logger.warning("Failed to credit web balance topup %s: %s", topup.topup_id, exc)
        return {"status": "failed"}


async def _credit_local_balance_topup(topup_id: str) -> dict[str, str | int]:
    async with async_session() as session:
        profile_token = await session.scalar(
            select(WebBalanceTopUp.profile_token).where(WebBalanceTopUp.topup_id == topup_id)
        )
    if not profile_token:
        return {"status": "failed"}
    async with _acquire_profile_lock(profile_token):
        return await _credit_local_balance_topup_locked(topup_id)


async def _credit_local_balance_topup_locked(topup_id: str) -> dict[str, str | int]:
    async with async_session() as session:
        result = await session.execute(
            select(WebBalanceTopUp).where(WebBalanceTopUp.topup_id == topup_id)
        )
        topup = result.scalar_one_or_none()
        if not topup:
            return {"status": "failed"}
        if topup.status == "completed":
            return {"status": "already_credited"}

        account = await _get_or_create_web_balance_account(session, topup.profile_token)
        _add_web_balance_transaction(
            session,
            account,
            amount_rub=int(topup.amount_rub),
            direction="credit",
            kind="topup",
            description="Пополнение баланса через сайт",
            source_id=topup.topup_id,
        )
        topup.status = "completed"
        topup.completed_at = datetime.utcnow()
        await session.commit()
        return {"status": "credited", "balance_rub": account.balance_rub}


async def _get_profile_bundle(
    session,
    token: str,
) -> tuple[list[WebOrder], WebProfileLink | None, list[WebTelegramItem]]:
    orders_result = await session.execute(
        select(WebOrder)
        .where(WebOrder.profile_token == token)
        .order_by(WebOrder.created_at.desc())
    )
    orders = orders_result.scalars().all()

    link = await session.get(WebProfileLink, token)

    items_result = await session.execute(
        select(WebTelegramItem)
        .where(WebTelegramItem.profile_token == token)
        .order_by(WebTelegramItem.updated_at.desc(), WebTelegramItem.id.desc())
    )
    items = items_result.scalars().all()
    for item in items:
        provider = item.provider or _get_access_provider(item.key_value, item.external_id)
        if item.provider != provider:
            item.provider = provider or None
    return orders, link, items


async def _replace_telegram_items(
    session,
    profile_token: str,
    telegram_id: int,
    items: list[dict],
) -> None:
    await session.execute(
        delete(WebTelegramItem).where(WebTelegramItem.profile_token == profile_token)
    )
    now = datetime.utcnow()
    seen_keys: set[tuple[str, str]] = set()
    for item in items:
        item_type = (item.get("item_type") or "vpn")[:16]
        external_id = (str(item.get("external_id", "")) or secrets.token_hex(8))[:64]
        key = (item_type, external_id)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        session.add(
            WebTelegramItem(
                profile_token=profile_token,
                telegram_id=telegram_id,
                item_type=item_type,
                external_id=external_id,
                title=(item.get("title") or "Telegram").strip()[:128],
                subtitle=(item.get("subtitle") or "").strip()[:256] or None,
                key_value=item.get("key_value"),
                provider=(item.get("provider") or "")[:16] or None,
                adapt_plan_uuid=(item.get("adapt_plan_uuid") or "")[:64] or None,
                status=(item.get("status") or "active")[:16],
                device_slots=item.get("device_slots"),
                expires_at=datetime.fromisoformat(item["expires_at"]) if item.get("expires_at") else None,
                created_at=now,
                updated_at=now,
            )
        )
    await session.flush()


async def _upsert_profile_link(
    session,
    profile_token: str,
    contact: str | None,
    telegram_id: int,
    telegram_username: str | None,
    telegram_full_name: str | None,
) -> WebProfileLink:
    existing_by_telegram = await session.execute(
        select(WebProfileLink).where(WebProfileLink.telegram_id == telegram_id)
    )
    other_link = existing_by_telegram.scalar_one_or_none()
    if other_link and other_link.profile_token != profile_token:
        await session.execute(
            delete(WebTelegramItem).where(
                (WebTelegramItem.profile_token == other_link.profile_token)
                | (WebTelegramItem.telegram_id == telegram_id)
            )
        )
        await session.delete(other_link)
        await session.flush()

    link = await session.get(WebProfileLink, profile_token)
    if link and link.telegram_id != telegram_id:
        await session.execute(
            delete(WebTelegramItem).where(WebTelegramItem.profile_token == profile_token)
        )

    if not link:
        link = WebProfileLink(
            profile_token=profile_token,
            contact=contact,
            telegram_id=telegram_id,
            telegram_username=telegram_username,
            telegram_full_name=telegram_full_name,
            linked_at=datetime.utcnow(),
            last_synced_at=datetime.utcnow(),
        )
        session.add(link)
    else:
        link.contact = contact
        link.telegram_id = telegram_id
        link.telegram_username = telegram_username
        link.telegram_full_name = telegram_full_name
        if not link.linked_at:
            link.linked_at = datetime.utcnow()
        link.last_synced_at = datetime.utcnow()

    await session.flush()
    return link


def _render(template_name: str, *, tariffs: list[dict] | None = None, **kwargs) -> str:
    path = TEMPLATES_DIR / template_name
    html = path.read_text(encoding="utf-8")
    html = html.replace("{{SITE_NAME}}", settings.site_name)
    html = html.replace("{{BOT_HANDLE}}", settings.bot_handle)
    for key, value in kwargs.items():
        html = html.replace("{{" + key + "}}", str(value))
    return html.replace("{{TARIFFS_JSON}}", json.dumps(tariffs or get_store_tariffs(), ensure_ascii=False))


def _build_vk_link_html(*, footer: bool = False) -> str:
    """Build VK icon link for header or text link for footer."""
    if not settings.vk_url:
        return ""
    if footer:
        return f' или <a href="{settings.vk_url}" target="_blank">ВКонтакте</a>'
    return (
        f'<a href="{settings.vk_url}" class="telegram-link" target="_blank"'
        f' aria-label="ВКонтакте" style="margin-left:2px">'
        '<svg viewBox="0 0 24 24" aria-hidden="true">'
        '<path d="M13.16 17.5c-5.63 0-8.84-3.87-8.97-10.32H7c.1 5.08 2.33'
        " 7.22 4.11 7.67V7.18h2.84v4.1c1.76-.19 3.61-2.17 4.23-4.1h2.82c-.47"
        " 2.41-2.43 4.39-3.82 5.2 1.39.65 3.6 2.4 4.43 5.12h-3.1c-.65-2.02"
        ' -2.27-3.59-4.35-3.79v3.79h-.01z"/>'
        "</svg></a>"
    )


def _build_webstore_device_rules_note(tariffs: list[dict] | None = None) -> str:


    marzban_tariffs = [t["label"] for t in (tariffs or get_store_tariffs()) if t.get("provider") == "marzban"]
    if not marzban_tariffs:
        return "Дополнительные устройства сейчас отдельно не подключаются."
    labels = ", ".join(marzban_tariffs)
    return f"Дополнительные устройства можно докупить только для тарифов на наших серверах: {labels}."


def _is_darimiru_store() -> bool:
    return urlparse(settings.subscription_base_url).hostname == "darimiru.ru"


def _is_anewka_store() -> bool:
    return urlparse(settings.subscription_base_url).hostname == "loonapie.xyz"


def _build_tariff_explanation_html(tariffs: list[dict] | None = None) -> str:
    if _is_anewka_store():
        return (
            '<div class="tariff-locations-note">'
            '💰 <strong>Не знаете, что выбрать?</strong><br><br>'
            '<strong>Пробный 7 дней</strong> — если хотите просто проверить сервис.<br>'
            '<strong>Стандарт 1 месяц</strong> — если нужен постоянный доступ и выгоднее по цене.<br><br>'
            'Оба тарифа дают ускоритель интернета на <strong>3 устройства</strong>.'
            '</div>'
            '<div class="tariff-locations-note">'
            '<strong>🌍 Доступные локации:</strong><br>'
            '🇳🇱 Нидерланды 1  •  🇳🇱 Нидерланды 2  •  🇩🇪 Германия<br>'
            '<span>Переключайтесь в приложении в любое время.</span>'
            '</div>'
        )
    if not _is_darimiru_store():
        return (
            '<div class="tariff-locations-note">'
            '<strong>🖥 Во всех тарифах уже включено 3 устройства.</strong><br>'
            f'<span>{_build_webstore_device_rules_note(tariffs)}</span>'
            '</div>'
            '<div class="tariff-locations-note">'
            '<strong>🌍 Доступные локации:</strong><br>'
            '🇳🇱 Нидерланды 1  •  🇳🇱 Нидерланды 2  •  🇩🇪 Германия  •  и другие<br>'
            '<span>Переключайтесь в приложении в любое время.</span>'
            '</div>'
        )

    return ""


# ── Pages ──


async def handle_store_page(request: web.Request) -> web.Response:
    config = await _fetch_public_balance_config()
    daily_charge_rub = (config or {}).get("daily_charge_rub")
    tariffs = get_store_tariffs()
    html = _render(
        "store.html",
        tariffs=tariffs,
        CHANNEL_URL=settings.channel_url,
        VK_LINK_HTML=_build_vk_link_html(),
        VK_FOOTER_HTML=_build_vk_link_html(footer=True),
        BOT_URL=settings.bot_url,
        SUPPORT_URL="/support",
        DAILY_CHARGE_RUB=f"{daily_charge_rub:.2f}" if daily_charge_rub is not None else "3.17",
        DEVICE_RULES_NOTE=_build_webstore_device_rules_note(tariffs),
        TARIFF_EXPLANATION_HTML=_build_tariff_explanation_html(tariffs),
        MIN_PURCHASE_PRICE_RUB=MIN_PURCHASE_PRICE_RUB,
    )
    return web.Response(text=html, content_type="text/html")


async def handle_success_page(request: web.Request) -> web.Response:
    order_id = request.query.get("order_id", "")
    if not order_id:
        raise web.HTTPFound("/")

    html = (TEMPLATES_DIR / "success.html").read_text(encoding="utf-8")
    html = html.replace("{{ORDER_ID}}", order_id)
    html = html.replace("{{SUPPORT_URL}}", "/support")
    return web.Response(text=html, content_type="text/html")


# ── API ──


async def _quote_adapt_purchase(
    session, *, current_plan_uuid, current_tariff, tariff, expires_at, is_trial=False, now=None,
) -> tuple[str, int]:
    from bot.services.purchase_intent import get_paid_adapt_upgrade_plans
    from bot.services.subscription_semantics import has_adapt_plan_metadata, is_adapt_customer_upgrade_eligible

    target_uuid = str(tariff.get("adapt_plan_uuid") or "").strip()
    target_meta = get_adapt_plan_metadata_by_uuid(target_uuid) or tariff
    if not target_uuid or not has_adapt_plan_metadata(target_meta):
        raise ValueError("Выбранный тариф недоступен для этой операции. Обратитесь в поддержку.")
    full_price = int(tariff["price_rub"])
    if full_price <= 0 or target_uuid == settings.adapt_demo_plan_uuid:
        raise ValueError("Выбранный тариф недоступен для этой операции. Обратитесь в поддержку.")
    if is_trial:
        return "upgrade", full_price
    if current_plan_uuid == target_uuid:
        return "renew", full_price
    if current_tariff is None:
        raise ValueError("Для этой подписки продление или улучшение через сайт недоступно. Обратитесь в поддержку.")
    provider_plans = await get_paid_adapt_upgrade_plans(session, current_tariff, target_meta)
    current_time = now or datetime.utcnow()
    if not provider_plans or not is_adapt_customer_upgrade_eligible(
        provider_plans[0], provider_plans[1], expires_at=expires_at, now=current_time,
    ):
        raise ValueError("Улучшение доступно только на более дорогой тариф")
    if not expires_at or expires_at <= current_time:
        return "upgrade", full_price
    price = calculate_upgrade_price_rub(
        current_price_rub=float(current_tariff.get("price_rub") or full_price),
        current_days=int(current_tariff.get("days") or tariff["days"]),
        new_price_rub=float(full_price), expires_at=expires_at, now=current_time,
    )
    if price <= 0:
        raise ValueError("Переход на этот тариф сейчас невозможен")
    return "upgrade", max(MIN_PURCHASE_PRICE_RUB, price)


_TRUSTED_PAYMENT_DOMAINS = frozenset({"darimiru.ru", "darimiru.xyz"})


def _resolve_payment_base_url(request: web.Request) -> str:
    """Resolve trusted base URL for post-payment return URLs and redirects.

    Strictly validates request Origin, Referer, and Host against an allowlist
    of trusted production domains (darimiru.ru, darimiru.xyz) and configured
    settings. Never trusts arbitrary host headers.
    """
    configured_base = settings.subscription_base_url.rstrip("/")

    candidates = [
        request.headers.get("Origin", "").strip(),
        request.headers.get("Referer", "").strip(),
        request.headers.get("X-Forwarded-Host", "").strip(),
        request.headers.get("Host", "").strip(),
    ]

    for candidate in candidates:
        if not candidate:
            continue
        try:
            candidate_url = candidate if "://" in candidate else f"https://{candidate}"
            parsed = urlparse(candidate_url)
            hostname = (parsed.hostname or "").lower()
            port = parsed.port
            scheme = parsed.scheme or "https"

            for trusted in _TRUSTED_PAYMENT_DOMAINS:
                if hostname == trusted or hostname.endswith(f".{trusted}"):
                    return f"https://{hostname}"

            parsed_cfg = urlparse(configured_base)
            if hostname and hostname == (parsed_cfg.hostname or "").lower():
                return configured_base

            if hostname in {"127.0.0.1", "localhost", "testserver"}:
                port_str = f":{port}" if port and port not in (80, 443) else ""
                return f"{scheme}://{hostname}{port_str}"
        except Exception:
            continue

    return configured_base


def _resolve_referral_base_url(request: web.Request | None = None) -> str:
    """Resolve base URL for public referral links (webstore profile, etc.).

    Defaults to settings.public_webstore_base_url ('https://darimiru.xyz').
    """
    configured = getattr(settings, "public_webstore_base_url", "").strip()
    if configured:
        return configured.rstrip("/")
    return "https://darimiru.xyz"


async def handle_create_order(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    tariff_key = body.get("tariff_key", "")
    contact_raw = body.get("contact", "").strip()
    contact = _normalize_contact(contact_raw) if contact_raw else None
    email = _extract_receipt_email(contact) if contact else None
    ref_source = body.get("ref", "").strip() or None
    use_balance = bool(body.get("use_balance"))
    purchase_action = str(body.get("purchase_action") or "new").strip().lower()
    target_order_id = str(body.get("target_order_id") or "").strip() or None
    entry_referrer = (body.get("entry_referrer") or "").strip()[:512] or None
    entry_url = (body.get("entry_url") or "").strip()[:512] or None

    tariff = get_store_tariffs_by_key().get(tariff_key)
    if not tariff:
        return web.json_response({"error": "Unknown tariff"}, status=400)
    if purchase_action not in {"new", "renew", "upgrade"}:
        return web.json_response({"error": "Неизвестное действие с подпиской"}, status=400)
    if purchase_action != "new" and not target_order_id:
        return web.json_response({"error": "Сначала выберите подписку в профиле"}, status=400)

    is_quick_renew = bool(
        target_order_id
        and (
            target_order_id.startswith("uuid:")
            or target_order_id.startswith("adapt:")
            or (not target_order_id.startswith("tg:") and _extract_adapt_uuid(target_order_id) is not None)
        )
    )

    saved_profile_token = await _authenticated_profile(request)
    linked_profile = None
    web_account = None
    confirmed_email = None
    if saved_profile_token:
        async with async_session() as session:
            linked_profile = await session.get(WebProfileLink, saved_profile_token)
            web_account = await session.scalar(
                select(WebAccount).where(WebAccount.profile_token == saved_profile_token).limit(1)
            )
            if web_account:
                confirmed_email = await session.scalar(select(WebEmail.email).where(WebEmail.profile_token == saved_profile_token))

    if not saved_profile_token and not contact:
        return web.json_response(
            {"error": "Сначала зарегистрируйтесь или войдите, чтобы ключ сохранился в личном кабинете.", "auth_required": True},
            status=401,
        )

    if web_account:
        profile_token = web_account.profile_token
        contact = confirmed_email or _normalize_contact(web_account.contact)
        email = _extract_receipt_email(contact)
    elif saved_profile_token:
        profile_token = saved_profile_token
    else:
        profile_token = _make_profile_token(contact) if contact else ""
        email = _extract_receipt_email(contact) if contact else None

    if not contact and linked_profile:
        contact = _normalize_contact(linked_profile.contact) if linked_profile.contact else None
        email = _extract_receipt_email(contact) if contact else None

    if not profile_token:
        return web.json_response(
            {"error": "Введите email для отправки чека."},
            status=400,
        )

    isolate_unauthenticated_guest_token = False
    detach_quick_renew_profile = False
    if saved_profile_token != profile_token:
        if use_balance:
            return web.json_response({"error": "Войдите в профиль", "auth_required": True}, status=401)
        async with async_session() as session:
            protected = bool(
                await session.scalar(select(WebAccount.id).where(WebAccount.profile_token == profile_token).limit(1))
                or await session.get(WebProfileLink, profile_token)
            )
            existing_guest_history = False
            if not protected:
                existing_order = await session.scalar(
                    select(WebOrder.id)
                    .where(
                        WebOrder.profile_token == profile_token,
                        WebOrder.status.in_(("delivered", "paid", "demo")),
                    )
                    .limit(1)
                )
                existing_balance = await session.get(WebBalanceAccount, profile_token)
                existing_guest_history = bool(
                    existing_order is not None
                    or (existing_balance is not None and int(existing_balance.balance_rub or 0) > 0)
                )
        if protected:
            if not is_quick_renew:
                return web.json_response({"error": "Войдите в профиль", "auth_required": True}, status=401)
            detach_quick_renew_profile = True
        elif existing_guest_history:
            if is_quick_renew:
                detach_quick_renew_profile = True
            else:
                isolate_unauthenticated_guest_token = True

    if _is_intro_basic_store_tariff(tariff_key) and not linked_profile:
        return web.json_response(
            {
                "error": (
                    "Тестовый тариф «Базовый (1 день)» доступен только после входа через Telegram. "
                    "Так мы можем выдать его одному пользователю только один раз."
                )
            },
            status=400,
        )

    if not settings.yookassa_enabled:
        return web.json_response(
            {"error": "Оплата временно недоступна. Попробуйте позже."},
            status=503,
        )

    order_id = uuid.uuid4().hex[:16]
    client_ip = _get_client_ip(request)
    if detach_quick_renew_profile:
        profile_token = None
    elif isolate_unauthenticated_guest_token:
        profile_token = f"guest_{order_id}"
    else:
        profile_token = profile_token or (_make_profile_token(contact) if contact else saved_profile_token)
    original_amount_rub = int(tariff["price_rub"])
    bonus_applied_rub = 0

    # Save order to DB
    async with async_session() as session:
        if saved_profile_token:
            session_linked_profile = await session.get(WebProfileLink, saved_profile_token)
            if session_linked_profile:
                profile_token = saved_profile_token
        target_order = None
        target_telegram_item = None
        target_snapshot_plan_uuid = None
        target_snapshot_expires_at = None
        if profile_token:
            profile_orders = (
                await session.execute(
                    select(WebOrder)
                    .where(WebOrder.profile_token == profile_token)
                    .where(WebOrder.status.in_(("delivered", "demo")))
                    .order_by(WebOrder.id.desc())
                )
            ).scalars().all()

            profile_tg_items = (
                await session.execute(
                    select(WebTelegramItem)
                    .where(WebTelegramItem.profile_token == profile_token)
                    .where(WebTelegramItem.item_type == "vpn")
                    .order_by(WebTelegramItem.id.desc())
                )
            ).scalars().all()

            now = datetime.utcnow()

            subs_by_uuid: dict[str, dict] = {}
            for o in profile_orders:
                u = _extract_adapt_uuid_from_web_order(o) or f"order_{o.id}"
                if u not in subs_by_uuid:
                    is_demo = bool(o.status == "demo" or o.tariff_key == "demo")
                    is_active = bool(
                        o.status == "delivered"
                        and not is_demo
                        and o.access_expires_at
                        and o.access_expires_at > now
                    )
                    subs_by_uuid[u] = {
                        "is_demo": is_demo,
                        "is_active_paid": is_active,
                        "order": o,
                        "tg_item": None,
                    }
            for item in profile_tg_items:
                u = _extract_adapt_uuid(item.key_value, item.external_id) or f"tg_{item.id}"
                if u not in subs_by_uuid:
                    is_demo = bool(str(item.adapt_plan_uuid or "").strip() == settings.adapt_demo_plan_uuid)
                    is_active = bool(
                        item.status == "active"
                        and not is_demo
                        and (not item.expires_at or item.expires_at > now)
                    )
                    subs_by_uuid[u] = {
                        "is_demo": is_demo,
                        "is_active_paid": is_active,
                        "order": None,
                        "tg_item": item,
                    }

            has_active_paid = any(info["is_active_paid"] for info in subs_by_uuid.values())
            distinct_subs = list(subs_by_uuid.values())

            if purchase_action == "new" and not has_active_paid and len(distinct_subs) > 0:
                if len(distinct_subs) == 1 and distinct_subs[0]["is_demo"]:
                    single = distinct_subs[0]
                    if single["order"]:
                        target_order_id = f"order:{single['order'].id}"
                    else:
                        target_order_id = f"tgitem:{single['tg_item'].id}"
                    purchase_action = "upgrade"

        if purchase_action != "new":
            store_tariffs_by_key = get_store_tariffs_by_key()
            old_plan_uuid = ""
            target_expires_at = None
            target_adapt_uuid = None
            target_vhq_spec = None

            if is_quick_renew:
                target_adapt_uuid = _extract_adapt_uuid(target_order_id)
                if not target_adapt_uuid:
                    return web.json_response(
                        {"error": "Не удалось определить ключ подписки"},
                        status=400,
                    )
                target_order_id = f"uuid:{target_adapt_uuid}"
                # 1. Search local known records first (zero Adapt API call)
                local_order = await session.scalar(
                    select(WebOrder)
                    .where(
                        or_(
                            WebOrder.subscription_url.contains(target_adapt_uuid),
                            WebOrder.marzban_username == f"adapt_{target_adapt_uuid}",
                        )
                    )
                    .order_by(WebOrder.id.desc())
                    .limit(1)
                )
                local_tg_item = None
                if not local_order:
                    local_tg_item = await session.scalar(
                        select(WebTelegramItem)
                        .where(
                            or_(
                                WebTelegramItem.key_value.contains(target_adapt_uuid),
                                WebTelegramItem.external_id.contains(target_adapt_uuid),
                            )
                        )
                        .order_by(WebTelegramItem.id.desc())
                        .limit(1)
                    )

                if local_order:
                    target_order = local_order
                    old_plan_uuid = _web_order_current_adapt_plan_uuid(local_order, store_tariffs_by_key)
                    target_expires_at = local_order.access_expires_at
                elif local_tg_item:
                    target_telegram_item = local_tg_item
                    old_plan_uuid = str(local_tg_item.adapt_plan_uuid or "").strip()
                    target_expires_at = local_tg_item.expires_at
                else:
                    # 2. Fallback to Adapt API only for genuinely unknown UUID
                    api = AdaptAPI()
                    try:
                        target_status = await api.get_status(target_adapt_uuid)
                    except Exception as exc:
                        return web.json_response(
                            {"error": f"Не удалось проверить статус подписки у провайдера: {exc}"},
                            status=400,
                        )
                    old_plan_uuid = str(target_status.get("plan_uuid") or "").strip()
                    target_expires_at = (
                        _adapt_expires_at_from_response(target_status, 0)
                        if target_status.get("end_date")
                        else None
                    )
            elif target_order_id.startswith("tgitem:"):
                raw_item_id = target_order_id.removeprefix("tgitem:").strip()
                if raw_item_id.isdigit():
                    target_telegram_item = await session.scalar(
                        select(WebTelegramItem)
                        .where(WebTelegramItem.id == int(raw_item_id))
                        .where(WebTelegramItem.profile_token == profile_token)
                        .where(WebTelegramItem.item_type == "vpn")
                    )
                    if target_telegram_item:
                        target_adapt_uuid = _extract_adapt_uuid(
                            target_telegram_item.key_value,
                            target_telegram_item.external_id,
                        )
                        old_plan_uuid = str(target_telegram_item.adapt_plan_uuid or "").strip()
                        target_expires_at = target_telegram_item.expires_at
                        target_order_id = f"uuid:{target_adapt_uuid}" if target_adapt_uuid else target_order_id
            elif target_order_id.startswith("tg:"):
                requested_adapt_uuid = target_order_id.removeprefix("tg:")
                telegram_items_result = await session.execute(
                    select(WebTelegramItem)
                    .where(WebTelegramItem.profile_token == profile_token)
                    .where(WebTelegramItem.item_type == "vpn")
                )
                target_telegram_item = next(
                    (
                        item for item in telegram_items_result.scalars().all()
                        if _extract_adapt_uuid(item.key_value, item.external_id) == requested_adapt_uuid
                    ),
                    None,
                )
                if target_telegram_item:
                    target_adapt_uuid = _extract_adapt_uuid(
                        target_telegram_item.key_value,
                        target_telegram_item.external_id,
                    )
                    old_plan_uuid = str(target_telegram_item.adapt_plan_uuid or "").strip()
                    target_expires_at = target_telegram_item.expires_at
                    target_order_id = f"uuid:{target_adapt_uuid}" if target_adapt_uuid else target_order_id
            else:
                raw_order_id = target_order_id.removeprefix("order:").strip()
                target_order = await session.scalar(
                    select(WebOrder)
                    .where(
                        or_(
                            WebOrder.order_id == raw_order_id,
                            WebOrder.id == int(raw_order_id) if raw_order_id.isdigit() else False,
                        )
                    )
                    .where(WebOrder.profile_token == profile_token)
                    .where(WebOrder.status.in_(("delivered", "demo", "paid")))
                )
                if target_order:
                    target_vhq_spec = (
                        get_vhq_spec_for_store_tariff(store_tariffs_by_key.get(target_order.tariff_key))
                        or ({"tier": "basic", "days": 30} if target_order.vhq_order_id else None)
                    )
                    if target_vhq_spec:
                        new_vhq_spec = get_vhq_spec_for_store_tariff(tariff)
                        if (
                            not new_vhq_spec
                            or str(new_vhq_spec.get("tier")) != "basic"
                            or int(new_vhq_spec.get("days") or 0) != 30
                        ):
                            return web.json_response(
                                {"error": "Для подписки База Про доступно только продление на 30 дней."},
                                status=400,
                            )
                        purchase_action = "renew"
                        original_amount_rub = int(tariff["price_rub"])
                        target_expires_at = target_order.access_expires_at
                        target_order_id = target_order.order_id
                        target_snapshot_expires_at = target_expires_at
                        target_snapshot_plan_uuid = target_order.tariff_key
                        target_snapshot_is_frozen = False
                    else:
                        target_adapt_uuid = _extract_adapt_uuid_from_web_order(target_order)
                        if target_adapt_uuid:
                            latest_local_order = await session.scalar(
                                select(WebOrder)
                                .where(WebOrder.profile_token == profile_token)
                                .where(WebOrder.status.in_(("delivered", "demo")))
                                .where(
                                    or_(
                                        WebOrder.subscription_url.contains(target_adapt_uuid),
                                        WebOrder.marzban_username == f"adapt_{target_adapt_uuid}",
                                    )
                                )
                                .order_by(WebOrder.id.desc())
                                .limit(1)
                            )
                            if latest_local_order:
                                target_order = latest_local_order

                        old_plan_uuid = _web_order_current_adapt_plan_uuid(target_order, store_tariffs_by_key)
                        target_expires_at = target_order.access_expires_at
                        target_order_id = f"uuid:{target_adapt_uuid}" if target_adapt_uuid else target_order_id

            if not target_vhq_spec:
                if not target_adapt_uuid:
                    return web.json_response(
                        {"error": "Выбранная подписка не найдена или не поддерживает эту операцию"},
                        status=400,
                    )

                new_plan_uuid = str(tariff.get("adapt_plan_uuid") or "").strip()
                if not new_plan_uuid:
                    return web.json_response(
                        {"error": "Для этой подписки выберите подходящий тариф в списке"},
                        status=400,
                    )

                has_canonical_snapshot = bool(
                    target_order and str(getattr(target_order, "target_snapshot_plan_uuid", None) or "").strip()
                )
                is_demo = bool(
                    old_plan_uuid == settings.adapt_demo_plan_uuid
                    or (
                        target_order
                        and (
                            target_order.status == "demo"
                            or target_order.tariff_key == "demo"
                        )
                    )
                )
                old_plan_meta = get_adapt_plan_metadata_by_uuid(old_plan_uuid)
                if not old_plan_meta and not has_canonical_snapshot:
                    old_plan_meta = get_adapt_plan_metadata_by_key(
                        target_order.tariff_key if target_order else ""
                    )
                if not old_plan_meta and not has_canonical_snapshot:
                    old_plan_meta = next(
                        (
                            candidate
                            for candidate in get_store_tariffs()
                            if str(candidate.get("adapt_plan_uuid") or "").strip() == old_plan_uuid
                        ),
                        None,
                    )

                try:
                    purchase_action, original_amount_rub = await _quote_adapt_purchase(
                        session, current_plan_uuid=old_plan_uuid, current_tariff=old_plan_meta,
                        tariff=tariff, expires_at=target_expires_at, is_trial=is_demo,
                    )
                except ValueError as exc:
                    return web.json_response({"error": str(exc)}, status=400)

                if original_amount_rub <= 0:
                    return web.json_response(
                        {"error": "Переход на этот тариф сейчас невозможен"},
                        status=400,
                    )

                target_snapshot_plan_uuid = old_plan_uuid
                target_snapshot_expires_at = target_expires_at
                target_snapshot_is_frozen = bool(
                    (target_order and (target_order.status == "frozen" or getattr(target_order, "target_snapshot_is_frozen", False)))
                    or (target_telegram_item and getattr(target_telegram_item, "is_frozen", False))
                )

        if _is_intro_basic_store_tariff(tariff_key):
            existing_intro_order = await session.scalar(
                select(WebOrder.id)
                .where(WebOrder.profile_token == profile_token)
                .where(WebOrder.tariff_key == tariff_key)
                .where(WebOrder.status.in_(("pending", "paid", "delivered")))
                .limit(1)
            )
            if existing_intro_order is not None:
                return web.json_response(
                    {
                        "error": (
                            "Тестовый тариф «Базовый (1 день)» можно оформить только один раз на пользователя."
                        )
                    },
                    status=400,
                )
        balance_account = (
            await _get_or_create_web_balance_account(session, profile_token, contact)
            if profile_token
            else None
        )
        if use_balance and balance_account and balance_account.balance_rub > 0:
            bonus_applied_rub = min(int(balance_account.balance_rub), original_amount_rub)
        order = WebOrder(
            order_id=order_id,
            contact=contact,
            email=email,
            tariff_key=tariff_key,
            tariff_label=tariff["label"],
            days=tariff["days"],
            amount_rub=max(0, original_amount_rub - bonus_applied_rub),
            original_amount_rub=original_amount_rub,
            bonus_applied_rub=bonus_applied_rub,
            status="pending",
            ip_address=client_ip,
            ref_source=ref_source,
            referral_status="ready" if ref_source else None,
            profile_token=profile_token,
            purchase_action=purchase_action,
            target_order_id=target_order_id,
            target_snapshot_plan_uuid=target_snapshot_plan_uuid,
            target_snapshot_expires_at=target_snapshot_expires_at,
            target_snapshot_is_frozen=target_snapshot_is_frozen if purchase_action != "new" else None,
            entry_referrer=entry_referrer,
            entry_url=entry_url,
        )
        session.add(order)
        await session.commit()
    _web_plog(
        "WEB_ЗАКАЗ",
        order_id=order_id,
        tariff=tariff["label"],
        amount=original_amount_rub - bonus_applied_rub,
        contact=contact or "—",
    )

    if original_amount_rub - bonus_applied_rub <= 0:
        async with async_session() as session:
            result = await session.execute(
                select(WebOrder).where(WebOrder.order_id == order_id)
            )
            db_order = result.scalar_one_or_none()
            if not db_order:
                return web.json_response({"error": "Заказ не найден"}, status=404)
            db_order.status = "paid"
            db_order.paid_at = datetime.utcnow()
            _web_plog(
                "WEB_ОПЛАТА",
                order_id=db_order.order_id,
                tariff=db_order.tariff_label,
                amount=db_order.amount_rub,
                method="web_bonus",
            )
            await _notify_admins_webstore(db_order, "paid")
            await _fulfill_order(session, db_order)
            is_delivered = db_order.status == "delivered"
            if is_delivered:
                _sync_bot_db_subscription_on_fulfillment(db_order)
                await _disable_web_balance_autodebit_after_tariff_purchase(session, db_order)
                await _apply_order_bonus_spend(session, db_order)
            await session.commit()
            if is_delivered:
                bot_result = await _notify_bot_order_delivered(db_order, session=session)
                await _finalize_order_cashback(session, db_order, bot_result)
                await session.commit()
                await _notify_admins_webstore(db_order, "delivered")
                if not bot_result.get("delivered"):
                    await _notify_linked_webstore_user(db_order, "delivered")
                await _sync_referral_credit_for_order(db_order.order_id)
        payment_base = _resolve_payment_base_url(request)
        order_resp = web.json_response({
            "redirect_url": f"{payment_base}/pay/success?order_id={order_id}",
            "order_id": order_id,
        })
        return order_resp

    # Create YooKassa payment
    try:
        import yookassa

        yookassa.Configuration.account_id = settings.yookassa_shop_id
        yookassa.Configuration.secret_key = settings.yookassa_secret_key

        price = original_amount_rub - bonus_applied_rub
        payment_base = _resolve_payment_base_url(request)
        return_url = f"{payment_base}/pay/success?order_id={order_id}"

        def _create():
            params = {
                "amount": {"value": f"{price:.2f}", "currency": "RUB"},
                "confirmation": {"type": "redirect", "return_url": return_url},
                "capture": True,
                "description": f"{settings.site_name} — {tariff['label']}",
                "metadata": {"order_id": order_id, "source": "webstore"},
            }
            if email:
                params["receipt"] = {
                    "customer": {"email": email},
                    "items": [
                        {
                            "description": f"{settings.site_name} {tariff['label']}",
                            "quantity": "1.00",
                            "amount": {"value": f"{price:.2f}", "currency": "RUB"},
                            "vat_code": 1,
                        }
                    ],
                }
            return yookassa.Payment.create(params, order_id)

        payment = await asyncio.to_thread(_create)

        # Save payment ID
        async with async_session() as session:
            result = await session.execute(
                select(WebOrder).where(WebOrder.order_id == order_id)
            )
            db_order = result.scalar_one()
            db_order.yookassa_payment_id = payment.id
            await session.commit()

        order_resp = web.json_response({
            "redirect_url": payment.confirmation.confirmation_url,
            "order_id": order_id,
        })
        return order_resp

    except Exception as e:
        logger.error("Failed to create YooKassa payment: %s", e)
        return web.json_response(
            {"error": "Ошибка создания платежа. Попробуйте позже."},
            status=500,
        )


async def handle_create_device_order(request: web.Request) -> web.Response:
    """Buy an extra device slot for the current profile's active subscription."""
    profile_token = (await _authenticated_profile(request))
    if not profile_token:
        return web.json_response({"error": "Сначала откройте профиль"}, status=400)

    if not settings.yookassa_enabled:
        return web.json_response({"error": "Оплата временно недоступна"}, status=503)

    now = datetime.utcnow()
    expires_at: datetime | None = None
    contact: str | None = None

    async with async_session() as session:
        # First check webstore orders (direct web purchase)
        primary = await _get_latest_web_access_order(session, profile_token)
        if primary and primary.access_expires_at and primary.access_expires_at > now:
            if _get_order_provider(primary) != "marzban":
                return web.json_response(
                    {"error": "Для этой подписки лимит устройств меняется через «Улучшить тариф»."},
                    status=400,
                )
            expires_at = primary.access_expires_at
            contact = primary.contact

        # Fallback: check Telegram-linked subscriptions (bot purchases)
        if not expires_at:
            tg_result = await session.execute(
                select(WebTelegramItem)
                .where(WebTelegramItem.profile_token == profile_token)
                .where(WebTelegramItem.item_type == "vpn")
                .where(WebTelegramItem.status == "active")
                .where(WebTelegramItem.expires_at > now)
                .order_by(WebTelegramItem.expires_at.desc())
                .limit(1)
            )
            tg_item = tg_result.scalar_one_or_none()
            if tg_item:
                if _get_access_provider(tg_item.key_value, tg_item.external_id) != "marzban":
                    return web.json_response(
                        {"error": "Для этой подписки лимит устройств меняется через «Улучшить тариф»."},
                        status=400,
                    )
                expires_at = tg_item.expires_at

        if not expires_at:
            return web.json_response(
                {"error": "Активная подписка не найдена. Сначала купите доступ."},
                status=400,
            )

        # Count existing delivered device_slot orders
        dev_count_result = await session.execute(
            select(WebOrder)
            .where(WebOrder.profile_token == profile_token)
            .where(WebOrder.tariff_key == "device_slot")
            .where(WebOrder.status == "delivered")
        )
        dev_count = len(dev_count_result.scalars().all())
        if dev_count >= settings.extra_device_max:
            return web.json_response(
                {"error": f"Достигнут лимит дополнительных устройств ({settings.extra_device_max})."},
                status=400,
            )

        confirmed_email = await session.scalar(select(WebEmail.email).where(WebEmail.profile_token == profile_token))
        contact = confirmed_email or contact
        email = _extract_receipt_email(contact) if contact else None
        order_id = uuid.uuid4().hex[:16]
        price = settings.extra_device_price_rub

        order = WebOrder(
            order_id=order_id,
            contact=contact,
            email=email,
            tariff_key="device_slot",
            tariff_label="Доп. устройство",
            days=0,
            amount_rub=price,
            original_amount_rub=price,
            bonus_applied_rub=0,
            status="pending",
            ip_address=_get_client_ip(request),
            profile_token=profile_token,
            access_expires_at=expires_at,
        )
        session.add(order)
        await session.commit()

    try:
        import yookassa

        yookassa.Configuration.account_id = settings.yookassa_shop_id
        yookassa.Configuration.secret_key = settings.yookassa_secret_key
        payment_base = _resolve_payment_base_url(request)
        return_url = f"{payment_base}/profile?order_id={order_id}"

        def _create():
            return yookassa.Payment.create(
                {
                    "amount": {"value": f"{price:.2f}", "currency": "RUB"},
                    "confirmation": {"type": "redirect", "return_url": return_url},
                    "capture": True,
                    "description": "Дополнительное устройство",
                    "receipt": {"customer": {"email": email} if email else {"phone": "79000000000"}, "items": [{"description": "Дополнительное устройство", "quantity": "1.00", "amount": {"value": f"{price:.2f}", "currency": "RUB"}, "vat_code": 1}]},
                    "metadata": {
                        "source": "webstore",
                        "order_id": order_id,
                    },
                },
                order_id,
            )

        payment = await asyncio.to_thread(_create)
    except Exception as exc:
        logger.error("Failed to create device payment: %s", exc)
        return web.json_response({"error": "Ошибка создания платежа. Попробуйте позже."}, status=500)

    async with async_session() as session:
        result = await session.execute(select(WebOrder).where(WebOrder.order_id == order_id))
        db_order = result.scalar_one_or_none()
        if db_order:
            db_order.yookassa_payment_id = payment.id
            await session.commit()

    return web.json_response({
        "redirect_url": payment.confirmation.confirmation_url,
        "order_id": order_id,
    })


async def handle_create_balance_topup(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    try:
        amount_rub = int(body.get("amount_rub") or 0)
    except (TypeError, ValueError):
        amount_rub = 0
    if amount_rub < 10 or amount_rub > 50000:
        return web.json_response({"error": "Введите сумму от 10 до 50 000 ₽"}, status=400)

    authenticated = (await _authenticated_profile(request)).strip()
    requested_token = str(body.get("token") or "").strip()
    if not authenticated:
        return web.json_response(
            {"error": "Сначала зарегистрируйтесь или войдите, чтобы пополнение сохранилось в личном кабинете.", "auth_required": True},
            status=403 if _has_raw_auth_cookie(request) else 401,
        )
    if requested_token and requested_token != authenticated:
        return web.json_response({"error": "Войдите в профиль", "auth_required": True}, status=403)
    profile_token = authenticated

    async with async_session() as session:
        link = await session.get(WebProfileLink, profile_token)
        account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == profile_token))
        has_order = await session.scalar(select(WebOrder.id).where(WebOrder.profile_token == profile_token).limit(1))
        if not account and not link and not has_order:
            return web.json_response({"error": "Профиль не найден", "auth_required": True}, status=404)
        topup_id = uuid.uuid4().hex[:16]
        topup = WebBalanceTopUp(
            topup_id=topup_id,
            profile_token=profile_token,
            telegram_id=int(link.telegram_id) if link and link.telegram_id else 0,
            amount_rub=amount_rub,
            status="pending",
        )
        session.add(topup)
        await session.commit()

    try:
        import yookassa

        yookassa.Configuration.account_id = settings.yookassa_shop_id
        yookassa.Configuration.secret_key = settings.yookassa_secret_key
        payment_base = _resolve_payment_base_url(request)
        return_url = f"{payment_base}/profile?topup_id={topup_id}"

        def _create():
            return yookassa.Payment.create(
                {
                    "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
                    "confirmation": {"type": "redirect", "return_url": return_url},
                    "capture": True,
                    "description": "Пополнение баланса",
                    "metadata": {
                        "source": "webstore_balance_topup",
                        "topup_id": topup_id,
                        "telegram_id": str(link.telegram_id) if link and link.telegram_id else "",
                    },
                },
                topup_id,
            )

        payment = await asyncio.to_thread(_create)
    except Exception as exc:
        logger.error("Failed to create balance topup payment: %s", exc)
        return web.json_response({"error": "Ошибка создания платежа"}, status=500)

    async with async_session() as session:
        result = await session.execute(
            select(WebBalanceTopUp).where(WebBalanceTopUp.topup_id == topup_id)
        )
        topup = result.scalar_one_or_none()
        if topup:
            topup.yookassa_payment_id = payment.id
            await session.commit()

    return web.json_response({
        "redirect_url": payment.confirmation.confirmation_url,
        "topup_id": topup_id,
    })


async def handle_order_status(request: web.Request) -> web.Response:
    order_id = request.query.get("order_id", "")
    if not order_id:
        return web.json_response({"error": "Missing order_id"}, status=400)

    authenticated = await _authenticated_profile(request)
    should_retry_referral = False
    mint_guest_checkout_session = False
    async with async_session() as session:
        result = await session.execute(
            select(WebOrder).where(WebOrder.order_id == order_id)
        )
        order = result.scalar_one_or_none()
        if not order:
            return web.json_response({"error": "Order not found"}, status=404)

        if order.status == "pending" and order.yookassa_payment_id and settings.yookassa_enabled:
            try:
                payment = await fetch_yookassa_payment(order.yookassa_payment_id)
                if payment.status == "canceled":
                    order.status = "canceled"
                    order.cancellation_confirmed = True
                    order.last_reconciled_at = datetime.utcnow()
                    await session.commit()
                elif payment.status == "succeeded" and payment.paid:
                    await reconcile_yookassa_web_order(order.order_id, order.yookassa_payment_id, source="order_status_poll")
                    await session.refresh(order)
            except Exception as e:
                logger.warning("Failed to refresh YooKassa payment status for %s: %s", order_id, e)

        should_retry_referral = (
            order.status == "delivered"
            and bool(order.ref_source)
            and order.referral_status in {None, "ready", "failed"}
        )

        is_delivered = order.status == "delivered"
        exposed_profile_token = None
        if is_delivered and order.profile_token:
            if authenticated == order.profile_token:
                exposed_profile_token = order.profile_token
            elif not authenticated and hasattr(session, "scalar") and hasattr(session, "get"):
                has_protected = bool(
                    await session.scalar(
                        select(WebAccount.id).where(WebAccount.profile_token == order.profile_token).limit(1)
                    )
                    or await session.get(WebProfileLink, order.profile_token)
                )
                has_other_orders = await session.scalar(
                    select(WebOrder.id)
                    .where(
                        WebOrder.profile_token == order.profile_token,
                        WebOrder.id != order.id,
                        WebOrder.status.in_(("delivered", "paid", "demo")),
                    )
                    .limit(1)
                )
                if not has_protected and has_other_orders is None:
                    exposed_profile_token = order.profile_token
                    mint_guest_checkout_session = True

        response_data = {
            "status": order.status,
            "subscription_url": (_display_order_subscription_url(order) or order.subscription_url) if is_delivered else None,
            "tariff_label": order.tariff_label,
            "days": order.days,
            "profile_token": exposed_profile_token,
            "failure_message": order.failure_message,
        }

    if should_retry_referral:
        await _sync_referral_credit_for_order(order_id)

    if mint_guest_checkout_session and response_data["profile_token"]:
        return await _auth_response(response_data, response_data["profile_token"], method="checkout")

    return web.json_response(response_data)


async def handle_internal_adapt_event(request: web.Request) -> web.Response:
    """Reconcile a web Adapt create from the provider webhook."""
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)
    try:
        payload = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    event = str(payload.get("event") or "").strip()
    data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
    external_user_id = str(data.get("external_user_id") or "").strip()
    if event != "subs.created" or not external_user_id.startswith("web_"):
        return web.json_response({"ok": True, "ignored": True})

    order_id = external_user_id.removeprefix("web_")
    adapt_uuid = str(data.get("subscription_uuid") or "").strip()
    if not order_id or not adapt_uuid:
        return web.json_response({"error": "Missing identifiers"}, status=400)

    async with async_session() as session:
        order = await session.scalar(select(WebOrder).where(WebOrder.order_id == order_id))
        if not order:
            logger.error("Adapt reconciliation order not found order_id=%s uuid=%s", order_id, adapt_uuid)
            return web.json_response({"ok": True, "missing": True})
        if order.status == "delivered":
            return web.json_response({"ok": True, "already_delivered": True})
        if not order.paid_at:
            logger.error("Refusing Adapt reconciliation for unpaid order order_id=%s", order_id)
            return web.json_response({"error": "Order is not paid"}, status=409)

        order.marzban_username = f"adapt_{adapt_uuid}"
        order.subscription_url = build_adapt_mirror_url(adapt_uuid)
        order.status = "delivered"
        order.failure_code = None
        order.failure_message = None
        order.failure_reason = None
        order.next_fulfillment_retry_at = None
        order.delivered_at = datetime.utcnow()
        order.access_expires_at = _adapt_expires_at_from_response(data, order.days)
        await _disable_web_balance_autodebit_after_tariff_purchase(session, order)
        await _apply_order_bonus_spend(session, order)
        await session.commit()

    _sync_bot_db_subscription_on_fulfillment(order)
    _web_plog("WEB_ВЫДАЧА_СВЕРКА", order_id=order_id, tariff=order.tariff_label, provider="adapt")
    bot_result = await _notify_bot_order_delivered(order)
    async with async_session() as session:
        db_order = await session.get(WebOrder, order.id)
        if db_order:
            await _finalize_order_cashback(session, db_order, bot_result)
            await session.commit()
    await _notify_admins_webstore(order, "delivered")
    if not bot_result.get("delivered"):
        await _notify_linked_webstore_user(order, "delivered")
    await _sync_referral_credit_for_order(order_id)
    return web.json_response({"ok": True, "delivered": True})


def _sync_bot_db_subscription_on_fulfillment(order: WebOrder) -> None:
    """Sync fulfillment result to bot DB if this order corresponds to a bot subscription."""
    from webstore.config import _bot_db_path
    path = _bot_db_path()
    if not path or not path.exists():
        return

    import sqlite3
    import re
    try:
        with sqlite3.connect(str(path), timeout=10.0) as con:
            con.execute("PRAGMA busy_timeout=5000")
            adapt_uuid = _get_order_adapt_uuid(order)

            target_sub_id = None
            if order.target_order_id and order.target_order_id.startswith("sub_"):
                try:
                    target_sub_id = int(order.target_order_id.removeprefix("sub_"))
                except ValueError:
                    pass

            sub_row = None
            if adapt_uuid:
                # Try by adapt_subscriptions table
                try:
                    sub_row = con.execute(
                        """
                        SELECT s.id, s.user_id, s.expires_at, s.status, s.tariff_days, s.billing_mode, s.device_slots
                        FROM subscriptions s
                        JOIN adapt_subscriptions a ON a.subscription_id = s.id
                        WHERE LOWER(a.adapt_uuid) = LOWER(?)
                        LIMIT 1
                        """,
                        (adapt_uuid,)
                    ).fetchone()
                except sqlite3.Error:
                    pass

                # If not found, try by client_name or vpn_key
                if not sub_row:
                    try:
                        sub_row = con.execute(
                            """
                            SELECT id, user_id, expires_at, status, tariff_days, billing_mode, device_slots
                            FROM subscriptions
                            WHERE LOWER(client_name) = LOWER(?) OR vpn_key LIKE ?
                            LIMIT 1
                            """,
                            (f"adapt_{adapt_uuid}", f"%{adapt_uuid}%")
                        ).fetchone()
                    except sqlite3.Error:
                        pass

            if not sub_row and target_sub_id:
                try:
                    sub_row = con.execute(
                        """
                        SELECT id, user_id, expires_at, status, tariff_days, billing_mode, device_slots
                        FROM subscriptions
                        WHERE id = ?
                        LIMIT 1
                        """,
                        (target_sub_id,)
                    ).fetchone()
                except sqlite3.Error:
                    pass

            if not sub_row and order.marzban_username:
                try:
                    sub_row = con.execute(
                        """
                        SELECT id, user_id, expires_at, status, tariff_days, billing_mode, device_slots
                        FROM subscriptions
                        WHERE client_name = ?
                        LIMIT 1
                        """,
                        (order.marzban_username,)
                    ).fetchone()
                except sqlite3.Error:
                    pass

            if sub_row:
                sub_id, user_id, old_expires, old_status, old_days, old_billing, old_slots = sub_row
                expires_iso = None
                if order.access_expires_at:
                    if isinstance(order.access_expires_at, str):
                        expires_iso = order.access_expires_at
                    else:
                        expires_iso = order.access_expires_at.strftime("%Y-%m-%d %H:%M:%S.%f")
                if expires_iso:
                    con.execute(
                        """
                        UPDATE subscriptions
                        SET expires_at = ?,
                            status = 'ACTIVE',
                            billing_mode = CASE WHEN billing_mode = 'demo' THEN 'tariff' ELSE billing_mode END,
                            vpn_key = CASE WHEN vpn_key IS NULL OR vpn_key = '' THEN ? ELSE vpn_key END
                        WHERE id = ?
                        """,
                        (expires_iso, order.subscription_url or "", sub_id)
                    )
                    if adapt_uuid:
                        try:
                            con.execute(
                                """
                                UPDATE adapt_subscriptions
                                SET end_date = ?, is_frozen = 0
                                WHERE subscription_id = ? OR LOWER(adapt_uuid) = LOWER(?)
                                """,
                                (expires_iso, sub_id, adapt_uuid)
                            )
                        except sqlite3.Error:
                            pass
                    con.commit()
                    logger.info(
                        "Synced web order %s to bot subscription %s (user %s): expires_at=%s",
                        order.order_id, sub_id, user_id, expires_iso
                    )
    except Exception as exc:
        logger.warning("Failed to sync web order %s to bot DB: %s", order.order_id, exc)


# ── YooKassa Webhook ──


async def _fulfill_order(session, order: WebOrder) -> None:
    """Create Marzban user and set subscription URL on the order."""
    if order.tariff_key == "device_slot":
        await _fulfill_device_slot_order(session, order)
        return

    tariff = get_store_tariffs_by_key().get(order.tariff_key)
    vhq_spec = get_vhq_spec_for_store_tariff(tariff)
    if vhq_spec:
        await _fulfill_vhq_order(session, order, vhq_spec)
        return

    # Adapt provider
    adapt_plan_uuid = tariff.get("adapt_plan_uuid") if tariff else None
    if adapt_plan_uuid:
        await _fulfill_adapt_order(session, order, adapt_plan_uuid)
        return

    preferred_username: str | None = None
    if order.profile_token:
        primary = await _get_latest_web_access_order(session, order.profile_token)
        if primary and primary.id != order.id:
            now = datetime.utcnow()
            base_expires_at = primary.access_expires_at if primary.access_expires_at and primary.access_expires_at > now else now
            expires_at = base_expires_at + timedelta(days=order.days)
            if await _ensure_web_access_until(primary, expires_at):
                order.marzban_username = primary.marzban_username
                order.subscription_url = primary.subscription_url
                order.status = "delivered"
                order.failure_message = None
                order.failure_reason = None
                order.delivered_at = datetime.utcnow()
                order.access_expires_at = expires_at
                _web_plog(
                    "WEB_ПРОДЛЕНИЕ",
                    order_id=order.order_id,
                    tariff=order.tariff_label,
                    amount=order.amount_rub,
                    status="success",
                    provider="marzban",
                    context=f"reused={primary.order_id} username={primary.marzban_username}",
                )
                return
            # Extension failed (e.g. user was on old Marzban instance). Prefer same username
            # so that if the customer already shared the link, it stays valid after re-creation.
            logger.warning(
                "Could not extend existing Marzban user %s for order %s — will recreate with same username",
                primary.marzban_username,
                order.order_id,
            )
            preferred_username = primary.marzban_username

    username = preferred_username or f"web_{order.order_id}"
    expire_ts = int((datetime.utcnow() + timedelta(days=order.days)).timestamp())

    try:
        async with MarzbanClient() as marzban:
            if int(order.fulfillment_attempts or 0) > 1:
                # The username is deterministic.  If create succeeded but its
                # response was lost, reconcile the existing account instead of
                # issuing another create request.
                existing_url = await marzban.get_subscription_url(username)
                if existing_url:
                    await marzban.update_user(username, expire=expire_ts, status="active")
                    order.marzban_username = username
                    order.subscription_url = existing_url
                    order.status = "delivered"
                    order.delivered_at = datetime.utcnow()
                    order.access_expires_at = datetime.utcfromtimestamp(expire_ts)
                    _web_plog(
                        "WEB_ВЫДАЧА_СВЕРКА",
                        order_id=order.order_id,
                        tariff=order.tariff_label,
                        provider="marzban",
                    )
                    return
            user_data = await marzban.create_user(
                username=username,
                expire=expire_ts,
                note=f"web order {order.order_id}" + (f" | {order.contact}" if order.contact else ""),
            )
            if not user_data:
                issue = build_internal_access_error(
                    provider="marzban",
                    code="marzban_create_failed",
                    admin_message=f"Failed to create Marzban user for order {order.order_id}",
                )
                logger.error(issue.admin_message)
                await _mark_order_failed(order, issue)
                return

            sub_url = await marzban.get_subscription_url(username)
            if not sub_url:
                issue = build_internal_access_error(
                    provider="marzban",
                    code="marzban_missing_url",
                    admin_message=f"Failed to get Marzban subscription URL for order {order.order_id}",
                )
                logger.error(issue.admin_message)
                await _mark_order_failed(order, issue)
                return

            order.marzban_username = username
            order.subscription_url = sub_url
            order.status = "delivered"
            order.failure_message = None
            order.failure_reason = None
            order.delivered_at = datetime.utcnow()
            order.access_expires_at = order.delivered_at + timedelta(days=order.days)
            _web_plog(
                "WEB_ВЫДАЧА",
                order_id=order.order_id,
                tariff=order.tariff_label,
                provider="marzban",
            )
            logger.info("Order %s fulfilled: username=%s", order.order_id, username)

    except Exception as e:
        issue = build_internal_access_error(
            provider="marzban",
            code="marzban_runtime",
            admin_message=f"Error fulfilling Marzban order {order.order_id}: {e}",
            raw_message=str(e),
        )
        logger.error(issue.admin_message)
        await _mark_order_failed(order, issue)


async def attempt_fulfill_order(session, order: WebOrder) -> None:
    """Run one tracked fulfillment attempt and always leave a visible outcome."""
    profile_lock_key = order.profile_token or f"web-order:{order.order_id}"
    async with _acquire_profile_lock(profile_lock_key, timeout=10.0):
        await session.refresh(order)
        if order.status == "delivered":
            logger.info(
                "Order %s is already delivered; skipping duplicate fulfillment attempt.",
                order.order_id,
            )
            return
        if order.status not in ("paid", "failed"):
            logger.warning(
                "Order %s has status '%s', expected 'paid' or 'failed'. Skipping fulfillment.",
                order.order_id,
                order.status,
            )
            return

        if order.status == "failed":
            if int(order.fulfillment_attempts or 0) >= _FULFILLMENT_MAX_ATTEMPTS:
                logger.info(
                    "Order %s has exhausted fulfillment attempts (%s); skipping replay.",
                    order.order_id,
                    order.fulfillment_attempts,
                )
                return
            if order.failure_code and not _is_retryable_provision_issue(
                build_internal_access_error(
                    provider=_get_order_provider(order) or "webstore",
                    code=order.failure_code,
                    admin_message=order.failure_reason or "",
                )
            ):
                logger.info(
                    "Order %s has terminal failure (%s); skipping replay.",
                    order.order_id,
                    order.failure_code,
                )
                return

        if order.failure_code in ("buy_fallback_inflight", "buy_inflight"):
            now = datetime.utcnow()
            if order.next_fulfillment_retry_at and order.next_fulfillment_retry_at > now:
                logger.info(
                    "Order %s has live buy_inflight until %s. Leaving untouched.",
                    order.order_id,
                    order.next_fulfillment_retry_at,
                )
                return

        if order.failure_code in _VHQ_NON_RETRYABLE_BUY_FAILURE_CODES and order.failure_code not in ("buy_fallback_inflight", "buy_inflight"):
            logger.critical(
                "Order %s has non-retryable failure_code='%s'. Re-invoking supplier buy is prohibited.",
                order.order_id, order.failure_code,
            )
            return

        order.fulfillment_attempts = int(order.fulfillment_attempts or 0) + 1
        order.last_fulfillment_attempt_at = datetime.utcnow()
        if order.failure_code not in ("buy_fallback_inflight", "buy_inflight"):
            order.next_fulfillment_retry_at = None
        try:
            await _fulfill_order(session, order)
        except Exception as exc:
            issue = build_internal_access_error(
                provider="webstore",
                code="fulfillment_runtime",
                admin_message=f"Unexpected webstore fulfillment error order_id={order.order_id}: {exc}",
                raw_message=str(exc),
            )
            logger.exception(issue.admin_message)
            await _mark_order_failed(order, issue)

        if order.status == "delivered":
            order.next_fulfillment_retry_at = None
            order.failure_message = None
            order.failure_reason = None
            order.failure_code = None
            _sync_bot_db_subscription_on_fulfillment(order)
        await session.commit()


async def _resolve_root_vhq_web_order(session, start_order: WebOrder) -> WebOrder:
    current = start_order
    visited = set()
    while current.target_order_id:
        target_ref = current.target_order_id.removeprefix("order:").strip()
        if not target_ref or target_ref in visited or target_ref == current.order_id:
            break
        visited.add(target_ref)
        parent = await session.scalar(
            select(WebOrder).where(
                or_(
                    WebOrder.order_id == target_ref,
                    WebOrder.id == int(target_ref) if target_ref.isdigit() else False,
                )
            ).limit(1)
        )
        if not parent:
            break
        current = parent
    return current


async def _mark_vhq_buy_outcome_unknown(
    session,
    order: WebOrder,
    *,
    reason: str,
    client_message: str | None = None,
    raw_error: Exception | str | None = None,
) -> None:
    """Mark a VHQ order as failed-closed with non-retryable manual review state.

    Used when a non-idempotent VHQ POST buy was initiated (or may have reached the
    supplier), but the outcome is unknown, malformed, or ambiguous. Automatic and
    generic admin retries are strictly prohibited to prevent duplicate purchases.
    """
    order.status = "failed"
    order.next_fulfillment_retry_at = None
    order.failure_code = "vhq_buy_outcome_unknown"
    order.failure_reason = reason
    order.failure_message = client_message or "Заказ требует проверки администратором. Оплата сохранена."
    issue = build_internal_access_error(
        provider="vhq",
        code="vhq_buy_outcome_unknown",
        admin_message=f"Order {order.order_id}: {reason}",
        client_message=order.failure_message,
        raw_message=str(raw_error) if raw_error is not None else None,
    )
    _web_plog(
        "WEB_ОШИБКА_ВЫДАЧИ",
        order_id=order.order_id,
        tariff=order.tariff_label,
        provider="vhq",
        code="vhq_buy_outcome_unknown",
        status=getattr(issue, "status", "") or "",
    )
    await session.commit()
    try:
        await _notify_admins_webstore(order, "failed", issue=issue)
    except Exception as exc:
        logger.warning("Failed to notify admins for order %s: %s", order.order_id, exc)
    try:
        await _notify_linked_webstore_user(order, "failed")
    except Exception as exc:
        logger.warning("Failed to notify linked user for order %s: %s", order.order_id, exc)


async def _fulfill_vhq_order(session, order: WebOrder, vhq_spec: dict[str, int | str]) -> None:
    api = VHQPartnerAPI(
        api_key=settings.vhq_partner_api_key,
        base_url=settings.vhq_partner_api_url,
    )
    if order.failure_code in _VHQ_NON_RETRYABLE_BUY_FAILURE_CODES:
        if order.failure_code in ("buy_fallback_inflight", "buy_inflight"):
            now = datetime.utcnow()
            if order.next_fulfillment_retry_at and order.next_fulfillment_retry_at > now:
                logger.info(
                    "Order %s has live buy_inflight until %s. Leaving untouched.",
                    order.order_id,
                    order.next_fulfillment_retry_at,
                )
                return
            logger.critical(
                "Order %s has stale buy inflight marker (%s, deadline=%s). Aborting to prevent duplicate buy.",
                order.order_id, order.failure_code, order.next_fulfillment_retry_at,
            )
            order.status = "failed"
            order.next_fulfillment_retry_at = None
            order.failure_code = "stale_buy_inflight"
            order.failure_reason = "Crash recovery: stale buy attempt detected; duplicate buy prohibited"
            order.failure_message = "Заказ требует проверки администратором. Оплата сохранена."
            issue = build_internal_access_error(
                provider="vhq",
                code="stale_buy_inflight",
                admin_message=f"Order {order.order_id} has stale buy_inflight after crash; duplicate buy prohibited",
                client_message=order.failure_message,
            )
            await session.commit()
            await _notify_admins_webstore(order, "failed", issue=issue)
            return
        else:
            logger.critical(
                "Order %s has non-retryable failure_code='%s'. Duplicate buy prohibited.",
                order.order_id, order.failure_code,
            )
            return

    is_renew = bool(order.purchase_action == "renew" or order.target_order_id)
    root_order = await _resolve_root_vhq_web_order(session, order) if is_renew else order

    if is_renew:
        supplier_order_id = root_order.vhq_order_id
        if not supplier_order_id and root_order.id != order.id:
            supplier_order_id = order.vhq_order_id

        if not supplier_order_id:
            issue = build_vhq_access_error(
                status=None,
                message=f"Missing VHQ supplier order_id for renewal of order {order.order_id}",
                context=f"order_id={order.order_id} target_order_id={order.target_order_id}",
            )
            logger.error(issue.admin_message)
            await _mark_order_failed(order, issue)
            return

        now = datetime.utcnow()

        # Blocker 4: Check for another open paid renewal targeting the same root identity
        concurrent_candidates = (
            await session.scalars(
                select(WebOrder).where(
                    WebOrder.id != order.id,
                    WebOrder.status == "paid",
                    or_(
                        WebOrder.target_order_id == f"order:{root_order.order_id}",
                        WebOrder.target_order_id == root_order.order_id,
                        WebOrder.target_order_id == str(root_order.id),
                        WebOrder.id == root_order.id,
                    ),
                ).order_by(WebOrder.id.asc())
            )
        ).all()

        open_concurrent = [
            o for o in concurrent_candidates
            if o.failure_code in ("supplier_renew_processing", "network_timeout", "concurrent_renewal_conflict")
            or o.id < order.id
        ]

        if open_concurrent:
            other = open_concurrent[0]
            logger.warning(
                "Web renewal order %s deferred: root order %s has active in-flight renewal %s (status=%s, failure_code=%s)",
                order.order_id, root_order.order_id, other.order_id, other.status, other.failure_code,
            )
            order.status = "paid"
            order.failure_code = "concurrent_renewal_conflict"
            order.failure_reason = f"Active renewal {other.order_id} in progress for root order {root_order.order_id}"
            order.failure_message = "Продление поставщика уже выполняется по предыдущему заказу. Ожидаем завершения цикла..."
            order.next_fulfillment_retry_at = now + timedelta(minutes=2)
            await _notify_admins_webstore(
                order,
                "conflict",
                issue=build_internal_access_error(
                    provider="webstore",
                    code="concurrent_renewal_conflict",
                    admin_message=f"Web renewal {order.order_id} deferred: active renewal {other.order_id} in progress for root {root_order.order_id}",
                ),
            )
            return

        renew_days = int(order.days or 30)

        # Blocker 3: Baseline snapshot from root_order or GET orders if root_order.vhq_active_until is NULL
        provider_baseline = root_order.vhq_active_until
        if provider_baseline is None and supplier_order_id:
            try:
                orders_resp = await api.get_orders()
                orders_list = orders_resp if isinstance(orders_resp, list) else (orders_resp.get("orders", []) if isinstance(orders_resp, dict) else [])
                matched_base = next(
                    (o for o in orders_list if str(o.get("order_id") or o.get("id") or "").strip() == str(supplier_order_id).strip()),
                    None,
                )
                if matched_base:
                    base_active = VHQPartnerAPI.extract_active_until(matched_base)
                    if base_active:
                        provider_baseline = base_active
                        root_order.vhq_active_until = base_active
                        logger.info("Established webstore root %s baseline from GET orders: %s", root_order.order_id, base_active.isoformat())
            except Exception as exc:
                logger.warning("Failed to establish baseline for web order %s: %s", order.order_id, exc)

        expected_provider_active_until = None
        if provider_baseline:
            ref_time = provider_baseline if provider_baseline > now else now
            expected_provider_active_until = ref_time + timedelta(days=renew_days)

        # 1. Reconciliation: only if retrying after a previous attempt
        should_reconcile = (
            order.failure_code in ("supplier_renew_processing", "network_timeout", "reconciliation_uncertain", "vhq_renew_uncertain")
            or (order.fulfillment_attempts and order.fulfillment_attempts > 1 and order.failure_code is not None)
        )
        if should_reconcile and supplier_order_id:
            try:
                orders_resp = await api.get_orders()
                orders_list = orders_resp if isinstance(orders_resp, list) else (orders_resp.get("orders", []) if isinstance(orders_resp, dict) else [])
                matched = next(
                    (o for o in orders_list if str(o.get("order_id") or o.get("id") or "").strip() == str(supplier_order_id).strip()),
                    None,
                )
                if matched:
                    sup_active = VHQPartnerAPI.extract_active_until(matched)
                    if sup_active:
                        is_ok = False
                        if expected_provider_active_until:
                            is_ok = sup_active >= expected_provider_active_until
                        else:
                            is_ok = False

                        if is_ok:
                            root_order.vhq_active_until = sup_active
                            root_order.access_expires_at = sup_active
                            order.subscription_url = root_order.subscription_url
                            order.vhq_order_id = root_order.vhq_order_id
                            order.vhq_upstream_url = root_order.vhq_upstream_url
                            order.vhq_active_until = root_order.vhq_active_until
                            order.access_expires_at = root_order.access_expires_at
                            order.status = "delivered"
                            order.failure_message = None
                            order.failure_reason = None
                            order.failure_code = None
                            order.delivered_at = datetime.utcnow()
                            _web_plog("WEB_ПРОДЛЕНИЕ", order_id=order.order_id, tariff=order.tariff_label, provider="vhq", note="reconciliation")
                            logger.info("Order %s renewed via VHQ reconciliation (root order %s)", order.order_id, root_order.order_id)
                            return
            except Exception as rec_exc:
                logger.warning("Reconciliation get_orders failed for web order %s: %s", order.order_id, rec_exc)

        try:
            response = await api.renew(order_id=supplier_order_id)
            resp_status = str(response.get("status", "")).strip().lower()
            new_active_until = VHQPartnerAPI.extract_active_until(response)
            new_upstream = VHQPartnerAPI.extract_subscription_url(response) or root_order.vhq_upstream_url

            is_confirmed = False
            if resp_status == "processing":
                is_confirmed = False
            elif expected_provider_active_until:
                if new_active_until and new_active_until >= expected_provider_active_until:
                    is_confirmed = True
                else:
                    is_confirmed = False
            else:
                if bool(response.get("already_requested")):
                    is_confirmed = False
                    order.status = "failed"
                    order.next_fulfillment_retry_at = None
                    order.failure_code = "missing_baseline_already_requested"
                    order.failure_reason = "VHQ renew returned already_requested=True without known baseline; cannot prove new renewal cycle"
                    order.failure_message = "Заказ требует проверки администратором. Оплата сохранена."
                    issue = build_internal_access_error(
                        provider="vhq",
                        code="missing_baseline_already_requested",
                        admin_message=order.failure_reason,
                        client_message=order.failure_message,
                    )
                    await session.commit()
                    await _notify_admins_webstore(order, "failed", issue=issue)
                    return
                elif resp_status in ("renewed", "completed", "success") and new_active_until and new_active_until > now:
                    is_confirmed = True
                else:
                    is_confirmed = False

            if not is_confirmed:
                logger.info(
                    "VHQ renew for order %s returned unconfirmed/processing: status=%s active_until=%s (expected=%s). Keeping paid/pending.",
                    order.order_id, resp_status, new_active_until, expected_provider_active_until,
                )
                order.status = "paid"
                order.next_fulfillment_retry_at = datetime.utcnow() + timedelta(minutes=1)
                order.failure_code = "supplier_renew_processing"
                order.failure_message = "Продление обрабатывается поставщиком. Проверяем статус..."
                return

            root_order.vhq_active_until = new_active_until
            if new_upstream:
                root_order.vhq_upstream_url = new_upstream
            now = datetime.utcnow()
            base_exp = root_order.access_expires_at if (root_order.access_expires_at and root_order.access_expires_at > now) else now
            root_order.access_expires_at = new_active_until or (base_exp + timedelta(days=order.days))

            # Maintain stable public URL on current order
            order.subscription_url = root_order.subscription_url
            order.vhq_order_id = root_order.vhq_order_id
            order.vhq_upstream_url = root_order.vhq_upstream_url
            order.vhq_active_until = root_order.vhq_active_until
            order.access_expires_at = root_order.access_expires_at
            order.status = "delivered"
            order.failure_message = None
            order.failure_reason = None
            order.delivered_at = now
            _web_plog(
                "WEB_ПРОДЛЕНИЕ",
                order_id=order.order_id,
                tariff=order.tariff_label,
                provider="vhq",
            )
            logger.info("Order %s renewed via VHQ (root order %s)", order.order_id, root_order.order_id)
            return
        except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as net_exc:
            logger.warning("VHQ renew network timeout for order %s: %s", order.order_id, net_exc)
            order.status = "paid"
            order.failure_code = "vhq_renew_uncertain"
            order.failure_reason = f"Network timeout during VHQ renew: {net_exc}"
            order.failure_message = "Продление обрабатывается поставщиком. Проверяем статус..."
            order.next_fulfillment_retry_at = datetime.utcnow() + timedelta(minutes=1)
            await session.commit()
            return
        except VHQPartnerAPIError as exc:
            exc_msg = str(exc).lower()
            is_expired_too_long = (
                "expired too long ago" in exc_msg
                or "please buy a new one" in exc_msg
            )
            if exc.status == 409 and is_expired_too_long:
                logger.warning(
                    "VHQ renew returned 409 expired_too_long for order %s (supplier_order_id=%s). Attempting fallback buy.",
                    order.order_id,
                    supplier_order_id,
                )
                order.failure_code = "buy_fallback_inflight"
                order.failure_reason = "Fallback buy initiated"
                order.next_fulfillment_retry_at = datetime.utcnow() + timedelta(minutes=2)
                await session.commit()

                try:
                    buy_response = await api.buy(
                        tier=str(vhq_spec["tier"]),
                        days=int(vhq_spec["days"]),
                    )
                    upstream_url = VHQPartnerAPI.extract_subscription_url(buy_response)
                    new_vhq_order_id = VHQPartnerAPI.extract_order_id(buy_response)
                    if not upstream_url or not new_vhq_order_id:
                        issue = build_internal_access_error(
                            provider="vhq",
                            code="fallback_buy_missing_data",
                            admin_message=f"Fallback buy missing subscription url or order_id in response: {buy_response}",
                            client_message="Ошибка получения данных от поставщика. Обратитесь в поддержку.",
                        )
                        logger.error(issue.admin_message)
                        order.status = "failed"
                        order.next_fulfillment_retry_at = None
                        order.failure_code = "fallback_buy_missing_data"
                        order.failure_reason = issue.admin_message
                        order.failure_message = issue.client_message
                        await session.commit()
                        await _notify_admins_webstore(order, "failed", issue=issue)
                        return
                    new_active_until = VHQPartnerAPI.extract_active_until(buy_response)

                    # Update root order with new supplier order, preserving public subscription_url!
                    root_order.vhq_order_id = new_vhq_order_id
                    root_order.vhq_upstream_url = upstream_url
                    root_order.vhq_active_until = new_active_until
                    now = datetime.utcnow()
                    root_order.access_expires_at = new_active_until or (now + timedelta(days=order.days))

                    order.subscription_url = root_order.subscription_url
                    order.vhq_order_id = new_vhq_order_id
                    order.vhq_upstream_url = upstream_url
                    order.vhq_active_until = new_active_until
                    order.access_expires_at = root_order.access_expires_at
                    order.status = "delivered"
                    order.failure_message = None
                    order.failure_reason = None
                    order.delivered_at = now
                    _web_plog(
                        "WEB_ВЫДАЧА",
                        order_id=order.order_id,
                        tariff=order.tariff_label,
                        provider="vhq",
                        note="fallback_buy_after_409",
                    )
                    logger.info("Order %s fulfilled via fallback buy (root order %s)", order.order_id, root_order.order_id)
                    return
                except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as net_exc:
                    logger.error(
                        "Ambiguous network error during fallback buy for order %s: %s",
                        order.order_id, net_exc,
                    )
                    issue = build_internal_access_error(
                        provider="vhq",
                        code="ambiguous_buy_network_error",
                        admin_message=f"Ambiguous network timeout during VHQ fallback buy for order {order.order_id}: {net_exc}",
                        client_message="Ошибка соединения при оформлении заказа. Оплата не потеряна, мы свяжемся с вами.",
                    )
                    order.status = "failed"
                    order.next_fulfillment_retry_at = None
                    order.failure_code = "ambiguous_buy_network_error"
                    order.failure_reason = issue.admin_message
                    order.failure_message = issue.client_message
                    await _notify_admins_webstore(order, "failed", issue=issue)
                    return
                except Exception as buy_exc:
                    logger.error("Fallback buy failed after 409 renew: %s", buy_exc)
                    await _mark_vhq_buy_outcome_unknown(
                        session,
                        order,
                        reason=f"Fallback buy failed after 409 renew: {buy_exc}",
                        raw_error=buy_exc,
                    )
                    return
            elif exc.status == 409:
                logger.error(
                    "VHQ renew returned 409 non-renewable for order %s: %s",
                    order.order_id, exc,
                )
                issue = build_internal_access_error(
                    provider="vhq",
                    code="renew_409_wrong_product",
                    admin_message=f"VHQ renew 409 non-renewable for order {order.order_id}: {exc}",
                    client_message="Тариф недоступен для автоматического продления. Обратитесь в поддержку.",
                )
                order.status = "failed"
                order.next_fulfillment_retry_at = None
                order.failure_code = "renew_409_wrong_product"
                order.failure_reason = issue.admin_message
                order.failure_message = issue.client_message
                await _notify_admins_webstore(order, "failed", issue=issue)
                return
            else:
                issue = build_vhq_access_error(
                    status=exc.status,
                    message=str(exc),
                    context=f"order_id={order.order_id} contact={order.contact or order.email or '—'}",
                )
                logger.error(issue.admin_message)
                await _mark_order_failed(order, issue)
                return
        except Exception as exc:
            issue = build_internal_access_error(
                provider="vhq",
                code="vhq_renew_runtime",
                admin_message=f"Unexpected VHQ renew error order_id={order.order_id}: {exc}",
                raw_message=str(exc),
            )
            logger.error(issue.admin_message)
            await _mark_order_failed(order, issue)
            return

    # NEW purchase
    # Preflight validation before marking buy_inflight or initiating supplier HTTP POST
    if not api.enabled:
        issue = build_internal_access_error(
            provider="vhq",
            code="vhq_not_configured",
            admin_message=f"VHQ Partner API is not configured for order {order.order_id}",
            client_message="Сервис временно недоступен. Мы уже решаем проблему.",
        )
        logger.error(issue.admin_message)
        await _mark_order_failed(order, issue)
        await session.commit()
        return

    tier_str = str(vhq_spec.get("tier") or "").strip().lower()
    days_val = int(vhq_spec.get("days") or 0)
    allowed_days = _ALLOWED_DAYS.get(tier_str)
    if not allowed_days or days_val not in allowed_days:
        issue = build_internal_access_error(
            provider="vhq",
            code="vhq_unsupported_spec",
            admin_message=f"Unsupported VHQ tier/days spec ({tier_str}, {days_val}) for order {order.order_id}",
            client_message="Тариф временно недоступен для оформления. Обратитесь в поддержку.",
        )
        logger.error(issue.admin_message)
        await _mark_order_failed(order, issue)
        await session.commit()
        return

    order.failure_code = "buy_inflight"
    order.failure_reason = "New VHQ buy initiated"
    order.next_fulfillment_retry_at = datetime.utcnow() + timedelta(minutes=2)
    await session.commit()

    try:
        response = await api.buy(
            tier=tier_str,
            days=days_val,
        )
    except (aiohttp.ClientError, asyncio.TimeoutError, TimeoutError) as net_exc:
        logger.error(
            "Ambiguous network error during VHQ new buy for order %s: %s",
            order.order_id, net_exc,
        )
        await _mark_vhq_buy_outcome_unknown(
            session,
            order,
            reason=f"Ambiguous network timeout during VHQ new buy for order {order.order_id}: {net_exc}",
            client_message="Ошибка соединения при оформлении заказа. Оплата сохранена, мы свяжемся с вами.",
            raw_error=net_exc,
        )
        return
    except VHQPartnerAPIError as exc:
        logger.error(
            "VHQ Partner API error during new buy for order %s (status=%s): %s",
            order.order_id, exc.status, exc,
        )
        await _mark_vhq_buy_outcome_unknown(
            session,
            order,
            reason=f"VHQ Partner API error during new buy for order {order.order_id} (status={exc.status}): {exc}",
            client_message="Ошибка оформления заказа у поставщика. Оплата сохранена, мы свяжемся с вами.",
            raw_error=exc,
        )
        return
    except Exception as exc:
        logger.error(
            "Unexpected error during VHQ new buy for order %s: %s",
            order.order_id, exc,
        )
        await _mark_vhq_buy_outcome_unknown(
            session,
            order,
            reason=f"Unexpected error during VHQ new buy for order {order.order_id}: {exc}",
            client_message="Непредвиденная ошибка при оформлении заказа. Оплата сохранена, мы свяжемся с вами.",
            raw_error=exc,
        )
        return

    try:
        if not isinstance(response, dict):
            raise ValueError(f"VHQ buy response is not a dict: {type(response)}")
        upstream_url = VHQPartnerAPI.extract_subscription_url(response)
        new_vhq_order_id = VHQPartnerAPI.extract_order_id(response)
        if not upstream_url or not new_vhq_order_id:
            logger.error(
                "VHQ new buy response missing identifiers for order %s: %s",
                order.order_id, response,
            )
            await _mark_vhq_buy_outcome_unknown(
                session,
                order,
                reason=f"VHQ new buy missing subscription url or order_id in response for order {order.order_id}: {response}",
                client_message="Ошибка получения данных от поставщика. Оплата сохранена. Обратитесь в поддержку.",
            )
            return

        order.vhq_order_id = new_vhq_order_id
        order.vhq_upstream_url = upstream_url
        order.vhq_active_until = VHQPartnerAPI.extract_active_until(response)

        public_ref_url = build_vhq_web_order_ref_url(
            order.order_id,
            public_base_url=settings.vhq_subscription_proxy_base_url,
            path_prefix=settings.bot_webhook_path_prefix,
            secret=settings.vhq_subscription_proxy_secret,
        )
        if public_ref_url:
            order.subscription_url = public_ref_url
        else:
            order.subscription_url = build_vhq_mirror_url(
                upstream_url,
                public_base_url=settings.vhq_subscription_proxy_base_url,
                path_prefix=settings.bot_webhook_path_prefix,
                secret=settings.vhq_subscription_proxy_secret,
                order_id=order.order_id,
            )
    except Exception as exc:
        logger.error(
            "Unexpected error parsing VHQ response for order %s: %s",
            order.order_id, exc,
        )
        await _mark_vhq_buy_outcome_unknown(
            session,
            order,
            reason=f"Unexpected error parsing VHQ response for order {order.order_id}: {exc}",
            client_message="Ошибка обработки данных поставщика. Оплата сохранена. Обратитесь в поддержку.",
            raw_error=exc,
        )
        return

    order.status = "delivered"
    order.failure_message = None
    order.failure_reason = None
    order.failure_code = None
    order.next_fulfillment_retry_at = None
    order.delivered_at = datetime.utcnow()
    order.access_expires_at = order.vhq_active_until or (order.delivered_at + timedelta(days=order.days))
    _web_plog(
        "WEB_ВЫДАЧА",
        order_id=order.order_id,
        tariff=order.tariff_label,
        provider="vhq",
    )
    logger.info("Order %s fulfilled via VHQ", order.order_id)


def _adapt_expires_at_from_response(resp: dict, fallback_days: int) -> datetime:
    raw_end_date = str(resp.get("end_date") or "").strip()
    if raw_end_date:
        try:
            parsed = datetime.fromisoformat(raw_end_date.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
            return parsed
        except ValueError:
            logger.warning("Invalid Adapt end_date in response: %s", raw_end_date)

    try:
        days = int(resp.get("days") or fallback_days)
    except (TypeError, ValueError):
        days = fallback_days
    return datetime.utcnow() + timedelta(days=max(days, 1))


def _extract_adapt_uuid_from_web_order(order: WebOrder) -> str | None:
    return _extract_adapt_uuid(order.subscription_url, order.marzban_username)


async def _fulfill_adapt_order(session, order: WebOrder, adapt_plan_uuid: str) -> None:
    """Execute the explicit web purchase operation against the selected UUID."""
    action = str(order.purchase_action or "new").strip().lower()
    primary = None
    target_telegram_item = None

    # If the user only has a demo/trial order and makes their first paid purchase,
    # upgrade that demo key rather than issuing an extra key.
    # If they already have active paid orders and chose to buy a new one, issue a separate key.
    if action == "new" and not order.target_order_id and order.profile_token:
        existing_demo_order = await session.scalar(
            select(WebOrder)
            .where(WebOrder.profile_token == order.profile_token)
            .where(or_(WebOrder.status == "demo", WebOrder.tariff_key == "demo"))
            .order_by(WebOrder.id.desc())
            .limit(1)
        )
        existing_demo_tg_item = await session.scalar(
            select(WebTelegramItem)
            .where(WebTelegramItem.profile_token == order.profile_token)
            .where(WebTelegramItem.item_type == "vpn")
            .where(WebTelegramItem.adapt_plan_uuid == settings.adapt_demo_plan_uuid)
            .order_by(WebTelegramItem.id.desc())
            .limit(1)
        )
        existing_paid_order = await session.scalar(
            select(WebOrder)
            .where(WebOrder.profile_token == order.profile_token)
            .where(WebOrder.status == "delivered")
            .where(WebOrder.tariff_key != "demo")
            .where(
                or_(
                    WebOrder.access_expires_at == None,
                    WebOrder.access_expires_at > datetime.utcnow(),
                )
            )
            .order_by(WebOrder.id.desc())
            .limit(1)
        )
        existing_paid_tg_item = await session.scalar(
            select(WebTelegramItem)
            .where(WebTelegramItem.profile_token == order.profile_token)
            .where(WebTelegramItem.item_type == "vpn")
            .where(WebTelegramItem.status == "active")
            .where(WebTelegramItem.adapt_plan_uuid != settings.adapt_demo_plan_uuid)
            .where(
                or_(
                    WebTelegramItem.expires_at == None,
                    WebTelegramItem.expires_at > datetime.utcnow(),
                )
            )
            .order_by(WebTelegramItem.id.desc())
            .limit(1)
        )
        if (existing_demo_order or existing_demo_tg_item) and not (existing_paid_order or existing_paid_tg_item):
            existing_adapt_uuid = (
                _extract_adapt_uuid_from_web_order(existing_demo_order)
                if existing_demo_order
                else _extract_adapt_uuid(existing_demo_tg_item.key_value, existing_demo_tg_item.external_id)
            )
            if existing_adapt_uuid:
                order.target_order_id = f"uuid:{existing_adapt_uuid}"
                order.target_snapshot_plan_uuid = settings.adapt_demo_plan_uuid
                action = "upgrade"
                order.purchase_action = "upgrade"

    if action in {"renew", "upgrade"} and not order.target_order_id:
        issue = build_internal_access_error(
            provider="adapt",
            code="adapt_target_missing",
            admin_message=f"Adapt target is missing for order {order.order_id} action={action}",
        )
        await _mark_order_failed(order, issue)
        return
    if action in {"renew", "upgrade"} and order.target_order_id:
        if order.target_order_id.startswith("tg:"):
            requested_adapt_uuid = order.target_order_id.removeprefix("tg:")
            telegram_items_result = await session.execute(
                select(WebTelegramItem)
                .where(WebTelegramItem.profile_token == order.profile_token)
                .where(WebTelegramItem.item_type == "vpn")
            )
            target_telegram_item = next(
                (
                    item for item in telegram_items_result.scalars().all()
                    if _extract_adapt_uuid(item.key_value, item.external_id) == requested_adapt_uuid
                ),
                None,
            )
            adapt_uuid = _extract_adapt_uuid(
                target_telegram_item.key_value if target_telegram_item else None,
                target_telegram_item.external_id if target_telegram_item else None,
            )
        elif (
            order.target_order_id.startswith("uuid:")
            or order.target_order_id.startswith("adapt:")
            or _extract_adapt_uuid(order.target_order_id)
        ):
            adapt_uuid = _extract_adapt_uuid(order.target_order_id)
        else:
            primary = await session.scalar(
                select(WebOrder)
                .where(WebOrder.order_id == order.target_order_id)
                .where(WebOrder.profile_token == order.profile_token)
                .where(WebOrder.status.in_(("delivered", "demo")))
            )
            adapt_uuid = _extract_adapt_uuid_from_web_order(primary) if primary else None
        if not adapt_uuid:
            issue = build_internal_access_error(
                provider="adapt",
                code="adapt_target_not_found",
                admin_message=f"Adapt target not found for order {order.order_id}: {order.target_order_id}",
            )
            await _mark_order_failed(order, issue)
            return
        mutation_state = _adapt_mutation_state(order, action)
        if mutation_state == _ADAPT_MUTATION_FAILED_CLOSED:
            logger.critical(
                "Order %s has a failed-closed Adapt mutation state; skipping supplier mutation.",
                order.order_id,
            )
            return
        if mutation_state in _ADAPT_MUTATION_POST_STATES and (
            not order.target_snapshot_expires_at
            or (action == "upgrade" and not order.target_snapshot_plan_uuid)
        ):
            order.adapt_mutation_state = _ADAPT_MUTATION_FAILED_CLOSED
            issue = build_internal_access_error(
                provider="adapt",
                code="adapt_baseline_missing",
                admin_message=(
                    f"Adapt mutation state for order {order.order_id} has no durable baseline; "
                    "supplier mutation is prohibited"
                ),
                client_message=(
                    "Оплата получена, но не удалось восстановить исходное состояние подписки. "
                    "Мы уже получили уведомление и разберёмся."
                ),
            )
            await _mark_order_failed(order, issue)
            return
        if mutation_state is None:
            if order.target_snapshot_expires_at is None:
                order.target_snapshot_expires_at = (
                    primary.access_expires_at if primary else (target_telegram_item.expires_at if target_telegram_item else None)
                )
            if not order.target_snapshot_plan_uuid:
                if target_telegram_item:
                    order.target_snapshot_plan_uuid = str(target_telegram_item.adapt_plan_uuid or "")
                elif primary:
                    order.target_snapshot_plan_uuid = _web_order_current_adapt_plan_uuid(primary)
        old_tariff = get_adapt_plan_metadata_by_uuid(order.target_snapshot_plan_uuid)
        if not order.target_snapshot_plan_uuid and primary:
            old_tariff = get_adapt_plan_metadata_by_key(primary.tariff_key)
        try:
            action = _effective_adapt_purchase_action(action, old_tariff, adapt_plan_uuid)
            order.purchase_action = action
        except ValueError as exc:
            issue = build_internal_access_error(
                provider="adapt",
                code="adapt_trial_requires_upgrade",
                admin_message=f"Invalid trial renewal for paid order {order.order_id}: {exc}",
                client_message="Не удалось перевести тестовую подписку на платный тариф. Мы уже получили уведомление.",
            )
            await _mark_order_failed(order, issue)
            return
        mutation_state = _adapt_mutation_state(order, action)
        if mutation_state in _ADAPT_MUTATION_POST_STATES and (
            not order.target_snapshot_expires_at
            or (action == "upgrade" and not order.target_snapshot_plan_uuid)
        ):
            order.adapt_mutation_state = _ADAPT_MUTATION_FAILED_CLOSED
            issue = build_internal_access_error(
                provider="adapt",
                code="adapt_baseline_missing",
                admin_message=(
                    f"Adapt mutation state for order {order.order_id} has no durable baseline; "
                    "supplier mutation is prohibited"
                ),
                client_message=(
                    "Оплата получена, но не удалось восстановить исходное состояние подписки. "
                    "Мы уже получили уведомление и разберёмся."
                ),
            )
            await _mark_order_failed(order, issue)
            return
        if mutation_state in _ADAPT_MUTATION_POST_STATES and not getattr(order, "adapt_mutation_state", None):
            order.adapt_mutation_state = mutation_state
            await session.commit()
        try:
            api = AdaptAPI()
            already_applied = False
            try:
                status_before = await api.get_status(adapt_uuid)
            except AdaptAPIError as exc:
                raise build_internal_access_error(
                    provider="adapt",
                    code=f"adapt_{action}_preflight_{exc.status or 'error'}",
                    status=exc.status,
                    admin_message=f"Adapt {action} preflight get_status failed for order {order.order_id}: {exc}",
                    raw_message=str(exc),
                ) from exc
            except (asyncio.TimeoutError, TimeoutError) as exc:
                raise build_internal_access_error(
                    provider="adapt",
                    code=f"adapt_{action}_preflight_timeout",
                    status=504,
                    admin_message=f"Adapt {action} preflight get_status timeout for order {order.order_id}: {exc}",
                    raw_message=str(exc),
                ) from exc
            # Handle frozen subscription: unfreeze before proceeding with renewal/upgrade
            freeze_state = getattr(order, "fulfillment_freeze_state", None)
            if not freeze_state:
                was_originally_frozen = bool(
                    status_before.get("is_frozen") is True
                    or getattr(order, "target_snapshot_is_frozen", False) is True
                    or (target_telegram_item and getattr(target_telegram_item, "is_frozen", False) is True)
                )
                if was_originally_frozen:
                    order.fulfillment_freeze_state = "pending_unfreeze"
                    order.target_snapshot_is_frozen = True
                    await session.commit()
                    freeze_state = "pending_unfreeze"
            else:
                was_originally_frozen = freeze_state in {"pending_unfreeze", "unfrozen_for_mutation", "mutation_applied", "freeze_restored"}

            if was_originally_frozen and freeze_state in {"pending_unfreeze", "unfrozen_for_mutation"}:
                if status_before.get("is_frozen") is True:
                    logger.info(
                        "Unfreezing frozen Adapt subscription before web fulfillment: order=%s adapt_uuid=%s",
                        order.order_id,
                        adapt_uuid,
                    )
                    try:
                        await api.unfreeze_subscription(adapt_uuid)
                        status_before = await api.get_status(adapt_uuid)
                        if status_before.get("is_frozen") is True:
                            raise RuntimeError("Adapt provider still reports subscription is frozen after unfreeze")
                    except Exception as exc:
                        issue = build_internal_access_error(
                            provider="adapt",
                            code="adapt_unfreeze_failed",
                            admin_message=(
                                f"Failed to unfreeze subscription before web fulfillment "
                                f"order={order.order_id} adapt_uuid={adapt_uuid}: {exc}"
                            ),
                            client_message=(
                                "Оплата получена. Не удалось разморозить подписку перед изменением. "
                                "Мы уже получили уведомление и разберёмся."
                            ),
                            raw_message=str(exc),
                        )
                        await _mark_order_failed(order, issue)
                        return
                order.fulfillment_freeze_state = "unfrozen_for_mutation"
                await session.commit()

            status_plan_uuid = str(status_before.get("plan_uuid") or "").strip()
            status_expires_at = (
                _adapt_expires_at_from_response(status_before, 0)
                if status_before.get("end_date")
                else None
            )
            initial_snapshot_plan_uuid = str(order.target_snapshot_plan_uuid or "").strip()
            if action == "renew" and status_plan_uuid != adapt_plan_uuid:
                issue = build_internal_access_error(
                    provider="adapt",
                    code="adapt_renew_plan_mismatch",
                    admin_message=(
                        f"Refusing web Adapt renew on stale plan for order {order.order_id}: "
                        f"provider_plan={status_plan_uuid} requested_plan={adapt_plan_uuid}"
                    ),
                    client_message=(
                        "Оплата получена, но состояние подписки изменилось. "
                        "Мы уже получили уведомление и разберёмся."
                    ),
                )
                await _mark_order_failed(order, issue)
                return

            if (
                action == "upgrade"
                and initial_snapshot_plan_uuid
                and status_plan_uuid
                and status_plan_uuid != initial_snapshot_plan_uuid
                and status_plan_uuid != adapt_plan_uuid
            ):
                issue = build_internal_access_error(
                    provider="adapt",
                    code="adapt_upgrade_plan_contradictory",
                    admin_message=(
                        f"Contradictory Adapt plan state for order {order.order_id}: "
                        f"baseline={initial_snapshot_plan_uuid} target={adapt_plan_uuid} supplier={status_plan_uuid}"
                    ),
                    client_message=(
                        "Оплата получена, но состояние подписки у провайдера изменилось. "
                        "Мы уже получили уведомление и разберёмся."
                    ),
                )
                await _mark_order_failed(order, issue)
                return

            if mutation_state is None:
                if not status_plan_uuid or not status_expires_at:
                    order.adapt_mutation_state = _ADAPT_MUTATION_FAILED_CLOSED
                    issue = build_internal_access_error(
                        provider="adapt",
                        code="adapt_baseline_missing",
                        admin_message=(
                            f"Adapt pre-mutation status for order {order.order_id} did not contain "
                            f"a durable plan and expiry baseline: plan={status_plan_uuid or 'missing'}"
                        ),
                        client_message=(
                            "Оплата получена, но провайдер не вернул полное исходное состояние подписки. "
                            "Мы уже получили уведомление и разберёмся."
                        ),
                    )
                    await _mark_order_failed(order, issue)
                    return
                order.target_snapshot_plan_uuid = status_plan_uuid
                order.target_snapshot_expires_at = status_expires_at
                await session.commit()

            mutation_already_applied = mutation_state == _ADAPT_MUTATION_CONFIRMED
            if mutation_already_applied:
                already_applied = True
            elif (
                action == "upgrade"
                and mutation_state in {
                    _ADAPT_MUTATION_INITIAL_INFLIGHT,
                    _ADAPT_MUTATION_RECOVERY_INFLIGHT,
                }
                and order.target_snapshot_plan_uuid
                and str(order.target_snapshot_plan_uuid).strip() != adapt_plan_uuid
                and status_plan_uuid == adapt_plan_uuid
            ):
                already_applied = True
            elif (
                action == "upgrade"
                and status_plan_uuid == adapt_plan_uuid
                and order.target_snapshot_expires_at
                and status_expires_at
                and status_expires_at > order.target_snapshot_expires_at + timedelta(seconds=30)
            ):
                already_applied = True
            elif (
                action == "upgrade"
                and status_plan_uuid == adapt_plan_uuid
            ):
                # Same plan alone cannot falsely prove fulfillment without proven extension
                issue = build_internal_access_error(
                    provider="adapt",
                    code="adapt_upgrade_same_plan_unextended",
                    admin_message=(
                        f"Refusing web Adapt upgrade for order {order.order_id}: provider already on requested "
                        f"plan {adapt_plan_uuid} without proven expiry extension "
                        f"(current={status_expires_at}, snapshot={order.target_snapshot_expires_at})"
                    ),
                    client_message=(
                        "Оплата получена, но состояние подписки не подтверждено у провайдера. "
                        "Мы уже получили уведомление и разберёмся."
                    ),
                )
                await _mark_order_failed(order, issue)
                return
            elif (
                action == "renew"
                and order.target_snapshot_expires_at
                and status_expires_at
                and status_expires_at > order.target_snapshot_expires_at + timedelta(seconds=30)
            ):
                already_applied = True

            reactivation_already_applied = bool(
                action == "upgrade"
                and int(order.fulfillment_attempts or 0) > 1
                and status_plan_uuid != adapt_plan_uuid
                and status_plan_uuid == str(order.target_snapshot_plan_uuid or "").strip()
                and order.target_snapshot_expires_at
                and order.target_snapshot_expires_at <= datetime.utcnow()
                and status_expires_at
                and status_expires_at > order.target_snapshot_expires_at + timedelta(seconds=30)
            )

            baseline_expiry_matches = bool(
                order.target_snapshot_expires_at
                and status_expires_at
                and abs(
                    status_expires_at - order.target_snapshot_expires_at
                ) <= timedelta(seconds=30)
            )

            if already_applied:
                resp = status_before
                _web_plog(
                    "WEB_ВЫДАЧА_СВЕРКА",
                    order_id=order.order_id,
                    tariff=order.tariff_label,
                    provider="adapt",
                    context=f"action={action} adapt_uuid={adapt_uuid}",
                )
            elif mutation_state == _ADAPT_MUTATION_RECOVERY_INFLIGHT:
                order.adapt_mutation_state = _ADAPT_MUTATION_FAILED_CLOSED
                issue = build_internal_access_error(
                    provider="adapt",
                    code=f"adapt_{action}_recovery_exhausted",
                    admin_message=(
                        f"Adapt {action} recovery retry was already used for order {order.order_id}, "
                        "but the authoritative state is still the durable baseline"
                    ),
                    client_message=(
                        "Оплата получена, но провайдер не подтвердил изменение подписки после повторной попытки. "
                        "Мы уже получили уведомление и разберёмся."
                    ),
                )
                await _mark_order_failed(order, issue)
                return
            elif (
                action == "upgrade"
                and mutation_state == _ADAPT_MUTATION_INITIAL_INFLIGHT
                and status_plan_uuid == initial_snapshot_plan_uuid
                and not baseline_expiry_matches
                and not reactivation_already_applied
            ):
                order.adapt_mutation_state = _ADAPT_MUTATION_FAILED_CLOSED
                issue = build_internal_access_error(
                    provider="adapt",
                    code="adapt_upgrade_reactivation_required",
                    admin_message=(
                        f"Refusing Adapt upgrade recovery for order {order.order_id}: supplier remains on "
                        f"baseline plan {initial_snapshot_plan_uuid}, but expiry changed from "
                        f"{order.target_snapshot_expires_at} to {status_expires_at}"
                    ),
                    client_message=(
                        "Оплата получена, но состояние подписки изменилось неоднозначно. "
                        "Мы уже получили уведомление и разберёмся."
                    ),
                )
                await _mark_order_failed(order, issue)
                await session.commit()
                return
            elif action == "renew":
                order.adapt_mutation_state = (
                    _ADAPT_MUTATION_RECOVERY_INFLIGHT
                    if mutation_state == _ADAPT_MUTATION_INITIAL_INFLIGHT
                    else _ADAPT_MUTATION_INITIAL_INFLIGHT
                )
                order.failure_code = "adapt_renew_inflight"
                await session.commit()
                resp = await api.renew_subscription(adapt_uuid)
            else:
                order.adapt_mutation_state = (
                    _ADAPT_MUTATION_RECOVERY_INFLIGHT
                    if mutation_state == _ADAPT_MUTATION_INITIAL_INFLIGHT
                    else _ADAPT_MUTATION_INITIAL_INFLIGHT
                )
                order.failure_code = "adapt_upgrade_inflight"
                await session.commit()
                if (
                    status_expires_at
                    and status_expires_at <= datetime.utcnow()
                    and not reactivation_already_applied
                ):
                    await api.renew_subscription_custom(
                        adapt_uuid,
                        ADAPT_MIN_CUSTOM_RENEW_DAYS,
                    )
                    reactivated_status = await api.get_status(adapt_uuid)
                    reactivated_plan_uuid = str(
                        reactivated_status.get("plan_uuid") or ""
                    ).strip()
                    reactivated_expires_at = (
                        _adapt_expires_at_from_response(reactivated_status, 0)
                        if reactivated_status.get("end_date")
                        else None
                    )
                    if (
                        reactivated_plan_uuid != status_plan_uuid
                        or not reactivated_expires_at
                        or reactivated_expires_at <= datetime.utcnow()
                    ):
                        raise build_internal_access_error(
                            provider="adapt",
                            code="adapt_reactivation_mismatch",
                            admin_message=(
                                f"Adapt custom renewal did not reactivate expected plan "
                                f"for web order {order.order_id}: expected={status_plan_uuid} "
                                f"actual={reactivated_plan_uuid or 'missing'}"
                            ),
                            client_message=(
                                "Оплата получена, но провайдер не подтвердил изменение подписки. "
                                "Мы уже получили уведомление и разберёмся."
                            ),
                        )
                    if primary:
                        primary.access_expires_at = reactivated_expires_at
                    if target_telegram_item:
                        target_telegram_item.expires_at = reactivated_expires_at
                        target_telegram_item.status = "active"
                        target_telegram_item.updated_at = datetime.utcnow()
                    _web_plog(
                        "WEB_РЕАКТИВАЦИЯ",
                        order_id=order.order_id,
                        tariff=order.tariff_label,
                        provider="adapt",
                        context=f"action=upgrade adapt_uuid={adapt_uuid}",
                    )
                elif reactivation_already_applied:
                    _web_plog(
                        "WEB_ВЫДАЧА_СВЕРКА",
                        order_id=order.order_id,
                        tariff=order.tariff_label,
                        provider="adapt",
                        context=f"action=upgrade_reactivation adapt_uuid={adapt_uuid}",
                    )
                try:
                    resp = await api.upgrade_subscription(adapt_uuid, adapt_plan_uuid)
                except Exception as upgrade_err:
                    if isinstance(upgrade_err, AdaptAPIError) and "Not enough days" in str(upgrade_err):
                        order.adapt_mutation_state = _ADAPT_MUTATION_FAILED_CLOSED
                        issue = build_internal_access_error(
                            provider="adapt",
                            code="adapt_upgrade_reactivation_required",
                            status=upgrade_err.status,
                            admin_message=(
                                f"Adapt upgrade requires explicit reactivation for order {order.order_id}: "
                                f"{upgrade_err}"
                            ),
                            client_message=(
                                "Оплата получена, но автоматическая выдача не завершена. "
                                "Мы уже получили уведомление и разберёмся."
                            ),
                            raw_message=str(upgrade_err),
                        )
                        await _mark_order_failed(order, issue)
                        await session.commit()
                        return
                    raise
            verified_status = status_before if already_applied else await api.get_status(adapt_uuid)
            verified_plan_uuid = str(verified_status.get("plan_uuid") or "").strip()
            verified_devices = verified_status.get("devices")
            if (
                verified_plan_uuid != adapt_plan_uuid
                or verified_devices is None
            ):
                raise build_internal_access_error(
                    provider="adapt",
                    code="adapt_result_mismatch",
                    admin_message=(
                        f"Adapt result mismatch for web order {order.order_id}: "
                        f"expected_plan={adapt_plan_uuid} actual_plan={verified_plan_uuid or 'missing'} "
                        f"actual_devices={verified_devices}"
                    ),
                    client_message=(
                        "Оплата получена, но провайдер не подтвердил выбранный тариф. "
                        "Мы уже получили уведомление и разберёмся."
                    ),
                )
            verified_expires_at = (
                _adapt_expires_at_from_response(verified_status, 0)
                if verified_status.get("end_date")
                else None
            )
            if (
                action == "renew"
                and (
                    not order.target_snapshot_expires_at
                    or not verified_expires_at
                    or verified_expires_at <= order.target_snapshot_expires_at + timedelta(seconds=30)
                )
            ):
                raise build_internal_access_error(
                    provider="adapt",
                    code="adapt_renew_same_expiry",
                    admin_message=(
                        f"Adapt renew did not extend expiry for order {order.order_id}: "
                        f"current={verified_expires_at} baseline={order.target_snapshot_expires_at}"
                    ),
                    client_message=(
                        "Оплата получена, но провайдер не подтвердил продление подписки. "
                        "Мы уже получили уведомление и разберёмся."
                    ),
                )
            resp = {
                **resp,
                **{key: value for key, value in verified_status.items() if value is not None},
            }

            order.adapt_mutation_state = _ADAPT_MUTATION_CONFIRMED
            await session.commit()

            if order.fulfillment_freeze_state == "unfrozen_for_mutation":
                order.fulfillment_freeze_state = "mutation_applied"
                await session.commit()

            if was_originally_frozen:
                try:
                    restoration_status = await api.get_status(adapt_uuid)
                    already_restored = restoration_status.get("is_frozen") is True
                    if not already_restored:
                        logger.info(
                            "Restoring frozen state for Adapt subscription after web fulfillment: order=%s adapt_uuid=%s",
                            order.order_id,
                            adapt_uuid,
                        )
                        await api.freeze_subscription(adapt_uuid)
                        final_status = await api.get_status(adapt_uuid)
                        if final_status.get("is_frozen") is not True:
                            raise RuntimeError("Adapt provider did not confirm frozen state after restoration")
                    order.target_snapshot_is_frozen = True
                    order.fulfillment_freeze_state = "freeze_restored"
                    if target_telegram_item:
                        target_telegram_item.is_frozen = True
                    await session.commit()
                except Exception as exc:
                    issue = build_internal_access_error(
                        provider="adapt",
                        code="adapt_refreeze_failed",
                        admin_message=(
                            f"Adapt web mutation succeeded but restoring freeze failed for order={order.order_id} "
                            f"adapt_uuid={adapt_uuid}: {exc}"
                        ),
                        client_message=(
                            "Оплата получена и подписка продлена, но не удалось восстановить состояние заморозки. "
                            "Мы уже получили уведомление и восстановим заморозку."
                        ),
                        raw_message=str(exc),
                    )
                    logger.error(issue.admin_message)
                    await _mark_order_failed(order, issue)
                    return
        except AccessProvisionError as issue:
            logger.error(issue.admin_message)
            await _mark_order_failed(order, issue)
            return
        except AdaptAPIError as exc:
            issue = build_internal_access_error(
                provider="adapt",
                code=f"adapt_{action}_{exc.status or 'error'}",
                status=exc.status,
                admin_message=(
                    f"Adapt {action} error for order {order.order_id}: {exc} "
                    f"target={order.target_order_id} adapt_uuid={adapt_uuid}"
                ),
                raw_message=str(exc),
            )
            logger.error(issue.admin_message)
            await _mark_order_failed(order, issue)
            return
        except (asyncio.TimeoutError, TimeoutError) as exc:
            issue = build_internal_access_error(
                provider="adapt",
                code=f"adapt_{action}_timeout",
                status=504,
                admin_message=f"Adapt {action} timeout for order {order.order_id}: {exc}",
                raw_message=str(exc),
            )
            logger.error(issue.admin_message)
            await _mark_order_failed(order, issue)
            return
        except Exception as exc:
            issue = build_internal_access_error(
                provider="adapt",
                code=f"adapt_{action}_runtime",
                admin_message=f"Unexpected Adapt {action} error for order {order.order_id}: {exc}",
                raw_message=str(exc),
            )
            logger.error(issue.admin_message)
            await _mark_order_failed(order, issue)
            return

        order.marzban_username = f"adapt_{adapt_uuid}"
        source_url = primary.subscription_url if primary else (target_telegram_item.key_value if target_telegram_item else None)
        order.subscription_url = source_url or build_adapt_mirror_url(adapt_uuid)
        order.status = "delivered"
        order.failure_code = None
        order.failure_message = None
        order.failure_reason = None
        order.delivered_at = datetime.utcnow()
        order.access_expires_at = _adapt_expires_at_from_response(resp, order.days)
        if target_telegram_item:
            target_telegram_item.provider = "adapt"
            target_telegram_item.adapt_plan_uuid = adapt_plan_uuid
            target_telegram_item.device_slots = int(resp["devices"]) if resp.get("devices") is not None else target_telegram_item.device_slots
            target_telegram_item.expires_at = order.access_expires_at
            target_telegram_item.status = "active"
            target_telegram_item.updated_at = datetime.utcnow()
        _web_plog(
            "WEB_УЛУЧШЕНИЕ" if action == "upgrade" else "WEB_ПРОДЛЕНИЕ",
            order_id=order.order_id,
            tariff=order.tariff_label,
            provider="adapt",
            context=f"target={order.target_order_id} adapt_uuid={adapt_uuid}",
        )
        logger.info("Order %s %sd via Adapt: target=%s uuid=%s", order.order_id, action, order.target_order_id, adapt_uuid)
        return

    # Explicit new purchase (and legacy rows without an action): issue a fresh UUID.
    if int(order.fulfillment_attempts or 0) > 1 and order.failure_code not in {
        "adapt_api_402",
        "adapt_api_429",
    }:
        issue = build_internal_access_error(
            provider="adapt",
            code="adapt_create_awaiting_webhook",
            admin_message=(
                f"Adapt create response was ambiguous for order {order.order_id}; "
                "waiting for subs.created webhook to avoid a duplicate subscription"
            ),
            client_message=(
                "Оплата получена. Мы проверяем создание доступа, "
                "чтобы не списать деньги и не создать ключ повторно."
            ),
        )
        await _mark_order_failed(order, issue)
        return
    try:
        api = AdaptAPI()
        resp = await api.create_subscription(
            plan_uuid=adapt_plan_uuid,
            external_user_id=f"web_{order.order_id}",
        )
    except AdaptAPIError as exc:
        issue = build_internal_access_error(
            provider="adapt",
            code=f"adapt_api_{exc.status or 'error'}",
            admin_message=f"Adapt API error for order {order.order_id}: {exc}",
            raw_message=str(exc),
        )
        logger.error(issue.admin_message)
        await _mark_order_failed(order, issue)
        return
    except Exception as exc:
        issue = build_internal_access_error(
            provider="adapt",
            code="adapt_runtime",
            admin_message=f"Unexpected Adapt error for order {order.order_id}: {exc}",
            raw_message=str(exc),
        )
        logger.error(issue.admin_message)
        await _mark_order_failed(order, issue)
        return

    adapt_uuid = resp.get("uuid") or resp.get("subscription_uuid") or resp.get("id")
    if not adapt_uuid:
        issue = build_internal_access_error(
            provider="adapt",
            code="adapt_missing_uuid",
            admin_message=f"Adapt returned no UUID for order {order.order_id}: {resp}",
        )
        logger.error(issue.admin_message)
        await _mark_order_failed(order, issue)
        return

    branded_url = build_adapt_mirror_url(adapt_uuid)
    order.marzban_username = f"adapt_{adapt_uuid}"
    order.subscription_url = branded_url
    order.status = "delivered"
    order.failure_message = None
    order.failure_reason = None
    order.delivered_at = datetime.utcnow()
    order.access_expires_at = _adapt_expires_at_from_response(resp, order.days)
    _web_plog(
        "WEB_ВЫДАЧА",
        order_id=order.order_id,
        tariff=order.tariff_label,
        provider="adapt",
        context=f"adapt_uuid={adapt_uuid}",
    )
    logger.info("Order %s fulfilled via Adapt: uuid=%s", order.order_id, adapt_uuid)


async def _fulfill_device_slot_order(session, order: WebOrder) -> None:
    """Add an extra device slot without creating a separate Marzban user/key."""
    now = datetime.utcnow()
    primary = await _get_latest_web_access_order(session, order.profile_token) if order.profile_token else None
    tg_item: WebTelegramItem | None = None

    if primary and primary.access_expires_at and primary.access_expires_at > now and primary.subscription_url:
        if _get_order_provider(primary) != "marzban":
            issue = build_internal_access_error(
                provider="adapt",
                code="device_requires_plan_upgrade",
                admin_message=f"Device slot order {order.order_id}: provider plan upgrade required",
                client_message="Для этой подписки лимит устройств меняется только через улучшение тарифа.",
            )
            await _mark_order_failed(order, issue)
            return
        order.marzban_username = primary.marzban_username
        order.subscription_url = primary.subscription_url
        order.access_expires_at = primary.access_expires_at
    else:
        tg_result = await session.execute(
            select(WebTelegramItem)
            .where(WebTelegramItem.profile_token == order.profile_token)
            .where(WebTelegramItem.item_type == "vpn")
            .where(WebTelegramItem.status == "active")
            .where(WebTelegramItem.expires_at > now)
            .order_by(WebTelegramItem.expires_at.desc(), WebTelegramItem.id.desc())
            .limit(1)
        )
        tg_item = tg_result.scalar_one_or_none()
        if tg_item and tg_item.key_value and tg_item.expires_at:
            if _get_access_provider(tg_item.key_value, tg_item.external_id) != "marzban":
                issue = build_internal_access_error(
                    provider="adapt",
                    code="device_requires_plan_upgrade",
                    admin_message=f"Device slot order {order.order_id}: provider plan upgrade required",
                    client_message="Для этой подписки лимит устройств меняется только через улучшение тарифа.",
                )
                await _mark_order_failed(order, issue)
                return
            tg_item.device_slots = int(tg_item.device_slots or 1) + 1
            tg_item.updated_at = now
            order.marzban_username = tg_item.external_id
            order.subscription_url = tg_item.key_value
            order.access_expires_at = tg_item.expires_at
        else:
            issue = build_internal_access_error(
                provider="marzban",
                code="device_missing_parent_key",
                admin_message=f"Device slot order {order.order_id}: no active parent subscription key found",
            )
            logger.error(issue.admin_message)
            await _mark_order_failed(order, issue)
            return

    if not order.access_expires_at or order.access_expires_at <= now:
        issue = build_internal_access_error(
            provider="marzban",
            code="device_expired_parent",
            admin_message=f"Device slot order {order.order_id}: parent subscription already expired",
        )
        logger.error(issue.admin_message)
        await _mark_order_failed(order, issue)
        return

    order.status = "delivered"
    order.failure_message = None
    order.failure_reason = None
    order.delivered_at = now
    _web_plog(
        "WEB_ДОП_УСТРОЙСТВО",
        order_id=order.order_id,
        tariff=order.tariff_label,
        provider="same_key",
    )
    logger.info(
        "Device order %s fulfilled on existing key: parent=%s telegram_item=%s",
        order.order_id,
        primary.order_id if primary else None,
        tg_item.external_id if tg_item else None,
    )



def _verify_web_order_payment_snapshot(
    order: WebOrder,
    payment: YooKassaPaymentSnapshot,
) -> tuple[bool, str]:
    local_payment_id = str(order.yookassa_payment_id or "").strip()
    if local_payment_id and local_payment_id != payment.payment_id:
        return False, "payment_id_mismatch"
    if payment.currency != "RUB":
        return False, "currency_mismatch"
    if payment.amount_rub is None or payment.amount_rub != Decimal(str(order.amount_rub)):
        return False, "amount_mismatch"
    if str(payment.metadata.get("source") or "").strip() != "webstore":
        return False, "source_mismatch"
    if str(payment.metadata.get("order_id") or "").strip() != order.order_id:
        return False, "order_id_mismatch"
    return True, "ok"


def _verify_web_topup_payment_snapshot(
    topup: WebBalanceTopUp,
    payment: YooKassaPaymentSnapshot,
) -> tuple[bool, str]:
    local_payment_id = str(topup.yookassa_payment_id or "").strip()
    if local_payment_id and local_payment_id != payment.payment_id:
        return False, "payment_id_mismatch"
    if payment.currency != "RUB":
        return False, "currency_mismatch"
    if payment.amount_rub is None or payment.amount_rub != Decimal(str(topup.amount_rub)):
        return False, "amount_mismatch"
    if str(payment.metadata.get("source") or "").strip() != "webstore_balance_topup":
        return False, "source_mismatch"
    if str(payment.metadata.get("topup_id") or "").strip() != topup.topup_id:
        return False, "topup_id_mismatch"
    return True, "ok"


async def reconcile_yookassa_web_topup(
    topup_id: str,
    payment_id: str,
    *,
    source: str = "reconcile",
) -> str:
    """Verify and credit one YooKassa-backed web balance top-up idempotently."""
    topup_id = str(topup_id or "").strip()
    payment_id = str(payment_id or "").strip()
    if not topup_id or not payment_id:
        return "missing_reference"

    async with async_session() as session:
        topup = await session.scalar(
            select(WebBalanceTopUp).where(WebBalanceTopUp.topup_id == topup_id)
        )
        if not topup:
            return "topup_not_found"
        if topup.status == "completed":
            return "already_completed"
        local_payment_id = str(topup.yookassa_payment_id or "").strip()
        if local_payment_id and local_payment_id != payment_id:
            logger.error(
                "YooKassa topup reconcile rejected topup=%s: local payment %s != reconcile payment %s",
                topup_id,
                local_payment_id,
                payment_id,
            )
            return "payment_id_mismatch"

    try:
        payment = await fetch_yookassa_payment(payment_id)
    except YooKassaVerificationError as exc:
        logger.warning(
            "YooKassa verification unavailable for web topup %s payment %s (%s): %s",
            topup_id,
            payment_id,
            source,
            exc,
        )
        async with async_session() as session:
            topup = await session.scalar(
                select(WebBalanceTopUp).where(WebBalanceTopUp.topup_id == topup_id)
            )
            if topup:
                topup.last_reconciled_at = datetime.utcnow()
                await session.commit()
        return "provider_unavailable"

    async with async_session() as session:
        topup = await session.scalar(
            select(WebBalanceTopUp).where(WebBalanceTopUp.topup_id == topup_id)
        )
        if not topup:
            return "topup_not_found"
        if topup.status == "completed":
            return "already_completed"

        verified, reason = _verify_web_topup_payment_snapshot(topup, payment)
        if not verified:
            logger.error(
                "YooKassa verification mismatch for web topup %s payment %s: %s",
                topup_id,
                payment_id,
                reason,
            )
            topup.last_reconciled_at = datetime.utcnow()
            await session.commit()
            return f"verification_{reason}"

        if payment.status == "canceled":
            if topup.status == "pending":
                topup.status = "canceled"
            topup.last_reconciled_at = datetime.utcnow()
            await session.commit()
            return "canceled"

        if payment.status != "succeeded" or not payment.paid:
            topup.last_reconciled_at = datetime.utcnow()
            await session.commit()
            return f"provider_{payment.status or 'unknown'}"

        first_confirmation = topup.status != "paid"
        topup.status = "paid"
        topup.yookassa_payment_id = payment.payment_id
        topup.last_reconciled_at = datetime.utcnow()
        await session.commit()
        amount_rub = topup.amount_rub
        telegram_id = topup.telegram_id

    if first_confirmation:
        _web_plog(
            "WEB_ОПЛАТА",
            order_id=topup_id,
            tariff="balance_topup",
            amount=amount_rub,
            method="yookassa",
            source=source,
        )

    credit_result = await _credit_local_balance_topup(topup_id)
    if credit_result.get("status") in {"credited", "already_credited"}:
        return "completed"

    if telegram_id:
        async with async_session() as session:
            topup = await session.scalar(
                select(WebBalanceTopUp).where(WebBalanceTopUp.topup_id == topup_id)
            )
            if not topup:
                return "topup_not_found"
        credit_result = await _credit_web_balance_topup(topup)
        if credit_result.get("status") in {"credited", "already_credited"}:
            async with async_session() as session:
                topup = await session.scalar(
                    select(WebBalanceTopUp).where(WebBalanceTopUp.topup_id == topup_id)
                )
                if not topup:
                    return "topup_not_found"
                topup.status = "completed"
                topup.completed_at = datetime.utcnow()
                await session.commit()
            _web_plog(
                "WEB_ПОПОЛНЕНИЕ",
                order_id=topup_id,
                tariff="balance_topup",
                amount=amount_rub,
                status="completed",
                source=source,
            )
            return "completed"

    # Keep provider-confirmed money in a retryable local state. The scheduler
    # will retry crediting instead of converting a transient delivery problem
    # into a terminal failed payment.
    logger.warning(
        "Verified YooKassa topup %s could not be credited yet; leaving status=paid for retry",
        topup_id,
    )
    return "paid_credit_pending"


async def reconcile_yookassa_web_order(
    order_id: str,
    payment_id: str,
    *,
    source: str = "reconcile",
) -> str:
    """Verify YooKassa state and safely advance one web order.

    The provider API is authoritative.  Webhooks and the scheduler only trigger
    this reconciliation; neither is trusted to declare a payment successful.
    """
    order_id = str(order_id or "").strip()
    payment_id = str(payment_id or "").strip()
    if not order_id or not payment_id:
        return "missing_reference"

    async with async_session() as session:
        order = await session.scalar(
            select(WebOrder).where(WebOrder.order_id == order_id)
        )
        if not order:
            return "order_not_found"
        if order.status == "delivered":
            return "already_delivered"
        local_payment_id = str(order.yookassa_payment_id or "").strip()
        if local_payment_id and local_payment_id != payment_id:
            logger.error(
                "YooKassa reconcile rejected order=%s: local payment %s != callback/reconcile payment %s",
                order_id,
                local_payment_id,
                payment_id,
            )
            return "payment_id_mismatch"
        profile_lock_key = order.profile_token or f"web-order:{order.order_id}"

    try:
        payment = await fetch_yookassa_payment(payment_id)
    except YooKassaVerificationError as exc:
        logger.warning(
            "YooKassa verification unavailable for web order %s payment %s (%s): %s",
            order_id,
            payment_id,
            source,
            exc,
        )
        async with async_session() as session:
            order = await session.scalar(
                select(WebOrder).where(WebOrder.order_id == order_id)
            )
            if order:
                order.last_reconciled_at = datetime.utcnow()
                await session.commit()
        return "provider_unavailable"

    async with _acquire_profile_lock(profile_lock_key):
        async with async_session() as session:
            order = await session.scalar(
                select(WebOrder).where(WebOrder.order_id == order_id)
            )
            if not order:
                return "order_not_found"
            if order.status == "delivered":
                return "already_delivered"
            if order.status == "failed":
                return "fulfillment_failed"

            verified, reason = _verify_web_order_payment_snapshot(order, payment)
            if not verified:
                logger.error(
                    "YooKassa verification mismatch for web order %s payment %s: %s",
                    order_id,
                    payment_id,
                    reason,
                )
                order.last_reconciled_at = datetime.utcnow()
                await session.commit()
                return f"verification_{reason}"

            if payment.status == "canceled":
                if order.status in {"pending", "canceled"}:
                    order.status = "canceled"
                    order.cancellation_confirmed = True
                    order.last_reconciled_at = datetime.utcnow()
                    await session.commit()
                return "canceled"

            if payment.status not in {"succeeded"} or not payment.paid:
                order.last_reconciled_at = datetime.utcnow()
                await session.commit()
                return f"provider_{payment.status or 'unknown'}"

            first_confirmation = order.status != "paid"
            if first_confirmation:
                order.status = "paid"
                order.cancellation_confirmed = False
                order.paid_at = order.paid_at or datetime.utcnow()
                order.yookassa_payment_id = payment.payment_id
                order.last_reconciled_at = datetime.utcnow()
                await session.commit()
                _web_plog(
                    "WEB_ОПЛАТА",
                    order_id=order.order_id,
                    tariff=order.tariff_label,
                    amount=order.amount_rub,
                    method="yookassa",
                    source=source,
                )
                try:
                    await _notify_admins_webstore(order, "paid")
                except Exception as exc:
                    logger.warning(
                        "Admin paid notification failed for reconciled web order %s: %s",
                        order.order_id,
                        exc,
                    )
            elif (
                order.next_fulfillment_retry_at
                and order.next_fulfillment_retry_at > datetime.utcnow()
            ):
                order.last_reconciled_at = datetime.utcnow()
                await session.commit()
                return "paid_retry_scheduled"
            else:
                order.last_reconciled_at = datetime.utcnow()
                await session.commit()

            await attempt_fulfill_order(session, order)
            is_delivered = order.status == "delivered"
            if is_delivered:
                await _disable_web_balance_autodebit_after_tariff_purchase(session, order)
                await _apply_order_bonus_spend(session, order)
            await session.commit()

            if not is_delivered:
                return order.status

            bot_result = await _notify_bot_order_delivered(order, session=session)
            await _finalize_order_cashback(session, order, bot_result)
            await session.commit()
            try:
                await _notify_admins_webstore(order, "delivered")
            except Exception as exc:
                logger.warning(
                    "Admin delivery notification failed for reconciled web order %s: %s",
                    order.order_id,
                    exc,
                )
            if not bot_result.get("delivered"):
                await _notify_linked_webstore_user(order, "delivered")
            await _sync_referral_credit_for_order(order.order_id)
            return "delivered"


async def handle_yookassa_webhook(request: web.Request) -> web.Response:
    # The webhook is only a trigger. Payment success is always re-read from the
    # authenticated YooKassa API before any balance credit or VPN fulfillment.
    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Bad JSON")

    if data.get("event", "") != "payment.succeeded":
        return web.Response(text="OK")

    obj = data.get("object", {}) or {}
    payment_id = str(obj.get("id") or "").strip()
    metadata = obj.get("metadata", {}) or {}
    order_id = str(metadata.get("order_id") or "").strip()
    topup_id = str(metadata.get("topup_id") or "").strip()

    if metadata.get("source") == "webstore_balance_topup" and topup_id:
        status = await reconcile_yookassa_web_topup(
            topup_id,
            payment_id,
            source="webhook",
        )
        if status == "provider_unavailable":
            return web.Response(status=503, text="Verification unavailable")
        logger.info(
            "YooKassa webhook reconciled web topup %s payment %s: %s",
            topup_id,
            payment_id,
            status,
        )
        return web.Response(text="OK")

    if not order_id or metadata.get("source") != "webstore" or not payment_id:
        return web.Response(text="OK")

    status = await reconcile_yookassa_web_order(
        order_id,
        payment_id,
        source="webhook",
    )
    if status == "provider_unavailable":
        # Non-2xx asks YooKassa to retry later. No service is issued on an
        # unverifiable callback.
        return web.Response(status=503, text="Verification unavailable")

    logger.info(
        "YooKassa webhook reconciled web order %s payment %s: %s",
        order_id,
        payment_id,
        status,
    )
    return web.Response(text="OK")


# ── Profile ──


async def handle_profile_page(request: web.Request) -> web.Response:
    login_code = request.query.get("login", "").strip()
    if login_code:
        async with async_session() as session:
            auth_code = await session.scalar(
                select(WebTelegramAuthCode).where(WebTelegramAuthCode.code == login_code)
            )
            if (
                auth_code
                and auth_code.profile_token
                and auth_code.consumed_at is not None
                and auth_code.expires_at >= datetime.utcnow()
            ):
                profile_token = auth_code.profile_token
                result = await session.execute(delete(WebTelegramAuthCode).where(WebTelegramAuthCode.id == auth_code.id))
                await session.commit()
                if not result.rowcount:
                    raise web.HTTPFound("/profile")
                response = await _auth_response({"ok": True}, profile_token, method="telegram")
                response.set_status(302)
                response.headers["Location"] = "/profile"
                return response

    token = request.query.get("token", "")
    if not token:
        # Show email lookup form
        html = _render("profile.html", PROFILE_MODE="lookup", SUPPORT_URL="/support", DEVICE_PRICE=settings.extra_device_price_rub, VK_FOOTER_HTML="")
        return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})
    html = _render("profile.html", PROFILE_MODE="token", SUPPORT_URL="/support", DEVICE_PRICE=settings.extra_device_price_rub, VK_FOOTER_HTML="")
    return web.Response(text=html, content_type="text/html", headers={"Cache-Control": "no-store"})


async def handle_auth_me(request: web.Request) -> web.Response:
    token = await _authenticated_profile(request)
    if not token:
        return web.json_response({"authenticated": False})

    async with async_session() as session:
        account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == token).limit(1))
        if account:
            email = await session.scalar(select(WebEmail.email).where(WebEmail.profile_token == token))
            return web.json_response({
                "authenticated": True,
                "web_account": True,
                "contact": email or account.contact,
                "email_confirmed": bool(email),
                "profile_token": account.profile_token,
            })
        link = await session.get(WebProfileLink, token)
        if link:
            return web.json_response({
                "authenticated": True,
                "contact": link.contact or link.telegram_username or "Telegram",
                "profile_token": token,
            })
        has_orders = await session.scalar(select(WebOrder.id).where(WebOrder.profile_token == token).limit(1))
        if has_orders:
            contact = await session.scalar(select(WebOrder.contact).where(WebOrder.profile_token == token).limit(1))
            return web.json_response({
                "authenticated": True,
                "contact": contact or "Профиль",
                "profile_token": token,
            })

    response = web.json_response({"authenticated": False})
    _clear_profile_cookie(response)
    return response


def _valid_email(value: str) -> bool:
    return len(value) <= 254 and bool(re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9-]+(?:\.[a-z0-9-]+)+", value))


async def _allow_auth_attempt(kind: str, value: str, limit: int, minutes: int) -> bool:
    now = datetime.utcnow()
    bucket = int(now.timestamp()) // (minutes * 60)
    subject = hashlib.sha256(f"{kind}:{value}:{bucket}".encode()).hexdigest()
    async with async_session() as session:
        statement = sqlite_insert(WebAuthLimit).values(subject=subject, count=1, expires_at=now + timedelta(minutes=minutes * 2))
        statement = statement.on_conflict_do_update(
            index_elements=[WebAuthLimit.subject], set_={"count": WebAuthLimit.count + 1},
            where=WebAuthLimit.count < limit).returning(WebAuthLimit.count)
        count = await session.scalar(statement)
        if secrets.randbelow(100) == 0:
            await session.execute(delete(WebAuthLimit).where(WebAuthLimit.expires_at < now))
            await session.execute(delete(WebEmailToken).where(WebEmailToken.expires_at < now))
            await session.execute(delete(WebSession).where(WebSession.expires_at < now))
        await session.commit()
        return count is not None


async def _send_auth_link(email: str, purpose: str, profile_token: str | None = None, password_hash: str | None = None,
                          session_hash: str | None = None, expected_password_hash: str | None = None) -> None:
    if not await _allow_auth_attempt("mail", email, 3, 60):
        return
    raw = secrets.token_urlsafe(32)
    async with async_session() as session:
        digest = hashlib.sha256(raw.encode()).hexdigest()
        expires = datetime.utcnow() + timedelta(minutes=30 if purpose == "reset" else 60)
        if purpose == "change":
            result = await session.execute(insert(WebEmailToken).from_select(
                ["token_hash", "purpose", "email", "profile_token", "password_hash", "expires_at"],
                select(literal(digest), literal(purpose), literal(email), WebAccount.profile_token, literal(None), literal(expires))
                .join(WebSession, WebSession.profile_token == WebAccount.profile_token)
                .where(WebAccount.profile_token == profile_token, WebAccount.password_hash == expected_password_hash,
                       WebSession.token_hash == session_hash, WebSession.expires_at > datetime.utcnow())))
            if result.rowcount != 1:
                await session.rollback()
                return
        else:
            session.add(WebEmailToken(token_hash=digest, purpose=purpose, email=email, profile_token=profile_token,
                                      password_hash=password_hash, expires_at=expires))
        await session.commit()
    path = "password-reset" if purpose == "reset" else "email-confirm"
    link = f"{settings.subscription_base_url.rstrip('/')}/{path}#{raw}"
    try:
        await send_email(email, "Darimiru: подтверждение", f"Откройте ссылку: {link}")
    except Exception:
        logger.error("Email delivery failed")


async def handle_auth_register(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        email = _normalize_contact(str(body.get("contact") or ""))
        password = str(body.get("password") or "")
        password2 = str(body.get("password2") or "")
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not _valid_email(email):
        return web.json_response({"error": "Введите email"}, status=400)
    if len(password) < 8 or len(password) > 1024 or password != password2:
        return web.json_response({"error": "Проверьте пароль (от 8 знаков)"}, status=400)
    if await _allow_auth_attempt("register-ip", _get_client_ip(request), 10, 60):
        async with async_session() as session:
            existing = await session.scalar(select(WebAccount.id).where(WebAccount.contact == email))
            confirmed = await session.get(WebEmail, email)
        if not existing and not confirmed:
            await _send_auth_link(email, "register", password_hash=_hash_password(password))
    return web.json_response({"ok": True})


async def handle_auth_login(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    contact = _normalize_contact(body.get("contact", ""))
    password = str(body.get("password", ""))
    if not contact or not password:
        return web.json_response({"error": "Введите контакт и пароль"}, status=400)

    async with async_session() as session:
        confirmed = await session.get(WebEmail, contact)
        account = await session.scalar(select(WebAccount).where(
            WebAccount.profile_token == confirmed.profile_token if confirmed else WebAccount.contact == contact).limit(1))
        linked_email = await session.scalar(select(WebEmail.email).where(WebEmail.profile_token == account.profile_token)) if account else None
        if not await _allow_auth_attempt("login-ip", _get_client_ip(request), 30, 15) or not await _allow_auth_attempt("login-contact", contact, 10, 15):
            return web.json_response({"error": "Неверный контакт или пароль"}, status=401)
        valid_password = _verify_password(password, account.password_hash if account else _DUMMY_PASSWORD_HASH)
        if not account or (linked_email and contact != linked_email) or not valid_password:
            return web.json_response({"error": "Неверный контакт или пароль"}, status=401)
        account.last_login_at = datetime.utcnow()
        await _get_or_create_web_balance_account(session, account.profile_token, account.contact)
        await session.commit()
        token = account.profile_token
        password_hash = account.password_hash

    return await _auth_response({"ok": True, "token": token, "contact": contact}, token, expected_password_hash=password_hash)


async def handle_email_change(request: web.Request) -> web.Response:
    profile = await _authenticated_profile(request)
    if not profile:
        return web.json_response({"error": "Войдите в профиль"}, status=401)
    try:
        body = await request.json()
        email = _normalize_contact(str(body.get("email") or ""))
        password = str(body.get("password") or "")
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    if not _valid_email(email):
        return web.json_response({"error": "Введите email"}, status=400)
    if not await _allow_auth_attempt("change-ip", _get_client_ip(request), 10, 60) or not await _allow_auth_attempt("change-profile", profile, 10, 15):
        return web.json_response({"error": "Попробуйте позже"}, status=429)
    async with async_session() as session:
        account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == profile))
        cookie = request.cookies.get(_AUTH_COOKIE, "")
        current_session = await session.get(WebSession, hashlib.sha256(cookie.encode()).hexdigest())
        telegram_verified = bool(current_session and current_session.profile_token == profile and
                                 current_session.method == "telegram" and
                                 current_session.created_at > datetime.utcnow() - timedelta(minutes=10))
        if not account or not (telegram_verified or _verify_password(password, account.password_hash)):
            return web.json_response({"error": "Неверный пароль"}, status=401)
        existing = await session.get(WebEmail, email)
        contact_owner = await session.scalar(select(WebAccount).where(WebAccount.contact == email))
        if (existing and existing.profile_token != profile) or (contact_owner and contact_owner.profile_token != profile):
            return web.json_response({"error": "Email уже используется"}, status=409)
        expected_hash = account.password_hash
    await _send_auth_link(email, "change", profile_token=profile,
                          session_hash=hashlib.sha256(cookie.encode()).hexdigest(), expected_password_hash=expected_hash)
    return web.json_response({"ok": True})


async def handle_forgot_password(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        email = _normalize_contact(str(body.get("email") or ""))
    except Exception:
        email = ""
    allowed = await _allow_auth_attempt("reset-ip", _get_client_ip(request), 10, 60)
    if allowed and _valid_email(email):
        async with async_session() as session:
            confirmed = await session.get(WebEmail, email)
            profile = confirmed.profile_token if confirmed else None
            if not confirmed:
                account = await session.scalar(select(WebAccount).where(WebAccount.contact == email))
                if account and not await session.scalar(select(WebEmail.email).where(WebEmail.profile_token == account.profile_token)):
                    profile = account.profile_token
        if profile:
            await _send_auth_link(email, "reset", profile_token=profile)
    return web.json_response({"ok": True})


async def handle_email_token(request: web.Request) -> web.Response:
    try:
        body = await request.json()
        raw = str(body.get("token") or "")
        password = str(body.get("password") or "")
    except Exception:
        return web.json_response({"error": "Недействительная ссылка"}, status=400)
    if len(raw) > 128 or not raw or not await _allow_auth_attempt("token-ip", _get_client_ip(request), 30, 15):
        return web.json_response({"error": "Недействительная ссылка"}, status=400)
    digest = hashlib.sha256(raw.encode()).hexdigest()
    purpose = "reset" if request.path.endswith("reset") else "confirm"
    async with async_session() as session:
        token = await session.get(WebEmailToken, digest)
        if not token or token.expires_at <= datetime.utcnow() or (token.purpose == "reset") != (purpose == "reset"):
            return web.json_response({"error": "Недействительная или устаревшая ссылка"}, status=400)
        if purpose == "reset" and (len(password) < 8 or len(password) > 1024):
            return web.json_response({"error": "Пароль должен быть не короче 8 знаков"}, status=400)
        result = await session.execute(delete(WebEmailToken).where(WebEmailToken.token_hash == digest, WebEmailToken.expires_at > datetime.utcnow()))
        if not result.rowcount:
            await session.rollback()
            return web.json_response({"error": "Недействительная ссылка"}, status=400)
        try:
            if token.purpose == "register":
                existing = await session.scalar(select(WebAccount.id).where(WebAccount.contact == token.email))
                if existing or await session.get(WebEmail, token.email):
                    await session.rollback()
                    return web.json_response({"error": "Email уже используется"}, status=409)
                profile = _make_profile_token(token.email)
                if await session.scalar(select(WebAccount.id).where(WebAccount.profile_token == profile)):
                    await session.rollback()
                    return web.json_response({"error": "Обратитесь в поддержку"}, status=409)
                session.add(WebAccount(contact=token.email, profile_token=profile, password_hash=token.password_hash))
                session.add(WebEmail(email=token.email, profile_token=profile))
                await _get_or_create_web_balance_account(session, profile, token.email)
            elif token.purpose == "change":
                owner = await session.get(WebEmail, token.email)
                contact_owner = await session.scalar(select(WebAccount).where(WebAccount.contact == token.email))
                if (owner and owner.profile_token != token.profile_token) or (contact_owner and contact_owner.profile_token != token.profile_token):
                    await session.rollback()
                    return web.json_response({"error": "Email уже используется"}, status=409)
                await session.execute(delete(WebEmail).where(WebEmail.profile_token == token.profile_token))
                await session.execute(delete(WebEmailToken).where(WebEmailToken.profile_token == token.profile_token, WebEmailToken.purpose == "change"))
                session.add(WebEmail(email=token.email, profile_token=token.profile_token))
            else:
                email = await session.get(WebEmail, token.email)
                account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == token.profile_token))
                if not account:
                    await session.rollback()
                    return web.json_response({"error": "Недействительная ссылка"}, status=400)
                if not email:
                    existing_email = await session.scalar(select(WebEmail.email).where(WebEmail.profile_token == token.profile_token))
                    if existing_email or _normalize_contact(account.contact) != token.email or not _valid_email(account.contact):
                        await session.rollback()
                        return web.json_response({"error": "Недействительная ссылка"}, status=400)
                    session.add(WebEmail(email=token.email, profile_token=token.profile_token))
                elif email.profile_token != token.profile_token:
                    await session.rollback()
                    return web.json_response({"error": "Недействительная ссылка"}, status=400)
                account.password_hash = _hash_password(password)
                await session.execute(delete(WebSession).where(WebSession.profile_token == token.profile_token))
                await session.execute(delete(WebTelegramAuthCode).where(WebTelegramAuthCode.profile_token == token.profile_token))
                await session.execute(delete(WebEmailToken).where(WebEmailToken.profile_token == token.profile_token))
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return web.json_response({"error": "Email уже используется"}, status=409)
    return web.json_response({"ok": True})


async def handle_email_page(request: web.Request) -> web.Response:
    reset = request.path == "/password-reset"
    if request.query.get("flow") == "otp":
        return web.Response(text=_render("email_otp.html"), content_type="text/html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
    return web.Response(text=_render("email_auth.html", RESET="true" if reset else "false"), content_type="text/html", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})


async def handle_auth_logout(request: web.Request) -> web.Response:
    token = request.cookies.get(_AUTH_COOKIE, "")
    if token:
        async with async_session() as session:
            await session.execute(delete(WebSession).where(WebSession.token_hash == hashlib.sha256(token.encode()).hexdigest()))
            await session.commit()
    response = web.json_response({"ok": True})
    _clear_profile_cookie(response)
    return response


async def handle_create_profile(request: web.Request) -> web.Response:
    """Create or return a web profile demo key for an authenticated user."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    authenticated = (await _authenticated_profile(request)).strip()
    requested_token = str(body.get("token") or "").strip()
    if not authenticated:
        return web.json_response(
            {"error": "Сначала зарегистрируйтесь или войдите, чтобы демо сохранилось в личном кабинете.", "auth_required": True},
            status=403 if _has_raw_auth_cookie(request) else 401,
        )
    if requested_token and requested_token != authenticated:
        return web.json_response({"error": "Войдите в профиль", "auth_required": True}, status=403)
    profile_token = authenticated

    async with async_session() as session:
        web_account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == profile_token).limit(1))
        linked_profile = await session.get(WebProfileLink, profile_token)
        confirmed_email = await session.scalar(select(WebEmail.email).where(WebEmail.profile_token == profile_token)) if web_account else None
        contact = confirmed_email or (_normalize_contact(web_account.contact) if web_account and web_account.contact else None)
        if not contact and linked_profile and linked_profile.contact:
            contact = _normalize_contact(linked_profile.contact)
        account = await _get_or_create_web_balance_account(session, profile_token, contact)
        demo_url = await _maybe_issue_web_demo_key(session, account)
        if not demo_url:
            # Return existing demo URL if user already received one
            existing_demo = await session.execute(
                select(WebOrder).where(
                    WebOrder.profile_token == account.profile_token,
                    WebOrder.tariff_key == "demo",
                ).limit(1)
            )
            existing_demo_order = existing_demo.scalar_one_or_none()
            if existing_demo_order and existing_demo_order.subscription_url:
                demo_url = _display_order_subscription_url(existing_demo_order) or existing_demo_order.subscription_url
        ref_code = account.ref_code
        await session.commit()

    site_url = f"{_resolve_referral_base_url(request)}/buy?ref={ref_code}"
    response: dict = {
        "token": profile_token,
        "ref_code": ref_code,
        "ref_url": site_url,
    }
    if demo_url:
        response["demo_subscription_url"] = demo_url
        response["demo_days"] = settings.demo_key_days
    return web.json_response(response)


async def handle_profile_lookup(request: web.Request) -> web.Response:
    """Look up guest profile token by verified contact and paid/delivered order ID."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    contact = _normalize_contact(body.get("contact", ""))
    order_id = body.get("order_id", "").strip().lower()
    if not contact or not order_id:
        return web.json_response({"error": "Введите контакт и номер заказа"}, status=400)

    if (
        not await _allow_auth_attempt("lookup-ip", _get_client_ip(request), 20, 15)
        or not await _allow_auth_attempt("lookup-contact", contact, 8, 15)
    ):
        return web.json_response({"error": "Слишком много попыток. Попробуйте позже."}, status=429)

    token = _make_profile_token(contact)

    async with async_session() as session:
        protected = bool(
            await session.scalar(select(WebAccount.id).where(WebAccount.profile_token == token).limit(1))
            or await session.get(WebProfileLink, token)
        )
        if protected:
            return web.json_response(
                {"error": "Для этого профиля включена защита входа. Войдите по паролю, коду с почты или через Telegram.", "auth_required": True},
                status=401,
            )
        result = await session.execute(
            select(WebOrder).where(
                WebOrder.profile_token == token,
                WebOrder.order_id == order_id,
                WebOrder.status.in_(("paid", "delivered")),
            ).limit(1)
        )
        order = result.scalar_one_or_none()
        if not order:
            return web.json_response(
                {"error": "Заказ с такими данными не найден"},
                status=404,
            )

    return await _auth_response({"token": token}, token, method="order_lookup")


async def handle_store_asset(request: web.Request) -> web.Response:
    filename = request.match_info.get("filename", "")
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return web.Response(status=400)
    file_path = Path(__file__).parent / "assets" / filename
    if not file_path.is_file():
        return web.Response(status=404)
    content_type = (
        "image/jpeg"
        if filename.lower().endswith((".jpeg", ".jpg"))
        else ("image/png" if filename.lower().endswith(".png") else "application/octet-stream")
    )
    return web.Response(
        body=file_path.read_bytes(),
        content_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def _resolve_profile_purchase_target(session, profile_token, target):
    if target.startswith("tgitem:"):
        raw_id = target.removeprefix("tgitem:")
        if not raw_id.isdigit():
            return None
        item = await session.scalar(select(WebTelegramItem).where(
            WebTelegramItem.id == int(raw_id), WebTelegramItem.profile_token == profile_token,
            WebTelegramItem.item_type == "vpn",
        ))
        return item
    if target.startswith("tg:"):
        uuid = target.removeprefix("tg:")
        items = (await session.scalars(select(WebTelegramItem).where(
            WebTelegramItem.profile_token == profile_token, WebTelegramItem.item_type == "vpn",
        ))).all()
        return next((item for item in items if _extract_adapt_uuid(item.key_value, item.external_id) == uuid), None)
    raw_id = target.removeprefix("order:")
    order = await session.scalar(select(WebOrder).where(
        or_(WebOrder.order_id == raw_id, WebOrder.id == int(raw_id) if raw_id.isdigit() else False),
        WebOrder.profile_token == profile_token, WebOrder.status.in_(("delivered", "demo", "paid")),
    ).limit(1))
    return order


async def handle_quick_renew_lookup(request: web.Request) -> web.Response:
    """Look up subscription details by subscription link or UUID for quick renew."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    query = str(body.get("query") or "").strip()
    target = str(body.get("target_order_id") or "").strip()
    profile_token = None
    profile_item = None
    if not query and target:
        if target.startswith(("uuid:", "adapt:")):
            query = target
        else:
            profile_token = (await _authenticated_profile(request))
            if not profile_token:
                return web.json_response({"error": "Сначала войдите в личный кабинет"}, status=401)
            async with async_session() as session:
                query = await _resolve_profile_purchase_target(session, profile_token, target)
            if isinstance(query, WebOrder) and (
                query.vhq_order_id or get_vhq_spec_for_store_tariff(get_store_tariffs_by_key().get(query.tariff_key))
            ):
                renew_tariffs = [
                    {**tariff, "payable_price_rub": int(tariff["price_rub"])}
                    for tariff in get_store_tariffs()
                    if get_vhq_spec_for_store_tariff(tariff) == {"tier": "basic", "days": 30}
                    and int(tariff.get("price_rub") or 0) > 0
                ]
                return web.json_response({
                    "valid": True, "is_trial": False,
                    "is_expired": bool(query.access_expires_at and query.access_expires_at <= datetime.utcnow()),
                    "renew_tariffs": renew_tariffs, "upgrade_tariffs": [],
                })
            if isinstance(query, WebOrder):
                query = _extract_adapt_uuid_from_web_order(query)
            elif isinstance(query, WebTelegramItem):
                profile_item = query
                query = _extract_adapt_uuid(profile_item.key_value, profile_item.external_id)
            if not query:
                return web.json_response({"error": "Подписка не найдена"}, status=404)
    if not query:
        return web.json_response({"error": "Введите ссылку или ключ подписки"}, status=400)

    adapt_uuid = _extract_adapt_uuid(query)
    if not adapt_uuid:
        return web.json_response(
            {"error": "Не удалось распознать ссылку или ключ подписки. Проверьте формат ссылки."},
            status=400,
        )

    current_plan_uuid = ""
    expires_at = None
    devices = None
    is_active = True
    is_explicit_demo = False

    # 1. Search local known records first (zero Adapt API call)
    async with async_session() as session:
        local_order = None if profile_item else await session.scalar(
            select(WebOrder)
            .where(
                or_(
                    WebOrder.subscription_url.contains(adapt_uuid),
                    WebOrder.marzban_username == f"adapt_{adapt_uuid}",
                )
            )
            .where(WebOrder.profile_token == profile_token if profile_token else True)
            .order_by(WebOrder.id.desc())
            .limit(1)
        )
        local_tg_item = profile_item
        if not local_order and not local_tg_item:
            local_tg_item = await session.scalar(
                select(WebTelegramItem)
                .where(
                    or_(
                        WebTelegramItem.key_value.contains(adapt_uuid),
                        WebTelegramItem.external_id.contains(adapt_uuid),
                    )
                )
                .where(WebTelegramItem.profile_token == profile_token if profile_token else True)
                .order_by(WebTelegramItem.id.desc())
                .limit(1)
            )

    if local_order:
        current_plan_uuid = _web_order_current_adapt_plan_uuid(local_order)
        expires_at = local_order.access_expires_at
        is_explicit_demo = bool(
            local_order.status == "demo" or local_order.tariff_key == "demo"
        )
    elif local_tg_item:
        current_plan_uuid = str(local_tg_item.adapt_plan_uuid or "").strip()
        expires_at = local_tg_item.expires_at
    else:
        # 2. Fallback to Adapt API only for genuinely unknown UUID
        try:
            api = AdaptAPI()
            status = await api.get_status(adapt_uuid)
        except AdaptAPIError as exc:
            if exc.status == 404:
                return web.json_response(
                    {"error": "Подписка с таким ключом не найдена у провайдера."},
                    status=404,
                )
            return web.json_response(
                {"error": f"Ошибка проверки подписки: {exc}"},
                status=502,
            )
        except Exception as exc:
            logger.error("Error looking up adapt status for uuid %s: %s", adapt_uuid, exc)
            return web.json_response(
                {"error": "Не удалось проверить статус подписки. Попробуйте позже."},
                status=502,
            )

        current_plan_uuid = str(status.get("plan_uuid") or "").strip()
        devices = status.get("devices")
        end_date_raw = status.get("end_date")
        expires_at = _adapt_expires_at_from_response(status, 0) if end_date_raw else None
        is_active = bool(status.get("is_active"))

    now = datetime.utcnow()
    is_expired = bool(expires_at and expires_at <= now)

    all_tariffs = get_store_tariffs()
    current_tariff = (
        get_adapt_plan_metadata_by_uuid(current_plan_uuid)
        if current_plan_uuid
        else None
    )
    if not current_tariff and current_plan_uuid:
        current_tariff = next(
            (
                tariff for tariff in all_tariffs
                if str(tariff.get("adapt_plan_uuid") or "").strip() == current_plan_uuid
            ),
            None,
        )
    is_trial = bool(
        is_explicit_demo
        or (current_plan_uuid and current_plan_uuid == settings.adapt_demo_plan_uuid)
    )

    from bot.services.subscription_semantics import has_adapt_plan_metadata

    if not current_tariff or not has_adapt_plan_metadata(current_tariff):
        return web.json_response(
            {"error": "Для этой подписки продление или улучшение через сайт недоступно. Обратитесь в поддержку."},
            status=400,
        )

    renew_tariffs = []
    upgrade_tariffs = []

    async with async_session() as lookup_session:
        for t in all_tariffs:
            plan_uuid = str(t.get("adapt_plan_uuid") or "").strip()
            if not plan_uuid:
                continue
            if int(t.get("price_rub") or 0) <= 0:
                continue
            if plan_uuid == settings.adapt_demo_plan_uuid:
                continue  # do not offer demo plan as purchase options

            try:
                action, price = await _quote_adapt_purchase(
                    lookup_session, current_plan_uuid=current_plan_uuid, current_tariff=current_tariff,
                    tariff=t, expires_at=expires_at, is_trial=is_trial, now=now,
                )
            except ValueError:
                continue
            tariff_item = {**t, "payable_price_rub": price}
            (renew_tariffs if action == "renew" else upgrade_tariffs).append(tariff_item)

    renew_tariffs.sort(key=lambda x: (x.get("days", 0), x.get("price_rub", 0)))
    upgrade_tariffs.sort(key=lambda x: (x.get("price_rub", 0), x.get("days", 0)))

    if is_trial:
        plan_title = "Тестовый доступ"
    elif current_tariff:
        plan_title = current_tariff.get("label")
    else:
        plan_title = f"Тариф {devices} устр." if devices else "Активный тариф"

    unavailable_message = None
    if is_trial and not upgrade_tariffs:
        unavailable_message = "Переход на платный тариф пока недоступен. Напишите в поддержку."

    return web.json_response({
        "valid": True,
        "adapt_uuid": adapt_uuid,
        "subscription_url": build_adapt_mirror_url(adapt_uuid),
        "plan_uuid": current_plan_uuid,
        "plan_title": plan_title,
        "devices": devices or (current_tariff.get("device_limit") if current_tariff else 1),
        "expires_at": expires_at.replace(tzinfo=timezone.utc).isoformat() if expires_at else None,
        "is_expired": is_expired,
        "is_trial": is_trial,
        "is_active": is_active,
        "renew_tariffs": renew_tariffs,
        "upgrade_tariffs": upgrade_tariffs,
        "unavailable_message": unavailable_message,
    })


async def handle_profile_link_init(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    authenticated = (await _authenticated_profile(request)).strip()
    requested_token = str(body.get("token") or "").strip()
    if not authenticated and not requested_token:
        return web.json_response({"error": "Missing token"}, status=400)
    if not authenticated:
        return web.json_response(
            {"error": "Войдите в профиль"},
            status=403 if _has_raw_auth_cookie(request) else 401,
        )
    if requested_token and requested_token != authenticated:
        return web.json_response({"error": "Войдите в профиль"}, status=403)
    token = authenticated

    async with async_session() as session:
        orders, _, _ = await _get_profile_bundle(session, token)
        account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == token))
        link = await session.get(WebProfileLink, token)
        if not orders and not account and not link:
            return web.json_response({"error": "Профиль не найден"}, status=404)

        code = secrets.token_urlsafe(18)
        session.add(
            WebProfileLinkCode(
                code=code,
                profile_token=token,
                expires_at=datetime.utcnow() + timedelta(minutes=settings.telegram_link_ttl_minutes),
            )
        )
        await session.commit()

    return web.json_response({
        "link_url": f"{settings.bot_url}?start=web_{code}",
        "expires_in_minutes": settings.telegram_link_ttl_minutes,
    })


async def handle_telegram_auth_init(request: web.Request) -> web.Response:
    code = secrets.token_urlsafe(18)
    async with async_session() as session:
        session.add(
            WebTelegramAuthCode(
                code=code,
                expires_at=datetime.utcnow() + timedelta(minutes=settings.telegram_link_ttl_minutes),
            )
        )
        await session.commit()

    return web.json_response({
        "code": code,
        "link_url": f"{settings.bot_url}?start=webauth_{code}",
        "expires_in_minutes": settings.telegram_link_ttl_minutes,
    })


async def handle_telegram_auth_status(request: web.Request) -> web.Response:
    code = request.query.get("code", "").strip()
    if not code:
        return web.json_response({"error": "Missing code"}, status=400)

    async with async_session() as session:
        result = await session.execute(
            select(WebTelegramAuthCode).where(WebTelegramAuthCode.code == code)
        )
        auth_code = result.scalar_one_or_none()
        if not auth_code:
            return web.json_response({"error": "Code not found"}, status=404)
        if auth_code.expires_at < datetime.utcnow():
            return web.json_response({"status": "expired"})
        if not auth_code.consumed_at or not auth_code.profile_token:
            return web.json_response({"status": "pending"})

        profile_token = auth_code.profile_token
        result = await session.execute(delete(WebTelegramAuthCode).where(WebTelegramAuthCode.id == auth_code.id))
        await session.commit()
        if not result.rowcount:
            return web.json_response({"status": "expired"})
    return await _auth_response({"status": "completed", "token": profile_token}, profile_token, method="telegram")


async def handle_profile_orders(request: web.Request) -> web.Response:
    """Get all orders for the authenticated profile session."""
    authenticated = (await _authenticated_profile(request)).strip()
    if not authenticated:
        requested = request.query.get("token", "").strip()
        if not requested and not _has_raw_auth_cookie(request):
            return web.json_response({"error": "Missing token", "auth_required": True}, status=401)
        return web.json_response(
            {"error": "Войдите в профиль", "auth_required": True},
            status=403 if _has_raw_auth_cookie(request) else 401,
        )
    token = authenticated

    balance_profile = None
    balance_history = None
    web_balance = None
    web_balance_history = None
    async with async_session() as session:
        orders, link, telegram_items = await _get_profile_bundle(session, token)
        account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == token))
        confirmed_email = await session.scalar(select(WebEmail.email).where(WebEmail.profile_token == token)) if account else None
        if not orders and not link and not telegram_items and not account:
            return web.json_response({"error": "Заказы не найдены"}, status=404)
        topup_status = None
        topup_id = request.query.get("topup_id", "").strip()
        if topup_id:
            topup_status = await session.scalar(select(WebBalanceTopUp.status).where(
                WebBalanceTopUp.topup_id == topup_id,
                WebBalanceTopUp.profile_token == token,
            )) or "not_found"
        web_balance_account = await _get_or_create_web_balance_account(
            session,
            token,
            (orders[0].contact or orders[0].email) if orders else (account.contact if account else (link.contact if link else None)),
        )
        web_balance = {
            "balance_rub": web_balance_account.balance_rub,
            "ref_code": web_balance_account.ref_code,
            "site_url": f"{_resolve_referral_base_url(request)}/buy?ref={web_balance_account.ref_code}",
        }
        web_balance_history = _serialize_web_balance_history(
            await _fetch_web_balance_history(session, token, 20)
        )
        local_balance_status = _build_local_balance_status(
            web_balance_account,
            await _get_web_daily_charge_rub(),
        )
        if link and link.telegram_id:
            balance_profile = await _fetch_balance_profile(int(link.telegram_id))
            balance_history = await _fetch_balance_history(int(link.telegram_id), 20)
        else:
            balance_profile = local_balance_status
            balance_history = web_balance_history
        if balance_profile is not None:
            balance_profile = dict(balance_profile)
        local_daily_active = bool(local_balance_status["balance_mode_enabled"] and local_balance_status["balance_autodebit_enabled"])
        if link and local_daily_active:
            if balance_profile is None:
                balance_profile = dict(local_balance_status)
            balance_profile["balance_mode_enabled"] = True
            balance_profile["balance_autodebit_enabled"] = True
            balance_profile["status_text"] = "Ежедневные списания на сайте включены"
        if balance_profile:
            balance_profile["can_manage_daily_billing"] = False
            balance_profile["can_enable_daily_billing"] = False
        await session.commit()

        return web.json_response({
            "contact": confirmed_email or (
                (orders[0].contact or orders[0].email)
                if orders
                else (account.contact if account else ((link.contact or link.telegram_username or "Telegram") if link else "Профиль"))
            ),
            "email_confirmed": bool(confirmed_email),
            "web_account": bool(account),
            "orders": _serialize_orders(orders, telegram_items),
            "telegram_link": (
                {
                    "telegram_id": str(link.telegram_id),
                    "telegram_username": link.telegram_username,
                    "telegram_full_name": link.telegram_full_name,
                    "linked_at": link.linked_at.isoformat() if link.linked_at else None,
                }
                if link else None
            ),
            "telegram_items": _serialize_telegram_items(telegram_items),
            "referral": web_balance,
            "web_balance_history": web_balance_history or [],
            "topup_status": topup_status,
            "balance": balance_profile,
            "balance_history": balance_history or [],
        })


async def handle_balance_toggle(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    enabled = bool(body.get("enabled"))
    if enabled:
        return web.json_response({"error": "Самостоятельное включение ежедневных списаний недоступно"}, status=403)

    authenticated = (await _authenticated_profile(request)).strip()
    requested_token = str(body.get("token") or "").strip()
    if not authenticated and not requested_token:
        return web.json_response({"error": "Сначала откройте профиль"}, status=400)
    if not authenticated:
        return web.json_response(
            {"error": "Войдите в профиль"},
            status=403 if _has_raw_auth_cookie(request) else 401,
        )
    if requested_token and requested_token != authenticated:
        return web.json_response({"error": "Войдите в профиль"}, status=403)
    token = authenticated

    async with async_session() as session:
        account_exists = await session.scalar(select(WebAccount.id).where(WebAccount.profile_token == token))
        orders, link, items = await _get_profile_bundle(session, token)
        if not account_exists and not orders and not link and not items:
            return web.json_response({"error": "Профиль не найден"}, status=404)
        if link and link.telegram_id:
            result, status_code = await _toggle_balance_mode(int(link.telegram_id), enabled)
            if not result:
                return web.json_response({"error": "Не удалось изменить настройку"}, status=500)
            if status_code != 200:
                return web.json_response({"error": result.get("error") or "Не удалось изменить настройку"}, status=status_code)
            local_account = await session.get(WebBalanceAccount, token)
            if local_account:
                await _disable_local_balance_mode(session, local_account)
                await session.commit()
            history = await _fetch_balance_history(int(link.telegram_id), 20)
            return web.json_response({
                "balance": result,
                "balance_history": history or [],
            })

        account = await _get_or_create_web_balance_account(session, token)
        await _disable_local_balance_mode(session, account)
        await session.commit()
        history = _serialize_web_balance_history(await _fetch_web_balance_history(session, token, 20))
        result = _build_local_balance_status(account, await _get_web_daily_charge_rub())

    return web.json_response({"balance": result, "balance_history": history or []})


async def handle_cancel_order(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    token = (body.get("token") or (await _authenticated_profile(request)) or "").strip()
    order_id = (body.get("order_id") or "").strip()
    if not token or not order_id:
        return web.json_response({"error": "order_id и token обязательны"}, status=400)

    async with async_session() as session:
        result = await session.execute(select(WebOrder).where(WebOrder.order_id == order_id))
        order = result.scalar_one_or_none()
        if not order:
            return web.json_response({"error": "Заказ не найден"}, status=404)
        if order.profile_token != token:
            return web.json_response({"error": "Нет доступа"}, status=403)
        protected = bool(
            await session.scalar(select(WebAccount.id).where(WebAccount.profile_token == token).limit(1))
            or await session.get(WebProfileLink, token)
        )
        if protected and await _authenticated_profile(request) != token:
            return web.json_response({"error": "Войдите в профиль"}, status=403)
        profile_lock_key = order.profile_token or f"web-order:{order.order_id}"
        async with _acquire_profile_lock(profile_lock_key, timeout=10.0):
            await session.refresh(order)
            if order.status != "pending":
                return web.json_response({"error": "Отменить можно только заказ в статусе «Ожидает оплаты»"}, status=400)
            order.status = "canceled"
            order.cancellation_confirmed = False if order.yookassa_payment_id else True
            await session.commit()
    return web.json_response({"ok": True})


async def handle_internal_telegram_link_claim(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    code = body.get("code", "").strip()
    telegram_id = body.get("telegram_id")
    if not code or not telegram_id:
        return web.json_response({"error": "Missing code or telegram_id"}, status=400)

    # 1. Pre-check code validity to extract profile_token
    async with async_session() as session:
        result = await session.execute(
            select(WebProfileLinkCode.profile_token)
            .where(
                WebProfileLinkCode.code == code,
                WebProfileLinkCode.consumed_at.is_(None),
                WebProfileLinkCode.expires_at >= datetime.utcnow(),
            )
            .limit(1)
        )
        profile_token = result.scalar_one_or_none()
        if not profile_token:
            return web.json_response({"error": "Link code expired"}, status=404)

    # 2. Acquire cross-process and in-task profile lock
    async with _acquire_profile_lock(profile_token):
        async with async_session() as session:
            # Re-verify code validity under lock
            result = await session.execute(
                select(WebProfileLinkCode).where(WebProfileLinkCode.code == code).limit(1)
            )
            link_code = result.scalar_one_or_none()
            now = datetime.utcnow()
            if (
                not link_code
                or link_code.profile_token != profile_token
                or link_code.consumed_at is not None
                or link_code.expires_at < now
            ):
                return web.json_response({"error": "Link code expired"}, status=404)

            # Check existing pending transfer recipient immutability
            pending_res = await session.execute(
                select(WebBalanceTransfer).where(
                    WebBalanceTransfer.profile_token == profile_token,
                    WebBalanceTransfer.status == "pending",
                )
                .order_by(WebBalanceTransfer.id.asc())
                .limit(1)
            )
            pending_tx = pending_res.scalar_one_or_none()
            pending_transfer_id = pending_tx.transfer_id if pending_tx else None

            # Check existing profile link
            existing_link = await session.get(WebProfileLink, profile_token)

            prev_recipient = None
            if pending_tx and pending_tx.telegram_id != int(telegram_id):
                prev_recipient = pending_tx.telegram_id
            elif existing_link and existing_link.telegram_id and existing_link.telegram_id != int(telegram_id):
                prev_recipient = existing_link.telegram_id

            if prev_recipient is not None:
                # Re-link A -> B: Reconcile A's transfer/migration first
                reconciled = await _migrate_web_balance_to_telegram(
                    session, profile_token, prev_recipient, reconcile_existing_only=True
                )
                if pending_tx and (not reconciled or reconciled.status == "pending"):
                    # Unsettled state for previous recipient: block re-link without consuming code
                    return web.json_response(
                        {
                            "error": "Pending balance transfer is awaiting confirmation for previous Telegram account. Please retry later.",
                            "pending_transfer_id": pending_transfer_id,
                        },
                        status=409,
                    )

            # Atomic compare-and-set to consume the one-time link code
            _assert_profile_lock(profile_token)
            claim_time = datetime.utcnow()
            consume_res = await session.execute(
                update(WebProfileLinkCode)
                .where(
                    WebProfileLinkCode.code == code,
                    WebProfileLinkCode.consumed_at.is_(None),
                    WebProfileLinkCode.expires_at >= claim_time,
                )
                .values(consumed_at=claim_time)
            )
            if consume_res.rowcount != 1:
                return web.json_response({"error": "Link code expired"}, status=404)

            orders, linked_profile, _ = await _get_profile_bundle(session, profile_token)
            account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == profile_token))
            if not orders and not account and not linked_profile:
                return web.json_response({"error": "Profile not found"}, status=404)

            await _upsert_profile_link(
                session,
                profile_token,
                (orders[0].contact or orders[0].email) if orders else (account.contact if account else linked_profile.contact),
                int(telegram_id),
                body.get("telegram_username"),
                body.get("telegram_full_name"),
            )
            await _replace_telegram_items(
                session,
                profile_token,
                int(telegram_id),
                body.get("items") or [],
            )
            await session.commit()
            await _migrate_web_balance_to_telegram(session, profile_token, int(telegram_id))

        return web.json_response({"ok": True})


async def handle_internal_telegram_auth_claim(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    code = body.get("code", "").strip()
    telegram_id = body.get("telegram_id")
    if not code or not telegram_id:
        return web.json_response({"error": "Missing code or telegram_id"}, status=400)

    async with async_session() as session:
        # Pre-check code validity and retrieve profile_token
        result = await session.execute(
            select(WebTelegramAuthCode).where(
                WebTelegramAuthCode.code == code,
                WebTelegramAuthCode.consumed_at.is_(None),
                WebTelegramAuthCode.expires_at >= datetime.utcnow(),
            ).limit(1)
        )
        auth_code_row = result.scalar_one_or_none()
        if not auth_code_row:
            return web.json_response({"error": "Auth code expired"}, status=404)

        result = await session.execute(
            select(WebProfileLink).where(WebProfileLink.telegram_id == int(telegram_id)).limit(1)
        )
        link = result.scalar_one_or_none()
        profile_token = link.profile_token if link else _make_telegram_profile_token(int(telegram_id))

    async with _acquire_profile_lock(profile_token):
        async with async_session() as session:
            # Atomic compare-and-set to consume the one-time auth code
            now = datetime.utcnow()
            consume_res = await session.execute(
                update(WebTelegramAuthCode)
                .where(
                    WebTelegramAuthCode.code == code,
                    WebTelegramAuthCode.consumed_at.is_(None),
                    WebTelegramAuthCode.expires_at >= now,
                )
                .values(consumed_at=now)
            )
            if consume_res.rowcount != 1:
                return web.json_response({"error": "Auth code expired"}, status=404)

            result = await session.execute(
                select(WebTelegramAuthCode).where(WebTelegramAuthCode.code == code).limit(1)
            )
            auth_code = result.scalar_one_or_none()
            if not auth_code:
                return web.json_response({"error": "Auth code expired"}, status=404)

            result = await session.execute(
                select(WebProfileLink).where(WebProfileLink.telegram_id == int(telegram_id)).limit(1)
            )
            link = result.scalar_one_or_none()
            resolved_token = link.profile_token if link else _make_telegram_profile_token(int(telegram_id))
            if resolved_token != profile_token:
                await session.rollback()
                return web.json_response({"error": "Profile changed; retry authentication"}, status=409)
            _assert_profile_lock(profile_token)
            await _upsert_profile_link(
                session,
                profile_token,
                link.contact if link else None,
                int(telegram_id),
                body.get("telegram_username"),
                body.get("telegram_full_name"),
            )
            await _replace_telegram_items(
                session,
                profile_token,
                int(telegram_id),
                body.get("items") or [],
            )
            auth_code.profile_token = profile_token
            auth_code.telegram_id = int(telegram_id)
            auth_code.telegram_username = body.get("telegram_username")
            auth_code.telegram_full_name = body.get("telegram_full_name")
            await session.commit()
            await _migrate_web_balance_to_telegram(session, profile_token, int(telegram_id))

        return web.json_response({"ok": True, "profile_token": profile_token})


async def handle_internal_telegram_sync(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    telegram_id = body.get("telegram_id")
    if not telegram_id:
        return web.json_response({"error": "Missing telegram_id"}, status=400)

    async with async_session() as session:
        result = await session.execute(
            select(WebProfileLink).where(WebProfileLink.telegram_id == int(telegram_id))
        )
        link = result.scalar_one_or_none()
        if not link:
            return web.json_response({"error": "Link not found"}, status=404)

        link.telegram_username = body.get("telegram_username")
        link.telegram_full_name = body.get("telegram_full_name")
        link.last_synced_at = datetime.utcnow()
        await _replace_telegram_items(
            session,
            link.profile_token,
            int(telegram_id),
            body.get("items") or [],
        )
        await session.commit()

    return web.json_response({"ok": True})


async def handle_internal_web_profile(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    telegram_id = request.query.get("telegram_id", "").strip()
    if not telegram_id:
        return web.json_response({"error": "Missing telegram_id"}, status=400)

    async with async_session() as session:
        result = await session.execute(
            select(WebProfileLink).where(WebProfileLink.telegram_id == int(telegram_id))
        )
        link = result.scalar_one_or_none()
        if not link:
            return web.json_response({"error": "Link not found"}, status=404)

        orders, _, tg_items = await _get_profile_bundle(session, link.profile_token)
        return web.json_response({
            "contact": link.contact,
            "orders": _serialize_orders(orders, tg_items),
        })


async def handle_internal_admin_stats(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    now = datetime.utcnow()
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    w7 = now - timedelta(days=7)
    w30 = now - timedelta(days=30)

    async with async_session() as session:
        def _rev_q(since):
            return select(func.sum(WebOrder.amount_rub)).where(
                WebOrder.status == "delivered",
                WebOrder.paid_at >= since,
            )

        rev_today = await session.scalar(_rev_q(today)) or 0
        rev_7d = await session.scalar(_rev_q(w7)) or 0
        rev_30d = await session.scalar(_rev_q(w30)) or 0
        rev_all = await session.scalar(
            select(func.sum(WebOrder.amount_rub)).where(WebOrder.status == "delivered")
        ) or 0

        status_counts = {}
        for s in ["pending", "delivered", "canceled", "demo"]:
            status_counts[s] = await session.scalar(
                select(func.count(WebOrder.id)).where(WebOrder.status == s)
            ) or 0

        total_profiles = await session.scalar(
            select(func.count(WebBalanceAccount.profile_token))
        ) or 0

        ref_credited = await session.scalar(
            select(func.sum(WebBalanceTransaction.amount_rub)).where(
                WebBalanceTransaction.kind.in_(["referral", "referral_bonus"])
            )
        ) or 0
        ref_orders = await session.scalar(
            select(func.count(WebOrder.id)).where(
                WebOrder.referrer_telegram_id.isnot(None),
                WebOrder.referral_status == "credited",
            )
        ) or 0

        total_30d = await session.scalar(
            select(func.count(WebOrder.id)).where(WebOrder.created_at >= w30)
        ) or 0
        paid_30d = await session.scalar(
            select(func.count(WebOrder.id)).where(
                WebOrder.status == "delivered", WebOrder.paid_at >= w30,
            )
        ) or 0

        # Active & expired access on website
        web_active_paid = await session.scalar(
            select(func.count(WebOrder.id)).where(
                WebOrder.status == "delivered",
                WebOrder.access_expires_at > now,
            )
        ) or 0
        web_active_demo = await session.scalar(
            select(func.count(WebOrder.id)).where(
                WebOrder.status == "demo",
                WebOrder.access_expires_at > now,
            )
        ) or 0
        web_expired = await session.scalar(
            select(func.count(WebOrder.id)).where(
                WebOrder.status.in_(["delivered", "demo"]),
                WebOrder.access_expires_at <= now,
            )
        ) or 0

        # Demo to Paid conversion on website
        paid_prof_q = select(WebOrder.profile_token).where(
            WebOrder.status == "delivered",
            WebOrder.profile_token.isnot(None),
        ).distinct()
        web_demo_total_users = await session.scalar(
            select(func.count(func.distinct(WebOrder.profile_token))).where(
                WebOrder.status == "demo",
                WebOrder.profile_token.isnot(None),
            )
        ) or 0
        web_demo_converted_users = await session.scalar(
            select(func.count(func.distinct(WebOrder.profile_token))).where(
                WebOrder.status == "demo",
                WebOrder.profile_token.isnot(None),
                WebOrder.profile_token.in_(paid_prof_q),
            )
        ) or 0

        # Churn on website
        web_ever_paid_users = await session.scalar(
            select(func.count(func.distinct(WebOrder.profile_token))).where(
                WebOrder.status == "delivered",
                WebOrder.profile_token.isnot(None),
            )
        ) or 0
        web_still_active_users = await session.scalar(
            select(func.count(func.distinct(WebOrder.profile_token))).where(
                WebOrder.status == "delivered",
                WebOrder.profile_token.isnot(None),
                WebOrder.access_expires_at > now,
            )
        ) or 0
        web_churned_users = max(0, web_ever_paid_users - web_still_active_users)

        from sqlalchemy import desc as _desc
        orders_result = await session.execute(
            select(WebOrder).order_by(_desc(WebOrder.created_at)).limit(200)
        )
        recent_orders = [
            {
                "order_id": o.order_id,
                "status": o.status,
                "amount_rub": o.amount_rub,
                "original_amount_rub": o.original_amount_rub or o.amount_rub,
                "bonus_applied_rub": o.bonus_applied_rub or 0,
                "tariff_key": o.tariff_key,
                "tariff_label": o.tariff_label,
                "days": o.days,
                "contact": (o.contact or "")[:30],
                "email": o.email or "",
                "profile_token": o.profile_token or "",
                "provider": _get_order_provider(o),
                "yookassa_payment_id": o.yookassa_payment_id or "",
                "subscription_url": _display_order_subscription_url(o),
                "failure_message": o.failure_message or "",
                "paid_at": o.paid_at.strftime("%d.%m %H:%M") if o.paid_at else "",
                "delivered_at": o.delivered_at.strftime("%d.%m %H:%M") if o.delivered_at else "",
                "access_expires_at": o.access_expires_at.strftime("%d.%m %H:%M") if o.access_expires_at else "",
                "created_at": o.created_at.strftime("%d.%m %H:%M") if o.created_at else "",
            }
            for o in orders_result.scalars().all()
        ]

    return web.json_response({
        "revenue": {"today": rev_today, "w7": rev_7d, "w30": rev_30d, "all": rev_all},
        "status_counts": status_counts,
        "total_profiles": total_profiles,
        "referrals": {"orders": ref_orders, "credited_rub": ref_credited},
        "conversion": {
            "total_30d": total_30d,
            "paid_30d": paid_30d,
            "demo_total": web_demo_total_users,
            "demo_converted": web_demo_converted_users,
        },
        "active": {
            "paid": web_active_paid,
            "demo": web_active_demo,
            "expired": web_expired,
        },
        "churn": {
            "ever_paid": web_ever_paid_users,
            "still_active": web_still_active_users,
            "churned": web_churned_users,
        },
        "recent_orders": recent_orders,
    })


async def handle_internal_admin_balance_lookup(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    query = (request.query.get("q") or "").strip()
    if not query:
        return web.json_response({"error": "Missing q"}, status=400)

    async with async_session() as session:
        account = None
        if query.lstrip("-").isdigit():
            link_res = await session.execute(
                select(WebProfileLink).where(WebProfileLink.telegram_id == int(query))
            )
            link = link_res.scalar_one_or_none()
            if link:
                account = await session.get(WebBalanceAccount, link.profile_token)
        if not account:
            from sqlalchemy import or_
            res = await session.execute(
                select(WebBalanceAccount).where(
                    or_(WebBalanceAccount.contact == query,
                        WebBalanceAccount.contact == query.lower())
                )
            )
            account = res.scalar_one_or_none()

    if not account:
        return web.json_response({"error": "Not found"}, status=404)
    return web.json_response({
        "profile_token": account.profile_token,
        "contact": account.contact or "",
        "balance_rub": float(account.balance_rub or 0),
    })


_WEB_SEARCH_GENERIC_URL_SEGMENTS = frozenset({
    "",
    "sub",
    "vpnbot",
    "adapt-sub",
    "vhq-sub",
    "proxy-subscription",
    "subscribe",
    "api",
    "v1",
    "v2",
    "client",
    "functions",
    "partner-api",
    "proxy",
    "socks",
})
_WEB_SEARCH_VHQ_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]+\.[0-9a-f]{64}$")
_WEB_SEARCH_VPN_URI_SCHEMES = (
    "vless://",
    "vmess://",
    "trojan://",
    "ss://",
    "ssr://",
    "hy2://",
    "hysteria2://",
    "tuic://",
    "tg://",
)


def _web_sql_exact_url_cs(col, target_url: str):
    return func.rtrim(col, "/") == target_url.rstrip("/")


def _web_sql_suffix_cs(col, suffix: str):
    return func.substr(func.rtrim(col, "/"), -len(suffix)) == suffix


def _web_sql_adapt_url_uuid_match(col, canon_uuid: str):
    suffix = f"/{canon_uuid.lower()}"
    lowered_trimmed = func.lower(func.rtrim(col, "/"))
    lowered_raw = func.lower(col)
    return or_(
        func.substr(lowered_trimmed, -len(suffix)) == suffix,
        lowered_raw.like(f"%{suffix}?%"),
        lowered_raw.like(f"%{suffix}/?%"),
        lowered_raw.like(f"%{suffix}#%"),
        lowered_raw.like(f"%{suffix}/#%"),
    )


def _resolve_web_vhq_token(token_part: str) -> dict | None:
    candidate_secrets: list[str] = []
    for sec in (
        getattr(settings, "vhq_subscription_proxy_secret", ""),
        getattr(settings, "bridge_shared_secret", ""),
    ):
        sec_clean = str(sec or "").strip()
        if sec_clean and sec_clean not in candidate_secrets:
            candidate_secrets.append(sec_clean)
    try:
        from bot.services.vhq_subscription_proxy import resolve_vhq_mirror_token
        for sec in candidate_secrets + [None]:
            resolved = resolve_vhq_mirror_token(token_part, secret=sec)
            if resolved:
                return resolved
    except Exception:
        pass
    return None


def _classify_web_admin_search_query(raw_query: str) -> dict:
    from urllib.parse import urlparse

    raw = str(raw_query or "").strip()
    if not raw:
        return {"kind": "invalid_link"}

    if "/vhq-sub/" in raw:
        path_part = urlparse(raw).path if "://" in raw else raw.split("?", 1)[0].split("#", 1)[0]
        token_part = path_part.rsplit("/vhq-sub/", 1)[-1].strip("/")
        if not token_part or "/" in token_part or not _WEB_SEARCH_VHQ_TOKEN_RE.fullmatch(token_part):
            return {"kind": "invalid_link"}
        return {"kind": "vhq_mirror", "token": token_part, "resolved": _resolve_web_vhq_token(token_part)}

    if " " not in raw and "://" not in raw and "/" not in raw and _WEB_SEARCH_VHQ_TOKEN_RE.fullmatch(raw):
        return {"kind": "vhq_mirror", "token": raw, "resolved": _resolve_web_vhq_token(raw)}

    if "/adapt-sub/" in raw.lower():
        path_part = urlparse(raw).path if "://" in raw else raw.split("?", 1)[0].split("#", 1)[0]
        idx = path_part.lower().rfind("/adapt-sub/")
        seg = path_part[idx + len("/adapt-sub/"):].strip("/")
        if "/" in seg or len(seg) != 36:
            return {"kind": "invalid_link"}
        try:
            return {"kind": "adapt_uuid", "adapt_uuid": str(uuid.UUID(seg)).lower()}
        except (ValueError, AttributeError):
            return {"kind": "invalid_link"}

    if raw.lower().startswith(("http://", "https://")):
        parsed = urlparse(raw)
        host = (parsed.netloc or "").strip().lower()
        norm_path = (parsed.path or "").rstrip("/")
        if not host:
            return {"kind": "invalid_link"}

        sub_idx = norm_path.lower().rfind("/sub/")
        if sub_idx != -1:
            seg = norm_path[sub_idx + len("/sub/"):]
            if "/" not in seg and len(seg) == 36:
                try:
                    return {"kind": "adapt_uuid", "adapt_uuid": str(uuid.UUID(seg)).lower()}
                except (ValueError, AttributeError):
                    pass

        if "adaptgroup." in host:
            return {"kind": "invalid_link"}

        last_seg = norm_path.rsplit("/", 1)[-1] if norm_path else ""
        has_specific_token = bool(
            last_seg and last_seg.lower() not in _WEB_SEARCH_GENERIC_URL_SEGMENTS and len(last_seg) >= 4
        )
        if not parsed.query and not has_specific_token:
            return {"kind": "invalid_link"}

        base_no_query = f"{parsed.scheme.lower()}://{host}{norm_path}"
        full_no_frag = f"{base_no_query}?{parsed.query}" if parsed.query else base_no_query
        allow_path_suffix = bool(has_specific_token and norm_path.count("/") >= 2)
        return {
            "kind": "http_url",
            "raw": raw,
            "full_no_frag": full_no_frag,
            "has_query": bool(parsed.query),
            "base_no_query": base_no_query if has_specific_token else None,
            "path_suffix": norm_path if allow_path_suffix else None,
        }

    if raw.lower().startswith(_WEB_SEARCH_VPN_URI_SCHEMES):
        after_scheme = raw.split("://", 1)[-1].strip()
        if len(after_scheme) < 8:
            return {"kind": "invalid_link"}
        return {"kind": "uri_key", "uri": raw}

    candidate = raw
    for prefix in ("adapt_", "uuid:", "adapt:"):
        if candidate.lower().startswith(prefix):
            candidate = candidate[len(prefix):].strip()
            break
    if len(candidate) == 36:
        try:
            return {"kind": "adapt_uuid", "adapt_uuid": str(uuid.UUID(candidate)).lower()}
        except (ValueError, AttributeError):
            pass

    return {"kind": "plain", "raw": raw}


async def handle_internal_admin_client_lookup(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    raw_query = (request.query.get("q") or "").strip()
    query = _normalize_contact(raw_query)
    if not query:
        return web.json_response({"error": "Missing q"}, status=400)

    parsed = _classify_web_admin_search_query(raw_query)
    kind = parsed["kind"]
    if kind == "invalid_link":
        return web.json_response({"error": "Not found"}, status=404)

    username_query = query.removeprefix("@")
    numeric_query = int(query) if query.isdigit() else -1
    like_query = f"%{query}%"
    username_like = f"%{username_query}%"

    order_conditions = []
    item_conditions = []

    if kind == "adapt_uuid":
        canon_uuid = parsed["adapt_uuid"]
        order_conditions.extend([
            func.lower(WebOrder.marzban_username) == f"adapt_{canon_uuid}",
            func.lower(WebOrder.target_order_id) == canon_uuid,
            _web_sql_adapt_url_uuid_match(WebOrder.subscription_url, canon_uuid),
        ])
        item_conditions.extend([
            _web_sql_adapt_url_uuid_match(WebTelegramItem.key_value, canon_uuid),
            func.lower(WebTelegramItem.external_id).in_([
                canon_uuid,
                f"uuid:{canon_uuid}",
                f"adapt:{canon_uuid}",
                f"adapt_{canon_uuid}",
            ]),
        ])
    elif kind == "vhq_mirror":
        token = parsed["token"]
        resolved = parsed["resolved"]
        order_conditions.append(_web_sql_suffix_cs(WebOrder.subscription_url, f"/vhq-sub/{token}"))
        item_conditions.append(_web_sql_suffix_cs(WebTelegramItem.key_value, f"/vhq-sub/{token}"))
        if resolved:
            if resolved.get("web_order_id"):
                order_conditions.append(WebOrder.order_id == str(resolved["web_order_id"]))
            if resolved.get("upstream_url"):
                up_url = str(resolved["upstream_url"])
                order_conditions.append(_web_sql_exact_url_cs(WebOrder.vhq_upstream_url, up_url))
                order_conditions.append(_web_sql_exact_url_cs(WebOrder.subscription_url, up_url))
                item_conditions.append(_web_sql_exact_url_cs(WebTelegramItem.key_value, up_url))
            if resolved.get("order_id"):
                ord_id = str(resolved["order_id"])
                order_conditions.append(WebOrder.vhq_order_id == ord_id)
                order_conditions.append(WebOrder.order_id == ord_id)
            if resolved.get("subscription_id"):
                item_conditions.append(WebTelegramItem.external_id == f"sub_{int(resolved['subscription_id'])}")
    elif kind == "http_url":
        if parsed["has_query"]:
            order_conditions.extend([
                WebOrder.subscription_url == parsed["raw"],
                WebOrder.subscription_url == parsed["full_no_frag"],
                WebOrder.vhq_upstream_url == parsed["raw"],
                WebOrder.vhq_upstream_url == parsed["full_no_frag"],
            ])
            item_conditions.extend([
                WebTelegramItem.key_value == parsed["raw"],
                WebTelegramItem.key_value == parsed["full_no_frag"],
            ])
        if parsed["base_no_query"]:
            order_conditions.extend([
                _web_sql_exact_url_cs(WebOrder.subscription_url, parsed["base_no_query"]),
                _web_sql_exact_url_cs(WebOrder.vhq_upstream_url, parsed["base_no_query"]),
            ])
            item_conditions.append(_web_sql_exact_url_cs(WebTelegramItem.key_value, parsed["base_no_query"]))
        if parsed["path_suffix"]:
            order_conditions.extend([
                _web_sql_suffix_cs(WebOrder.subscription_url, parsed["path_suffix"]),
                _web_sql_suffix_cs(WebOrder.vhq_upstream_url, parsed["path_suffix"]),
            ])
            item_conditions.append(_web_sql_suffix_cs(WebTelegramItem.key_value, parsed["path_suffix"]))
    elif kind == "uri_key":
        order_conditions.append(WebOrder.subscription_url == parsed["uri"])
        item_conditions.append(WebTelegramItem.key_value == parsed["uri"])
    else:
        order_conditions.extend([
            WebOrder.order_id == query,
            WebOrder.order_id == raw_query,
            WebOrder.vhq_order_id == raw_query,
            WebOrder.contact.ilike(like_query),
            WebOrder.email.ilike(like_query),
            WebOrder.profile_token == query,
            WebOrder.profile_token.like(f"{query}%"),
            WebOrder.yookassa_payment_id == query,
            WebOrder.yookassa_payment_id == raw_query,
        ])
        if raw_query.lower().startswith("vhq_"):
            ord_id = raw_query[4:].strip()
            if ord_id:
                order_conditions.append(WebOrder.vhq_order_id == ord_id)
            order_conditions.append(func.lower(WebOrder.marzban_username) == query)
        elif raw_query.lower().startswith("adapt_"):
            order_conditions.append(func.lower(WebOrder.marzban_username) == query)
        else:
            order_conditions.append(WebOrder.marzban_username.ilike(like_query))

        item_conditions.extend([
            WebTelegramItem.telegram_id == numeric_query,
            WebTelegramItem.external_id.ilike(like_query),
            WebTelegramItem.title.ilike(like_query),
        ])
        if len(raw_query) >= 8 and " " not in raw_query and "/" not in raw_query:
            order_conditions.extend([
                _web_sql_suffix_cs(WebOrder.subscription_url, f"/{raw_query}"),
                _web_sql_suffix_cs(WebOrder.vhq_upstream_url, f"/{raw_query}"),
            ])
            item_conditions.append(_web_sql_suffix_cs(WebTelegramItem.key_value, f"/{raw_query}"))

    async with async_session() as session:
        tokens: set[str] = set()
        if order_conditions:
            orders_result = await session.execute(
                select(WebOrder).where(or_(*order_conditions))
            )
            matched_orders = orders_result.scalars().all()
            tokens.update(o.profile_token for o in matched_orders if o.profile_token)

        if kind == "plain":
            accounts_result = await session.execute(
                select(WebAccount).where(
                    or_(
                        WebAccount.contact.ilike(like_query),
                        WebAccount.profile_token == query,
                        WebAccount.profile_token.like(f"{query}%"),
                    )
                )
            )
            accounts = accounts_result.scalars().all()
            tokens.update(a.profile_token for a in accounts)

            balances_result = await session.execute(
                select(WebBalanceAccount).where(
                    or_(
                        WebBalanceAccount.contact.ilike(like_query),
                        WebBalanceAccount.profile_token == query,
                        WebBalanceAccount.profile_token.like(f"{query}%"),
                    )
                )
            )
            balances = balances_result.scalars().all()
            tokens.update(a.profile_token for a in balances)

            links_result = await session.execute(
                select(WebProfileLink).where(
                    or_(
                        WebProfileLink.telegram_id == numeric_query,
                        WebProfileLink.telegram_username.ilike(username_like),
                        WebProfileLink.telegram_full_name.ilike(like_query),
                        WebProfileLink.contact.ilike(like_query),
                        WebProfileLink.profile_token.like(f"{query}%"),
                    )
                )
            )
            tokens.update(link.profile_token for link in links_result.scalars().all())

        if item_conditions:
            items_result = await session.execute(
                select(WebTelegramItem).where(or_(*item_conditions))
            )
            tokens.update(item.profile_token for item in items_result.scalars().all())

        if not tokens:
            return web.json_response({"error": "Not found"}, status=404)

        profiles = []
        for token in sorted(tokens):
            orders, link, telegram_items = await _get_profile_bundle(session, token)
            account = await session.scalar(
                select(WebAccount).where(
                    or_(WebAccount.profile_token == token, WebAccount.contact == token)
                ).limit(1)
            )
            balance = await session.scalar(
                select(WebBalanceAccount).where(
                    or_(WebBalanceAccount.profile_token == token, WebBalanceAccount.contact == token)
                ).limit(1)
            )
            contact = (
                (account.contact if account else None)
                or (balance.contact if balance else None)
                or ((orders[0].contact or orders[0].email) if orders else None)
                or (link.contact if link else None)
                or (link.telegram_username if link else None)
                or "—"
            )
            earliest_dt = None
            if account and account.created_at:
                earliest_dt = account.created_at
            if balance and balance.created_at:
                if earliest_dt is None or balance.created_at < earliest_dt:
                    earliest_dt = balance.created_at
            if orders:
                for o in orders:
                    if o.created_at:
                        if earliest_dt is None or o.created_at < earliest_dt:
                            earliest_dt = o.created_at

            referrer_tg_id = None
            traffic_source = None
            for o in orders:
                if not referrer_tg_id and o.referrer_telegram_id:
                    referrer_tg_id = o.referrer_telegram_id
                if not traffic_source:
                    ref = str(o.entry_referrer or o.ref_source or "").strip()
                    url = str(o.entry_url or "").strip()
                    if "vk.ru" in ref or "vk.com" in ref or "vk" in url:
                        traffic_source = "ВКонтакте (VK)"
                    elif ref:
                        traffic_source = ref
                    elif url:
                        traffic_source = url

            ref_code = _make_web_ref_code(token)
            ref_orders_count = await session.scalar(
                select(func.count(WebOrder.id)).where(
                    or_(
                        WebOrder.referrer_telegram_id == (link.telegram_id if link else -1),
                        WebOrder.ref_source == ref_code,
                    )
                )
            ) or 0

            profiles.append({
                "profile_token": token,
                "contact": contact,
                "created_at": (earliest_dt.isoformat() + "Z") if earliest_dt else None,
                "has_password": bool(account),
                "balance_rub": float(balance.balance_rub or 0) if balance else 0.0,
                "referrer_telegram_id": referrer_tg_id,
                "traffic_source": traffic_source or "—",
                "referrals_count": ref_orders_count,
                "telegram": (
                    {
                        "id": str(link.telegram_id),
                        "username": link.telegram_username,
                        "full_name": link.telegram_full_name,
                    }
                    if link else None
                ),
                "orders": _serialize_orders(orders),
                "telegram_items": _serialize_telegram_items(telegram_items),
            })

    return web.json_response({"profiles": profiles})


async def handle_internal_admin_clients_list(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    try:
        page = max(1, int(request.query.get("page", "1")))
    except ValueError:
        page = 1
    limit = 10
    offset = (page - 1) * limit

    async with async_session() as session:
        total_accounts = await session.scalar(select(func.count(WebAccount.id))) or 0
        if total_accounts == 0:
            total_accounts = await session.scalar(select(func.count(WebBalanceAccount.profile_token))) or 0

        accounts_res = await session.execute(
            select(WebAccount).order_by(WebAccount.created_at.desc()).offset(offset).limit(limit)
        )
        accounts = accounts_res.scalars().all()

        client_list = []
        for acc in accounts:
            token = acc.profile_token
            orders, link, items = await _get_profile_bundle(session, token)
            bal = await session.get(WebBalanceAccount, token)
            paid_sum = sum(o.amount_rub for o in orders if o.status in ("delivered", "paid"))
            paid_count = sum(1 for o in orders if o.status in ("delivered", "paid"))
            pending_count = sum(1 for o in orders if o.status == "pending")
            demo_count = sum(1 for o in orders if o.status == "demo" or o.tariff_key == "demo")

            referrer_tg_id = None
            traffic_source = None
            for o in orders:
                if not referrer_tg_id and o.referrer_telegram_id:
                    referrer_tg_id = o.referrer_telegram_id
                if not traffic_source:
                    ref = str(o.entry_referrer or o.ref_source or "").strip()
                    url = str(o.entry_url or "").strip()
                    if "vk.ru" in ref or "vk.com" in ref or "vk" in url:
                        traffic_source = "ВКонтакте (VK)"
                    elif ref:
                        traffic_source = ref
                    elif url:
                        traffic_source = url

            earliest_dt = acc.created_at
            if orders:
                order_dts = [o.created_at for o in orders if o.created_at]
                if order_dts:
                    min_o = min(order_dts)
                    if earliest_dt is None or min_o < earliest_dt:
                        earliest_dt = min_o

            client_list.append({
                "profile_token": token,
                "contact": acc.contact or "—",
                "created_at": (earliest_dt.isoformat() + "Z") if earliest_dt else None,
                "balance_rub": float(bal.balance_rub or 0) if bal else 0.0,
                "paid_sum": paid_sum,
                "paid_count": paid_count,
                "pending_count": pending_count,
                "demo_count": demo_count,
                "traffic_source": traffic_source or "—",
                "referrer_telegram_id": referrer_tg_id,
                "telegram": (
                    {
                        "id": str(link.telegram_id),
                        "username": link.telegram_username,
                        "full_name": link.telegram_full_name,
                    }
                    if link else None
                ),
            })

    return web.json_response({
        "page": page,
        "limit": limit,
        "total": total_accounts,
        "clients": client_list,
    })


def _serialize_admin_order(order: WebOrder) -> dict[str, object]:
    retry_due = not order.next_fulfillment_retry_at or order.next_fulfillment_retry_at <= datetime.utcnow()
    return {
        "id": order.id,
        "order_id": order.order_id,
        "status": order.status,
        "contact": order.contact or order.email or "—",
        "email": order.email or "",
        "tariff_key": order.tariff_key,
        "tariff_label": order.tariff_label,
        "days": order.days,
        "amount_rub": order.amount_rub,
        "original_amount_rub": order.original_amount_rub or order.amount_rub,
        "bonus_applied_rub": order.bonus_applied_rub or 0,
        "provider": _get_order_provider(order),
        "profile_token": order.profile_token or "",
        "purchase_action": order.purchase_action or "new",
        "target_order_id": order.target_order_id or "",
        "subscription_url": _display_order_subscription_url(order),
        "raw_subscription_url": order.subscription_url or "",
        "marzban_username": order.marzban_username or "",
        "yookassa_payment_id": order.yookassa_payment_id or "",
        "failure_message": order.failure_message or "",
        "failure_code": order.failure_code or "",
        "failure_reason": order.failure_reason or "",
        "fulfillment_attempts": order.fulfillment_attempts or 0,
        "created_at": (order.created_at.isoformat() + "Z") if order.created_at else None,
        "paid_at": (order.paid_at.isoformat() + "Z") if order.paid_at else None,
        "delivered_at": (order.delivered_at.isoformat() + "Z") if order.delivered_at else None,
        "access_expires_at": (order.access_expires_at.isoformat() + "Z") if order.access_expires_at else None,
        "last_fulfillment_attempt_at": (
            order.last_fulfillment_attempt_at.isoformat() + "Z"
            if order.last_fulfillment_attempt_at else None
        ),
        "next_fulfillment_retry_at": (
            order.next_fulfillment_retry_at.isoformat() + "Z"
            if order.next_fulfillment_retry_at else None
        ),
        "can_retry": bool(
            order.paid_at
            and order.status in {"paid", "failed"}
            and not order.delivered_at
            and retry_due
            and order.failure_code not in _VHQ_NON_RETRYABLE_BUY_FAILURE_CODES
        ),
        "can_resend": bool(order.status == "delivered" and order.subscription_url),
        "can_cancel": bool(order.status == "pending" and not order.paid_at),
        "cancellation_confirmed": bool(order.cancellation_confirmed),
    }


async def handle_internal_admin_orders(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)
    try:
        page = max(1, int(request.query.get("page", "1")))
    except ValueError:
        page = 1
    status_filter = (request.query.get("status") or "all").strip().lower()
    query = (request.query.get("q") or "").strip()
    conditions = []
    if status_filter == "attention":
        conditions.append(WebOrder.status.in_(("paid", "failed")))
    elif status_filter in {"pending", "paid", "delivered", "failed", "canceled", "demo"}:
        conditions.append(WebOrder.status == status_filter)
    if query:
        like = f"%{query}%"
        conditions.append(or_(
            WebOrder.order_id.ilike(like),
            WebOrder.contact.ilike(like),
            WebOrder.email.ilike(like),
            WebOrder.profile_token.ilike(like),
            WebOrder.yookassa_payment_id.ilike(like),
        ))
    page_size = 5
    async with async_session() as session:
        count_stmt = select(func.count(WebOrder.id))
        list_stmt = select(WebOrder)
        if conditions:
            count_stmt = count_stmt.where(*conditions)
            list_stmt = list_stmt.where(*conditions)
        total = await session.scalar(count_stmt) or 0
        total_pages = max(1, (total + page_size - 1) // page_size)
        page = min(page, total_pages)
        result = await session.execute(
            list_stmt.order_by(WebOrder.created_at.desc(), WebOrder.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
        orders = result.scalars().all()
    return web.json_response({
        "page": page,
        "limit": page_size,
        "total": total,
        "total_pages": total_pages,
        "status": status_filter,
        "orders": [_serialize_admin_order(order) for order in orders],
    })


async def handle_internal_admin_order(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)
    order_id = (request.query.get("order_id") or "").strip()
    db_id_raw = (request.query.get("id") or "").strip()
    if not order_id and not db_id_raw.isdigit():
        return web.json_response({"error": "order_id or id required"}, status=400)
    async with async_session() as session:
        condition = WebOrder.id == int(db_id_raw) if db_id_raw.isdigit() else WebOrder.order_id == order_id
        order = await session.scalar(select(WebOrder).where(condition))
        if not order:
            return web.json_response({"error": "Заказ не найден"}, status=404)
        return web.json_response({"order": _serialize_admin_order(order)})


async def handle_internal_admin_order_action(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)
    order_id = str(body.get("order_id") or "").strip()
    action = str(body.get("action") or "").strip().lower()
    if not order_id or action not in {"retry", "resend", "cancel"}:
        return web.json_response({"error": "order_id/action invalid"}, status=400)

    warnings: list[str] = []
    async with async_session() as session:
        order = await session.scalar(select(WebOrder).where(WebOrder.order_id == order_id))
        if not order:
            return web.json_response({"error": "Заказ не найден"}, status=404)
        before = _serialize_admin_order(order)
        if action == "retry":
            if order.failure_code in _VHQ_NON_RETRYABLE_BUY_FAILURE_CODES:
                return web.json_response({
                    "error": f"Повтор закупки заблокирован: заказ в статусе ручной проверки ({order.failure_code}). Повторная закупка поставщика запрещена."
                }, status=409)
            if not before["can_retry"]:
                return web.json_response({"error": "Повтор недоступен: заказ не оплачен, уже выдан или попытка уже выполняется"}, status=409)
            profile_lock_key = order.profile_token or f"web-order:{order.order_id}"
            async with _acquire_profile_lock(profile_lock_key, timeout=10.0):
                await session.refresh(order)
                if order.status == "delivered":
                    return web.json_response({"error": "Заказ уже успешно выдан"}, status=409)
                if order.status not in {"paid", "failed"}:
                    return web.json_response({"error": f"Заказ в неверном статусе: {order.status}"}, status=409)
                if order.failure_code in _VHQ_NON_RETRYABLE_BUY_FAILURE_CODES:
                    return web.json_response({
                        "error": f"Повтор закупки заблокирован: заказ в статусе ручной проверки ({order.failure_code}). Повторная закупка поставщика запрещена."
                    }, status=409)
                # Claim the order before the provider call so the automatic worker
                # and another administrator do not start a simultaneous attempt.
                order.next_fulfillment_retry_at = datetime.utcnow() + timedelta(minutes=15)
                await session.commit()
                await attempt_fulfill_order(session, order)
                # Persist the access result before optional accounting and
                # notification work. A notification outage must not hide a
                # successfully issued subscription from the customer profile.
                await session.commit()
                if order.status == "delivered":
                    try:
                        await _disable_web_balance_autodebit_after_tariff_purchase(session, order)
                        await _apply_order_bonus_spend(session, order)
                        await session.commit()
                    except Exception as exc:
                        await session.rollback()
                        await session.refresh(order)
                        logger.exception("Admin fulfillment accounting follow-up failed order_id=%s", order.order_id)
                        warnings.append(f"Не завершена обработка баланса: {exc}")
                if order.status == "delivered":
                    _sync_bot_db_subscription_on_fulfillment(order)
                    bot_result = await _notify_bot_order_delivered(order, session=session)
                    try:
                        await _finalize_order_cashback(session, order, bot_result)
                        await session.commit()
                    except Exception as exc:
                        await session.rollback()
                        await session.refresh(order)
                        logger.exception("Admin fulfillment cashback follow-up failed order_id=%s", order.order_id)
                        warnings.append(f"Не завершена обработка кешбэка: {exc}")
                    client_notify_coro = (
                        _notify_linked_webstore_user(order, "delivered")
                        if not bot_result.get("delivered")
                        else asyncio.sleep(0)
                    )
                    for label, operation in (
                        ("уведомления сотрудникам", _notify_admins_webstore(order, "delivered")),
                        ("уведомления клиенту", client_notify_coro),
                        ("реферальной обработки", _sync_referral_credit_for_order(order.order_id)),
                    ):
                        try:
                            await operation
                        except Exception as exc:
                            logger.exception("Admin fulfillment %s failed order_id=%s", label, order.order_id)
                            warnings.append(f"Ошибка {label}: {exc}")
        elif action == "resend":
            if not before["can_resend"]:
                return web.json_response({"error": "Повтор уведомления доступен только для выданного заказа"}, status=409)
            bot_result = await _notify_bot_order_delivered(order, force=True)
            if not bot_result.get("delivered"):
                queued = await _notify_linked_webstore_user(
                    order,
                    "delivered",
                    dedupe_suffix=f":admin:{uuid.uuid4().hex[:12]}",
                )
                if not queued:
                    return web.json_response({"error": "К заказу не привязан Telegram-профиль"}, status=409)
        else:
            profile_lock_key = order.profile_token or f"web-order:{order.order_id}"
            async with _acquire_profile_lock(profile_lock_key, timeout=10.0):
                await session.refresh(order)
                before = _serialize_admin_order(order)
                if not before["can_cancel"] or order.status != "pending":
                    return web.json_response({"error": "Отменить можно только неоплаченный ожидающий заказ"}, status=409)
                order.status = "canceled"
                order.cancellation_confirmed = False if order.yookassa_payment_id else True
                await session.commit()
        after = _serialize_admin_order(order)

    return web.json_response({
        "ok": True,
        "action": action,
        "before": before,
        "order": after,
        "warnings": warnings,
    })


async def handle_internal_admin_balance_adjust(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    profile_token = (body.get("profile_token") or "").strip()
    amount = body.get("amount")
    if not profile_token or amount is None:
        return web.json_response({"error": "profile_token and amount required"}, status=400)
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        return web.json_response({"error": "amount must be integer"}, status=400)
    if amount == 0:
        return web.json_response({"error": "amount must not be 0"}, status=400)

    async with async_session() as session:
        account = await session.get(WebBalanceAccount, profile_token)
        if not account:
            return web.json_response({"error": "Account not found"}, status=404)
        _add_web_balance_transaction(
            session, account,
            amount_rub=abs(amount),
            direction="credit" if amount > 0 else "debit",
            kind="admin_adjust",
            description="Ручная корректировка администратором",
        )
        await session.commit()
        new_balance = float(account.balance_rub or 0)

    return web.json_response({"ok": True, "new_balance_rub": new_balance})


# ── Route setup ──


def _asset_path(value: str) -> Path:
    return Path(value).expanduser()


async def _serve_asset(path_value: str) -> web.Response:
    asset_path = _asset_path(path_value)
    if asset_path.exists():
        return web.FileResponse(asset_path, headers={"Cache-Control": "public, max-age=86400"})
    return web.Response(status=404)


async def handle_logo(request: web.Request) -> web.Response:
    return await _serve_asset(settings.site_logo_path)


async def handle_favicon(request: web.Request) -> web.Response:
    return await _serve_asset(settings.site_favicon_path)


# ---------------------------------------------------------------------------
# Support chat — connection pool
# ---------------------------------------------------------------------------

# ticket_token -> {"client": WebSocketResponse | None, "agents": list[WebSocketResponse]}
_support_ws_pool: dict[str, dict] = {}


def _support_pool_get(token: str) -> dict:
    if token not in _support_ws_pool:
        _support_ws_pool[token] = {"client": None, "agents": []}
    return _support_ws_pool[token]


async def close_support_websockets() -> int:
    """Close tracked support transports without waiting for remote peers."""
    sockets: list[web.WebSocketResponse] = []
    seen: set[int] = set()
    for pool in _support_ws_pool.values():
        candidates = [pool.get("client"), *pool.get("agents", [])]
        for ws in candidates:
            if ws is None or ws.closed or id(ws) in seen:
                continue
            seen.add(id(ws))
            sockets.append(ws)

    for ws in sockets:
        # Waiting for a remote close frame can exceed systemd's stop timeout.
        # aiohttp itself uses this transport helper on abnormal/timeout closes.
        ws._set_code_close_transport(WSCloseCode.GOING_AWAY)
    _support_ws_pool.clear()
    return len(sockets)


async def _support_broadcast(token: str, payload: dict, *, skip: object = None) -> None:
    data = json.dumps(payload, ensure_ascii=False)
    pool = _support_ws_pool.get(token, {})
    targets: list = []
    if pool.get("client") and pool["client"] is not skip:
        targets.append(pool["client"])
    for ws in list(pool.get("agents", [])):
        if ws is not skip:
            targets.append(ws)
    for ws in targets:
        try:
            if not ws.closed:
                await ws.send_str(data)
        except Exception:
            pass


def _verify_tg_login(data: dict, bot_token: str) -> bool:
    """Verify Telegram Login Widget data integrity."""
    check_hash = data.get("hash", "")
    fields = {k: v for k, v in data.items() if k != "hash"}
    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(fields.items()))
    secret = hashlib.sha256(bot_token.encode()).digest()
    computed = hmac.new(secret, data_check_string.encode(), hashlib.sha256).hexdigest()
    return bool(check_hash) and hmac.compare_digest(computed, str(check_hash))


async def _get_all_webstore_admin_ids() -> set[int]:
    return all_staff_recipient_ids()


async def _get_all_target_agent_ids() -> set[int]:
    agent_ids = set(settings.notification_recipient_ids)
    agent_ids.update(await _get_all_webstore_admin_ids())
    return agent_ids


SUPPORT_UPLOAD_DIR = Path(__file__).resolve().parent / "assets" / "uploads" / "support"
SUPPORT_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

_ALLOWED_IMAGE_TYPES = {
    b"\x89PNG\r\n\x1a\n": ("image/png", ".png"),
    b"\xff\xd8\xff": ("image/jpeg", ".jpg"),
    b"GIF87a": ("image/gif", ".gif"),
    b"GIF89a": ("image/gif", ".gif"),
}


def _detect_image_type(data: bytes) -> tuple[str, str] | None:
    if len(data) < 12:
        return None
    for magic, (mime, ext) in _ALLOWED_IMAGE_TYPES.items():
        if data.startswith(magic):
            return mime, ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None


async def handle_support_upload(request: web.Request) -> web.Response:
    if not request.can_read_body:
        return web.json_response({"error": "no_body"}, status=400)
    try:
        reader = await request.multipart()
    except Exception:
        return web.json_response({"error": "invalid_multipart"}, status=400)

    field = await reader.next()
    if not field:
        return web.json_response({"error": "no_file"}, status=400)

    file_bytes = bytearray()
    max_size = 10 * 1024 * 1024  # 10MB limit
    while True:
        chunk = await field.read_chunk(65536)
        if not chunk:
            break
        file_bytes.extend(chunk)
        if len(file_bytes) > max_size:
            return web.json_response({"error": "file_too_large"}, status=413)

    detected = _detect_image_type(bytes(file_bytes[:32]))
    if not detected:
        return web.json_response({"error": "unsupported_media_type"}, status=415)

    mime_type, ext = detected
    unique_name = f"sup_{secrets.token_hex(16)}{ext}"
    dest_path = SUPPORT_UPLOAD_DIR / unique_name
    dest_path.write_bytes(file_bytes)

    url = f"/api/support/media/{unique_name}"
    return web.json_response({
        "ok": True,
        "url": url,
        "filename": unique_name,
        "media_type": "image",
    })


async def handle_support_media(request: web.Request) -> web.Response:
    filename = request.match_info.get("filename", "")
    if not re.match(r"^[a-zA-Z0-9_\-\.]+$", filename) or ".." in filename:
        raise web.HTTPBadRequest(text="Invalid filename")

    file_path = (SUPPORT_UPLOAD_DIR / filename).resolve()
    if not file_path.is_file() or file_path.parent != SUPPORT_UPLOAD_DIR.resolve():
        raise web.HTTPNotFound()

    ext = file_path.suffix.lower()
    mime_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
    }
    content_type = mime_map.get(ext, "application/octet-stream")
    return web.FileResponse(
        file_path,
        headers={
            "Content-Type": content_type,
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "public, max-age=86400",
        },
    )


async def _get_ticket_client_info(
    session,
    profile_token: str | None,
    client_contact: str | None = None,
) -> dict | None:
    """Retrieve client's Personal Cabinet (ЛК) info: Telegram, balance, and subscription."""
    info: dict = {
        "profile_token": profile_token,
        "telegram_id": None,
        "telegram_username": None,
        "telegram_full_name": None,
        "contact": None,
        "balance_rub": None,
        "subscription": None,
        "profile_url": None,
    }

    link = None
    if profile_token:
        link = await session.get(WebProfileLink, profile_token)

    if not link and client_contact:
        contact_norm = client_contact.strip()
        clean_username = contact_norm.lstrip("@").strip()
        numeric_id = int(contact_norm) if contact_norm.isdigit() else None

        conds = [WebProfileLink.contact == contact_norm]
        if numeric_id:
            conds.append(WebProfileLink.telegram_id == numeric_id)
        if clean_username:
            conds.append(WebProfileLink.telegram_username.ilike(clean_username))

        res = await session.execute(select(WebProfileLink).where(or_(*conds)).limit(1))
        link = res.scalar_one_or_none()
        if link and not profile_token:
            profile_token = link.profile_token
            info["profile_token"] = profile_token

    if not profile_token and client_contact:
        res = await session.execute(
            select(WebAccount).where(WebAccount.contact == client_contact.strip().lower()).limit(1)
        )
        acc = res.scalar_one_or_none()
        if acc:
            profile_token = acc.profile_token
            info["profile_token"] = profile_token
            if not link:
                link = await session.get(WebProfileLink, profile_token)

    if link:
        info["telegram_id"] = link.telegram_id
        info["telegram_username"] = link.telegram_username
        info["telegram_full_name"] = link.telegram_full_name
        info["contact"] = link.contact

    if profile_token:
        info["profile_url"] = f"{settings.subscription_base_url}/profile?token={profile_token}"
        bal_acc = await session.get(WebBalanceAccount, profile_token)
        if bal_acc:
            info["balance_rub"] = bal_acc.balance_rub
            if not info["contact"] and bal_acc.contact:
                info["contact"] = bal_acc.contact

        orders_res = await session.execute(
            select(WebOrder)
            .where(WebOrder.profile_token == profile_token)
            .order_by(WebOrder.created_at.desc())
            .limit(5)
        )
        orders = orders_res.scalars().all()
        now = datetime.utcnow()
        active_order = next((o for o in orders if o.status in ("paid", "active") and o.access_expires_at and o.access_expires_at > now), None)
        if not active_order and orders:
            active_order = orders[0]
        if active_order:
            is_active = bool(active_order.access_expires_at and active_order.access_expires_at > now)
            status_label = "Активна" if is_active else ("Истекла" if active_order.access_expires_at else active_order.status)
            exp_str = active_order.access_expires_at.strftime("%d.%m.%Y %H:%M") if active_order.access_expires_at else "—"
            info["subscription"] = {
                "status": "active" if is_active else "expired",
                "status_label": status_label,
                "tariff": active_order.tariff_label or active_order.tariff_key or "Подписка",
                "expires_at": exp_str,
                "is_active": is_active,
            }

    telegram_id = info["telegram_id"]
    if telegram_id:
        from webstore.config import _bot_db_path
        path = _bot_db_path()
        if path and path.exists():
            try:
                import sqlite3
                with sqlite3.connect(str(path), timeout=5.0) as con:
                    u_row = con.execute(
                        "SELECT id, username, full_name, balance_rub FROM users WHERE telegram_id = ? LIMIT 1",
                        (telegram_id,)
                    ).fetchone()
                    if u_row:
                        if not info["telegram_username"] and u_row[1]:
                            info["telegram_username"] = u_row[1]
                        if not info["telegram_full_name"] and u_row[2]:
                            info["telegram_full_name"] = u_row[2]
                        if info["balance_rub"] is None and u_row[3] is not None:
                            info["balance_rub"] = round(float(u_row[3]), 2)

                        s_row = con.execute(
                            """
                            SELECT id, status, expires_at, tariff_days, billing_mode
                            FROM subscriptions
                            WHERE user_id = ?
                            ORDER BY expires_at DESC
                            LIMIT 1
                            """,
                            (u_row[0],)
                        ).fetchone()
                        if s_row:
                            s_status, s_expires, s_days, s_mode = s_row[1], s_row[2], s_row[3], s_row[4]
                            exp_dt = None
                            if isinstance(s_expires, str):
                                try:
                                    exp_dt = datetime.fromisoformat(s_expires)
                                except Exception:
                                    pass
                            elif isinstance(s_expires, datetime):
                                exp_dt = s_expires

                            is_active = exp_dt > datetime.utcnow() if exp_dt else (str(s_status).upper() == "ACTIVE")
                            status_label = "Активна" if is_active else "Истекла"
                            exp_str = exp_dt.strftime("%d.%m.%Y %H:%M") if exp_dt else str(s_expires or "—")
                            tariff_desc = f"{s_days} дн." if s_days else (s_mode or "Тариф")
                            if not info["subscription"] or is_active:
                                info["subscription"] = {
                                    "status": "active" if is_active else "expired",
                                    "status_label": status_label,
                                    "tariff": tariff_desc,
                                    "expires_at": exp_str,
                                    "is_active": is_active,
                                }
            except Exception as e:
                logger.warning("Error fetching bot DB info for telegram_id %s: %s", telegram_id, e)

    if not any([info["profile_token"], info["telegram_id"], info["telegram_username"], info["subscription"], info["balance_rub"] is not None]):
        return None
    return info


async def _notify_support_agents(
    ticket: SupportTicket,
    first_message: str,
    media_url: str | None = None,
    client_lk: dict | None = None,
) -> None:
    """Send Telegram notification to support agents about a new ticket."""
    if not settings.admin_bot_token:
        return
    agent_ids = await _get_all_target_agent_ids()
    if not agent_ids:
        return
    contact = ticket.client_contact or "—"
    messenger = ticket.client_messenger or "—"

    lk_lines = []
    if client_lk:
        tg_parts = []
        if client_lk.get("telegram_username"):
            tg_parts.append(f"@{client_lk['telegram_username']}")
        if client_lk.get("telegram_id"):
            tg_parts.append(f"ID: <code>{client_lk['telegram_id']}</code>")
        if tg_parts:
            lk_lines.append(f"👤 TG: {' '.join(tg_parts)}")
        if client_lk.get("telegram_full_name"):
            lk_lines.append(f"👤 Имя: {html.escape(str(client_lk['telegram_full_name']))}")
        if client_lk.get("balance_rub") is not None:
            lk_lines.append(f"💰 Баланс: {client_lk['balance_rub']} ₽")
        sub = client_lk.get("subscription")
        if sub:
            lk_lines.append(f"🔑 Подписка: {html.escape(sub['status_label'])} ({html.escape(sub['tariff'])}) до {sub['expires_at']}")
        if client_lk.get("profile_url"):
            lk_lines.append(f"🌐 <a href=\"{client_lk['profile_url']}\">Личный кабинет клиента</a>")

    lk_section = ("\n" + "\n".join(lk_lines) + "\n") if lk_lines else ""

    text = (
        f"💬 <b>Новый тикет поддержки</b>\n\n"
        f"🎫 Тикет: <code>{ticket.token[:12]}…</code>\n"
        f"📞 Контакт: {html.escape(contact)}\n"
        f"📱 Мессенджер: {html.escape(messenger)}\n"
        f"{lk_section}\n"
        f"💬 <i>{html.escape(first_message[:200])}</i>\n\n"
        f"👉 {settings.subscription_base_url}/support/admin"
    )

    if media_url and media_url.startswith("/api/support/media/"):
        filename = media_url.removeprefix("/api/support/media/")
        photo_path = SUPPORT_UPLOAD_DIR / filename
        if photo_path.is_file():
            photo_bytes = photo_path.read_bytes()
            delivered = await send_telegram_photo(
                settings.admin_bot_token,
                agent_ids,
                photo_bytes,
                caption=text[:1024],
            )
            if delivered > 0:
                return

    if media_url:
        text += f"\n🖼 Вложено фото: {settings.subscription_base_url}{media_url}"
    delivered = await send_telegram_notifications(settings.admin_bot_token, agent_ids, text)
    if delivered == 0:
        logger.warning("New support ticket notification was not delivered")


async def _notify_support_agents_message(
    ticket: SupportTicket,
    message_text: str,
    media_url: str | None = None,
) -> None:
    """Notify agents about a new client message in an existing ticket."""
    if not settings.admin_bot_token:
        return
    agent_ids = await _get_all_target_agent_ids()
    if not agent_ids:
        return
    contact = ticket.client_contact or "—"
    text = (
        f"💬 <b>Новое сообщение в тикете</b>\n"
        f"🎫 Тикет: <code>{ticket.token[:12]}…</code>\n"
        f"📞 Контакт: {html.escape(contact)}\n\n"
        f"💬 <i>{html.escape(message_text[:200])}</i>\n\n"
        f"👉 {settings.subscription_base_url}/support/admin"
    )

    if media_url and media_url.startswith("/api/support/media/"):
        filename = media_url.removeprefix("/api/support/media/")
        photo_path = SUPPORT_UPLOAD_DIR / filename
        if photo_path.is_file():
            photo_bytes = photo_path.read_bytes()
            delivered = await send_telegram_photo(
                settings.admin_bot_token,
                agent_ids,
                photo_bytes,
                caption=text[:1024],
            )
            if delivered > 0:
                return

    if media_url:
        text += f"\n🖼 Вложено фото: {settings.subscription_base_url}{media_url}"
    delivered = await send_telegram_notifications(settings.admin_bot_token, agent_ids, text)
    if delivered == 0:
        logger.warning("Support message notification was not delivered")


# ---------------------------------------------------------------------------
# Support chat — REST handlers
# ---------------------------------------------------------------------------

async def handle_support_page(request: web.Request) -> web.Response:
    tpl = Path(__file__).parent / "templates" / "support.html"
    content = tpl.read_text(encoding="utf-8")
    content = content.replace("{{SITE_NAME}}", settings.site_name)
    content = content.replace("{{SUPPORT_URL}}", "/support")
    content = content.replace("{{SUPPORT_RECOVERY_MODE}}", "true" if request.rel_url.query.get("flow") == "recovery" else "false")
    return web.Response(text=content, content_type="text/html")


async def handle_support_admin_page(request: web.Request) -> web.Response:
    tpl = Path(__file__).parent / "templates" / "support_admin.html"
    content = tpl.read_text(encoding="utf-8")
    content = content.replace("{{SITE_NAME}}", settings.site_name)
    content = content.replace("{{TELEGRAM_BOT_NAME}}", settings.telegram_bot_name or "")
    bot_name = re.sub(r"[^A-Za-z0-9_]+", "", settings.telegram_bot_name or "")
    widget = (
        f'<script async src="https://telegram.org/js/telegram-widget.js?22" data-telegram-login="{html.escape(bot_name)}" '
        'data-size="large" data-userpic="false" data-request-access="write" data-onauth="onTelegramAuth(user)"></script>'
        if bot_name
        else '<span style="color:#60707a;font-size:12px">Telegram-авторизация администратора не настроена.</span>'
    )
    content = content.replace("<!--TELEGRAM_WIDGET-->", widget)
    return web.Response(text=content, content_type="text/html")


def _recovery_contact_key(value: str | None) -> str:
    normalized = _normalize_contact(str(value or ""))
    digits = re.sub(r"[^0-9]", "", normalized)
    compact = re.sub(r"[\s().-]", "", normalized)
    if digits and (normalized.startswith("+") or compact.isdigit()):
        if len(digits) == 11 and digits.startswith("8"):
            digits = "7" + digits[1:]
        return "+" + digits if len(digits) == 11 and digits.startswith("7") else digits
    return normalized


def _recovery_contacts_match(*values: str | None) -> bool:
    keys = [_recovery_contact_key(value) for value in values if str(value or "").strip()]
    return bool(keys) and len(set(keys)) == 1


def _trusted_recovery_order(order: WebOrder) -> bool:
    return bool(
        order.profile_token
        and (order.amount_rub or 0) > 0
        and order.status in ("paid", "delivered")
        and order.yookassa_payment_id
        and order.paid_at
    )


def _recovery_order_snapshot(order: WebOrder) -> tuple:
    return (
        order.id,
        order.order_id,
        order.profile_token,
        order.contact,
        order.email,
        order.status,
        order.amount_rub,
        order.yookassa_payment_id,
        order.paid_at,
        order.subscription_url,
        order.marzban_username,
    )


def _provider_value(value, name: str, default=None):
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)


def _provider_payment_amount(payment) -> tuple[Decimal | None, str | None]:
    amount = _provider_value(payment, "amount")
    raw_value = _provider_value(amount, "value")
    currency = _provider_value(amount, "currency")
    try:
        return Decimal(str(raw_value)), str(currency or "").upper() or None
    except (InvalidOperation, TypeError, ValueError):
        return None, str(currency or "").upper() or None


def _provider_customer_contacts(payment) -> list[str]:
    values: list[str] = []
    receipt = _provider_value(payment, "receipt")
    customer = _provider_value(receipt, "customer")
    for field in ("email", "phone"):
        value = _provider_value(customer, field)
        if value:
            values.append(str(value))
    return values


_RECOVERY_MANUAL_BASES = {
    "account_history": "История заказов и подписки",
    "verified_customer_channel": "Подтверждённый канал клиента",
    "trusted_external_evidence": "Другое доверенное подтверждение, проверенное поддержкой",
}
_RECOVERY_CONTACT_EVIDENCE = {"provider_contact_missing", "provider_contact_mismatch"}


async def _verify_recovery_payment(order: WebOrder, recovery: SupportRecoveryRequest) -> dict:
    payment_id = str(order.yookassa_payment_id or "")
    result = {
        "ok": False,
        "status": "manual_review",
        "reason": "Платёж нельзя подтвердить автоматически. Обращение оставлено на ручной проверке.",
        "payment_id": payment_id or None,
        "payment_status": None,
        "payment_currency": None,
        "payment_amount_rub": None,
        "claimant_evidence_type": None,
        "payment_core_verified": False,
        "manual_review_required": False,
    }
    if not settings.yookassa_enabled:
        result["status"] = "provider_unavailable"
        result["reason"] = "Проверка YooKassa временно недоступна. Обращение оставлено на ручной проверке."
        return result
    if not payment_id:
        return result
    try:
        import yookassa

        yookassa.Configuration.account_id = settings.yookassa_shop_id
        yookassa.Configuration.secret_key = settings.yookassa_secret_key
        payment = await asyncio.wait_for(
            asyncio.to_thread(yookassa.Payment.find_one, payment_id),
            timeout=8,
        )
    except Exception as exc:
        logger.warning("Recovery payment verification failed for payment %s: %s", payment_id, type(exc).__name__)
        result["status"] = "provider_unavailable"
        result["reason"] = "YooKassa не ответила. Обращение оставлено на ручной проверке."
        return result

    actual_id = str(_provider_value(payment, "id") or "")
    payment_status = str(_provider_value(payment, "status") or "")
    payment_paid = _provider_value(payment, "paid")
    amount, currency = _provider_payment_amount(payment)
    metadata = _provider_value(payment, "metadata") or {}
    provider_order_id = str(_provider_value(metadata, "order_id") or "")
    result.update({
        "payment_id": actual_id or payment_id,
        "payment_status": payment_status or None,
        "payment_currency": currency,
        "payment_amount_rub": int(amount) if amount is not None and amount == amount.to_integral_value() else None,
    })
    expected_amount = Decimal(str(order.amount_rub or 0))
    contacts = _provider_customer_contacts(payment)
    claimant_match = any(_recovery_contacts_match(recovery.old_contact, contact) for contact in contacts)
    if (
        actual_id != payment_id
        or payment_status != "succeeded"
        or payment_paid is not True
        or amount != expected_amount
        or currency != "RUB"
        or provider_order_id != str(order.order_id)
    ):
        result["reason"] = "Данные платежа YooKassa не совпали с заказом. Обращение оставлено на ручной проверке."
        return result
    result["payment_core_verified"] = True
    if not contacts:
        result.update({
            "claimant_evidence_type": "provider_contact_missing",
            "manual_review_required": True,
            "reason": "Платёж подтверждён YooKassa, но контакт плательщика в ответе отсутствует. Нужна ручная проверка владельца.",
        })
        return result
    if not claimant_match:
        result.update({
            "claimant_evidence_type": "provider_contact_mismatch",
            "manual_review_required": True,
            "reason": "Платёж подтверждён YooKassa, но контакт плательщика не совпал с данными аккаунта. Нужна ручная проверка владельца.",
        })
        return result
    result.update({
        "ok": True,
        "status": "verified",
        "reason": "Платёж и контакт плательщика подтверждены YooKassa.",
        "claimant_evidence_type": "provider_customer_contact",
    })
    return result


def _recovery_reference_matches(order: WebOrder, reference: str | None) -> bool:
    value = str(reference or "").strip().lower().rstrip("/")
    if not value:
        return False
    candidates = {
        str(order.order_id or "").strip().lower(),
        str(order.subscription_url or "").strip().lower().rstrip("/"),
        str(order.marzban_username or "").strip().lower(),
    }
    return value in {candidate for candidate in candidates if candidate}


async def _recovery_candidates(session, recovery: SupportRecoveryRequest) -> list[dict]:
    old_contact = _normalize_contact(recovery.old_contact)
    reference = str(recovery.subscription_reference or recovery.order_reference or "").strip()
    conditions = [
        WebOrder.contact == old_contact,
        WebOrder.email == old_contact,
    ]
    if reference:
        conditions.extend([
            WebOrder.order_id == reference,
            WebOrder.subscription_url == reference,
            WebOrder.marzban_username == reference,
        ])
    orders = (await session.scalars(select(WebOrder).where(or_(*conditions)).order_by(WebOrder.created_at.desc()).limit(50))).all()
    accounts = (await session.scalars(select(WebAccount).where(WebAccount.contact == old_contact).limit(10))).all()
    by_profile: dict[str, dict] = {}

    async def add_candidate(profile_token: str | None, order: WebOrder | None, account: WebAccount | None) -> None:
        if not profile_token:
            return
        if not account:
            account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == profile_token))
        if not account:
            return
        order_contact = order.contact if order else None
        email_contact = order.email if order else None
        contact_match = _recovery_contacts_match(recovery.old_contact, account.contact) or _recovery_contacts_match(
            recovery.old_contact, order_contact
        ) or _recovery_contacts_match(recovery.old_contact, email_contact)
        reference_match = _recovery_reference_matches(order, reference) if order else False
        trusted = bool(order and contact_match and reference_match and _trusted_recovery_order(order))
        current = by_profile.get(profile_token)
        if current and current["trusted_payment"]:
            return
        if current and not trusted:
            return
        by_profile[profile_token] = {
            "profile_token": profile_token,
            "contact": account.contact,
            "order_id": order.order_id if order else None,
            "order_status": order.status if order else None,
            "amount_rub": order.amount_rub if order else None,
            "reference_match": reference_match,
            "trusted_payment": trusted,
            "payment_id_present": bool(order and order.yookassa_payment_id),
            "paid_at": order.paid_at.isoformat() if order and order.paid_at else None,
        }

    for order in orders:
        await add_candidate(order.profile_token, order, None)
        if not order.profile_token:
            account = await session.scalar(select(WebAccount).where(
                or_(WebAccount.contact == _normalize_contact(order.contact or ""),
                    WebAccount.contact == _normalize_contact(order.email or ""))
            ).limit(1)) if (order.contact or order.email) else None
            await add_candidate(account.profile_token if account else None, order, account)
    for account in accounts:
        await add_candidate(account.profile_token, None, account)
    return list(by_profile.values())


async def _get_recovery_by_ticket_token(session, token: str) -> SupportRecoveryRequest | None:
    return await session.scalar(
        select(SupportRecoveryRequest)
        .join(SupportTicket, SupportTicket.id == SupportRecoveryRequest.ticket_id)
        .where(SupportTicket.token == token)
    )


def _recovery_status(recovery: SupportRecoveryRequest, now: datetime | None = None) -> str:
    now = now or datetime.utcnow()
    if recovery.status == "approved" and recovery.approval_expires_at and recovery.approval_expires_at <= now:
        return "expired"
    return recovery.status


def _recovery_payload(
    recovery: SupportRecoveryRequest,
    *,
    candidates: list[dict] | None = None,
    include_profile_token: bool = False,
) -> dict:
    payload = {
        "id": recovery.id,
        "ticket_id": recovery.ticket_id,
        "status": _recovery_status(recovery),
        "new_email": recovery.new_email,
        "created_at": recovery.created_at.isoformat() if recovery.created_at else None,
        "approved_at": recovery.approved_at.isoformat() if recovery.approved_at else None,
        "approval_expires_at": recovery.approval_expires_at.isoformat() if recovery.approval_expires_at else None,
        "otp_started": bool(recovery.otp_started_at),
        "otp_verified": bool(recovery.otp_verified_at),
        "matched_order_id": recovery.matched_order_id,
        "approved_by": recovery.approved_by_name,
        "denied_reason": recovery.denied_reason,
    }
    if include_profile_token:
        payload["profile_token"] = recovery.profile_token
        payload["payment_check"] = {
            "status": recovery.payment_check_status,
            "reason": recovery.payment_check_reason,
            "verified_at": recovery.payment_verified_at.isoformat() if recovery.payment_verified_at else None,
            "source": recovery.payment_verification_source,
            "payment_id": recovery.payment_id,
            "payment_status": recovery.payment_status,
            "payment_currency": recovery.payment_currency,
            "payment_amount_rub": recovery.payment_amount_rub,
            "claimant_evidence_type": recovery.claimant_evidence_type,
            "payment_core_verified": recovery.claimant_evidence_type in _RECOVERY_CONTACT_EVIDENCE or recovery.payment_check_status == "verified",
            "manual_review_required": recovery.payment_check_status == "manual_review" and recovery.claimant_evidence_type in _RECOVERY_CONTACT_EVIDENCE,
        }
    if candidates is not None:
        payload["candidates"] = candidates
    return payload


async def handle_support_new_ticket(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Invalid JSON")
    recovery_data = body.get("recovery") if isinstance(body.get("recovery"), dict) else None
    message_text = (body.get("message") or "").strip()
    media_url = (body.get("media_url") or "").strip() or None
    media_type = (body.get("media_type") or "image").strip() if media_url else None
    recovery_request_data = None
    if recovery_data is not None:
        old_contact = str(recovery_data.get("old_contact") or "").strip()[:256]
        reference = str(recovery_data.get("reference") or "").strip()[:1024]
        new_email = _normalize_contact(str(recovery_data.get("new_email") or ""))
        details = str(recovery_data.get("details") or "").strip()[:4000]
        if not old_contact or not _valid_email(new_email) or not reference:
            return web.json_response({"error": "Укажите старый контакт, ссылку или номер заказа и новый email"}, status=400)
        message_text = (
            "Запрос на восстановление доступа.\n"
            f"Старый контакт: {old_contact}\n"
            f"Ссылка подписки или номер заказа: {reference}\n"
            f"Новый email: {new_email}\n"
            f"Дополнительные сведения: {details or 'не указаны'}"
        )
        recovery_request_data = {
            "old_contact": old_contact,
            "reference": reference,
            "new_email": new_email,
            "details": details or None,
        }
        if (
            not await _allow_auth_attempt("support-recovery-ip", _get_client_ip(request), 3, 60)
            or not await _allow_auth_attempt("support-recovery-contact", _recovery_contact_key(old_contact), 3, 60)
        ):
            return web.json_response({"error": "Слишком много обращений. Попробуйте позже."}, status=429)
    if not message_text and not media_url:
        return web.json_response({"error": "message_required"}, status=400)
    if not message_text and media_url:
        message_text = "Скриншот"

    client_contact = (body.get("contact") or "").strip() or None
    client_messenger = (body.get("messenger") or "").strip() or None
    if recovery_request_data:
        client_contact = recovery_request_data["old_contact"]
        client_messenger = "recovery"
    authenticated_profile = await _authenticated_profile(request)
    profile_token = (
        authenticated_profile
        if recovery_request_data
        else ((body.get("profile_token") or "").strip() or authenticated_profile or None)
    )

    token = secrets.token_hex(24)
    now = datetime.utcnow()
    ticket = SupportTicket(
        token=token,
        profile_token=profile_token,
        client_contact=client_contact,
        client_messenger=client_messenger,
        status="open",
        created_at=now,
        last_message_at=now,
    )
    async with async_session() as session:
        if not ticket.client_contact and profile_token:
            link = await session.get(WebProfileLink, profile_token)
            if link and link.telegram_username:
                ticket.client_contact = f"@{link.telegram_username}"
            elif link and link.contact:
                ticket.client_contact = link.contact
            elif link and link.telegram_id:
                ticket.client_contact = f"tg:{link.telegram_id}"
            else:
                ticket.client_contact = "Личный кабинет"

        session.add(ticket)
        await session.flush()
        msg = SupportMessage(
            ticket_id=ticket.id,
            sender="client",
            text=message_text,
            media_url=media_url,
            media_type=media_type,
            created_at=now,
        )
        session.add(msg)
        recovery_request = None
        if recovery_request_data:
            reference = recovery_request_data["reference"]
            recovery_request = SupportRecoveryRequest(
                ticket_id=ticket.id,
                old_contact=recovery_request_data["old_contact"],
                subscription_reference=reference,
                order_reference=reference if not reference.lower().startswith(("http://", "https://")) else None,
                new_email=recovery_request_data["new_email"],
                verification_details=recovery_request_data["details"],
                status="pending",
                created_at=now,
            )
            session.add(recovery_request)
        await session.commit()
        ticket_id = ticket.id
        client_lk = await _get_ticket_client_info(session, ticket.profile_token, ticket.client_contact)

    asyncio.create_task(_notify_support_agents(ticket, message_text, media_url=media_url, client_lk=client_lk))
    response = {"token": token, "ticket_id": ticket_id}
    if recovery_request_data:
        response["recovery"] = {"status": "pending", "poll_url": f"/api/support/recovery/{token}"}
    return web.json_response(response)


async def handle_support_ticket_info(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    async with async_session() as session:
        result = await session.execute(select(SupportTicket).where(SupportTicket.token == token))
        ticket = result.scalar_one_or_none()
        if not ticket:
            raise web.HTTPNotFound()
        msgs = await session.execute(
            select(SupportMessage).where(SupportMessage.ticket_id == ticket.id).order_by(SupportMessage.created_at)
        )
        messages = [
            {
                "id": m.id,
                "sender": m.sender,
                "agent_name": m.agent_name,
                "text": m.text,
                "media_url": m.media_url,
                "media_type": m.media_type,
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs.scalars()
        ]
        recovery = await _get_recovery_by_ticket_token(session, token)
    return web.json_response({
        "token": ticket.token,
        "status": ticket.status,
        "messages": messages,
        "recovery": _recovery_payload(recovery) if recovery else None,
    })


async def handle_support_recovery_status(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    async with async_session() as session:
        recovery = await _get_recovery_by_ticket_token(session, token)
        if not recovery:
            raise web.HTTPNotFound()
        payload = _recovery_payload(recovery)
    return web.json_response(payload)


async def handle_support_send_message(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest()
    text_val = (body.get("text") or "").strip()
    media_url = (body.get("media_url") or "").strip() or None
    media_type = (body.get("media_type") or "image").strip() if media_url else None
    if not text_val and not media_url:
        return web.json_response({"error": "empty"}, status=400)

    async with async_session() as session:
        result = await session.execute(select(SupportTicket).where(SupportTicket.token == token))
        ticket = result.scalar_one_or_none()
        if not ticket:
            raise web.HTTPNotFound()
        if ticket.status == "resolved":
            return web.json_response({"error": "ticket_closed"}, status=400)
        now = datetime.utcnow()
        msg = SupportMessage(
            ticket_id=ticket.id,
            sender="client",
            text=text_val or None,
            media_url=media_url,
            media_type=media_type,
            created_at=now,
        )
        ticket.last_message_at = now
        session.add(msg)
        await session.commit()
        msg_id = msg.id

    payload = {
        "type": "message",
        "sender": "client",
        "text": text_val,
        "media_url": media_url,
        "media_type": media_type,
        "id": msg_id,
        "created_at": now.isoformat(),
    }
    asyncio.create_task(_support_broadcast(token, payload))
    asyncio.create_task(_notify_support_agents_message(ticket, text_val or "Вложено фото", media_url=media_url))
    return web.json_response({"ok": True})


# ---------------------------------------------------------------------------
# Support chat — Agent REST handlers
# ---------------------------------------------------------------------------

async def _get_agent_session(request: web.Request) -> SupportAgentSession | None:
    agent_token = (
        request.cookies.get("support_agent_token")
        or request.headers.get("X-Support-Agent-Token", "")
        or request.rel_url.query.get("token", "")
    )
    if not agent_token:
        return None
    async with async_session() as session:
        result = await session.execute(
            select(SupportAgentSession).where(
                SupportAgentSession.token == agent_token,
                SupportAgentSession.expires_at > datetime.utcnow(),
            )
        )
        agent = result.scalar_one_or_none()
        if agent and agent.auth_method == "telegram" and agent.telegram_id not in settings.support_agent_ids:
            return None
        return agent


async def handle_support_admin_login(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest()

    client_ip = _get_client_ip(request)
    if (
        not await _allow_auth_attempt("support-admin-login-ip", client_ip, 12, 15)
        or not await _allow_auth_attempt("support-admin-login-global", "support-admin", 30, 15)
    ):
        return web.json_response({"error": "Слишком много попыток входа. Попробуйте позже."}, status=429)

    auth_method = "password"
    if body.get("password"):
        if not settings.support_admin_password:
            return web.json_response({"error": "password_not_configured"}, status=500)
        if body["password"] != settings.support_admin_password:
            return web.json_response({"error": "invalid_password"}, status=403)
        telegram_id = 0
        username = body.get("username") or "admin"
        full_name = body.get("name") or "Агент"
    else:
        # Telegram Login Widget fallback
        if not settings.admin_bot_token:
            return web.json_response({"error": "bot_not_configured"}, status=500)
        if not _verify_tg_login(body, settings.admin_bot_token):
            return web.json_response({"error": "invalid_hash"}, status=403)
        try:
            auth_date = int(body.get("auth_date", 0))
            telegram_id = int(body.get("id", 0))
        except (TypeError, ValueError):
            return web.json_response({"error": "invalid_telegram_identity"}, status=403)
        if abs(datetime.utcnow().timestamp() - auth_date) > 86400:
            return web.json_response({"error": "expired"}, status=403)
        if telegram_id not in settings.support_agent_ids:
            return web.json_response({"error": "not_authorized"}, status=403)
        auth_method = "telegram"
        username = body.get("username")
        full_name = " ".join(filter(None, [body.get("first_name"), body.get("last_name")]))

    session_token = secrets.token_hex(32)
    expires_at = datetime.utcnow() + timedelta(days=7)
    agent_session = SupportAgentSession(
        token=session_token,
        telegram_id=telegram_id,
        telegram_username=username,
        telegram_full_name=full_name,
        auth_method=auth_method,
        expires_at=expires_at,
        created_at=datetime.utcnow(),
    )
    async with async_session() as session:
        session.add(agent_session)
        await session.commit()

    resp = web.json_response({
        "ok": True,
        "token": session_token,
        "auth_method": auth_method,
        "can_approve_recovery": auth_method == "telegram",
        "name": full_name or username or "Агент",
    })
    resp.set_cookie("support_agent_token", session_token, max_age=7 * 86400, path="/", secure=True, httponly=True, samesite="Lax")
    return resp


async def handle_support_admin_tickets(request: web.Request) -> web.Response:
    agent = await _get_agent_session(request)
    if not agent:
        raise web.HTTPUnauthorized()
    status_filter = request.rel_url.query.get("status", "open")
    async with async_session() as session:
        q = select(SupportTicket).order_by(SupportTicket.last_message_at.desc().nullslast())
        if status_filter != "all":
            q = q.where(SupportTicket.status == status_filter)
        result = await session.execute(q)
        tickets = result.scalars().all()

    data = []
    async with async_session() as session:
        for t in tickets:
            pool = _support_ws_pool.get(t.token, {})
            client_online = pool.get("client") and not pool["client"].closed
            last_message = (
                await session.execute(
                    select(SupportMessage)
                    .where(SupportMessage.ticket_id == t.id)
                    .order_by(SupportMessage.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            recovery = await session.scalar(select(SupportRecoveryRequest).where(SupportRecoveryRequest.ticket_id == t.id))
            preview = last_message.text if (last_message and last_message.text) else ("📷 Скриншот" if (last_message and last_message.media_url) else None)
            data.append({
                "id": t.id,
                "token": t.token,
                "profile_token": t.profile_token,
                "client_contact": t.client_contact,
                "client_messenger": t.client_messenger,
                "status": t.status,
                "created_at": t.created_at.isoformat(),
                "last_message_at": t.last_message_at.isoformat() if t.last_message_at else None,
                "client_online": bool(client_online),
                "last_message_text": preview,
                "last_client_message": bool(last_message and last_message.sender == "client"),
                "recovery": _recovery_payload(recovery) if recovery else None,
            })
    return web.json_response(data)


async def handle_support_admin_ticket_messages(request: web.Request) -> web.Response:
    agent = await _get_agent_session(request)
    if not agent:
        raise web.HTTPUnauthorized()
    ticket_id = int(request.match_info["ticket_id"])
    async with async_session() as session:
        ticket = await session.get(SupportTicket, ticket_id)
        if not ticket:
            raise web.HTTPNotFound()
        result = await session.execute(
            select(SupportMessage).where(SupportMessage.ticket_id == ticket_id).order_by(SupportMessage.created_at)
        )
        msgs = result.scalars().all()
        client_lk = await _get_ticket_client_info(session, ticket.profile_token, ticket.client_contact)
        recovery = await session.scalar(select(SupportRecoveryRequest).where(SupportRecoveryRequest.ticket_id == ticket_id))
        recovery_data = None
        if recovery:
            recovery_data = _recovery_payload(
                recovery,
                candidates=await _recovery_candidates(session, recovery),
                include_profile_token=True,
            )
    return web.json_response({
        "ticket": {
            "id": ticket.id,
            "token": ticket.token,
            "profile_token": ticket.profile_token,
            "client_contact": ticket.client_contact,
            "client_messenger": ticket.client_messenger,
            "status": ticket.status,
            "created_at": ticket.created_at.isoformat(),
            "client_lk": client_lk,
            "recovery": recovery_data,
        },
        "messages": [
            {
                "id": m.id,
                "sender": m.sender,
                "agent_name": m.agent_name,
                "text": m.text,
                "media_url": m.media_url,
                "media_type": m.media_type,
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs
        ],
    })


async def handle_support_admin_recovery_candidates(request: web.Request) -> web.Response:
    agent = await _get_agent_session(request)
    if not agent:
        raise web.HTTPUnauthorized()
    ticket_id = int(request.match_info["ticket_id"])
    async with async_session() as session:
        ticket = await session.get(SupportTicket, ticket_id)
        if not ticket:
            raise web.HTTPNotFound()
        recovery = await session.scalar(select(SupportRecoveryRequest).where(SupportRecoveryRequest.ticket_id == ticket_id))
        if not recovery:
            return web.json_response({"error": "recovery_not_found"}, status=404)
        candidates = await _recovery_candidates(session, recovery)
        return web.json_response(_recovery_payload(recovery, candidates=candidates, include_profile_token=True))


async def handle_support_admin_recovery_approve(request: web.Request) -> web.Response:
    agent = await _get_agent_session(request)
    if not agent:
        raise web.HTTPUnauthorized()
    if agent.auth_method != "telegram" or agent.telegram_id not in settings.support_agent_ids:
        return web.json_response(
            {"error": "Для одобрения восстановления войдите через проверенный Telegram-аккаунт администратора."},
            status=403,
        )
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest()
    profile_token = str(body.get("profile_token") or "").strip()
    order_id = str(body.get("order_id") or "").strip()
    verification_source = str(body.get("verification_source") or "").strip()
    verification_note = str(body.get("verification_note") or "").strip()[:2000]
    manual_basis = str(body.get("manual_basis") or "").strip()
    is_manual_review = verification_source == "yookassa_manual"
    if (
        not profile_token
        or not order_id
        or verification_source not in {"yookassa_api", "yookassa_manual"}
        or len(verification_note) < 10
        or (is_manual_review and manual_basis not in _RECOVERY_MANUAL_BASES)
    ):
        return web.json_response({"error": "Укажите подробности проверки и основание ручного решения"}, status=400)
    ticket_id = int(request.match_info["ticket_id"])
    async with async_session() as session:
        recovery = await session.scalar(select(SupportRecoveryRequest).where(SupportRecoveryRequest.ticket_id == ticket_id))
        ticket = await session.get(SupportTicket, ticket_id)
        if not recovery or not ticket:
            raise web.HTTPNotFound()
        if recovery.status != "pending":
            return web.json_response({"error": "Запрос уже обработан", "status": recovery.status}, status=409)
        recovery_snapshot = (recovery.id, recovery.old_contact, recovery.new_email, recovery.profile_token)
        candidates = await _recovery_candidates(session, recovery)
        candidate = next((item for item in candidates if item["profile_token"] == profile_token and item["order_id"] == order_id), None)
        if not candidate or not candidate["trusted_payment"]:
            return web.json_response({"error": "Недостаточно доверенных данных для подтверждения владения"}, status=403)
        order = await session.scalar(select(WebOrder).where(
            WebOrder.order_id == order_id,
            WebOrder.profile_token == profile_token,
        ))
        if not order:
            return web.json_response({"error": "Заказ для проверки не найден"}, status=403)
        account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == profile_token))
        if not account or await session.scalar(select(WebEmail.email).where(WebEmail.profile_token == profile_token)):
            return web.json_response({"error": "Профиль уже защищён email или недоступен"}, status=409)
        email_owner = await session.get(WebEmail, recovery.new_email)
        contact_owner = await session.scalar(select(WebAccount).where(WebAccount.contact == recovery.new_email))
        if (email_owner and email_owner.profile_token != profile_token) or (contact_owner and contact_owner.profile_token != profile_token):
            return web.json_response({"error": "Новый email уже используется"}, status=409)
        order_snapshot = _recovery_order_snapshot(order)

    payment_check = await _verify_recovery_payment(order, recovery)
    manual_approval_allowed = (
        is_manual_review
        and payment_check["payment_core_verified"]
        and (payment_check["manual_review_required"] or payment_check["ok"])
    )

    async with async_session() as session:
        await session.execute(__import__("sqlalchemy").text("BEGIN IMMEDIATE"))
        recovery = await session.scalar(select(SupportRecoveryRequest).where(SupportRecoveryRequest.ticket_id == ticket_id))
        ticket = await session.get(SupportTicket, ticket_id)
        if not recovery or not ticket:
            await session.rollback()
            raise web.HTTPNotFound()
        if recovery.status != "pending":
            current_status = recovery.status
            await session.rollback()
            return web.json_response({"error": "Запрос уже обработан", "status": current_status}, status=409)
        if (recovery.id, recovery.old_contact, recovery.new_email, recovery.profile_token) != recovery_snapshot:
            await session.rollback()
            return web.json_response({"error": "Данные обращения изменились во время проверки. Запустите проверку заново.", "status": "stale"}, status=409)
        candidates = await _recovery_candidates(session, recovery)
        candidate = next((item for item in candidates if item["profile_token"] == profile_token and item["order_id"] == order_id), None)
        if not candidate or not candidate["trusted_payment"]:
            await session.rollback()
            return web.json_response({"error": "Недостаточно доверенных данных для подтверждения владения"}, status=403)
        order = await session.scalar(select(WebOrder).where(
            WebOrder.order_id == order_id,
            WebOrder.profile_token == profile_token,
        ))
        if not order or _recovery_order_snapshot(order) != order_snapshot:
            await session.rollback()
            return web.json_response({"error": "Данные заказа изменились во время проверки. Запустите проверку заново.", "status": "stale"}, status=409)
        account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == profile_token))
        if not account or await session.scalar(select(WebEmail.email).where(WebEmail.profile_token == profile_token)):
            await session.rollback()
            return web.json_response({"error": "Профиль уже защищён email или недоступен"}, status=409)
        email_owner = await session.get(WebEmail, recovery.new_email)
        contact_owner = await session.scalar(select(WebAccount).where(WebAccount.contact == recovery.new_email))
        if (email_owner and email_owner.profile_token != profile_token) or (contact_owner and contact_owner.profile_token != profile_token):
            await session.rollback()
            return web.json_response({"error": "Новый email уже используется"}, status=409)
        if not payment_check["ok"] and not manual_approval_allowed:
            now = datetime.utcnow()
            recovery.payment_check_status = payment_check["status"]
            recovery.payment_check_reason = payment_check["reason"]
            recovery.payment_verification_source = "yookassa_api"
            recovery.payment_id = payment_check["payment_id"]
            recovery.payment_status = payment_check["payment_status"]
            recovery.payment_currency = payment_check["payment_currency"]
            recovery.payment_amount_rub = payment_check["payment_amount_rub"]
            recovery.claimant_evidence_type = payment_check["claimant_evidence_type"]
            session.add(SupportRecoveryAudit(
                request_id=recovery.id,
                ticket_id=ticket_id,
                action="payment_check_failed",
                profile_token=profile_token,
                order_id=order_id,
                agent_telegram_id=agent.telegram_id,
                agent_name=agent.telegram_full_name or agent.telegram_username or "Агент",
                verification_source=verification_source,
                payment_id=payment_check["payment_id"],
                payment_status=payment_check["payment_status"],
                payment_currency=payment_check["payment_currency"],
                payment_amount_rub=payment_check["payment_amount_rub"],
                details=(
                    f"result={payment_check['reason']}; manual_basis={manual_basis or 'none'}; "
                    f"note_sha256={hashlib.sha256(verification_note.encode()).hexdigest()}"
                ),
                created_at=now,
            ))
            await session.commit()
            response_status = 503 if payment_check["status"] == "provider_unavailable" else 403
            return web.json_response({
                "error": payment_check["reason"],
                "status": payment_check["status"],
                "recovery": _recovery_payload(recovery, candidates=candidates, include_profile_token=True),
            }, status=response_status)
        now = datetime.utcnow()
        recovery.status = "approved"
        recovery.profile_token = profile_token
        recovery.matched_order_id = order_id
        recovery.approved_by = agent.telegram_id
        recovery.approved_by_name = agent.telegram_full_name or agent.telegram_username or "Агент"
        recovery.approved_at = now
        recovery.approval_expires_at = now + timedelta(minutes=30)
        approval_source = "yookassa_manual" if is_manual_review else "yookassa_api"
        approval_evidence = f"manual:{manual_basis}" if is_manual_review else payment_check["claimant_evidence_type"]
        approval_reason = (
            f"Платёж подтверждён YooKassa; ручная проверка: {_RECOVERY_MANUAL_BASES[manual_basis]}."
            if is_manual_review
            else payment_check["reason"]
        )
        recovery.payment_check_status = "verified"
        recovery.payment_check_reason = approval_reason
        recovery.payment_verified_at = now
        recovery.payment_verification_source = approval_source
        recovery.payment_id = payment_check["payment_id"]
        recovery.payment_status = payment_check["payment_status"]
        recovery.payment_currency = payment_check["payment_currency"]
        recovery.payment_amount_rub = payment_check["payment_amount_rub"]
        recovery.claimant_evidence_type = approval_evidence
        session.add(SupportRecoveryAudit(
            request_id=recovery.id,
            ticket_id=ticket_id,
            action="approved",
            profile_token=profile_token,
            order_id=order_id,
            agent_telegram_id=agent.telegram_id,
            agent_name=recovery.approved_by_name,
            verification_source=approval_source,
            payment_id=payment_check["payment_id"],
            payment_status=payment_check["payment_status"],
            payment_currency=payment_check["payment_currency"],
            payment_amount_rub=payment_check["payment_amount_rub"],
            claimant_evidence_type=approval_evidence,
            details=(
                f"provider_result={payment_check['reason']}; manual_basis={manual_basis or 'none'}; "
                f"note_sha256={hashlib.sha256(verification_note.encode()).hexdigest()}"
            ),
            created_at=now,
        ))
        support_message = SupportMessage(
            ticket_id=ticket_id,
            sender="agent",
            agent_telegram_id=agent.telegram_id,
            agent_name=recovery.approved_by_name,
            text=(
                "Платёж подтверждён YooKassa, ручная проверка основания зафиксирована. "
                "Нажмите кнопку восстановления и подтвердите новый email кодом из письма."
                if is_manual_review
                else "Платёж и контакт плательщика подтверждены YooKassa. Нажмите кнопку восстановления и подтвердите новый email кодом из письма."
            ),
            created_at=now,
        )
        session.add(support_message)
        await session.commit()
        token = ticket.token
    asyncio.create_task(_support_broadcast(token, {"type": "message", "sender": "agent", "agent_name": recovery.approved_by_name,
                                                    "text": support_message.text, "created_at": now.isoformat(), "id": support_message.id}))
    return web.json_response({"ok": True, "recovery": _recovery_payload(recovery, include_profile_token=True)})


async def handle_support_admin_recovery_deny(request: web.Request) -> web.Response:
    agent = await _get_agent_session(request)
    if not agent:
        raise web.HTTPUnauthorized()
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest()
    reason = str(body.get("reason") or "").strip()[:2000]
    if len(reason) < 5:
        return web.json_response({"error": "Укажите причину отказа"}, status=400)
    ticket_id = int(request.match_info["ticket_id"])
    now = datetime.utcnow()
    async with async_session() as session:
        await session.execute(__import__("sqlalchemy").text("BEGIN IMMEDIATE"))
        recovery = await session.scalar(select(SupportRecoveryRequest).where(SupportRecoveryRequest.ticket_id == ticket_id))
        ticket = await session.get(SupportTicket, ticket_id)
        if not recovery or not ticket:
            await session.rollback()
            raise web.HTTPNotFound()
        if recovery.status != "pending":
            current_status = recovery.status
            await session.rollback()
            return web.json_response({"error": "Запрос уже обработан", "status": current_status}, status=409)
        agent_telegram_id = agent.telegram_id if agent.auth_method == "telegram" else None
        recovery.status = "denied"
        recovery.denied_reason = reason
        recovery.approved_by = agent_telegram_id
        recovery.approved_by_name = agent.telegram_full_name or agent.telegram_username or "Агент"
        session.add(SupportRecoveryAudit(
            request_id=recovery.id,
            ticket_id=ticket_id,
            action="denied",
            agent_telegram_id=agent_telegram_id,
            agent_name=recovery.approved_by_name,
            details=reason,
            created_at=now,
        ))
        await session.commit()
    return web.json_response({"ok": True, "recovery": _recovery_payload(recovery, include_profile_token=True)})


async def handle_support_admin_send_message(request: web.Request) -> web.Response:
    agent = await _get_agent_session(request)
    if not agent:
        raise web.HTTPUnauthorized()
    ticket_id = int(request.match_info["ticket_id"])
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest()
    text_val = (body.get("text") or "").strip()
    media_url = (body.get("media_url") or "").strip() or None
    media_type = (body.get("media_type") or "image").strip() if media_url else None
    if not text_val and not media_url:
        return web.json_response({"error": "empty"}, status=400)

    async with async_session() as session:
        ticket = await session.get(SupportTicket, ticket_id)
        if not ticket:
            raise web.HTTPNotFound()
        if ticket.status == "resolved":
            return web.json_response({"error": "ticket_closed"}, status=400)
        now = datetime.utcnow()
        agent_name = agent.telegram_full_name or agent.telegram_username or "Поддержка"
        msg = SupportMessage(
            ticket_id=ticket_id,
            sender="agent",
            agent_telegram_id=agent.telegram_id,
            agent_name=agent_name,
            text=text_val or None,
            media_url=media_url,
            media_type=media_type,
            created_at=now,
        )
        ticket.last_message_at = now
        session.add(msg)
        await session.commit()
        msg_id = msg.id
        token = ticket.token

    payload = {
        "type": "message",
        "sender": "agent",
        "agent_name": agent_name,
        "text": text_val,
        "media_url": media_url,
        "media_type": media_type,
        "id": msg_id,
        "created_at": now.isoformat(),
    }
    asyncio.create_task(_support_broadcast(token, payload))
    return web.json_response({"ok": True})


async def handle_support_admin_resolve(request: web.Request) -> web.Response:
    agent = await _get_agent_session(request)
    if not agent:
        raise web.HTTPUnauthorized()
    ticket_id = int(request.match_info["ticket_id"])
    async with async_session() as session:
        ticket = await session.get(SupportTicket, ticket_id)
        if not ticket:
            raise web.HTTPNotFound()
        ticket.status = "resolved"
        ticket.resolved_at = datetime.utcnow()
        await session.commit()
        token = ticket.token

    asyncio.create_task(_support_broadcast(token, {"type": "resolved"}))
    return web.json_response({"ok": True})


async def handle_support_admin_me(request: web.Request) -> web.Response:
    agent = await _get_agent_session(request)
    if not agent:
        raise web.HTTPUnauthorized()
    return web.json_response({
        "telegram_id": agent.telegram_id,
        "name": agent.telegram_full_name or agent.telegram_username or "Агент",
        "username": agent.telegram_username,
        "auth_method": agent.auth_method,
        "can_approve_recovery": agent.auth_method == "telegram" and agent.telegram_id in settings.support_agent_ids,
    })


# ---------------------------------------------------------------------------
# Support chat — WebSocket handlers
# ---------------------------------------------------------------------------

async def handle_support_ws_client(request: web.Request) -> web.WebSocketResponse:
    token = request.match_info["token"]
    async with async_session() as session:
        result = await session.execute(select(SupportTicket).where(SupportTicket.token == token))
        ticket = result.scalar_one_or_none()
    if not ticket:
        raise web.HTTPNotFound()

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    pool = _support_pool_get(token)
    pool["client"] = ws
    await _support_broadcast(token, {"type": "presence", "who": "client", "online": True}, skip=ws)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                if data.get("type") == "typing":
                    await _support_broadcast(token, {"type": "typing", "sender": "client"}, skip=ws)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        pool = _support_ws_pool.get(token, {})
        if pool.get("client") is ws:
            pool["client"] = None
        await _support_broadcast(token, {"type": "presence", "who": "client", "online": False})

    return ws


async def handle_support_ws_agent(request: web.Request) -> web.WebSocketResponse:
    agent = await _get_agent_session(request)
    if not agent:
        raise web.HTTPUnauthorized()
    ticket_id = int(request.match_info["ticket_id"])

    async with async_session() as session:
        ticket = await session.get(SupportTicket, ticket_id)
    if not ticket:
        raise web.HTTPNotFound()
    token = ticket.token

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)

    pool = _support_pool_get(token)
    pool["agents"].append(ws)
    await _support_broadcast(token, {"type": "presence", "who": "agent", "online": True}, skip=ws)

    try:
        async for msg in ws:
            if msg.type == web.WSMsgType.TEXT:
                try:
                    data = json.loads(msg.data)
                except Exception:
                    continue
                if data.get("type") == "typing":
                    await _support_broadcast(token, {"type": "typing", "sender": "agent"}, skip=ws)
            elif msg.type in (web.WSMsgType.ERROR, web.WSMsgType.CLOSE):
                break
    finally:
        pool = _support_ws_pool.get(token, {})
        pool["agents"] = [w for w in pool.get("agents", []) if w is not ws]
        remaining = [w for w in pool.get("agents", []) if not w.closed]
        if not remaining:
            await _support_broadcast(token, {"type": "presence", "who": "agent", "online": False})

    return ws


# ---------------------------------------------------------------------------
# Adapt Device Management (Web Profile)
# ---------------------------------------------------------------------------

async def _resolve_owned_adapt_device_target(
    session: AsyncSession, profile_token: str, device_target: str
) -> str | None:
    """Resolve an opaque server-issued device_target to a trusted adapt_uuid.

    Rejects any unowned, foreign, or malformed target.
    """
    if not profile_token or not device_target:
        return None
    device_target = device_target.strip()

    if device_target.startswith("tgitem:"):
        raw_id = device_target.removeprefix("tgitem:").strip()
        try:
            item_id = int(raw_id)
        except (ValueError, TypeError):
            return None
        item = await session.scalar(
            select(WebTelegramItem)
            .where(WebTelegramItem.id == item_id)
            .where(WebTelegramItem.profile_token == profile_token)
            .limit(1)
        )
        if item and (item.provider == "adapt" or _get_access_provider(item.key_value, item.external_id) == "adapt"):
            return _extract_adapt_uuid(item.key_value, item.external_id)
        return None

    if device_target.startswith("order:"):
        raw_order_id = device_target.removeprefix("order:").strip()
        order = await session.scalar(
            select(WebOrder)
            .where(
                or_(
                    WebOrder.order_id == raw_order_id,
                    WebOrder.id == int(raw_order_id) if raw_order_id.isdigit() else False,
                )
            )
            .where(WebOrder.profile_token == profile_token)
            .where(WebOrder.status.in_(("delivered", "demo", "paid")))
            .limit(1)
        )
        if not order or _get_order_provider(order) != "adapt":
            return None

        adapt_uuid = _extract_adapt_uuid_from_web_order(order)
        if not adapt_uuid:
            return None

        # Verify true ownership of this adapt_uuid under this profile_token:
        # 1. Profile owns a root issuance order that created/issued this UUID
        has_root_order = await session.scalar(
            select(WebOrder.id)
            .where(WebOrder.profile_token == profile_token)
            .where(WebOrder.status.in_(("delivered", "demo")))
            .where(
                or_(
                    WebOrder.subscription_url.contains(adapt_uuid),
                    WebOrder.marzban_username == f"adapt_{adapt_uuid}",
                )
            )
            .where(
                or_(
                    WebOrder.purchase_action == "new",
                    WebOrder.target_order_id == None,
                    WebOrder.target_order_id == "",
                    WebOrder.tariff_key == "demo",
                    WebOrder.status == "demo",
                )
            )
            .limit(1)
        )
        if has_root_order:
            return adapt_uuid

        # 2. Profile has a WebTelegramItem for this UUID
        has_tg_item = await session.scalar(
            select(WebTelegramItem.id)
            .where(WebTelegramItem.profile_token == profile_token)
            .where(
                or_(
                    WebTelegramItem.key_value.contains(adapt_uuid),
                    WebTelegramItem.external_id.contains(adapt_uuid),
                )
            )
            .limit(1)
        )
        if has_tg_item:
            return adapt_uuid

        # Raw renewal order without proven origin does not grant device management
        return None

    return None


async def handle_devices_list(request: web.Request) -> web.Response:
    profile_token = await _authenticated_profile(request)
    if not profile_token:
        return web.json_response({"error": "Требуется авторизация"}, status=401)

    device_target = str(request.query.get("target") or "").strip()
    if not device_target:
        return web.json_response({"error": "Цель не указана"}, status=400)

    async with async_session() as session:
        adapt_uuid = await _resolve_owned_adapt_device_target(session, profile_token, device_target)

    if not adapt_uuid:
        return web.json_response({"error": "Подписка не найдена"}, status=404)

    api = AdaptAPI()
    try:
        devices_res, status_res = await asyncio.wait_for(
            asyncio.gather(
                api.get_devices(adapt_uuid),
                api.get_status(adapt_uuid),
                return_exceptions=True,
            ),
            timeout=8.0,
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Timed out loading Adapt devices for web target %s", device_target)
        return web.json_response(
            {"error": "Время ожидания ответа от провайдера истекло. Пожалуйста, попробуйте позже."},
            status=504,
        )
    except Exception as exc:
        logger.error("Failed to fetch devices for adapt_uuid %s: %s", adapt_uuid, exc)
        return web.json_response({"error": "Не удалось загрузить список устройств"}, status=502)

    if isinstance(devices_res, Exception):
        logger.error("Error from AdaptAPI.get_devices: %s", devices_res)
        return web.json_response({"error": "Не удалось получить список устройств у провайдера"}, status=502)

    raw_devices = devices_res if isinstance(devices_res, list) else []
    device_limit = 1
    if not isinstance(status_res, Exception) and isinstance(status_res, dict):
        device_limit = int(status_res.get("devices") or 1)

    formatted_devices = [adapt_device_details(dev, idx + 1) for idx, dev in enumerate(raw_devices)]

    return web.json_response({
        "ok": True,
        "device_target": device_target,
        "device_limit": device_limit,
        "used_slots": len(formatted_devices),
        "devices": formatted_devices,
    })


async def handle_device_delete(request: web.Request) -> web.Response:
    profile_token = await _authenticated_profile(request)
    if not profile_token:
        return web.json_response({"error": "Требуется авторизация"}, status=401)

    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    device_target = str(body.get("device_target") or body.get("target") or "").strip()
    raw_device_id = body.get("device_id")
    if not device_target or raw_device_id is None:
        return web.json_response({"error": "Не указаны параметры устройства"}, status=400)

    try:
        device_id = int(raw_device_id)
    except (ValueError, TypeError):
        return web.json_response({"error": "Неверный идентификатор устройства"}, status=400)

    async with async_session() as session:
        adapt_uuid = await _resolve_owned_adapt_device_target(session, profile_token, device_target)

    if not adapt_uuid:
        return web.json_response({"error": "Подписка не найдена"}, status=404)

    api = AdaptAPI()
    try:
        devices = await asyncio.wait_for(api.get_devices(adapt_uuid), timeout=8.0)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Timed out fetching devices before delete for web target %s", device_target)
        return web.json_response(
            {"error": "Время ожидания ответа от провайдера истекло. Пожалуйста, попробуйте позже."},
            status=504,
        )
    except Exception as exc:
        logger.error("Failed to fetch devices before deletion for %s: %s", adapt_uuid, exc)
        return web.json_response({"error": "Не удалось проверить список устройств у провайдера"}, status=502)

    if not isinstance(devices, list):
        return web.json_response({"error": "Некорректный ответ списка устройств от провайдера"}, status=502)

    device_exists = any(
        str(d.get("id") or d.get("device_id")) == str(device_id)
        for d in devices
    )
    if not device_exists:
        return web.json_response({"error": "Устройство не найдено в этой подписке"}, status=404)

    try:
        success = await asyncio.wait_for(api.delete_device(adapt_uuid, device_id), timeout=8.0)
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("Timed out deleting device %s for web target %s", device_id, device_target)
        return web.json_response(
            {"error": "Время ожидания ответа от провайдера при удалении устройства истекло. Пожалуйста, попробуйте позже."},
            status=504,
        )
    except Exception as exc:
        logger.error("Failed to delete device %s for adapt_uuid %s: %s", device_id, adapt_uuid, exc)
        return web.json_response({"error": "Ошибка удаления устройства у провайдера"}, status=502)

    if not success:
        return web.json_response({"error": "Провайдер отклонил удаление устройства"}, status=502)

    return web.json_response({"ok": True})


def setup_routes(app: web.Application) -> None:
    app.router.add_get("/", handle_store_page)
    app.router.add_get("/buy", handle_store_page)
    app.router.add_get("/renew", handle_store_page)
    app.router.add_get("/pay/success", handle_success_page)
    app.router.add_get("/api/store/logo", handle_logo)
    app.router.add_get("/api/store/logo.jpg", handle_logo)
    app.router.add_get("/api/store/favicon", handle_favicon)

    # API
    app.router.add_post("/api/store/create-order", handle_create_order)
    app.router.add_get("/api/store/auth/me", handle_auth_me)
    app.router.add_post("/api/store/auth/register", handle_auth_register)
    app.router.add_post("/api/store/auth/login", handle_auth_login)
    app.router.add_post("/api/store/auth/logout", handle_auth_logout)
    app.router.add_post("/api/store/auth/forgot", handle_forgot_password)
    app.router.add_post("/api/store/auth/reset", handle_email_token)
    app.router.add_post("/api/store/auth/confirm", handle_email_token)
    app.router.add_post("/api/store/auth/email", handle_email_change)
    app.router.add_post("/api/store/auth/otp/start", otp_auth.start)
    app.router.add_post("/api/store/auth/otp/status", otp_auth.status)
    app.router.add_post("/api/store/auth/otp/resend", otp_auth.resend)
    app.router.add_post("/api/store/auth/otp/verify", otp_auth.verify)
    app.router.add_post("/api/store/auth/otp/reset", otp_auth.reset)
    app.router.add_get("/email-confirm", handle_email_page)
    app.router.add_get("/password-reset", handle_email_page)
    app.router.add_post("/api/store/create-device-order", handle_create_device_order)
    app.router.add_post("/api/store/create-balance-topup", handle_create_balance_topup)
    app.router.add_post("/api/store/balance-toggle", handle_balance_toggle)
    app.router.add_get("/api/store/order-status", handle_order_status)
    app.router.add_post("/api/store/profile-lookup", handle_profile_lookup)
    app.router.add_post("/api/store/quick-renew/lookup", handle_quick_renew_lookup)
    app.router.add_get("/api/store/devices/list", handle_devices_list)
    app.router.add_post("/api/store/devices/delete", handle_device_delete)
    app.router.add_post("/api/store/create-profile", handle_create_profile)
    app.router.add_post("/api/store/profile-link-init", handle_profile_link_init)
    app.router.add_post("/api/store/telegram-auth-init", handle_telegram_auth_init)
    app.router.add_get("/api/store/telegram-auth-status", handle_telegram_auth_status)
    app.router.add_get("/api/store/profile-orders", handle_profile_orders)
    app.router.add_post("/api/store/cancel-order", handle_cancel_order)
    app.router.add_post("/api/store/internal/telegram-link-claim", handle_internal_telegram_link_claim)
    app.router.add_post("/api/store/internal/telegram-auth-claim", handle_internal_telegram_auth_claim)
    app.router.add_post("/api/store/internal/telegram-sync", handle_internal_telegram_sync)
    app.router.add_get("/api/store/internal/web-profile", handle_internal_web_profile)
    app.router.add_get("/api/store/internal/admin-stats", handle_internal_admin_stats)
    app.router.add_get("/api/store/internal/admin-client-lookup", handle_internal_admin_client_lookup)
    app.router.add_get("/api/store/internal/admin-clients-list", handle_internal_admin_clients_list)
    app.router.add_get("/api/store/internal/admin-orders", handle_internal_admin_orders)
    app.router.add_get("/api/store/internal/admin-order", handle_internal_admin_order)
    app.router.add_post("/api/store/internal/admin-order-action", handle_internal_admin_order_action)
    app.router.add_get("/api/store/internal/admin-balance-lookup", handle_internal_admin_balance_lookup)
    app.router.add_post("/api/store/internal/admin-balance-adjust", handle_internal_admin_balance_adjust)
    app.router.add_post("/api/store/internal/adapt-event", handle_internal_adapt_event)

    # Profile
    app.router.add_get("/profile", handle_profile_page)

    # Support chat
    app.router.add_get("/support", handle_support_page)
    app.router.add_get("/support/admin", handle_support_admin_page)
    app.router.add_post("/api/support/new-ticket", handle_support_new_ticket)
    app.router.add_post("/api/support/upload", handle_support_upload)
    app.router.add_get("/api/support/media/{filename}", handle_support_media)
    app.router.add_get("/api/support/ticket/{token}", handle_support_ticket_info)
    app.router.add_get("/api/support/recovery/{token}", handle_support_recovery_status)
    app.router.add_post("/api/support/ticket/{token}/message", handle_support_send_message)
    app.router.add_post("/api/support/admin/login", handle_support_admin_login)
    app.router.add_get("/api/support/admin/me", handle_support_admin_me)
    app.router.add_get("/api/support/admin/tickets", handle_support_admin_tickets)
    app.router.add_get("/api/support/admin/ticket/{ticket_id}/messages", handle_support_admin_ticket_messages)
    app.router.add_get("/api/support/admin/ticket/{ticket_id}/recovery", handle_support_admin_recovery_candidates)
    app.router.add_post("/api/support/admin/ticket/{ticket_id}/recovery/approve", handle_support_admin_recovery_approve)
    app.router.add_post("/api/support/admin/ticket/{ticket_id}/recovery/deny", handle_support_admin_recovery_deny)
    app.router.add_post("/api/support/admin/ticket/{ticket_id}/message", handle_support_admin_send_message)
    app.router.add_post("/api/support/admin/ticket/{ticket_id}/resolve", handle_support_admin_resolve)
    app.router.add_get("/ws/support/{token}", handle_support_ws_client)
    app.router.add_get("/ws/support/admin/{ticket_id}", handle_support_ws_agent)

    # Static Assets
    app.router.add_get("/api/store/assets/{filename}", handle_store_asset)
    app.router.add_static("/assets", Path(__file__).parent / "assets")

    # Webhook
    app.router.add_post("/pay/webhook", handle_yookassa_webhook)
