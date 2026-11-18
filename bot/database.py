"""Async SQLAlchemy engine & session factory."""

import logging
import re
from sqlite3 import Connection as SQLite3Connection

from sqlalchemy import text
from sqlalchemy import event
from sqlalchemy.pool import NullPool
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings

logger = logging.getLogger(__name__)

_VHQ_RENEWAL_INDEX_NAME = "uq_active_vhq_renewal_per_sub_v2"
_VHQ_RENEWAL_INDEX_TABLE = "vhq_subscription_fulfillments"
_VHQ_RENEWAL_INDEX_COLUMNS = ("subscription_id",)
_VHQ_RENEWAL_INDEX_PREDICATE = (
    "status IN ('pending', 'processing', 'retry_wait', 'uncertain', 'buy_inflight', 'renew_inflight')"
)

engine = create_async_engine(settings.database_url, echo=False, poolclass=NullPool if settings.database_url.startswith("sqlite") else None)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        raw_connection = getattr(dbapi_connection, "driver_connection", dbapi_connection)
        native_connection = getattr(raw_connection, "_conn", raw_connection)
        if not isinstance(native_connection, SQLite3Connection):
            return

        cursor = native_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


async def init_db():
    """Create all tables, then apply incremental column migrations."""
    from bot.models import Base  # noqa: F811

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    await _run_migrations()


async def _run_migrations() -> None:
    """Safely add new columns to existing tables (idempotent)."""
    # (table, column, sql_type_with_default)
    migrations = [
        ("users",    "referral_balance",   "REAL DEFAULT 0.0"),
        ("users",    "balance_rub",        "REAL DEFAULT 0.0"),
        ("users",    "balance_autodebit_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
        ("users",    "balance_mode_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
        ("users",    "balance_grace_until", "DATETIME"),
        ("users",    "last_daily_charge_at", "DATETIME"),
        ("users",    "next_daily_charge_at", "DATETIME"),
        ("users",    "balance_warning_for_charge_at", "DATETIME"),
        ("subscriptions", "billing_mode", "VARCHAR(16) DEFAULT 'tariff'"),
        ("subscriptions", "tariff_days", "INTEGER NOT NULL DEFAULT 0"),
        ("users",    "bonus_days",         "INTEGER NOT NULL DEFAULT 0"),
        ("payments", "telegram_chat_id",   "BIGINT"),
        ("payments", "provisioning_operation_id", "VARCHAR(64)"),
        ("payments", "provisioning_baseline_expires_at", "DATETIME"),
        ("payments", "provisioning_baseline_plan_uuid", "VARCHAR(64)"),
        ("payments", "provisioning_failure_code", "VARCHAR(64)"),
        ("payments", "provisioning_freeze_state", "VARCHAR(32)"),
        ("payments", "telegram_delivery_error", "VARCHAR(256)"),
        ("payments", "purchase_kopecks", "INTEGER"),
        ("payments", "discount_kopecks", "INTEGER"),
        ("payments", "provider_status", "VARCHAR(32)"),
        ("payments", "provider_idempotence_key", "VARCHAR(64)"),
        ("payments", "provider_paid", "BOOLEAN"),
        ("payments", "provider_amount_kopecks", "INTEGER"),
        ("payments", "provider_currency", "VARCHAR(8)"),
        ("payments", "provider_verified_at", "DATETIME"),
        ("payments", "reconciliation_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("payments", "next_attempt_at", "DATETIME"),
        ("balance_topups", "amount_kopecks", "INTEGER"),
        ("balance_topups", "provider_status", "VARCHAR(32)"),
        ("balance_topups", "provider_idempotence_key", "VARCHAR(64)"),
        ("balance_topups", "provider_paid", "BOOLEAN"),
        ("balance_topups", "provider_amount_kopecks", "INTEGER"),
        ("balance_topups", "provider_currency", "VARCHAR(8)"),
        ("balance_topups", "provider_verified_at", "DATETIME"),
        ("balance_topups", "provider_failure_code", "VARCHAR(64)"),
        ("balance_topups", "reconciliation_attempts", "INTEGER NOT NULL DEFAULT 0"),
        ("balance_topups", "next_attempt_at", "DATETIME"),
        ("recurring_payment_profiles", "recurring_operation_key", "VARCHAR(64)"),
        ("recurring_payment_profiles", "recurring_operation_amount_kopecks", "INTEGER"),
        ("recurring_payment_profiles", "recurring_operation_description", "VARCHAR(255)"),
        ("recurring_payment_profiles", "recurring_operation_payment_method_id", "VARCHAR(128)"),
        ("recurring_payment_profiles", "recurring_operation_provider_payment_id", "VARCHAR(128)"),
        ("recurring_payment_profiles", "recurring_operation_started_at", "DATETIME"),
        ("recurring_payment_profiles", "recurring_operation_generation", "INTEGER NOT NULL DEFAULT 0"),
        ("servers",  "api_url",            "VARCHAR(256)"),
        ("servers",  "api_username",       "VARCHAR(64)"),
        ("servers",  "api_password",       "VARCHAR(128)"),
        ("referral_config", "btn_name",            "VARCHAR DEFAULT '👥 Пригласить друзей'"),
        ("referral_config", "sub_btn_name",         "VARCHAR DEFAULT '🤝 Бонус за приглашение'"),
        ("referral_config", "bonus_days_referrer",  "INTEGER NOT NULL DEFAULT 1"),
        ("referral_config", "bonus_days_referral",  "INTEGER NOT NULL DEFAULT 1"),
        ("referral_config", "pay_bonus_enabled",    "BOOLEAN NOT NULL DEFAULT 0"),
        ("referral_config", "pay_bonus_days",       "INTEGER NOT NULL DEFAULT 1"),
        ("referral_config", "pay_bonus_first_only", "BOOLEAN NOT NULL DEFAULT 1"),
        ("referral_config", "commission_percent",   "REAL DEFAULT 10.0"),
        ("payments", "discount_applied", "REAL DEFAULT 0.0"),
        ("robokassa_payments", "discount_applied", "REAL DEFAULT 0.0"),
        ("followup_logs", "campaign_id", "INTEGER REFERENCES followup_campaigns(id)"),
        ("followup_campaigns", "buttons_json", "TEXT"),
        ("mailings", "buttons_json", "TEXT"),
        ("users", "partner_id", "INTEGER REFERENCES partners(id)"),
        ("users", "partner_link_id", "INTEGER REFERENCES partner_links(id)"),
        ("users", "ad_source", "VARCHAR(64)"),
        ("partners", "payouts_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
        ("partners", "commission_l2_percent", "REAL NOT NULL DEFAULT 0.0"),
        ("partners", "commission_l3_percent", "REAL NOT NULL DEFAULT 0.0"),
        ("partner_earnings", "depth", "INTEGER NOT NULL DEFAULT 1"),
        ("partner_earnings", "commission_percent", "REAL NOT NULL DEFAULT 0.0"),
        ("web_partner_earnings", "depth", "INTEGER NOT NULL DEFAULT 1"),
        ("web_partner_earnings", "commission_percent", "REAL NOT NULL DEFAULT 0.0"),
        ("tariffs", "tariff_type", "VARCHAR(16) NOT NULL DEFAULT 'VPN'"),
        ("tariffs", "adapt_plan_uuid", "VARCHAR(64)"),
        ("tariffs", "vhq_tier", "VARCHAR(16)"),
        ("subscriptions", "tariff_id", "INTEGER REFERENCES tariffs(id)"),
        ("subscriptions", "vhq_order_id", "VARCHAR(64)"),
        ("subscriptions", "vhq_active_until", "DATETIME"),
        ("users", "ad_source_kind", "VARCHAR(32)"),
        ("users", "referral_root_partner_id", "INTEGER REFERENCES partners(id)"),
        ("users", "referral_depth", "INTEGER DEFAULT 1"),
        ("users", "daily_charge_tariff_id", "INTEGER REFERENCES tariffs(id)"),
        ("platform_guides", "guide_text", "TEXT"),
        ("platform_guides", "buttons_json", "TEXT"),
        ("manual_subscription_operations", "completed_at", "DATETIME"),
        ("vhq_subscription_fulfillments", "recurring_profile_id", "INTEGER REFERENCES recurring_payment_profiles(id)"),
        ("referral_payment_logs", "payment_id", "INTEGER REFERENCES payments(id)"),
    ]
    async with engine.begin() as conn:
        for table, column, col_def in migrations:
            try:
                await conn.execute(
                    text(f"ALTER TABLE {table} ADD COLUMN {column} {col_def}")
                )
            except Exception:
                pass  # Column already exists - that's fine

        # Index migrations
        index_sqls = [
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_followup_user_campaign ON followup_logs (user_id, campaign_id)",
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_balance_tx_web_cashback "
                "ON balance_transactions (source_type, source_id) "
                "WHERE kind = 'CASHBACK' AND source_type = 'web_order_cashback' AND source_id IS NOT NULL"
            ),
            "CREATE INDEX IF NOT EXISTS ix_subscriptions_vhq_order_id ON subscriptions (vhq_order_id)",
            "CREATE INDEX IF NOT EXISTS ix_payments_next_attempt_at ON payments (next_attempt_at)",
            "CREATE INDEX IF NOT EXISTS ix_balance_topups_next_attempt_at ON balance_topups (next_attempt_at)",
            (
                "CREATE INDEX IF NOT EXISTS ix_payment_notification_outbox_due "
                "ON payment_notification_outbox (status, next_attempt_at)"
            ),
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_vhq_fulfillment_source "
                "ON vhq_subscription_fulfillments (source_type, source_id)"
            ),
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_manual_sub_op_uuid "
                "ON manual_subscription_operations (operation_uuid)"
            ),
        ]
        for sql in index_sqls:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass

        await _migrate_vhq_renewal_unique_index(conn)

        # Critical checked financial unique index migrations (fail loudly, verify presence, preflight duplicates)
        await _migrate_critical_financial_unique_indexes(conn)

        # Critical index migrations for atomic idempotency of new wbtr_* balance transfers
        critical_index_sqls = [
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_balance_topup_web_balance_wbtr "
                "ON balance_topups (provider_payment_id) "
                "WHERE provider = 'web_balance_migration' AND provider_payment_id LIKE 'wbtr_%'"
            ),
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_balance_tx_payment_discount "
                "ON balance_transactions (user_id, source_type, source_id) "
                "WHERE direction = 'DEBIT' AND source_type IN ('payment_discount', 'balance_payment') AND source_id IS NOT NULL"
            ),
            (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_balance_tx_payment_refund "
                "ON balance_transactions (user_id, source_type, source_id) "
                "WHERE direction = 'CREDIT' AND source_type IN ('payment_refund', 'payment_discount_refund') AND source_id IS NOT NULL"
            ),
        ]
        for sql in critical_index_sqls:
            try:
                await conn.execute(text(sql))
            except Exception as exc:
                logger.error("Failed to create critical unique index: %s: %s", sql, exc)
                raise

    await _backfill_vhq_order_ids()
    await _migrate_followup_to_campaigns()
    await _migrate_referral_balance_to_main_balance()
    await _normalize_tariff_type_values()


def _normalize_sql_fragment(value: str) -> str:
    return re.sub(r"\s+", "", value).lower()


async def _migrate_vhq_renewal_unique_index(conn) -> None:
    create_sql = (
        f"CREATE UNIQUE INDEX IF NOT EXISTS {_VHQ_RENEWAL_INDEX_NAME} "
        f"ON {_VHQ_RENEWAL_INDEX_TABLE} ({', '.join(_VHQ_RENEWAL_INDEX_COLUMNS)}) "
        f"WHERE {_VHQ_RENEWAL_INDEX_PREDICATE}"
    )
    try:
        await conn.execute(text(create_sql))
    except Exception as exc:
        message = (
            "CRITICAL MIGRATION FAILURE: unable to create required VHQ renewal index "
            f"'{_VHQ_RENEWAL_INDEX_NAME}'. Conflicting active fulfillment rows or an invalid schema "
            f"must be investigated manually; no data was changed automatically: {exc}"
        )
        logger.critical(message)
        raise RuntimeError(message) from exc

    index_sql_result = await conn.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'index' AND name = :index_name"),
        {"index_name": _VHQ_RENEWAL_INDEX_NAME},
    )
    index_sql = index_sql_result.scalar_one_or_none()
    if not index_sql:
        raise RuntimeError(
            "CRITICAL MIGRATION FAILURE: required VHQ renewal index "
            f"'{_VHQ_RENEWAL_INDEX_NAME}' is missing after creation"
        )

    index_list_result = await conn.execute(
        text(f'PRAGMA index_list("{_VHQ_RENEWAL_INDEX_TABLE}")')
    )
    index_row = next((row for row in index_list_result.fetchall() if row[1] == _VHQ_RENEWAL_INDEX_NAME), None)
    if index_row is None or int(index_row[2]) != 1 or int(index_row[4]) != 1:
        raise RuntimeError(
            "CRITICAL MIGRATION FAILURE: VHQ renewal index "
            f"'{_VHQ_RENEWAL_INDEX_NAME}' must be unique and partial"
        )

    index_info_result = await conn.execute(
        text(f'PRAGMA index_info("{_VHQ_RENEWAL_INDEX_NAME}")')
    )
    columns = [row[2] for row in index_info_result.fetchall()]
    if columns != list(_VHQ_RENEWAL_INDEX_COLUMNS):
        raise RuntimeError(
            "CRITICAL MIGRATION FAILURE: VHQ renewal index "
            f"'{_VHQ_RENEWAL_INDEX_NAME}' has columns {columns!r}, "
            f"expected {list(_VHQ_RENEWAL_INDEX_COLUMNS)!r}"
        )

    where_match = re.search(r"\bWHERE\b(.+)$", str(index_sql), flags=re.IGNORECASE)
    actual_predicate = where_match.group(1) if where_match else ""
    if _normalize_sql_fragment(actual_predicate) != _normalize_sql_fragment(_VHQ_RENEWAL_INDEX_PREDICATE):
        raise RuntimeError(
            "CRITICAL MIGRATION FAILURE: VHQ renewal index "
            f"'{_VHQ_RENEWAL_INDEX_NAME}' has an unexpected WHERE condition: {actual_predicate!r}"
        )


async def _migrate_critical_financial_unique_indexes(conn) -> None:
    """Migrate critical financial unique indexes with duplicate preflight and presence verification.

    Never swallows errors or alters production data automatically to deduplicate.
    Fails loudly if duplicates exist or if an index is missing after creation.
    """
    financial_indexes = [
        {
            "name": "uq_referral_payment_logs_payment_id",
            "table": "referral_payment_logs",
            "preflight_sql": (
                "SELECT payment_id, COUNT(*) as cnt "
                "FROM referral_payment_logs "
                "WHERE payment_id IS NOT NULL "
                "GROUP BY payment_id HAVING COUNT(*) > 1"
            ),
            "create_sql": (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_referral_payment_logs_payment_id "
                "ON referral_payment_logs (payment_id) "
                "WHERE payment_id IS NOT NULL"
            ),
        },
        {
            "name": "uq_referral_earnings_payment_id",
            "table": "referral_earnings",
            "preflight_sql": (
                "SELECT payment_id, COUNT(*) as cnt "
                "FROM referral_earnings "
                "WHERE payment_id IS NOT NULL "
                "GROUP BY payment_id HAVING COUNT(*) > 1"
            ),
            "create_sql": (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_referral_earnings_payment_id "
                "ON referral_earnings (payment_id) "
                "WHERE payment_id IS NOT NULL"
            ),
        },
        {
            "name": "uq_partner_earnings_payment_depth",
            "table": "partner_earnings",
            "preflight_sql": (
                "SELECT payment_id, depth, COUNT(*) as cnt "
                "FROM partner_earnings "
                "WHERE payment_id IS NOT NULL "
                "GROUP BY payment_id, depth HAVING COUNT(*) > 1"
            ),
            "create_sql": (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_partner_earnings_payment_depth "
                "ON partner_earnings (payment_id, depth) "
                "WHERE payment_id IS NOT NULL"
            ),
        },
        {
            "name": "uq_balance_tx_payment_cashback",
            "table": "balance_transactions",
            "preflight_sql": (
                "SELECT source_type, source_id, COUNT(*) as cnt "
                "FROM balance_transactions "
                "WHERE kind = 'CASHBACK' AND source_type = 'payment' AND source_id IS NOT NULL "
                "GROUP BY source_type, source_id HAVING COUNT(*) > 1"
            ),
            "create_sql": (
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_balance_tx_payment_cashback "
                "ON balance_transactions (source_type, source_id) "
                "WHERE kind = 'CASHBACK' AND source_type = 'payment' AND source_id IS NOT NULL"
            ),
        },
    ]

    for item in financial_indexes:
        idx_name = item["name"]
        table_name = item["table"]

        # 1. Preflight duplicate check: fail loudly without mutating production data
        try:
            dup_result = await conn.execute(text(item["preflight_sql"]))
            dups = dup_result.fetchall()
            if dups:
                msg = (
                    f"CRITICAL MIGRATION FAILURE: table '{table_name}' contains {len(dups)} duplicate key group(s) "
                    f"violating unique index '{idx_name}'. Examples: {dups[:3]}. "
                    f"Manual investigation is required; automatic deduplication of production financial data is prohibited."
                )
                logger.critical(msg)
                raise RuntimeError(msg)
        except Exception as exc:
            if "no such table" in str(exc).lower():
                continue
            if isinstance(exc, RuntimeError):
                raise
            logger.error("Error during preflight check for index %s: %s", idx_name, exc)
            raise

        # 2. Execute CREATE UNIQUE INDEX (loud failure on error)
        try:
            await conn.execute(text(item["create_sql"]))
        except Exception as exc:
            logger.critical("Failed to create critical financial index %s: %s", idx_name, exc)
            raise

        # 3. Verify presence in sqlite_master
        try:
            verify_res = await conn.execute(
                text("SELECT name FROM sqlite_master WHERE type = 'index' AND name = :idx_name"),
                {"idx_name": idx_name},
            )
            row = verify_res.scalar()
            if not row:
                raise RuntimeError(
                    f"CRITICAL MIGRATION FAILURE: index '{idx_name}' is not present in database after creation!"
                )
        except Exception as exc:
            if "no such table" in str(exc).lower():
                pass
            else:
                if isinstance(exc, RuntimeError):
                    raise
                logger.critical("Failed to verify presence of index %s: %s", idx_name, exc)
                raise


async def _backfill_vhq_order_ids() -> None:
    """No-op: automatic blind backfill from client_name is prohibited to avoid stale order IDs."""
    pass


async def _normalize_tariff_type_values() -> None:
    """Normalize legacy tariff_type values to the enum names used by SQLAlchemy."""
    updates = {
        "vpn": "VPN",
        "tg_proxy": "TG_PROXY",
        "both": "BOTH",
    }
    async with engine.begin() as conn:
        for legacy_value, enum_name in updates.items():
            await conn.execute(
                text(
                    "UPDATE tariffs "
                    "SET tariff_type = :enum_name "
                    "WHERE lower(coalesce(tariff_type, '')) = :legacy_value"
                ),
                {"enum_name": enum_name, "legacy_value": legacy_value},
            )


async def _migrate_followup_to_campaigns() -> None:
    """One-time: migrate old BotSettings follow-up into a FollowUpCampaign row."""
    from bot.models import BotSettings, FollowUpCampaign
    from sqlalchemy import select, func

    async with async_session() as session:
        count = await session.scalar(select(func.count()).select_from(FollowUpCampaign))
        if count and count > 0:
            return  # already migrated

        text_row = await session.get(BotSettings, "followup_text")
        if not text_row or not text_row.value:
            return  # nothing to migrate

        days_row = await session.get(BotSettings, "followup_days")
        enabled_row = await session.get(BotSettings, "followup_enabled")
        media_fid_row = await session.get(BotSettings, "followup_media_file_id")
        media_type_row = await session.get(BotSettings, "followup_media_type")

        try:
            days = int(days_row.value) if days_row else 4
        except ValueError:
            days = 4

        campaign = FollowUpCampaign(
            name=f"Рассылка (день {days})",
            days_after_demo=days,
            text=text_row.value,
            media_file_id=media_fid_row.value if media_fid_row and media_fid_row.value else None,
            media_type=media_type_row.value if media_type_row and media_type_row.value else None,
            is_enabled=enabled_row is not None and enabled_row.value == "1",
        )
        session.add(campaign)
        await session.commit()


async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session


async def _migrate_referral_balance_to_main_balance() -> None:
    """One-time migration: move old referral_balance into the new main balance."""
    from sqlalchemy import select

    from bot.models import BalanceDirection, BalanceTransaction, BalanceTransactionKind, User

    async with async_session() as session:
        result = await session.execute(
            select(User).where(
                User.referral_balance > 0,
                User.balance_rub <= 0,
            )
        )
        users = result.scalars().all()
        for user in users:
            amount = round(float(user.referral_balance or 0.0), 2)
            if amount <= 0:
                continue
            user.balance_rub = amount
            user.referral_balance = 0.0
            session.add(
                BalanceTransaction(
                    user_id=user.id,
                    kind=BalanceTransactionKind.MIGRATION,
                    direction=BalanceDirection.CREDIT,
                    amount_rub=amount,
                    balance_after_rub=amount,
                    description="Перенос старого реферального баланса в общий баланс",
                    source_type="migration",
                    source_id="referral_balance",
                )
            )
        if users:
            await session.commit()
