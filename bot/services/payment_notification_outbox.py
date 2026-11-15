"""Durable customer-facing Telegram notifications for paid bot payments."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, or_, select, update
from sqlalchemy.exc import IntegrityError

from bot import database as database_module
from bot.database import async_session
from bot.models import Payment, PaymentNotificationOutbox

logger = logging.getLogger(__name__)

_LEASE_DURATION = timedelta(minutes=2)
_RETRY_DELAYS = (60, 300, 1200, 3600, 21600)
_UNKNOWN_DELIVERY_ERROR = "Telegram delivery outcome unknown after worker restart"
_DEFAULT_ASYNC_SESSION = async_session


def _session_factory():
    if async_session is not _DEFAULT_ASYNC_SESSION:
        return async_session
    return database_module.async_session


def _payload_messages(payload) -> list[dict]:
    if isinstance(payload, dict):
        messages = [payload]
    elif isinstance(payload, list):
        messages = payload
    else:
        messages = [{"text": str(payload)}]
    normalized = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("payment notification payload must contain message objects")
        if not str(message.get("text") or "") and not message.get("action"):
            raise ValueError("payment notification payload must contain text or action")
        normalized.append(dict(message))
    return normalized


async def enqueue_payment_notification(
    session,
    *,
    payment_id: int,
    kind: str,
    recipient_telegram_id: int,
    payload,
) -> PaymentNotificationOutbox:
    existing = await session.scalar(
        select(PaymentNotificationOutbox)
        .where(PaymentNotificationOutbox.payment_id == payment_id)
        .where(PaymentNotificationOutbox.kind == kind)
        .limit(1)
    )
    if existing:
        return existing

    row = PaymentNotificationOutbox(
        payment_id=payment_id,
        kind=kind,
        recipient_telegram_id=recipient_telegram_id,
        payload_json=json.dumps(_payload_messages(payload), ensure_ascii=False),
    )
    try:
        async with session.begin_nested():
            session.add(row)
            await session.flush()
    except IntegrityError:
        existing = await session.scalar(
            select(PaymentNotificationOutbox)
            .where(PaymentNotificationOutbox.payment_id == payment_id)
            .where(PaymentNotificationOutbox.kind == kind)
            .limit(1)
        )
        if existing is None:
            raise
        return existing
    return row


async def enqueue_subscription_delivery(
    session,
    *,
    payment_id: int,
    recipient_telegram_id: int,
    telegram_user_id: int,
    subscription_id: int,
    delivery_platform: str,
    needs_platform_choice: bool,
    kind: str = "subscription_delivery",
) -> PaymentNotificationOutbox:
    return await enqueue_payment_notification(
        session,
        payment_id=payment_id,
        kind=kind,
        recipient_telegram_id=recipient_telegram_id,
        payload={
            "action": "subscription_delivery",
            "telegram_user_id": int(telegram_user_id),
            "subscription_id": int(subscription_id),
            "delivery_platform": str(delivery_platform),
            "needs_platform_choice": bool(needs_platform_choice),
        },
    )


async def _recover_expired_claims(
    session,
    *,
    now: datetime,
    payment_id: int | None,
    kind: str | None = None,
) -> int:
    conditions = [
        PaymentNotificationOutbox.status == "sending",
        PaymentNotificationOutbox.lease_expires_at.is_not(None),
        PaymentNotificationOutbox.lease_expires_at <= now,
    ]
    if payment_id is not None:
        conditions.append(PaymentNotificationOutbox.payment_id == payment_id)
    if kind is not None:
        conditions.append(PaymentNotificationOutbox.kind == kind)
    result = await session.execute(
        update(PaymentNotificationOutbox)
        .where(and_(*conditions))
        .values(
            status="unknown",
            lease_token=None,
            lease_expires_at=None,
            next_attempt_at=now,
            last_error=_UNKNOWN_DELIVERY_ERROR,
        )
    )
    await session.commit()
    return int(result.rowcount or 0)


async def _claim_notification(
    *,
    now: datetime,
    payment_id: int | None,
    kind: str | None = None,
    session_factory=None,
) -> tuple[int, str] | None:
    factory = session_factory or _session_factory()
    async with factory() as session:
        conditions = [
            or_(
                and_(
                    PaymentNotificationOutbox.status.in_(
                        ("pending", "unknown")
                    ),
                    or_(
                        PaymentNotificationOutbox.next_attempt_at.is_(None),
                        PaymentNotificationOutbox.next_attempt_at <= now,
                    ),
                ),
                and_(
                    PaymentNotificationOutbox.status == "sending",
                    PaymentNotificationOutbox.lease_expires_at <= now,
                ),
            )
        ]
        if payment_id is not None:
            conditions.append(PaymentNotificationOutbox.payment_id == payment_id)
        if kind is not None:
            conditions.append(PaymentNotificationOutbox.kind == kind)
        row_id = await session.scalar(
            select(PaymentNotificationOutbox.id)
            .where(and_(*conditions))
            .order_by(
                PaymentNotificationOutbox.next_attempt_at.asc(),
                PaymentNotificationOutbox.created_at.asc(),
                PaymentNotificationOutbox.id.asc(),
            )
            .limit(1)
        )
        if row_id is None:
            return None

        token = uuid4().hex
        result = await session.execute(
            update(PaymentNotificationOutbox)
            .where(PaymentNotificationOutbox.id == row_id)
            .where(
                or_(
                    and_(
                        PaymentNotificationOutbox.status.in_(("pending", "unknown")),
                        or_(
                            PaymentNotificationOutbox.next_attempt_at.is_(None),
                            PaymentNotificationOutbox.next_attempt_at <= now,
                        ),
                    ),
                    and_(
                        PaymentNotificationOutbox.status == "sending",
                        PaymentNotificationOutbox.lease_expires_at <= now,
                    ),
                )
            )
            .values(
                status="sending",
                lease_token=token,
                lease_expires_at=now + _LEASE_DURATION,
                last_attempt_at=now,
            )
        )
        if result.rowcount != 1:
            await session.rollback()
            return None
        await session.commit()
        return int(row_id), token


def _send_kwargs(message: dict) -> dict:
    kwargs = {}
    for key in ("parse_mode", "disable_web_page_preview"):
        if key in message:
            kwargs[key] = message[key]
    return kwargs


async def _send_action(
    bot,
    *,
    recipient: int,
    payment_id: int,
    message: dict,
    session_factory=None,
) -> None:
    action = message.get("action")
    if action != "subscription_delivery":
        raise ValueError(f"unsupported payment notification action: {action}")

    from bot.models import ProxyAccount, Subscription, User
    from bot.services.vhq_subscription_proxy import get_subscription_display_key
    from bot.webhooks import send_subscription_delivery_messages, _platform_from_str

    factory = session_factory or _session_factory()
    async with factory() as session:
        subscription = await session.get(Subscription, int(message["subscription_id"]))
        if not subscription:
            raise RuntimeError("subscription for payment delivery was not found")
        user = await session.get(User, subscription.user_id)
        if not user or int(user.telegram_id) != int(message["telegram_user_id"]):
            raise RuntimeError("payment delivery recipient no longer owns subscription")
        proxy_account = await session.scalar(
            select(ProxyAccount).where(ProxyAccount.subscription_id == subscription.id).limit(1)
        )
        proxy_link = proxy_account.get_proxy_link() if proxy_account else None
        vpn_key = get_subscription_display_key(subscription) or subscription.vpn_key
        expires_str = subscription.expires_at.strftime("%d.%m.%Y") if subscription.expires_at else "N/A"

    delivered, error = await send_subscription_delivery_messages(
        bot=bot,
        chat_id=recipient,
        telegram_user_id=int(message["telegram_user_id"]),
        subscription_id=subscription.id,
        delivery_platform=_platform_from_str(str(message["delivery_platform"])),
        vpn_key_str=vpn_key,
        proxy_link=proxy_link,
        expires_str=expires_str,
        needs_platform_choice=bool(message.get("needs_platform_choice")),
        payment_id=payment_id,
        record_db_error=False,
    )
    if not delivered:
        raise RuntimeError(error or "subscription Telegram delivery failed")


async def _mark_sent(
    *,
    row_id: int,
    lease_token: str,
    message_index: int,
    message_count: int,
    session_factory=None,
) -> bool:
    factory = session_factory or _session_factory()
    async with factory() as session:
        row = await session.get(PaymentNotificationOutbox, row_id)
        if not row or row.status != "sending" or row.lease_token != lease_token:
            return False
        row.message_index = message_index + 1
        row.last_error = None
        if row.message_index >= message_count:
            row.status = "sent"
            row.sent_at = datetime.utcnow()
            row.lease_token = None
            row.lease_expires_at = None
            payment = await session.get(Payment, row.payment_id)
            active = await session.scalar(
                select(PaymentNotificationOutbox.id)
                .where(PaymentNotificationOutbox.payment_id == row.payment_id)
                .where(PaymentNotificationOutbox.status != "sent")
                .limit(1)
            )
            if payment and active is None:
                payment.telegram_delivery_error = None
        await session.commit()
        return True


async def _mark_retry(
    *,
    row_id: int,
    lease_token: str,
    error: Exception,
    session_factory=None,
) -> bool:
    factory = session_factory or _session_factory()
    async with factory() as session:
        row = await session.get(PaymentNotificationOutbox, row_id)
        if not row or row.status != "sending" or row.lease_token != lease_token:
            return False
        row.attempts = int(row.attempts or 0) + 1
        delay_index = min(row.attempts - 1, len(_RETRY_DELAYS) - 1)
        row.status = "pending"
        row.next_attempt_at = datetime.utcnow() + timedelta(seconds=_RETRY_DELAYS[delay_index])
        row.lease_token = None
        row.lease_expires_at = None
        row.last_error = f"{type(error).__name__}: {str(error)[:500]}"
        payment = await session.get(Payment, row.payment_id)
        if payment:
            payment.telegram_delivery_error = (
                f"notification:{row.kind}:{row.last_error}"
            )[:255]
        await session.commit()
        return True


async def process_payment_notification_outbox(
    bot,
    *,
    limit: int = 50,
    payment_id: int | None = None,
    kind: str | None = None,
    session_factory=None,
) -> dict[str, int]:
    now = datetime.utcnow()
    stats = {"scanned": 0, "sent": 0, "retrying": 0, "unknown": 0, "locked": 0}
    factory = session_factory or _session_factory()
    async with factory() as session:
        stats["unknown"] = await _recover_expired_claims(
            session,
            now=now,
            payment_id=payment_id,
            kind=kind,
        )

    if limit <= 0:
        return stats

    for _ in range(limit):
        claimed = await _claim_notification(
            now=datetime.utcnow(),
            payment_id=payment_id,
            kind=kind,
            session_factory=factory,
        )
        if claimed is None:
            break
        row_id, lease_token = claimed
        stats["scanned"] += 1

        async with factory() as session:
            row = await session.get(PaymentNotificationOutbox, row_id)
            if not row or row.lease_token != lease_token:
                stats["locked"] += 1
                continue
            payment_id_for_delivery = row.payment_id
            try:
                messages = _payload_messages(json.loads(row.payload_json))
            except Exception as exc:
                if await _mark_retry(
                    row_id=row_id,
                    lease_token=lease_token,
                    error=exc,
                    session_factory=factory,
                ):
                    stats["retrying"] += 1
                else:
                    stats["locked"] += 1
                continue

        while True:
            async with factory() as session:
                row = await session.get(PaymentNotificationOutbox, row_id)
                if not row or row.lease_token != lease_token or row.status != "sending":
                    stats["locked"] += 1
                    break
                if row.message_index >= len(messages):
                    await _mark_sent(
                        row_id=row_id,
                        lease_token=lease_token,
                        message_index=len(messages),
                        message_count=len(messages),
                        session_factory=factory,
                    )
                    stats["sent"] += 1
                    break
                message_index = row.message_index
                message = messages[message_index]
                recipient = row.recipient_telegram_id

            try:
                if message.get("action"):
                    await _send_action(
                        bot,
                        recipient=recipient,
                        payment_id=payment_id_for_delivery,
                        message=message,
                        session_factory=factory,
                    )
                else:
                    await bot.send_message(
                        recipient,
                        message["text"],
                        **_send_kwargs(message),
                    )
            except Exception as exc:
                if await _mark_retry(
                    row_id=row_id,
                    lease_token=lease_token,
                    error=exc,
                    session_factory=factory,
                ):
                    stats["retrying"] += 1
                else:
                    stats["locked"] += 1
                break

            if not await _mark_sent(
                row_id=row_id,
                lease_token=lease_token,
                message_index=message_index,
                message_count=len(messages),
                session_factory=factory,
            ):
                stats["locked"] += 1
                break
            if message_index + 1 >= len(messages):
                stats["sent"] += 1
                break

    return stats
