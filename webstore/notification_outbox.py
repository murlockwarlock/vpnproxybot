"""Durable delivery of payment-critical Telegram notifications."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import select

from webstore.config import settings
from webstore.database import async_session
from webstore.models import WebNotificationOutbox
from webstore.telegram_notify import send_telegram_notifications

logger = logging.getLogger(__name__)

_RETRY_DELAYS_MINUTES = (1, 5, 20, 60, 240, 720)


def all_staff_recipient_ids() -> set[int]:
    """Owners, administrators and support receive operational payment events."""
    return {
        int(item)
        for item in (*settings.admin_ids, *settings.owner_ids, *settings.support_agent_ids)
        if item
    }


async def enqueue_notifications(
    *,
    event: str,
    dedupe_prefix: str,
    recipient_ids: set[int] | list[int],
    text: str,
) -> int:
    """Persist messages before attempting Telegram delivery."""
    recipients = sorted({int(item) for item in recipient_ids if item})
    if not recipients:
        logger.warning("Notification outbox has no recipients event=%s key=%s", event, dedupe_prefix)
        return 0

    created = 0
    async with async_session() as session:
        keys = [f"{dedupe_prefix}:{recipient_id}" for recipient_id in recipients]
        existing = set(
            (
                await session.execute(
                    select(WebNotificationOutbox.dedupe_key).where(
                        WebNotificationOutbox.dedupe_key.in_(keys)
                    )
                )
            ).scalars().all()
        )
        for recipient_id in recipients:
            dedupe_key = f"{dedupe_prefix}:{recipient_id}"
            if dedupe_key in existing:
                continue
            row = WebNotificationOutbox(
                dedupe_key=dedupe_key,
                recipient_id=recipient_id,
                event=event,
                text=text,
            )
            session.add(row)
            created += 1
        await session.commit()
    return created


async def process_notification_outbox(*, limit: int = 50) -> int:
    """Deliver due rows; unsuccessful messages remain for later retries."""
    if not settings.admin_bot_token:
        logger.warning("Notification outbox paused: ADMIN_BOT_TOKEN is not configured")
        return 0

    now = datetime.utcnow()
    async with async_session() as session:
        result = await session.execute(
            select(WebNotificationOutbox)
            .where(WebNotificationOutbox.status == "pending")
            .where(WebNotificationOutbox.next_attempt_at <= now)
            .order_by(WebNotificationOutbox.created_at.asc())
            .limit(limit)
        )
        rows = result.scalars().all()
        sent = 0
        for row in rows:
            row.attempts = int(row.attempts or 0) + 1
            row.last_attempt_at = now
            delivered = await send_telegram_notifications(
                settings.admin_bot_token,
                [row.recipient_id],
                row.text,
            )
            if delivered:
                row.status = "sent"
                row.sent_at = datetime.utcnow()
                row.last_error = None
                sent += 1
                continue

            delay_index = min(row.attempts - 1, len(_RETRY_DELAYS_MINUTES) - 1)
            row.next_attempt_at = datetime.utcnow() + timedelta(minutes=_RETRY_DELAYS_MINUTES[delay_index])
            row.last_error = "Telegram delivery returned no successful recipients"
            if row.attempts % len(_RETRY_DELAYS_MINUTES) == 0:
                logger.error(
                    "Notification is still undelivered event=%s recipient_id=%s key=%s attempts=%s",
                    row.event,
                    row.recipient_id,
                    row.dedupe_key,
                    row.attempts,
                )
        await session.commit()
        return sent
