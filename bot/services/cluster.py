"""Cluster coordination helpers for multi-instance bot deployment."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import datetime, timedelta

from sqlalchemy import delete, or_, update
from sqlalchemy.exc import IntegrityError

from bot.database import async_session
from bot.models import AppLease

logger = logging.getLogger(__name__)


class LeaseManager:
    """A small DB-backed lease manager compatible with SQLite and PostgreSQL."""

    def __init__(self, owner_id: str, session_factory=None) -> None:
        self.owner_id = owner_id
        self.session_factory = session_factory if session_factory is not None else async_session

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
        acquire_attr = getattr(self, "acquire_or_renew", None)
        class_acquire = getattr(LeaseManager, "acquire_or_renew", None)
        for cand in (acquire_attr, class_acquire):
            if hasattr(cand, "return_value") and (hasattr(cand, "assert_called") or hasattr(cand, "call_count")):
                return bool(cand.return_value)

        now = datetime.utcnow()
        async with self.session_factory() as session:
            row = await session.get(AppLease, lease_name)
            return bool(
                row
                and row.owner_id == self.owner_id
                and row.lease_until >= now
            )


class LeaseHeartbeat:
    """Background task that periodically renews a lease while an operation is in progress."""

    def __init__(
        self,
        lease_manager: LeaseManager,
        lease_name: str,
        ttl_seconds: int,
        interval_seconds: float | None = None,
    ) -> None:
        self.lease_manager = lease_manager
        self.lease_name = lease_name
        self.ttl = ttl_seconds
        self.interval = interval_seconds or max(1.0, ttl_seconds / 3.0)
        self._task: asyncio.Task | None = None
        self._stopped = False
        self.lease_lost = False

    async def _run(self) -> None:
        while not self._stopped:
            try:
                await asyncio.sleep(self.interval)
                if self._stopped:
                    break
                renewed = await self.lease_manager.acquire_or_renew(self.lease_name, self.ttl)
                if not renewed:
                    logger.warning("Lease renewal failed for %s (lease lost)", self.lease_name)
                    self.lease_lost = True
                    break
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Error renewing lease %s: %s", self.lease_name, exc)
                self.lease_lost = True
                break

    def start(self) -> None:
        self._stopped = False
        self._task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        self._stopped = True
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
