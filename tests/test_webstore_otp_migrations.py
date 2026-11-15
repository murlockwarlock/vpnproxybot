from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import webstore.database as database
from webstore.database import _ensure_otp_schema, _ensure_sqlite_columns

pytestmark = pytest.mark.asyncio


async def _create_legacy_schema(engine) -> None:
    statements = (
        "CREATE TABLE web_otp_mail (id INTEGER PRIMARY KEY, address_digest VARCHAR(64) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE web_otp_challenges (token_hash VARCHAR(64) PRIMARY KEY, code_digest VARCHAR(64) NOT NULL, purpose VARCHAR(16) NOT NULL, email VARCHAR(256) NOT NULL, attempts INTEGER NOT NULL, expires_at DATETIME NOT NULL)",
        "CREATE TABLE web_otp_grants (token_hash VARCHAR(64) PRIMARY KEY, email VARCHAR(256) NOT NULL, profile_token VARCHAR(64) NOT NULL, expires_at DATETIME NOT NULL)",
        "CREATE TABLE support_agent_sessions (token VARCHAR(64) PRIMARY KEY, telegram_id BIGINT NOT NULL, expires_at DATETIME NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE support_recovery_requests (id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL, old_contact VARCHAR(256) NOT NULL, new_email VARCHAR(256) NOT NULL, status VARCHAR(16) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE support_recovery_audit (id INTEGER PRIMARY KEY, request_id INTEGER NOT NULL, ticket_id INTEGER NOT NULL, action VARCHAR(24) NOT NULL, created_at DATETIME NOT NULL)",
        "CREATE TABLE web_orders (id INTEGER PRIMARY KEY, order_id VARCHAR(64) NOT NULL, amount_rub INTEGER NOT NULL, status VARCHAR(16) NOT NULL)",
        "CREATE TABLE web_balance_accounts (profile_token VARCHAR(64) PRIMARY KEY, balance_rub INTEGER NOT NULL, next_daily_charge_at DATETIME)",
    )
    async with engine.begin() as conn:
        for statement in statements:
            await conn.execute(text(statement))
        await conn.execute(text("INSERT INTO web_otp_mail (id, address_digest, created_at) VALUES (1, 'digest', '2026-01-01 00:00:00')"))
        await conn.execute(text("INSERT INTO web_otp_grants (token_hash, email, profile_token, expires_at) VALUES ('grant', 'owner@example.com', 'legacy-profile', '2026-01-01 00:00:00')"))
        await conn.execute(text("INSERT INTO web_orders (id, order_id, amount_rub, status) VALUES (1, 'order-1', 499, 'delivered')"))
        await conn.execute(text("INSERT INTO web_balance_accounts (profile_token, balance_rub, next_daily_charge_at) VALUES ('legacy-profile', 321, '2026-01-02 00:00:00')"))


async def _column_names(conn, table: str) -> set[str]:
    result = await conn.execute(text(f'PRAGMA table_info("{table}")'))
    return {row[1] for row in result.fetchall()}


async def test_otp_schema_migration_is_idempotent_and_preserves_financial_rows(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-webstore.db'}")
    try:
        await _create_legacy_schema(engine)
        async with engine.begin() as conn:
            await _ensure_otp_schema(conn)
            await _ensure_otp_schema(conn)
            assert await conn.scalar(text("SELECT purpose FROM web_otp_mail WHERE id = 1")) == "auth"
            assert await conn.scalar(text("SELECT purpose FROM web_otp_grants WHERE token_hash = 'grant'")) == "reset"
            assert await conn.scalar(text("SELECT amount_rub FROM web_orders WHERE order_id = 'order-1'")) == 499
            assert await conn.scalar(text("SELECT balance_rub FROM web_balance_accounts WHERE profile_token = 'legacy-profile'")) == 321
            assert await conn.scalar(text("SELECT next_daily_charge_at FROM web_balance_accounts WHERE profile_token = 'legacy-profile'")) == "2026-01-02 00:00:00"
            assert "auth_method" in await _column_names(conn, "support_agent_sessions")
            assert "claimant_evidence_type" in await _column_names(conn, "support_recovery_requests")
            assert {"order_id", "payment_id"}.issubset(await _column_names(conn, "support_recovery_audit"))
    finally:
        await engine.dispose()


async def test_otp_schema_migration_reports_wrong_existing_type(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'malformed-webstore.db'}")
    try:
        async with engine.begin() as conn:
            await conn.execute(text("CREATE TABLE web_otp_mail (id INTEGER PRIMARY KEY, purpose INTEGER NOT NULL DEFAULT 1)"))
            with pytest.raises(RuntimeError, match=r"web_otp_mail\.purpose"):
                await _ensure_sqlite_columns(conn, "web_otp_mail", (
                    ("purpose", "ALTER TABLE web_otp_mail ADD COLUMN purpose VARCHAR(16) NOT NULL DEFAULT 'auth'", "VARCHAR(16)", 1, "auth"),
                ))
    finally:
        await engine.dispose()


async def test_init_db_fresh_and_restart_validate_otp_schema(tmp_path, monkeypatch):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'fresh-webstore.db'}")
    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "async_session", async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False))
    try:
        await database.init_db()
        await database.init_db()
        async with engine.connect() as conn:
            assert "purpose" in await _column_names(conn, "web_otp_mail")
            assert "auth_method" in await _column_names(conn, "support_agent_sessions")
    finally:
        await engine.dispose()
