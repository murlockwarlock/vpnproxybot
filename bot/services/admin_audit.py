"""Small fail-safe writer for the Telegram admin audit journal."""

from __future__ import annotations

import logging

from bot.database import async_session
from bot.models import AdminActionLog

logger = logging.getLogger(__name__)


async def write_admin_audit(
    *,
    actor_telegram_id: int,
    action: str,
    summary: str,
    entity_type: str = "system",
    entity_id: str | None = None,
    target_telegram_id: int | None = None,
    status: str = "success",
    details: str | None = None,
) -> None:
    """Write an audit record without ever breaking the admin action itself."""
    try:
        async with async_session() as session:
            session.add(AdminActionLog(
                actor_telegram_id=actor_telegram_id,
                action=action[:64],
                entity_type=entity_type[:32],
                entity_id=(entity_id or "")[:128] or None,
                target_telegram_id=target_telegram_id,
                status=status[:16],
                summary=summary[:255],
                details=(details or "")[:2000] or None,
            ))
            await session.commit()
    except Exception:
        logger.exception("Failed to write admin audit action=%s actor=%s", action, actor_telegram_id)
