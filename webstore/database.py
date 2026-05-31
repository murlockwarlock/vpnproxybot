"""Database initialization for the web store."""

from __future__ import annotations

import hashlib
import hmac

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from webstore.config import settings
from webstore.models import Base, SupportTicket, SupportMessage, SupportAgentSession, WebBalanceAccount, WebOrder

_db_url = f"sqlite+aiosqlite:///{settings.database_path}"
engine = create_async_engine(_db_url, echo=False)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _make_profile_token(email: str) -> str:
    secret = settings.yookassa_secret_key or "webstore-profile-secret"
    return hmac.new(
        secret.encode(), email.lower().strip().encode(), hashlib.sha256
    ).hexdigest()[:32]


async def _ensure_web_telegram_auth_codes_nullable(conn) -> None:
    table_info = await conn.execute(
        __import__("sqlalchemy").text("PRAGMA table_info(web_telegram_auth_codes)")
    )
    columns = table_info.mappings().all()
    if not columns:
        return

    telegram_id_column = next((col for col in columns if col["name"] == "telegram_id"), None)
    if not telegram_id_column or int(telegram_id_column["notnull"] or 0) == 0:
        return

    await conn.execute(__import__("sqlalchemy").text("""
        CREATE TABLE web_telegram_auth_codes_new (
            id INTEGER NOT NULL PRIMARY KEY,
            code VARCHAR(64) NOT NULL,
            profile_token VARCHAR(64),
            telegram_id BIGINT,
            telegram_username VARCHAR(64),
            telegram_full_name VARCHAR(128),
            expires_at DATETIME NOT NULL,
            created_at DATETIME NOT NULL,
            consumed_at DATETIME
        )
    """))
    await conn.execute(__import__("sqlalchemy").text("""
        INSERT INTO web_telegram_auth_codes_new
            (id, code, profile_token, telegram_id, telegram_username, telegram_full_name, expires_at, created_at, consumed_at)
        SELECT
            id, code, profile_token, telegram_id, telegram_username, telegram_full_name, expires_at, created_at, consumed_at
        FROM web_telegram_auth_codes
    """))
    await conn.execute(__import__("sqlalchemy").text("DROP TABLE web_telegram_auth_codes"))
    await conn.execute(
        __import__("sqlalchemy").text(
            "ALTER TABLE web_telegram_auth_codes_new RENAME TO web_telegram_auth_codes"
        )
    )
    await conn.execute(__import__("sqlalchemy").text(
        "CREATE UNIQUE INDEX ix_web_telegram_auth_codes_code ON web_telegram_auth_codes (code)"
    ))
    await conn.execute(__import__("sqlalchemy").text(
        "CREATE INDEX ix_web_telegram_auth_codes_profile_token ON web_telegram_auth_codes (profile_token)"
    ))
    await conn.execute(__import__("sqlalchemy").text(
        "CREATE INDEX ix_web_telegram_auth_codes_telegram_id ON web_telegram_auth_codes (telegram_id)"
    ))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        # Add contact column if missing (migration for existing DBs)
        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE web_orders ADD COLUMN contact VARCHAR(256)"
                )
            )
        except Exception:
            pass  # column already exists
        # Add profile_token column if missing (migration for existing DBs)
        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE web_orders ADD COLUMN profile_token VARCHAR(64)"
                )
            )
        except Exception:
            pass  # column already exists
        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE web_orders ADD COLUMN ref_source VARCHAR(128)"
                )
            )
        except Exception:
            pass
        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE web_orders ADD COLUMN entry_referrer VARCHAR(512)"
                )
            )
        except Exception:
            pass
        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE web_orders ADD COLUMN entry_url VARCHAR(512)"
                )
            )
        except Exception:
            pass
        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE web_orders ADD COLUMN referrer_telegram_id BIGINT"
                )
            )
        except Exception:
            pass
        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE web_orders ADD COLUMN referral_status VARCHAR(16)"
                )
            )
        except Exception:
            pass
        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE web_orders ADD COLUMN referral_credited_at DATETIME"
                )
            )
        except Exception:
            pass
        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE web_orders ADD COLUMN original_amount_rub INTEGER"
                )
            )
        except Exception:
            pass
        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE web_orders ADD COLUMN bonus_applied_rub INTEGER DEFAULT 0"
                )
            )
        except Exception:
            pass
        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE web_orders ADD COLUMN bonus_spent_at DATETIME"
                )
            )
        except Exception:
            pass
        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE web_orders ADD COLUMN access_expires_at DATETIME"
                )
            )
        except Exception:
            pass
        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE web_orders ADD COLUMN failure_message VARCHAR(255)"
                )
            )
        except Exception:
            pass
        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "ALTER TABLE web_orders ADD COLUMN failure_reason TEXT"
                )
            )
        except Exception:
            pass
        for sql in (
            "ALTER TABLE web_balance_accounts ADD COLUMN balance_mode_enabled INTEGER DEFAULT 0",
            "ALTER TABLE web_balance_accounts ADD COLUMN balance_autodebit_enabled INTEGER DEFAULT 0",
            "ALTER TABLE web_balance_accounts ADD COLUMN balance_grace_until DATETIME",
            "ALTER TABLE web_balance_accounts ADD COLUMN last_daily_charge_at DATETIME",
            "ALTER TABLE web_balance_accounts ADD COLUMN next_daily_charge_at DATETIME",
            "ALTER TABLE web_balance_accounts ADD COLUMN balance_warning_for_charge_at DATETIME",
        ):
            try:
                await conn.execute(__import__("sqlalchemy").text(sql))
            except Exception:
                pass
        await _ensure_web_telegram_auth_codes_nullable(conn)
        # Support chat tables — created via Base.metadata.create_all above,
        # no extra migrations needed for fresh install.
        # For existing DBs the tables will simply be created if missing.

    async with async_session() as session:
        result = await session.execute(
            select(WebOrder).where(
                (WebOrder.contact.is_(None) & WebOrder.email.is_not(None))
                | (
                    WebOrder.profile_token.is_(None)
                    & (WebOrder.contact.is_not(None) | WebOrder.email.is_not(None))
                ),
            )
        )
        orders = result.scalars().all()
        for order in orders:
            contact = (order.contact or order.email or "").strip()
            if order.contact is None and contact:
                order.contact = contact
            if contact and not order.profile_token:
                order.profile_token = _make_profile_token(contact)
            if order.status == "delivered" and not getattr(order, "access_expires_at", None) and order.delivered_at:
                order.access_expires_at = order.delivered_at + __import__("datetime").timedelta(days=int(order.days or 0))
        if orders:
            await session.commit()

    async with async_session() as session:
        result = await session.execute(select(WebOrder).where(WebOrder.profile_token.is_not(None)))
        orders = result.scalars().all()
        for order in orders:
            account = await session.get(WebBalanceAccount, order.profile_token)
            if account:
                if not account.contact and order.contact:
                    account.contact = order.contact
                continue
            session.add(
                WebBalanceAccount(
                    profile_token=order.profile_token,
                    contact=order.contact or order.email,
                    ref_code=hmac.new(
                        (settings.yookassa_secret_key or "webstore-profile-secret").encode(),
                        f"ref:{order.profile_token}".encode(),
                        hashlib.sha256,
                    ).hexdigest()[:16],
                )
            )
        if orders:
            await session.commit()

    async with async_session() as session:
        result = await session.execute(select(WebBalanceAccount))
        accounts = result.scalars().all()
        dirty = False
        for account in accounts:
            if account.balance_rub is None:
                account.balance_rub = 0
                dirty = True
            if account.total_earned_rub is None:
                account.total_earned_rub = 0
                dirty = True
            if account.total_spent_rub is None:
                account.total_spent_rub = 0
                dirty = True
            for attr in ("balance_mode_enabled", "balance_autodebit_enabled"):
                if getattr(account, attr, None) is None:
                    setattr(account, attr, 0)
                    dirty = True
        if dirty:
            await session.commit()
