"""HTTP route handlers for the web store."""

from __future__ import annotations

import asyncio
import base64
import html
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

import aiohttp
from aiohttp import web
from sqlalchemy import delete, func, or_, select

from bot.services.payment_logger import plog
from bot.services.provisioning_issues import (
    AccessProvisionError,
    build_internal_access_error,
    build_vhq_access_error,
)
from bot.services.vhq_partner_api import VHQPartnerAPI, VHQPartnerAPIError
from bot.services.vhq_routing import get_vhq_spec_for_store_tariff
from bot.services.adapt_api import AdaptAPI, AdaptAPIError
try:
    from bot.services.adapt_subscription_proxy import build_adapt_mirror_url
except (ImportError, Exception):
    async def build_adapt_mirror_url(*args, **kwargs):  # type: ignore[misc]
        raise RuntimeError("Adapt subscription proxy not available in this environment")
from webstore.config import get_store_tariffs, get_store_tariffs_by_key, settings
from webstore.database import async_session
from webstore.marzban import MarzbanClient
from webstore.models import (
    SupportAgentSession,
    SupportMessage,
    SupportTicket,
    WebBalanceAccount,
    WebBalanceTopUp,
    WebBalanceTransaction,
    WebAccount,
    WebOrder,
    WebProfileLink,
    WebProfileLinkCode,
    WebTelegramAuthCode,
    WebTelegramItem,
)
from webstore.vhq_proxy import build_vhq_mirror_url

logger = logging.getLogger(__name__)
_MSK = timezone(timedelta(hours=3))


def _web_plog(event: str, **fields: object) -> None:
    plog(event, _instance_hint=f"webstore_{settings.port}", **fields)


def _support_hint_text() -> str:
    if settings.support_url:
        return f" Если вопрос срочный, напишите в поддержку: {settings.support_url}"
    return ""


async def _notify_admins_webstore(
    order: WebOrder,
    event: str = "paid",
    *,
    issue: AccessProvisionError | None = None,
) -> None:
    """Send Telegram notification to admins about webstore payment."""
    if not settings.admin_bot_token or not settings.admin_ids:
        return

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
            f"Сумма: {order.amount_rub}₽\n"
            f"Контакт: {html.escape(order.contact or order.email or '—')}\n"
            f"Заказ: <code>{order.order_id}</code>\n"
            f"Источник: {source_line}"
        )
    elif event == "delivered":
        text = (
            f"✅ <b>Ключ выдан (веб-магазин)</b>\n\n"
            f"Тариф: {order.tariff_label}\n"
            f"Заказ: <code>{order.order_id}</code>\n"
            f"Marzban: <code>{order.marzban_username}</code>"
        )
    elif event == "failed" and issue is not None:
        text = (
            f"🚨 <b>Ошибка выдачи (веб-магазин)</b>\n\n"
            f"Тариф: {html.escape(order.tariff_label)}\n"
            f"Сумма: {order.amount_rub}₽\n"
            f"Контакт: {html.escape(order.contact or order.email or '—')}\n"
            f"Заказ: <code>{order.order_id}</code>\n"
            f"Провайдер: {html.escape(issue.provider)}\n"
            f"Код: {html.escape(issue.code)}\n"
            f"HTTP статус: {html.escape(str(issue.status or '—'))}\n"
            f"Причина: {html.escape(issue.admin_message)}"
        )
    else:
        return

    api_url = f"https://api.telegram.org/bot{settings.admin_bot_token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as http:
            for admin_id in settings.admin_ids:
                try:
                    await http.post(api_url, json={
                        "chat_id": admin_id,
                        "text": text,
                        "parse_mode": "HTML",
                    })
                except Exception:
                    pass
    except Exception as e:
        logger.warning("Failed to notify admins: %s", e)


async def _mark_order_failed(order: WebOrder, issue: AccessProvisionError) -> None:
    order.status = "failed"
    order.failure_message = f"{issue.client_message}{_support_hint_text()}".strip()
    order.failure_reason = issue.admin_message
    _web_plog(
        "WEB_ОШИБКА_ВЫДАЧИ",
        order_id=order.order_id,
        tariff=order.tariff_label,
        provider=issue.provider,
        code=issue.code,
        status=issue.status or "",
    )
    await _notify_admins_webstore(order, "failed", issue=issue)

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
    real_ip = request.headers.get("X-Real-IP", "")
    if real_ip:
        return real_ip.strip()
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote or "unknown"


_PROFILE_SECRET = settings.yookassa_secret_key or "webstore-profile-secret"
_INTERNAL_SECRET_HEADER = "X-Internal-Secret"
_AUTH_COOKIE = "webstore_profile_token"
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


def _set_profile_cookie(response: web.StreamResponse, token: str) -> None:
    response.set_cookie(
        _AUTH_COOKIE,
        token,
        max_age=_AUTH_COOKIE_MAX_AGE,
        path="/",
        secure=True,
        httponly=False,
        samesite="Lax",
    )


def _clear_profile_cookie(response: web.StreamResponse) -> None:
    response.del_cookie(_AUTH_COOKIE, path="/")


def _auth_response(payload: dict, token: str | None = None) -> web.Response:
    response = web.json_response(payload)
    if token:
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
    raw_url = str(order.subscription_url or "").strip()
    if not raw_url or order.status not in ("delivered", "demo"):
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


def _next_charge_datetime(now: datetime | None = None) -> datetime:
    current = now or datetime.utcnow().replace(tzinfo=timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    local = current.astimezone(_MSK)
    candidate = local.replace(hour=5, minute=0, second=0, microsecond=0)
    if local >= candidate:
        candidate = candidate + timedelta(days=1)
    return candidate.astimezone(timezone.utc).replace(tzinfo=None)


def _serialize_orders(orders: list[WebOrder]) -> list[dict[str, str | int | None]]:
    return [
        {
            "order_id": o.order_id,
            "tariff_label": o.tariff_label,
            "tariff_key": o.tariff_key,
            "days": o.days,
            "amount_rub": o.amount_rub,
            "original_amount_rub": o.original_amount_rub or o.amount_rub,
            "bonus_applied_rub": o.bonus_applied_rub or 0,
            "status": o.status,
            "provider": _get_order_provider(o),
            "subscription_url": _display_order_subscription_url(o),
            "raw_subscription_url": o.subscription_url or "",
            "failure_message": o.failure_message if o.status == "failed" else None,
            "entry_referrer": o.entry_referrer or "",
            "entry_url": o.entry_url or "",
            "ref_source": o.ref_source or "",
            "referrer_telegram_id": o.referrer_telegram_id,
            "created_at": (o.created_at.isoformat() + "Z") if o.created_at else None,
            "paid_at": (o.paid_at.isoformat() + "Z") if o.paid_at else None,
            "delivered_at": (o.delivered_at.isoformat() + "Z") if o.delivered_at else None,
            "access_expires_at": (o.access_expires_at.isoformat() + "Z") if o.access_expires_at else None,
        }
        for o in orders
    ]


def _serialize_telegram_items(items: list[WebTelegramItem]) -> list[dict[str, str | int | None]]:
    return [
        {
            "item_type": item.item_type,
            "title": item.title,
            "subtitle": item.subtitle,
            "key_value": item.key_value,
            "status": item.status,
            "device_slots": item.device_slots,
            "expires_at": item.expires_at.isoformat() if item.expires_at else None,
        }
        for item in items
    ]


def _get_order_provider(order: WebOrder) -> str:
    tariff = get_store_tariffs_by_key().get(order.tariff_key)
    if tariff and tariff.get("provider"):
        return str(tariff["provider"])
    sub_url = str(order.subscription_url or "")
    m_user = str(order.marzban_username or "")
    if m_user.startswith("adapt_") or "/adapt-sub/" in sub_url or "adaptgroup" in sub_url:
        return "adapt"
    if "/proxy-subscription/" in sub_url or "vhq-connect" in sub_url or "/vhq-sub/" in sub_url:
        return "vhq"
    if m_user:
        return "marzban"
    return ""


def _verify_internal_secret(request: web.Request) -> bool:
    expected = settings.bridge_shared_secret.strip()
    actual = request.headers.get(_INTERNAL_SECRET_HEADER, "").strip()
    return bool(expected and actual and hmac.compare_digest(expected, actual))


def _bridge_headers() -> dict[str, str]:
    return {_INTERNAL_SECRET_HEADER: settings.bridge_shared_secret}


async def _migrate_web_balance_to_telegram(session, profile_token: str, telegram_id: int) -> None:
    """Transfer web-only balance to Telegram user via bridge on profile link."""
    account = await session.get(WebBalanceAccount, profile_token)
    if not account or not account.balance_rub or account.balance_rub <= 0:
        return
    if not settings.bridge_shared_secret or not settings.referral_bridge_url:
        return

    amount = int(account.balance_rub)
    if amount <= 0:
        return

    url = f"{settings.referral_bridge_url.rstrip('/')}/internal/balance-credit"
    payload = {
        "telegram_id": telegram_id,
        "amount_rub": amount,
        "source_type": "web_balance_migration",
        "source_id": f"web_migrate_{profile_token[:16]}",
        "description": "Перенос баланса с сайта",
    }
    timeout = aiohttp.ClientTimeout(total=8)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as http:
            async with http.post(url, json=payload, headers=_bridge_headers()) as resp:
                data = await resp.json() if resp.content_type == "application/json" else {}
                if data.get("status") in ("credited", "already_credited"):
                    _add_web_balance_transaction(
                        session,
                        account,
                        amount_rub=amount,
                        direction="debit",
                        kind="balance_migration",
                        description="Перенос баланса в Telegram",
                        source_id=f"tg:{telegram_id}",
                    )
                    logger.info("Migrated %d₽ web balance to Telegram %d", amount, telegram_id)
                else:
                    logger.warning("Web balance migration failed for %s: %s", profile_token, data)
    except Exception as exc:
        logger.warning("Web balance migration error for %s: %s", profile_token, exc)


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
                tariff_label=f"Демо-доступ {demo_days} дн. (Adapt)",
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


async def _fetch_public_balance_config() -> dict[str, float] | None:
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
                result: dict[str, float] = {}
                if daily_value is not None:
                    result["daily_charge_rub"] = round(float(daily_value), 2)
                if referral_value is not None:
                    result["referral_commission_percent"] = round(float(referral_value), 2)
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
    for item in items:
        external_id = (str(item.get("external_id", "")) or secrets.token_hex(8))[:64]
        session.add(
            WebTelegramItem(
                profile_token=profile_token,
                telegram_id=telegram_id,
                item_type=(item.get("item_type") or "vpn")[:16],
                external_id=external_id,
                title=(item.get("title") or "Telegram").strip()[:128],
                subtitle=(item.get("subtitle") or "").strip()[:256] or None,
                key_value=item.get("key_value"),
                status=(item.get("status") or "active")[:16],
                device_slots=item.get("device_slots"),
                expires_at=datetime.fromisoformat(item["expires_at"]) if item.get("expires_at") else None,
                created_at=now,
                updated_at=now,
            )
        )


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
            delete(WebTelegramItem).where(WebTelegramItem.profile_token == other_link.profile_token)
        )
        await session.delete(other_link)

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
            '🇪🇪 Эстония  •  🇳🇱 Нидерланды  •  🇩🇪 Германия<br>'
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
            '🇪🇪 Эстония  •  🇳🇱 Нидерланды  •  🇩🇪 Германия  •  и другие<br>'
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
    entry_referrer = (body.get("entry_referrer") or "").strip()[:512] or None
    entry_url = (body.get("entry_url") or "").strip()[:512] or None

    tariff = get_store_tariffs_by_key().get(tariff_key)
    if not tariff:
        return web.json_response({"error": "Unknown tariff"}, status=400)

    saved_profile_token = request.cookies.get(_AUTH_COOKIE, "").strip()
    linked_profile = None
    web_account = None
    if saved_profile_token:
        async with async_session() as session:
            linked_profile = await session.get(WebProfileLink, saved_profile_token)
            web_account = await session.scalar(
                select(WebAccount).where(WebAccount.profile_token == saved_profile_token).limit(1)
            )

    if not saved_profile_token or (not linked_profile and not web_account):
        return web.json_response(
            {"error": "Сначала зарегистрируйтесь или войдите, чтобы ключ сохранился в личном кабинете.", "auth_required": True},
            status=401,
        )

    if web_account:
        profile_token = web_account.profile_token
        contact = _normalize_contact(web_account.contact)
        email = _extract_receipt_email(contact)
    else:
        profile_token = saved_profile_token

    if not contact and linked_profile:
        contact = _normalize_contact(linked_profile.contact) if linked_profile.contact else None
        email = _extract_receipt_email(contact) if contact else None

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
    profile_token = profile_token or (_make_profile_token(contact) if contact else saved_profile_token)
    original_amount_rub = int(tariff["price_rub"])
    bonus_applied_rub = 0

    # Save order to DB
    async with async_session() as session:
        if saved_profile_token:
            session_linked_profile = await session.get(WebProfileLink, saved_profile_token)
            if session_linked_profile:
                profile_token = saved_profile_token
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
        balance_account = await _get_or_create_web_balance_account(session, profile_token, contact)
        if use_balance and balance_account.balance_rub > 0:
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
            if db_order.status == "delivered":
                await _disable_web_balance_autodebit_after_tariff_purchase(session, db_order)
                await _apply_order_bonus_spend(session, db_order)
                await _notify_admins_webstore(db_order, "delivered")
            await session.commit()
            if db_order.status == "delivered":
                await _sync_referral_credit_for_order(db_order.order_id)
        return web.json_response({
            "redirect_url": f"{settings.subscription_base_url}/pay/success?order_id={order_id}",
            "order_id": order_id,
        })

    # Create YooKassa payment
    try:
        import yookassa

        yookassa.Configuration.account_id = settings.yookassa_shop_id
        yookassa.Configuration.secret_key = settings.yookassa_secret_key

        price = original_amount_rub - bonus_applied_rub
        return_url = f"{settings.subscription_base_url}/pay/success?order_id={order_id}"

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

        return web.json_response({
            "redirect_url": payment.confirmation.confirmation_url,
            "order_id": order_id,
        })

    except Exception as e:
        logger.error("Failed to create YooKassa payment: %s", e)
        return web.json_response(
            {"error": "Ошибка создания платежа. Попробуйте позже."},
            status=500,
        )


async def handle_create_device_order(request: web.Request) -> web.Response:
    """Buy an extra device slot for the current profile's active subscription."""
    profile_token = request.cookies.get("webstore_profile_token", "").strip()
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
        return_url = f"{settings.subscription_base_url.rstrip('/')}/profile?order_id={order_id}"

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

    profile_token = (body.get("token") or request.cookies.get("webstore_profile_token") or "").strip()
    if not profile_token:
        return web.json_response(
            {"error": "Сначала зарегистрируйтесь или войдите, чтобы пополнение сохранилось в личном кабинете.", "auth_required": True},
            status=401,
        )

    async with async_session() as session:
        link = await session.get(WebProfileLink, profile_token)
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
        return_url = f"{settings.subscription_base_url.rstrip('/')}/profile?topup_id={topup_id}"

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

    should_retry_referral = False
    async with async_session() as session:
        result = await session.execute(
            select(WebOrder).where(WebOrder.order_id == order_id)
        )
        order = result.scalar_one_or_none()
        if not order:
            return web.json_response({"error": "Order not found"}, status=404)

        if order.status == "pending" and order.yookassa_payment_id and settings.yookassa_enabled:
            try:
                import yookassa

                yookassa.Configuration.account_id = settings.yookassa_shop_id
                yookassa.Configuration.secret_key = settings.yookassa_secret_key
                payment = await asyncio.to_thread(yookassa.Payment.find_one, order.yookassa_payment_id)
                payment_status = getattr(payment, "status", "")
                if payment_status == "canceled":
                    order.status = "canceled"
                    await session.commit()
            except Exception as e:
                logger.warning("Failed to refresh YooKassa payment status for %s: %s", order_id, e)

        should_retry_referral = (
            order.status == "delivered"
            and bool(order.ref_source)
            and order.referral_status in {None, "ready", "failed"}
        )

        response_data = {
            "status": order.status,
            "subscription_url": order.subscription_url,
            "tariff_label": order.tariff_label,
            "days": order.days,
            "profile_token": order.profile_token,
            "failure_message": order.failure_message,
        }

    if should_retry_referral:
        await _sync_referral_credit_for_order(order_id)

    return web.json_response(response_data)


# ── YooKassa Webhook ──


async def _fulfill_order(session, order: WebOrder) -> None:
    """Create Marzban user and set subscription URL on the order."""
    if order.tariff_key == "device_slot":
        await _fulfill_device_slot_order(session, order)
        return

    tariff = get_store_tariffs_by_key().get(order.tariff_key)
    vhq_spec = get_vhq_spec_for_store_tariff(tariff)
    if vhq_spec:
        await _fulfill_vhq_order(order, vhq_spec)
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


async def _fulfill_vhq_order(order: WebOrder, vhq_spec: dict[str, int | str]) -> None:
    try:
        response = await VHQPartnerAPI(
            api_key=settings.vhq_partner_api_key,
            base_url=settings.vhq_partner_api_url,
        ).buy(
            tier=str(vhq_spec["tier"]),
            days=int(vhq_spec["days"]),
        )
    except VHQPartnerAPIError as exc:
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
            code="vhq_runtime",
            admin_message=f"Unexpected VHQ web fulfillment error order_id={order.order_id}: {exc}",
            raw_message=str(exc),
        )
        logger.error(issue.admin_message)
        await _mark_order_failed(order, issue)
        return

    try:
        upstream_url = VHQPartnerAPI.extract_subscription_url(response)
        if not upstream_url:
            issue = build_vhq_access_error(
                status=None,
                message=f"missing subscription url response={response}",
                context=f"order_id={order.order_id}",
            )
            logger.error(issue.admin_message)
            await _mark_order_failed(order, issue)
            return

        order.subscription_url = build_vhq_mirror_url(
            upstream_url,
            public_base_url=settings.vhq_subscription_proxy_base_url,
            path_prefix=settings.bot_webhook_path_prefix,
            secret=settings.vhq_subscription_proxy_secret,
            order_id=order.order_id,
        )
    except Exception as exc:
        issue = build_internal_access_error(
            provider="vhq",
            code="vhq_response_runtime",
            admin_message=f"Unexpected VHQ response handling error order_id={order.order_id}: {exc}",
            raw_message=str(exc),
        )
        logger.error(issue.admin_message)
        await _mark_order_failed(order, issue)
        return
    order.status = "delivered"
    order.failure_message = None
    order.failure_reason = None
    order.delivered_at = datetime.utcnow()
    order.access_expires_at = order.delivered_at + timedelta(days=order.days)
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
    raw_username = str(order.marzban_username or "").strip()
    if raw_username.startswith("adapt_"):
        return raw_username.removeprefix("adapt_").strip() or None

    raw_url = str(order.subscription_url or "").strip()
    marker = "/adapt-sub/"
    if marker in raw_url:
        return raw_url.rsplit(marker, 1)[-1].split("?", 1)[0].split("#", 1)[0].strip() or None
    return None


async def _fulfill_adapt_order(session, order: WebOrder, adapt_plan_uuid: str) -> None:
    """Renew an existing Adapt web subscription when possible, otherwise create one."""
    if order.profile_token:
        primary = await _get_latest_web_access_order(session, order.profile_token)
        if primary and primary.id != order.id and primary.tariff_key == order.tariff_key:
            adapt_uuid = _extract_adapt_uuid_from_web_order(primary)
            if adapt_uuid:
                try:
                    resp = await AdaptAPI().renew_subscription(adapt_uuid)
                except AdaptAPIError as exc:
                    issue = build_internal_access_error(
                        provider="adapt",
                        code=f"adapt_renew_{exc.status or 'error'}",
                        admin_message=(
                            f"Adapt renew error for order {order.order_id}: {exc} "
                            f"primary_order={primary.order_id} adapt_uuid={adapt_uuid}"
                        ),
                        raw_message=str(exc),
                    )
                    logger.error(issue.admin_message)
                    await _mark_order_failed(order, issue)
                    return
                except Exception as exc:
                    issue = build_internal_access_error(
                        provider="adapt",
                        code="adapt_renew_runtime",
                        admin_message=(
                            f"Unexpected Adapt renew error for order {order.order_id}: {exc} "
                            f"primary_order={primary.order_id} adapt_uuid={adapt_uuid}"
                        ),
                        raw_message=str(exc),
                    )
                    logger.error(issue.admin_message)
                    await _mark_order_failed(order, issue)
                    return

                order.marzban_username = primary.marzban_username
                order.subscription_url = primary.subscription_url or build_adapt_mirror_url(adapt_uuid)
                order.status = "delivered"
                order.failure_message = None
                order.failure_reason = None
                order.delivered_at = datetime.utcnow()
                order.access_expires_at = _adapt_expires_at_from_response(resp, order.days)
                _web_plog(
                    "WEB_ПРОДЛЕНИЕ",
                    order_id=order.order_id,
                    tariff=order.tariff_label,
                    provider="adapt",
                    context=f"reused={primary.order_id} adapt_uuid={adapt_uuid}",
                )
                logger.info(
                    "Order %s renewed via Adapt: primary_order=%s uuid=%s",
                    order.order_id,
                    primary.order_id,
                    adapt_uuid,
                )
                return

    # No matching previous Adapt order for this profile: issue a fresh subscription.
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


async def handle_yookassa_webhook(request: web.Request) -> web.Response:
    # Note: IP check disabled — behind double nginx proxy (stream 443 → http 8444)
    # the original sender IP is lost. Security ensured by checking
    # metadata.source == "webstore" and order_id match below.

    try:
        data = await request.json()
    except Exception:
        return web.Response(status=400, text="Bad JSON")

    event = data.get("event", "")
    if event != "payment.succeeded":
        return web.Response(text="OK")

    obj = data.get("object", {})
    metadata = obj.get("metadata", {})
    order_id = metadata.get("order_id")
    topup_id = metadata.get("topup_id")

    if metadata.get("source") == "webstore_balance_topup" and topup_id:
        async with async_session() as session:
            result = await session.execute(
                select(WebBalanceTopUp).where(WebBalanceTopUp.topup_id == topup_id)
            )
            topup = result.scalar_one_or_none()
            if not topup:
                return web.Response(text="OK")
            if topup.status == "completed":
                return web.Response(text="OK")

            topup.status = "paid"
            topup.yookassa_payment_id = obj.get("id")
            await session.commit()
        _web_plog(
            "WEB_ОПЛАТА",
            order_id=topup_id,
            tariff="balance_topup",
            amount=topup.amount_rub,
            method="yookassa",
        )

        credit_result = await _credit_local_balance_topup(topup_id)
        if credit_result.get("status") not in {"credited", "already_credited"} and topup.telegram_id:
            credit_result = await _credit_web_balance_topup(topup)
            async with async_session() as session:
                result = await session.execute(
                    select(WebBalanceTopUp).where(WebBalanceTopUp.topup_id == topup_id)
                )
                topup = result.scalar_one_or_none()
                if not topup:
                    return web.Response(text="OK")
                if credit_result.get("status") in {"credited", "already_credited"}:
                    topup.status = "completed"
                    topup.completed_at = datetime.utcnow()
                    _web_plog(
                        "WEB_ПОПОЛНЕНИЕ",
                        order_id=topup.topup_id,
                        tariff="balance_topup",
                        amount=topup.amount_rub,
                        status="completed",
                    )
                else:
                    topup.status = "failed"
                    _web_plog(
                        "WEB_ОШИБКА_ПОПОЛНЕНИЯ",
                        order_id=topup.topup_id,
                        tariff="balance_topup",
                        amount=topup.amount_rub,
                        status="failed",
                    )
                await session.commit()

        return web.Response(text="OK")

    if not order_id or metadata.get("source") != "webstore":
        return web.Response(text="OK")

    logger.info("YooKassa webhook: payment.succeeded for order %s", order_id)

    async with async_session() as session:
        result = await session.execute(
            select(WebOrder).where(WebOrder.order_id == order_id)
        )
        order = result.scalar_one_or_none()
        if not order:
            logger.error("Order not found for webhook: %s", order_id)
            return web.Response(text="OK")

        if order.status == "delivered":
            logger.info("Order %s already processed, skipping", order_id)
            return web.Response(text="OK")
        if order.status == "paid":
            logger.warning("Order %s is paid but not delivered; skipping automatic retry", order_id)
            return web.Response(text="OK")

        order.status = "paid"
        order.paid_at = datetime.utcnow()
        order.yookassa_payment_id = obj.get("id")
        await session.commit()
        _web_plog(
            "WEB_ОПЛАТА",
            order_id=order.order_id,
            tariff=order.tariff_label,
            amount=order.amount_rub,
            method="yookassa",
        )

        # Notify admins about payment
        await _notify_admins_webstore(order, "paid")

        # Fulfill the order (create Marzban/VHQ access).
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
            await _disable_web_balance_autodebit_after_tariff_purchase(session, order)
            await _apply_order_bonus_spend(session, order)
        await session.commit()

        # Notify admins about key delivery
        if order.status == "delivered":
            await _notify_admins_webstore(order, "delivered")
            await _sync_referral_credit_for_order(order.order_id)

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
                response = web.HTTPFound("/profile")
                _set_profile_cookie(response, auth_code.profile_token)
                return response

    token = request.query.get("token", "")
    if not token:
        # Show email lookup form
        html = _render("profile.html", PROFILE_MODE="lookup", SUPPORT_URL="/support", DEVICE_PRICE=settings.extra_device_price_rub, VK_FOOTER_HTML="")
        return web.Response(text=html, content_type="text/html")
    html = _render("profile.html", PROFILE_MODE="token", SUPPORT_URL="/support", DEVICE_PRICE=settings.extra_device_price_rub, VK_FOOTER_HTML="")
    return web.Response(text=html, content_type="text/html")


async def handle_auth_me(request: web.Request) -> web.Response:
    token = request.cookies.get(_AUTH_COOKIE, "").strip()
    if not token:
        return web.json_response({"authenticated": False})

    async with async_session() as session:
        account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == token).limit(1))
        if account:
            return web.json_response({
                "authenticated": True,
                "contact": account.contact,
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


async def handle_auth_register(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    contact = _normalize_contact(body.get("contact", ""))
    password = str(body.get("password", ""))
    password2 = str(body.get("password2", ""))
    if not contact:
        return web.json_response({"error": "Введите email или номер телефона"}, status=400)
    if len(password) < 8:
        return web.json_response({"error": "Пароль должен быть не короче 8 знаков"}, status=400)
    if password != password2:
        return web.json_response({"error": "Пароли не совпадают"}, status=400)

    profile_token = _make_profile_token(contact)
    async with async_session() as session:
        existing = await session.scalar(select(WebAccount).where(WebAccount.contact == contact).limit(1))
        if existing:
            return web.json_response({"error": "Профиль уже есть. Войдите с этим контактом и паролем."}, status=409)
        account = WebAccount(
            contact=contact,
            profile_token=profile_token,
            password_hash=_hash_password(password),
            last_login_at=datetime.utcnow(),
        )
        session.add(account)
        balance = await _get_or_create_web_balance_account(session, profile_token, contact)
        balance.contact = contact
        await session.commit()

    return _auth_response({"ok": True, "token": profile_token, "contact": contact}, profile_token)


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
        account = await session.scalar(select(WebAccount).where(WebAccount.contact == contact).limit(1))
        if not account or not _verify_password(password, account.password_hash):
            return web.json_response({"error": "Неверный контакт или пароль"}, status=401)
        account.last_login_at = datetime.utcnow()
        await _get_or_create_web_balance_account(session, account.profile_token, account.contact)
        await session.commit()
        token = account.profile_token

    return _auth_response({"ok": True, "token": token, "contact": contact}, token)


async def handle_auth_logout(request: web.Request) -> web.Response:
    response = web.json_response({"ok": True})
    _clear_profile_cookie(response)
    return response


async def handle_create_profile(request: web.Request) -> web.Response:
    """Create or return a web profile demo key for an authenticated user."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    profile_token = (body.get("token") or request.cookies.get("webstore_profile_token") or "").strip()
    if not profile_token:
        return web.json_response(
            {"error": "Сначала зарегистрируйтесь или войдите, чтобы демо сохранилось в личном кабинете.", "auth_required": True},
            status=401,
        )

    async with async_session() as session:
        web_account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == profile_token).limit(1))
        linked_profile = await session.get(WebProfileLink, profile_token)
        if not web_account and not linked_profile:
            return web.json_response(
                {"error": "Сначала зарегистрируйтесь или войдите, чтобы демо сохранилось в личном кабинете.", "auth_required": True},
                status=401,
            )
        contact = _normalize_contact(web_account.contact) if web_account and web_account.contact else None
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
                demo_url = existing_demo_order.subscription_url
        ref_code = account.ref_code
        await session.commit()

    site_url = f"{settings.subscription_base_url.rstrip('/')}/buy?ref={ref_code}"
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
    """Look up profile token by email and order ID."""
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    contact = _normalize_contact(body.get("contact", ""))
    order_id = body.get("order_id", "").strip().lower()
    if not contact or not order_id:
        return web.json_response({"error": "Введите контакт и номер заказа"}, status=400)

    token = _make_profile_token(contact)

    async with async_session() as session:
        result = await session.execute(
            select(WebOrder).where(
                WebOrder.profile_token == token,
                WebOrder.order_id == order_id,
            ).limit(1)
        )
        order = result.scalar_one_or_none()
        if not order:
            return web.json_response(
                {"error": "Заказ с такими данными не найден"},
                status=404,
            )

    return web.json_response({"token": token})


async def handle_profile_link_init(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    token = body.get("token", "").strip()
    if not token:
        return web.json_response({"error": "Missing token"}, status=400)

    async with async_session() as session:
        orders, _, _ = await _get_profile_bundle(session, token)
        if not orders:
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

    return web.json_response({
        "status": "completed",
        "token": auth_code.profile_token,
    })


async def handle_profile_orders(request: web.Request) -> web.Response:
    """Get all orders for a profile token."""
    token = request.query.get("token", "")
    if not token:
        return web.json_response({"error": "Missing token"}, status=400)

    balance_profile = None
    balance_history = None
    web_balance = None
    web_balance_history = None
    async with async_session() as session:
        orders, link, telegram_items = await _get_profile_bundle(session, token)
        if not orders and not link and not telegram_items:
            return web.json_response({"error": "Заказы не найдены"}, status=404)
        web_balance_account = await _get_or_create_web_balance_account(
            session,
            token,
            (orders[0].contact or orders[0].email) if orders else (link.contact if link else None),
        )
        web_balance = {
            "balance_rub": web_balance_account.balance_rub,
            "ref_code": web_balance_account.ref_code,
            "site_url": f"{settings.subscription_base_url.rstrip('/')}/buy?ref={web_balance_account.ref_code}",
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
        await session.commit()

        return web.json_response({
            "contact": (
                (orders[0].contact or orders[0].email)
                if orders
                else (link.contact or link.telegram_username or "Telegram")
            ),
            "orders": _serialize_orders(orders),
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
            "balance": balance_profile,
            "balance_history": balance_history or [],
        })


async def handle_balance_toggle(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    token = (body.get("token") or request.cookies.get("webstore_profile_token") or "").strip()
    enabled = bool(body.get("enabled"))
    if not token:
        return web.json_response({"error": "Сначала откройте профиль"}, status=400)

    async with async_session() as session:
        link = await session.get(WebProfileLink, token)
        if link and link.telegram_id:
            result, status_code = await _toggle_balance_mode(int(link.telegram_id), enabled)
            if not result:
                return web.json_response({"error": "Не удалось изменить настройку"}, status=500)
            if status_code != 200:
                return web.json_response({"error": result.get("error") or "Не удалось изменить настройку"}, status=status_code)
            history = await _fetch_balance_history(int(link.telegram_id), 20)
            return web.json_response({
                "balance": result,
                "balance_history": history or [],
            })

        account = await _get_or_create_web_balance_account(session, token)
        if enabled:
            ok, error = await _enable_local_balance_mode(session, account)
            if not ok:
                await session.commit()
                return web.json_response({"error": error or "Не удалось включить режим"}, status=400)
        else:
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

    token = (body.get("token") or request.cookies.get("webstore_profile_token") or "").strip()
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
        if order.status != "pending":
            return web.json_response({"error": "Отменить можно только заказ в статусе «Ожидает оплаты»"}, status=400)
        order.status = "canceled"
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

    async with async_session() as session:
        result = await session.execute(
            select(WebProfileLinkCode).where(WebProfileLinkCode.code == code)
        )
        link_code = result.scalar_one_or_none()
        if (
            not link_code
            or link_code.consumed_at is not None
            or link_code.expires_at < datetime.utcnow()
        ):
            return web.json_response({"error": "Link code expired"}, status=404)

        orders, _, _ = await _get_profile_bundle(session, link_code.profile_token)
        if not orders:
            return web.json_response({"error": "Profile not found"}, status=404)

        link_code.consumed_at = datetime.utcnow()
        await _upsert_profile_link(
            session,
            link_code.profile_token,
            orders[0].contact or orders[0].email,
            int(telegram_id),
            body.get("telegram_username"),
            body.get("telegram_full_name"),
        )
        await _replace_telegram_items(
            session,
            link_code.profile_token,
            int(telegram_id),
            body.get("items") or [],
        )
        await _migrate_web_balance_to_telegram(session, link_code.profile_token, int(telegram_id))
        await session.commit()

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
        result = await session.execute(
            select(WebTelegramAuthCode).where(WebTelegramAuthCode.code == code)
        )
        auth_code = result.scalar_one_or_none()
        if (
            not auth_code
            or auth_code.consumed_at is not None
            or auth_code.expires_at < datetime.utcnow()
        ):
            return web.json_response({"error": "Auth code expired"}, status=404)

        result = await session.execute(
            select(WebProfileLink).where(WebProfileLink.telegram_id == int(telegram_id))
        )
        link = result.scalar_one_or_none()
        profile_token = link.profile_token if link else _make_telegram_profile_token(int(telegram_id))
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
        await _migrate_web_balance_to_telegram(session, profile_token, int(telegram_id))
        auth_code.profile_token = profile_token
        auth_code.telegram_id = int(telegram_id)
        auth_code.telegram_username = body.get("telegram_username")
        auth_code.telegram_full_name = body.get("telegram_full_name")
        auth_code.consumed_at = datetime.utcnow()
        await session.commit()

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

        orders, _, _ = await _get_profile_bundle(session, link.profile_token)
        return web.json_response({
            "contact": link.contact,
            "orders": _serialize_orders(orders),
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
                WebBalanceTransaction.kind == "referral"
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
        "conversion": {"total_30d": total_30d, "paid_30d": paid_30d},
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


async def handle_internal_admin_client_lookup(request: web.Request) -> web.Response:
    if not _verify_internal_secret(request):
        return web.json_response({"error": "Forbidden"}, status=403)

    query = _normalize_contact(request.query.get("q") or "")
    if not query:
        return web.json_response({"error": "Missing q"}, status=400)

    async with async_session() as session:
        tokens: set[str] = set()
        orders_result = await session.execute(
            select(WebOrder).where(
                or_(
                    WebOrder.order_id == query,
                    WebOrder.contact == query,
                    WebOrder.email == query,
                    WebOrder.profile_token == query,
                    WebOrder.profile_token.like(f"{query}%"),
                )
            )
        )
        matched_orders = orders_result.scalars().all()
        tokens.update(o.profile_token for o in matched_orders if o.profile_token)

        accounts_result = await session.execute(
            select(WebAccount).where(
                or_(
                    WebAccount.contact == query,
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
                    WebBalanceAccount.contact == query,
                    WebBalanceAccount.profile_token == query,
                    WebBalanceAccount.profile_token.like(f"{query}%"),
                )
            )
        )
        balances = balances_result.scalars().all()
        tokens.update(a.profile_token for a in balances)

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
    return computed == check_hash


async def _get_all_target_agent_ids() -> set[int]:
    agent_ids = set(settings.support_agent_ids)
    agent_ids.update(settings.admin_ids)
    try:
        from bot.models import User
        from bot.database import async_session as bot_async_session
        async with bot_async_session() as bot_sess:
            db_adm = await bot_sess.execute(select(User.telegram_id).where(User.is_admin == True))
            for uid in db_adm.scalars().all():
                if uid:
                    agent_ids.add(int(uid))
    except Exception as e:
        logger.warning("Failed to fetch bot DB admins for support notify: %s", e)
    return agent_ids


async def _notify_support_agents(ticket: SupportTicket, first_message: str) -> None:
    """Send Telegram notification to support agents about a new ticket."""
    if not settings.admin_bot_token:
        return
    agent_ids = await _get_all_target_agent_ids()
    if not agent_ids:
        return
    contact = ticket.client_contact or "—"
    messenger = ticket.client_messenger or "—"
    text = (
        f"💬 <b>Новый тикет поддержки</b>\n\n"
        f"🎫 Тикет: <code>{ticket.token[:12]}…</code>\n"
        f"📞 Контакт: {html.escape(contact)}\n"
        f"📱 Мессенджер: {html.escape(messenger)}\n\n"
        f"💬 <i>{html.escape(first_message[:200])}</i>\n\n"
        f"👉 {settings.subscription_base_url}/support/admin"
    )
    api_url = f"https://api.telegram.org/bot{settings.admin_bot_token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as http:
            for agent_id in agent_ids:
                try:
                    await http.post(api_url, json={"chat_id": agent_id, "text": text, "parse_mode": "HTML"})
                except Exception:
                    pass
    except Exception as e:
        logger.warning("Failed to notify support agents: %s", e)


async def _notify_support_agents_message(ticket: SupportTicket, message_text: str) -> None:
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
    api_url = f"https://api.telegram.org/bot{settings.admin_bot_token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as http:
            for agent_id in agent_ids:
                try:
                    await http.post(api_url, json={"chat_id": agent_id, "text": text, "parse_mode": "HTML"})
                except Exception:
                    pass
    except Exception as e:
        logger.warning("Failed to notify support agents: %s", e)


# ---------------------------------------------------------------------------
# Support chat — REST handlers
# ---------------------------------------------------------------------------

async def handle_support_page(request: web.Request) -> web.Response:
    tpl = Path(__file__).parent / "templates" / "support.html"
    content = tpl.read_text(encoding="utf-8")
    content = content.replace("{{SITE_NAME}}", settings.site_name)
    content = content.replace("{{SUPPORT_URL}}", "/support")
    vk_btn = ""
    if settings.vk_url:
        vk_btn = (
            f'<a href="{settings.vk_url}" target="_blank"'
            f' class="support-social-btn support-social-vk">'
            '<svg viewBox="0 0 24 24" aria-hidden="true">'
            '<path d="M13.16 17.5c-5.63 0-8.84-3.87-8.97-10.32H7c.1 5.08 2.33'
            " 7.22 4.11 7.67V7.18h2.84v4.1c1.76-.19 3.61-2.17 4.23-4.1h2.82c-.47"
            " 2.41-2.43 4.39-3.82 5.2 1.39.65 3.6 2.4 4.43 5.12h-3.1c-.65-2.02"
            ' -2.27-3.59-4.35-3.79v3.79h-.01z"/>'
            "</svg>ВКонтакте</a>"
        )
    content = content.replace("{{SUPPORT_VK_BTN}}", vk_btn)
    return web.Response(text=content, content_type="text/html")


async def handle_support_admin_page(request: web.Request) -> web.Response:
    tpl = Path(__file__).parent / "templates" / "support_admin.html"
    content = tpl.read_text(encoding="utf-8")
    content = content.replace("{{SITE_NAME}}", settings.site_name)
    content = content.replace("{{TELEGRAM_BOT_NAME}}", settings.telegram_bot_name or "")
    return web.Response(text=content, content_type="text/html")


async def handle_support_new_ticket(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest(text="Invalid JSON")
    message_text = (body.get("message") or "").strip()
    if not message_text:
        return web.json_response({"error": "message_required"}, status=400)
    client_contact = (body.get("contact") or "").strip() or None
    client_messenger = (body.get("messenger") or "").strip() or None

    token = secrets.token_hex(24)
    now = datetime.utcnow()
    ticket = SupportTicket(
        token=token,
        client_contact=client_contact,
        client_messenger=client_messenger,
        status="open",
        created_at=now,
        last_message_at=now,
    )
    async with async_session() as session:
        session.add(ticket)
        await session.flush()
        msg = SupportMessage(
            ticket_id=ticket.id,
            sender="client",
            text=message_text,
            created_at=now,
        )
        session.add(msg)
        await session.commit()
        ticket_id = ticket.id

    asyncio.create_task(_notify_support_agents(ticket, message_text))
    return web.json_response({"token": token, "ticket_id": ticket_id})


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
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs.scalars()
        ]
    return web.json_response({
        "token": ticket.token,
        "status": ticket.status,
        "messages": messages,
    })


async def handle_support_send_message(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest()
    text_val = (body.get("text") or "").strip()
    if not text_val:
        return web.json_response({"error": "empty"}, status=400)

    async with async_session() as session:
        result = await session.execute(select(SupportTicket).where(SupportTicket.token == token))
        ticket = result.scalar_one_or_none()
        if not ticket:
            raise web.HTTPNotFound()
        if ticket.status == "resolved":
            return web.json_response({"error": "ticket_closed"}, status=400)
        now = datetime.utcnow()
        msg = SupportMessage(ticket_id=ticket.id, sender="client", text=text_val, created_at=now)
        ticket.last_message_at = now
        session.add(msg)
        await session.commit()
        msg_id = msg.id

    payload = {"type": "message", "sender": "client", "text": text_val,
               "id": msg_id, "created_at": now.isoformat()}
    asyncio.create_task(_support_broadcast(token, payload))
    asyncio.create_task(_notify_support_agents_message(ticket, text_val))
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
        return result.scalar_one_or_none()


async def handle_support_admin_login(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except Exception:
        raise web.HTTPBadRequest()

    # Password-based login (simple, no BotFather domain setup needed)
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
        auth_date = int(body.get("auth_date", 0))
        if abs(datetime.utcnow().timestamp() - auth_date) > 86400:
            return web.json_response({"error": "expired"}, status=403)
        telegram_id = int(body.get("id", 0))
        if telegram_id not in settings.support_agent_ids:
            return web.json_response({"error": "not_authorized"}, status=403)
        username = body.get("username")
        full_name = " ".join(filter(None, [body.get("first_name"), body.get("last_name")]))

    session_token = secrets.token_hex(32)
    expires_at = datetime.utcnow() + timedelta(days=7)
    agent_session = SupportAgentSession(
        token=session_token,
        telegram_id=telegram_id,
        telegram_username=username,
        telegram_full_name=full_name,
        expires_at=expires_at,
        created_at=datetime.utcnow(),
    )
    async with async_session() as session:
        session.add(agent_session)
        await session.commit()

    resp = web.json_response({"ok": True, "token": session_token})
    resp.set_cookie("support_agent_token", session_token, max_age=7 * 86400, httponly=True, samesite="Lax")
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
            data.append({
                "id": t.id,
                "token": t.token,
                "client_contact": t.client_contact,
                "client_messenger": t.client_messenger,
                "status": t.status,
                "created_at": t.created_at.isoformat(),
                "last_message_at": t.last_message_at.isoformat() if t.last_message_at else None,
                "client_online": bool(client_online),
                "last_message_text": last_message.text if last_message else None,
                "last_client_message": bool(last_message and last_message.sender == "client"),
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
    return web.json_response({
        "ticket": {
            "id": ticket.id,
            "token": ticket.token,
            "client_contact": ticket.client_contact,
            "client_messenger": ticket.client_messenger,
            "status": ticket.status,
            "created_at": ticket.created_at.isoformat(),
        },
        "messages": [
            {
                "id": m.id,
                "sender": m.sender,
                "agent_name": m.agent_name,
                "text": m.text,
                "created_at": m.created_at.isoformat(),
            }
            for m in msgs
        ],
    })


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
    if not text_val:
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
            text=text_val,
            created_at=now,
        )
        ticket.last_message_at = now
        session.add(msg)
        await session.commit()
        msg_id = msg.id
        token = ticket.token

    payload = {"type": "message", "sender": "agent", "agent_name": agent_name,
               "text": text_val, "id": msg_id, "created_at": now.isoformat()}
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


def setup_routes(app: web.Application) -> None:
    app.router.add_get("/", handle_store_page)
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
    app.router.add_post("/api/store/create-device-order", handle_create_device_order)
    app.router.add_post("/api/store/create-balance-topup", handle_create_balance_topup)
    app.router.add_post("/api/store/balance-toggle", handle_balance_toggle)
    app.router.add_get("/api/store/order-status", handle_order_status)
    app.router.add_post("/api/store/profile-lookup", handle_profile_lookup)
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
    app.router.add_get("/api/store/internal/admin-balance-lookup", handle_internal_admin_balance_lookup)
    app.router.add_post("/api/store/internal/admin-balance-adjust", handle_internal_admin_balance_adjust)

    # Profile
    app.router.add_get("/profile", handle_profile_page)

    # Support chat
    app.router.add_get("/support", handle_support_page)
    app.router.add_get("/support/admin", handle_support_admin_page)
    app.router.add_post("/api/support/new-ticket", handle_support_new_ticket)
    app.router.add_get("/api/support/ticket/{token}", handle_support_ticket_info)
    app.router.add_post("/api/support/ticket/{token}/message", handle_support_send_message)
    app.router.add_post("/api/support/admin/login", handle_support_admin_login)
    app.router.add_get("/api/support/admin/me", handle_support_admin_me)
    app.router.add_get("/api/support/admin/tickets", handle_support_admin_tickets)
    app.router.add_get("/api/support/admin/ticket/{ticket_id}/messages", handle_support_admin_ticket_messages)
    app.router.add_post("/api/support/admin/ticket/{ticket_id}/message", handle_support_admin_send_message)
    app.router.add_post("/api/support/admin/ticket/{ticket_id}/resolve", handle_support_admin_resolve)
    app.router.add_get("/ws/support/{token}", handle_support_ws_client)
    app.router.add_get("/ws/support/admin/{ticket_id}", handle_support_ws_agent)

    # Webhook
    app.router.add_post("/pay/webhook", handle_yookassa_webhook)
