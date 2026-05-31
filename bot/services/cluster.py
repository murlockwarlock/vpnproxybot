"""Cluster coordination helpers for multi-instance bot deployment."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, update
from sqlalchemy.exc import IntegrityError

from bot.database import async_session
from bot.models import AppLease

logger = logging.getLogger(__name__)


class LeaseManager:
    """A small DB-backed lease manager compatible with SQLite and PostgreSQL."""

    def __init__(self, owner_id: str, session_factory=async_session) -> None:
        self.owner_id = owner_id
        self.session_factory = session_factory

    async def acquire_or_renew(self, lease_name: str, ttl_seconds: int) -> bool:
        now = datetime.utcnow()
        lease_until = now + timedelta(seconds=ttl_seconds)

        async with self.session_factory() as session:
            result = await session.execute(
                update(AppLease)
                .where(AppLease.name == lease_name)
                .where(
                    or_(
                        AppLease.owner_id == self.owner_id,
                        AppLease.lease_until < now,
                    )
                )
                .values(
                    owner_id=self.owner_id,
                    lease_until=lease_until,
                    updated_at=now,
                )
            )
            if result.rowcount:
                await session.commit()
                return True

            session.add(
                AppLease(
                    name=lease_name,
                    owner_id=self.owner_id,
                    lease_until=lease_until,
                    created_at=now,
                    updated_at=now,
                )
            )
            try:
                await session.commit()
                return True
            except IntegrityError:
                await session.rollback()

            result = await session.execute(
                update(AppLease)
                .where(AppLease.name == lease_name)
                .where(
                    or_(
                        AppLease.owner_id == self.owner_id,
                        AppLease.lease_until < now,
                    )
                )
                .values(
                    owner_id=self.owner_id,
                    lease_until=lease_until,
                    updated_at=now,
                )
            )
            if result.rowcount:
                await session.commit()
                return True

            await session.rollback()
            return False

    async def release(self, lease_name: str) -> None:
        async with self.session_factory() as session:
            await session.execute(
                delete(AppLease)
                .where(AppLease.name == lease_name)
                .where(AppLease.owner_id == self.owner_id)
            )
            await session.commit()

    async def is_owned_by_me(self, lease_name: str) -> bool:
        now = datetime.utcnow()
        async with self.session_factory() as session:
            row = await session.get(AppLease, lease_name)
            return bool(
                row
                and row.owner_id == self.owner_id
                and row.lease_until >= now
            )
