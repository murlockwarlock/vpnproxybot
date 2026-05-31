"""Async SQLAlchemy engine & session factory."""

from sqlite3 import Connection as SQLite3Connection

from sqlalchemy import text
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.config import settings

engine = create_async_engine(settings.database_url, echo=False)
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
        ("users", "ad_source_kind", "VARCHAR(32)"),
        ("partners", "payouts_enabled", "BOOLEAN NOT NULL DEFAULT 0"),
        ("tariffs", "tariff_type", "VARCHAR(16) NOT NULL DEFAULT 'VPN'"),
        ("tariffs", "adapt_plan_uuid", "VARCHAR(64)"),
        ("tariffs", "vhq_tier", "VARCHAR(16)"),
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
        ]
        for sql in index_sqls:
            try:
                await conn.execute(text(sql))
            except Exception:
                pass

    await _migrate_followup_to_campaigns()
    await _migrate_referral_balance_to_main_balance()
    await _normalize_tariff_type_values()


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
