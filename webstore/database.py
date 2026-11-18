"""Database initialization for the web store."""

from __future__ import annotations

import hashlib
import hmac
import logging

from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from webstore.config import settings
from webstore.models import Base, SupportTicket, SupportMessage, SupportAgentSession, WebBalanceAccount, WebBalanceTransfer, WebOrder

logger = logging.getLogger(__name__)

HISTORICAL_CANCELLATION_AUDIT_CUTOFF = "2026-11-03 11:55:00"

_db_url = f"sqlite+aiosqlite:///{settings.database_path}"
engine = create_async_engine(_db_url, echo=False, poolclass=NullPool)
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


async def _ensure_sqlite_columns(conn, table: str, definitions: tuple[tuple[str, str, str, int | None, str | None], ...]) -> None:
    columns = await conn.execute(text(f'PRAGMA table_info("{table}")'))
    current = {row[1]: row for row in columns.fetchall()}
    if not current:
        raise RuntimeError(f"Required SQLite table '{table}' is missing")
    for name, ddl, expected_type, expected_notnull, expected_default in definitions:
        column = current.get(name)
        existed_before_migration = column is not None
        if column is None:
            try:
                await conn.execute(text(ddl))
            except OperationalError as exc:
                if "duplicate column name" not in str(exc).lower():
                    raise RuntimeError(f"Failed to add SQLite column '{table}.{name}': {exc}") from exc
            columns = await conn.execute(text(f'PRAGMA table_info("{table}")'))
            current = {row[1]: row for row in columns.fetchall()}
            column = current.get(name)
        if column is None:
            raise RuntimeError(f"SQLite migration did not create '{table}.{name}'")
        actual_type = str(column[2] or "").upper()
        if expected_type and actual_type != expected_type.upper():
            raise RuntimeError(
                f"SQLite column '{table}.{name}' has type '{actual_type}', expected '{expected_type}'"
            )
        if expected_notnull is not None and int(column[3] or 0) != expected_notnull:
            raise RuntimeError(
                f"SQLite column '{table}.{name}' has not-null={column[3]}, expected {expected_notnull}"
            )
        actual_default = str(column[4] or "").strip("'")
        if expected_default is not None and actual_default not in (expected_default, ""):
            raise RuntimeError(
                f"SQLite column '{table}.{name}' has default '{column[4]}', expected '{expected_default}'"
            )
        if expected_default is not None and not actual_default and not existed_before_migration:
            raise RuntimeError(
                f"SQLite migration did not set default for '{table}.{name}'"
            )


async def _ensure_otp_schema(conn) -> None:
    await _ensure_sqlite_columns(conn, "web_otp_mail", (
        ("purpose", "ALTER TABLE web_otp_mail ADD COLUMN purpose VARCHAR(16) NOT NULL DEFAULT 'auth'", "VARCHAR(16)", 1, "auth"),
    ))
    await _ensure_sqlite_columns(conn, "web_otp_challenges", (
        ("support_recovery_id", "ALTER TABLE web_otp_challenges ADD COLUMN support_recovery_id INTEGER", "INTEGER", 0, None),
    ))
    await _ensure_sqlite_columns(conn, "web_otp_grants", (
        ("purpose", "ALTER TABLE web_otp_grants ADD COLUMN purpose VARCHAR(16) NOT NULL DEFAULT 'reset'", "VARCHAR(16)", 1, "reset"),
        ("support_recovery_id", "ALTER TABLE web_otp_grants ADD COLUMN support_recovery_id INTEGER", "INTEGER", 0, None),
    ))
    await _ensure_sqlite_columns(conn, "support_agent_sessions", (
        ("auth_method", "ALTER TABLE support_agent_sessions ADD COLUMN auth_method VARCHAR(16) NOT NULL DEFAULT 'password'", "VARCHAR(16)", 1, "password"),
    ))
    await _ensure_sqlite_columns(conn, "support_recovery_requests", (
        ("payment_check_status", "ALTER TABLE support_recovery_requests ADD COLUMN payment_check_status VARCHAR(24)", "VARCHAR(24)", 0, None),
        ("payment_check_reason", "ALTER TABLE support_recovery_requests ADD COLUMN payment_check_reason TEXT", "TEXT", 0, None),
        ("payment_verified_at", "ALTER TABLE support_recovery_requests ADD COLUMN payment_verified_at DATETIME", "DATETIME", 0, None),
        ("payment_verification_source", "ALTER TABLE support_recovery_requests ADD COLUMN payment_verification_source VARCHAR(32)", "VARCHAR(32)", 0, None),
        ("payment_id", "ALTER TABLE support_recovery_requests ADD COLUMN payment_id VARCHAR(128)", "VARCHAR(128)", 0, None),
        ("payment_status", "ALTER TABLE support_recovery_requests ADD COLUMN payment_status VARCHAR(32)", "VARCHAR(32)", 0, None),
        ("payment_currency", "ALTER TABLE support_recovery_requests ADD COLUMN payment_currency VARCHAR(8)", "VARCHAR(8)", 0, None),
        ("payment_amount_rub", "ALTER TABLE support_recovery_requests ADD COLUMN payment_amount_rub INTEGER", "INTEGER", 0, None),
        ("claimant_evidence_type", "ALTER TABLE support_recovery_requests ADD COLUMN claimant_evidence_type VARCHAR(32)", "VARCHAR(32)", 0, None),
    ))
    await _ensure_sqlite_columns(conn, "support_recovery_audit", (
        ("verification_source", "ALTER TABLE support_recovery_audit ADD COLUMN verification_source VARCHAR(32)", "VARCHAR(32)", 0, None),
        ("order_id", "ALTER TABLE support_recovery_audit ADD COLUMN order_id VARCHAR(64)", "VARCHAR(64)", 0, None),
        ("payment_id", "ALTER TABLE support_recovery_audit ADD COLUMN payment_id VARCHAR(128)", "VARCHAR(128)", 0, None),
        ("payment_status", "ALTER TABLE support_recovery_audit ADD COLUMN payment_status VARCHAR(32)", "VARCHAR(32)", 0, None),
        ("payment_currency", "ALTER TABLE support_recovery_audit ADD COLUMN payment_currency VARCHAR(8)", "VARCHAR(8)", 0, None),
        ("payment_amount_rub", "ALTER TABLE support_recovery_audit ADD COLUMN payment_amount_rub INTEGER", "INTEGER", 0, None),
        ("claimant_evidence_type", "ALTER TABLE support_recovery_audit ADD COLUMN claimant_evidence_type VARCHAR(32)", "VARCHAR(32)", 0, None),
    ))


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_otp_schema(conn)
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
        for sql in (
            "ALTER TABLE web_orders ADD COLUMN purchase_action VARCHAR(16) NOT NULL DEFAULT 'new'",
            "ALTER TABLE web_orders ADD COLUMN target_order_id VARCHAR(64)",
            "ALTER TABLE web_orders ADD COLUMN fulfillment_attempts INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE web_orders ADD COLUMN next_fulfillment_retry_at DATETIME",
            "ALTER TABLE web_orders ADD COLUMN last_fulfillment_attempt_at DATETIME",
            "ALTER TABLE web_orders ADD COLUMN target_snapshot_expires_at DATETIME",
            "ALTER TABLE web_orders ADD COLUMN target_snapshot_plan_uuid VARCHAR(64)",
            "ALTER TABLE web_orders ADD COLUMN target_snapshot_is_frozen BOOLEAN",
            "ALTER TABLE web_orders ADD COLUMN fulfillment_freeze_state VARCHAR(32)",
            "ALTER TABLE web_orders ADD COLUMN adapt_mutation_state VARCHAR(32)",
            "ALTER TABLE web_orders ADD COLUMN failure_code VARCHAR(64)",
            "ALTER TABLE web_telegram_items ADD COLUMN provider VARCHAR(16)",
            "ALTER TABLE web_telegram_items ADD COLUMN adapt_plan_uuid VARCHAR(64)",
        ):
            try:
                await conn.execute(__import__("sqlalchemy").text(sql))
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
        for sql in (
            "ALTER TABLE support_tickets ADD COLUMN profile_token VARCHAR(64)",
            "ALTER TABLE support_messages ADD COLUMN media_url VARCHAR(512)",
            "ALTER TABLE support_messages ADD COLUMN media_type VARCHAR(32)",
        ):
            try:
                await conn.execute(__import__("sqlalchemy").text(sql))
            except Exception:
                pass
        cashback_col_existed = True
        try:
            info_res = await conn.execute(__import__("sqlalchemy").text("PRAGMA table_info(web_orders)"))
            cols = {row[1] for row in info_res.fetchall()}
            cashback_col_existed = "cashback_status" in cols
        except Exception:
            pass

        for sql in (
            "ALTER TABLE web_orders ADD COLUMN cashback_status VARCHAR(32) DEFAULT 'pending'",
            "ALTER TABLE web_orders ADD COLUMN cashback_amount_rub INTEGER DEFAULT 0",
            "ALTER TABLE web_orders ADD COLUMN cashback_processed_at DATETIME",
            "ALTER TABLE web_orders ADD COLUMN vhq_order_id VARCHAR(64)",
            "ALTER TABLE web_orders ADD COLUMN vhq_active_until DATETIME",
            "ALTER TABLE web_orders ADD COLUMN vhq_upstream_url TEXT",
        ):
            try:
                await conn.execute(__import__("sqlalchemy").text(sql))
            except Exception:
                pass

        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "CREATE INDEX IF NOT EXISTS ix_web_orders_vhq_order_id ON web_orders (vhq_order_id)"
                )
            )
        except Exception:
            pass

        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "UPDATE web_orders "
                    "SET vhq_order_id = substr(marzban_username, 5) "
                    "WHERE vhq_order_id IS NULL "
                    "  AND marzban_username LIKE 'vhq_%' "
                    "  AND length(marzban_username) > 4"
                )
            )
        except Exception:
            pass

        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "UPDATE web_orders "
                    "SET vhq_upstream_url = subscription_url "
                    "WHERE vhq_upstream_url IS NULL "
                    "  AND subscription_url LIKE 'http%' "
                    "  AND subscription_url NOT LIKE '%/vhq-sub/%' "
                    "  AND ("
                    "      vhq_order_id IS NOT NULL "
                    "      OR (marzban_username LIKE 'vhq_%' AND length(marzban_username) > 4) "
                    "      OR subscription_url LIKE '%/proxy-subscription/%' "
                    "      OR subscription_url LIKE '%vhq-connect%'"
                    "  )"
                )
            )
        except Exception:
            pass

        if not cashback_col_existed:
            # Historical backfill runs ONLY when cashback_status is first introduced
            try:
                # 1. Historical delivered order with existing web cashback -> web_credited + backfill amount
                await conn.execute(
                    __import__("sqlalchemy").text(
                        "UPDATE web_orders "
                        "SET cashback_status = 'web_credited', "
                        "    cashback_amount_rub = COALESCE("
                        "        (SELECT CAST(ROUND(wbt.amount_rub) AS INTEGER) "
                        "         FROM web_balance_transactions wbt "
                        "         WHERE wbt.kind = 'cashback' AND wbt.source_id = web_orders.order_id "
                        "         LIMIT 1), "
                        "        cashback_amount_rub"
                        "    ) "
                        "WHERE status = 'delivered' "
                        "  AND order_id IN ("
                        "      SELECT source_id FROM web_balance_transactions WHERE kind = 'cashback' AND source_id IS NOT NULL"
                        "  )"
                    )
                )
            except Exception:
                pass

            try:
                # 2. Historical delivered order WITHOUT existing cashback transaction -> terminal legacy_skipped
                await conn.execute(
                    __import__("sqlalchemy").text(
                        "UPDATE web_orders "
                        "SET cashback_status = 'legacy_skipped' "
                        "WHERE status = 'delivered' "
                        "  AND (cashback_status IS NULL OR cashback_status = 'pending') "
                        "  AND order_id NOT IN ("
                        "      SELECT source_id FROM web_balance_transactions WHERE kind = 'cashback' AND source_id IS NOT NULL"
                        "  )"
                    )
                )
            except Exception:
                pass

        try:
            await conn.execute(
                __import__("sqlalchemy").text(
                    "CREATE INDEX IF NOT EXISTS ix_support_tickets_profile_token ON support_tickets (profile_token)"
                )
            )
        except Exception:
            pass

        cancellation_confirmed_col_existed = True
        try:
            info_res = await conn.execute(text("PRAGMA table_info(web_orders)"))
            cols = {row[1] for row in info_res.fetchall()}
            cancellation_confirmed_col_existed = "cancellation_confirmed" in cols
        except Exception:
            cancellation_confirmed_col_existed = False

        if not cancellation_confirmed_col_existed:
            await conn.execute(
                text("ALTER TABLE web_orders ADD COLUMN cancellation_confirmed BOOLEAN NOT NULL DEFAULT 0")
            )

        for sql in (
            "ALTER TABLE web_orders ADD COLUMN last_reconciled_at DATETIME",
            "ALTER TABLE web_balance_topups ADD COLUMN last_reconciled_at DATETIME",
        ):
            try:
                await conn.execute(text(sql))
            except Exception:
                pass

        for idx_sql in (
            "CREATE INDEX IF NOT EXISTS ix_web_orders_last_reconciled_at ON web_orders (last_reconciled_at)",
            "CREATE INDEX IF NOT EXISTS ix_web_balance_topups_last_reconciled_at ON web_balance_topups (last_reconciled_at)",
        ):
            try:
                await conn.execute(text(idx_sql))
            except Exception:
                pass

        if not cancellation_confirmed_col_existed:
            # Historical backfill runs ONLY when cancellation_confirmed is first introduced:
            # Only canceled orders created on or before the independent audit cutoff (2026-11-03 11:55:00 UTC)
            # are marked cancellation_confirmed = 1 (True). Orders canceled after the cutoff remain 0 (False)
            # so they can undergo YooKassa reconciliation if paid.
            # Fail-closed: Must NOT swallow exceptions!
            await conn.execute(
                text(
                    "UPDATE web_orders "
                    "SET cancellation_confirmed = 1 "
                    "WHERE status = 'canceled' "
                    "  AND (datetime(replace(created_at, 'T', ' ')) <= datetime(:cutoff) OR created_at IS NULL)"
                ),
                {"cutoff": HISTORICAL_CANCELLATION_AUDIT_CUTOFF},
            )

        # Fail-closed postcondition verification:
        info_check = await conn.execute(text("PRAGMA table_info(web_orders)"))
        present_cols = {row[1] for row in info_check.fetchall()}
        if "cancellation_confirmed" not in present_cols:
            raise RuntimeError(
                "Migration verification failed: 'cancellation_confirmed' column missing from web_orders"
            )

        unprotected_count = await conn.scalar(
            text(
                "SELECT COUNT(*) "
                "FROM web_orders "
                "WHERE status = 'canceled' "
                "  AND (datetime(replace(created_at, 'T', ' ')) <= datetime(:cutoff) OR created_at IS NULL) "
                "  AND (cancellation_confirmed IS NULL OR cancellation_confirmed != 1)"
            ),
            {"cutoff": HISTORICAL_CANCELLATION_AUDIT_CUTOFF},
        )
        if (unprotected_count or 0) > 0:
            raise RuntimeError(
                f"Historical cancellation safety verification failed: {unprotected_count} "
                f"legacy canceled order(s) on or before {HISTORICAL_CANCELLATION_AUDIT_CUTOFF} UTC "
                "have cancellation_confirmed != 1. Aborting startup to prevent accidental automated recovery."
            )

        # Critical financial indexes
        critical_indexes = [
            (
                "uq_web_balance_transfers_profile_pending",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_web_balance_transfers_profile_pending "
                "ON web_balance_transfers(profile_token) WHERE status = 'pending'",
                "SELECT profile_token, count(*) as c FROM web_balance_transfers WHERE status = 'pending' GROUP BY profile_token HAVING c > 1",
            ),
            (
                "uq_web_balance_tx_migration_source",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_web_balance_tx_migration_source "
                "ON web_balance_transactions(source_id) WHERE kind = 'balance_migration' AND source_id IS NOT NULL",
                "SELECT source_id, count(*) as c FROM web_balance_transactions WHERE kind = 'balance_migration' AND source_id IS NOT NULL GROUP BY source_id HAVING c > 1",
            ),
            (
                "uq_web_balance_tx_order_payment",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_web_balance_tx_order_payment "
                "ON web_balance_transactions(source_id) WHERE kind = 'order_payment' AND source_id IS NOT NULL",
                "SELECT source_id, count(*) as c FROM web_balance_transactions WHERE kind = 'order_payment' AND source_id IS NOT NULL GROUP BY source_id HAVING c > 1",
            ),
            (
                "uq_web_balance_tx_order_refund",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_web_balance_tx_order_refund "
                "ON web_balance_transactions(source_id) WHERE kind = 'order_bonus_refund' AND source_id IS NOT NULL",
                "SELECT source_id, count(*) as c FROM web_balance_transactions WHERE kind = 'order_bonus_refund' AND source_id IS NOT NULL GROUP BY source_id HAVING c > 1",
            ),
            (
                "uq_web_balance_tx_referral_source",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_web_balance_tx_referral_source "
                "ON web_balance_transactions(profile_token, source_id) WHERE kind = 'referral_bonus' AND source_id IS NOT NULL",
                "SELECT profile_token, source_id, count(*) as c FROM web_balance_transactions WHERE kind = 'referral_bonus' AND source_id IS NOT NULL GROUP BY profile_token, source_id HAVING c > 1",
            ),
            (
                "uq_web_balance_tx_topup_source",
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_web_balance_tx_topup_source "
                "ON web_balance_transactions(source_id) WHERE kind = 'topup' AND source_id IS NOT NULL",
                "SELECT source_id, count(*) as c FROM web_balance_transactions WHERE kind = 'topup' AND source_id IS NOT NULL GROUP BY source_id HAVING c > 1",
            ),
        ]
        for idx_name, create_sql, check_dup_sql in critical_indexes:
            try:
                await conn.execute(__import__("sqlalchemy").text(create_sql))
            except Exception as exc:
                dup_info = []
                try:
                    dup_res = await conn.execute(__import__("sqlalchemy").text(check_dup_sql))
                    dup_info = dup_res.fetchall()
                except Exception:
                    pass
                logger.critical(
                    "FAILED TO CREATE CRITICAL FINANCIAL UNIQUE INDEX '%s': %s. Conflicting rows: %s",
                    idx_name,
                    exc,
                    dup_info,
                )
                raise RuntimeError(
                    f"Critical unique index '{idx_name}' creation failed: {exc}. Conflicting rows: {dup_info}. Manual resolution required."
                ) from exc

    async with async_session() as session:
        result = await session.execute(
            select(WebOrder).where(
                (WebOrder.contact.is_(None) & WebOrder.email.is_not(None))
                | (
                    WebOrder.profile_token.is_(None)
                    & (WebOrder.purchase_action == "new")
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
