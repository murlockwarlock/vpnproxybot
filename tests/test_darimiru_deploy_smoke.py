from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path
import sqlite3
import sys
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from bot.models import Base as BotBase, User
from test_webstore_telegram_linking import db_engine, db_session_factory, patched_webstore, _Request
from test_webstore_account_ux import account_server
from webstore import routes
from webstore.models import WebBalanceAccount
from scripts.darimiru_deploy_safety import SafetyError, daily_snapshot, inspect_database, financial_snapshot
from scripts.darimiru_deploy_runner import Host
from scripts.smoke_darimiru_financial import Client, run_readonly_smoke, run_smoke, preflight_qa

pytestmark = pytest.mark.asyncio


async def prepare_qa(server, tmp_path, monkeypatch):
    url, factory = server
    sender = AsyncMock()
    monkeypatch.setattr(routes, 'send_email', sender)
    response = await routes.handle_auth_register(_Request(_json_body={'contact': 'qa@example.test', 'password': 'qa-password-long', 'password2': 'qa-password-long'}))
    assert response.status == 200 and sender.await_count == 1
    token = sender.await_args.args[2].split('#')[-1]
    confirmation = await routes.handle_email_token(_Request(_json_body={'token': token}, path='/api/store/auth/confirm'))
    assert confirmation.status == 200
    async with factory() as session:
        session.add(WebBalanceAccount(profile_token='existing-active', ref_code='existing-active', balance_rub=80, balance_mode_enabled=1, balance_autodebit_enabled=1))
        await session.commit()
    bot_path = tmp_path / 'bot.sqlite3'
    engine = create_engine('sqlite:///' + str(bot_path))
    BotBase.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([User(id=1, telegram_id=123), User(id=2, telegram_id=456, balance_rub=100, balance_mode_enabled=True, balance_autodebit_enabled=True)])
        session.commit()
    engine.dispose()
    paths = {'bot': bot_path, 'web': Path(factory.kw['bind'].url.database)}
    qa = {'contact': 'qa@example.test', 'password': 'qa-password-long', 'telegram_id': 123}
    return url, paths, qa


async def test_real_http_smoke_qa_login_referral_link_and_daily_preservation(account_server, tmp_path, monkeypatch):
    url, paths, qa = await prepare_qa(account_server, tmp_path, monkeypatch)
    monkeypatch.setattr(routes, '_fetch_balance_profile', AsyncMock(return_value={'balance_rub': 0, 'balance_mode_enabled': False, 'balance_autodebit_enabled': False}))
    monkeypatch.setattr(routes, '_fetch_balance_history', AsyncMock(return_value=[]))
    before = {kind: daily_snapshot(path, kind, active_only=True) for kind, path in paths.items()}
    calls = []
    original_call = Client.call

    def record_call(self, path, body=None, internal=False):
        calls.append(path)
        return original_call(self, path, body, internal)

    monkeypatch.setattr(Client, 'call', record_call)
    preflight_qa(paths, qa)
    result = await asyncio.to_thread(run_smoke, url, 'test-bridge-secret', paths, qa)
    assert result == {'qa_without_orders': True, 'login_logout': True, 'referral': True, 'telegram_linking': True}
    assert '/api/store/auth/register' not in calls and '/api/store/auth/confirm' not in calls
    assert {kind: daily_snapshot(path, kind, active_only=True) for kind, path in paths.items()} == before
    with sqlite3.connect(paths['web']) as connection:
        assert connection.execute('SELECT telegram_id FROM web_profile_links').fetchall() == [(123,)]
        assert connection.execute('SELECT count(*) FROM web_accounts').fetchone()[0] == 1
        assert connection.execute('SELECT count(*) FROM web_orders').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM web_balance_transactions').fetchone()[0] == 0
    preflight_qa(paths, qa)
    again = await asyncio.to_thread(run_smoke, url, 'test-bridge-secret', paths, qa)
    assert all(again.values())
    assert '/api/store/auth/register' not in calls and '/api/store/auth/confirm' not in calls
    with sqlite3.connect(paths['web']) as connection:
        assert connection.execute('SELECT count(*) FROM web_accounts').fetchone()[0] == 1


async def test_real_http_readonly_smoke_needs_no_production_qa(account_server, tmp_path, monkeypatch):
    url, _, _ = await prepare_qa(account_server, tmp_path, monkeypatch)
    result = await asyncio.to_thread(run_readonly_smoke, url, 'test-bridge-secret')
    assert all(result.values())


async def test_real_smoke_rejects_silently_changed_active_daily_settings(account_server, tmp_path, monkeypatch):
    url, paths, qa = await prepare_qa(account_server, tmp_path, monkeypatch)
    monkeypatch.setattr(routes, '_fetch_balance_profile', AsyncMock(return_value={'balance_rub': 0, 'balance_mode_enabled': False, 'balance_autodebit_enabled': False}))
    monkeypatch.setattr(routes, '_fetch_balance_history', AsyncMock(return_value=[]))
    original = routes._upsert_profile_link

    async def corrupt_settings(*args, **kwargs):
        value = await original(*args, **kwargs)
        account = await args[0].get(WebBalanceAccount, 'existing-active')
        account.balance_autodebit_enabled = 0
        return value

    monkeypatch.setattr(routes, '_upsert_profile_link', corrupt_settings)
    with pytest.raises(SafetyError, match='active daily settings'):
        await asyncio.to_thread(run_smoke, url, 'test-bridge-secret', paths, qa)


async def test_real_migrations_on_isolated_copies_preserve_balances_and_flags(account_server, tmp_path, monkeypatch):
    _, paths, _ = await prepare_qa(account_server, tmp_path, monkeypatch)
    root = Path(__file__).resolve().parents[1]
    empty = tmp_path / 'empty.env'
    empty.touch()
    host = Host(root, sys.executable, 'darimiru_bot', 'webstore-darimiru.service')
    before = {kind: financial_snapshot(path) for kind, path in paths.items()}
    flags = {kind: daily_snapshot(path, kind) for kind, path in paths.items()}
    env = {**os.environ, 'DATABASE_URL': 'sqlite+aiosqlite:///' + str(paths['bot']), 'WEBSTORE_DB': str(paths['web']), 'WEBSTORE_ENV_FILE': str(empty)}
    for kind, path in paths.items():
        await asyncio.to_thread(host.call_helper, '_migrate', kind, root, env=env)
        inspect_database(path, kind, require_indexes=True)
        assert financial_snapshot(path, before[kind]) == before[kind]
        assert daily_snapshot(path, kind) == flags[kind]


async def test_vhq_index_is_optional_before_rehearsal_and_required_after_migration(
    account_server, tmp_path, monkeypatch
):
    _, paths, _ = await prepare_qa(account_server, tmp_path, monkeypatch)
    index_name = "uq_active_vhq_renewal_per_sub_v2"
    with sqlite3.connect(paths["bot"]) as connection:
        connection.execute(f'DROP INDEX "{index_name}"')

    initial = inspect_database(paths["bot"], "bot")
    assert index_name in initial["missing_indexes"]

    root = Path(__file__).resolve().parents[1]
    empty = tmp_path / "empty.env"
    empty.touch()
    host = Host(root, sys.executable, "darimiru_bot", "webstore-darimiru.service")
    env = {
        **os.environ,
        "DATABASE_URL": "sqlite+aiosqlite:///" + str(paths["bot"]),
        "WEBSTORE_DB": str(paths["web"]),
        "WEBSTORE_ENV_FILE": str(empty),
    }
    await asyncio.to_thread(host.call_helper, "_migrate", "bot", root, env=env)
    result = inspect_database(paths["bot"], "bot", require_indexes=True)
    assert index_name not in result["missing_indexes"]


async def test_vhq_index_migration_rejects_conflicting_active_rows_without_deleting_data():
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    import bot.database as bot_database

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.execute(
            text("CREATE TABLE vhq_subscription_fulfillments (subscription_id INTEGER, status TEXT)")
        )
        await conn.execute(
            text(
                "INSERT INTO vhq_subscription_fulfillments (subscription_id, status) "
                "VALUES (319, 'pending'), (319, 'renew_inflight')"
            )
        )
        with pytest.raises(RuntimeError, match="uq_active_vhq_renewal_per_sub_v2"):
            await bot_database._migrate_vhq_renewal_unique_index(conn)

    async with engine.connect() as conn:
        count = await conn.scalar(text("SELECT COUNT(*) FROM vhq_subscription_fulfillments"))
        assert count == 2
    await engine.dispose()


async def test_bridge_health_checks_real_authenticated_status_endpoint(account_server, tmp_path, monkeypatch):
    from aiohttp import web
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from bot import webhooks

    _, paths, _ = await prepare_qa(account_server, tmp_path, monkeypatch)
    engine = create_async_engine('sqlite+aiosqlite:///' + str(paths['bot']))
    monkeypatch.setattr(webhooks, 'async_session', async_sessionmaker(engine, expire_on_commit=False))
    monkeypatch.setattr(webhooks.settings, 'webstore_bridge_secret', 'test-bridge-secret')
    app = web.Application()
    webhooks.setup_webhooks(app, '/vpnbot')
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    root = Path(__file__).resolve().parents[1]
    host = Host(root, sys.executable, 'darimiru_bot', 'webstore-darimiru.service')
    context = {'config': {'port': port, 'prefix': '/vpnbot', 'secret': 'test-bridge-secret'}, 'uid': os.geteuid()}
    host.contexts = {'bot': context}
    monkeypatch.setattr(host, 'assert_listener', lambda kind: None)
    monkeypatch.setattr(host, 'processes', lambda: [{'pid': 12, 'pm2_env': {'status': 'online'}}])
    fake_proc = tmp_path / 'proc' / '12'
    fake_proc.mkdir(parents=True)
    (fake_proc / 'cmdline').write_bytes(b'python\0-m\0bot\0')
    host.proc = fake_proc.parent
    monkeypatch.setattr('scripts.darimiru_deploy_runner.process_tree', lambda *args: {12: {'pid': 12}})
    monkeypatch.setattr(host, 'configuration', lambda *args: context)
    try:
        await asyncio.to_thread(host.health, 'bot')
        with sqlite3.connect(paths['bot']) as connection:
            assert connection.execute('SELECT count(*) FROM balance_topups').fetchone()[0] == 0
            assert connection.execute('SELECT balance_rub FROM users WHERE id=2').fetchone()[0] == 100
    finally:
        await runner.cleanup()
        await engine.dispose()


async def test_registration_confirmation_in_isolated_http_with_mock_smtp(account_server, monkeypatch):
    from sqlalchemy import select
    from webstore.models import WebAccount, WebEmailToken

    url, factory = account_server
    sender = AsyncMock()
    monkeypatch.setattr(routes, 'send_email', sender)
    client = Client(url, 'test-bridge-secret')
    contact, password = 'registration@example.test', 'test-password-long'
    status, registered = await asyncio.to_thread(client.call, '/api/store/auth/register', {'contact': contact, 'password': password, 'password2': password})
    assert status == 200 and registered == {'ok': True}
    assert sender.await_count == 1
    assert (await asyncio.to_thread(client.ok, '/api/store/auth/me'))['authenticated'] is False
    assert (await asyncio.to_thread(client.call, '/api/store/auth/login', {'contact': contact, 'password': password}))[0] == 401
    token = sender.await_args.args[2].split('#')[-1]
    async with factory() as session:
        assert await session.scalar(select(WebAccount).where(WebAccount.contact == contact)) is None
        record = await session.scalar(select(WebEmailToken).where(WebEmailToken.email == contact))
        assert record is not None and record.token_hash != token
    assert (await asyncio.to_thread(client.call, '/api/store/auth/confirm', {'token': token}))[0] == 200
    assert (await asyncio.to_thread(client.call, '/api/store/auth/confirm', {'token': token}))[0] == 400
    logged_in = await asyncio.to_thread(client.ok, '/api/store/auth/login', {'contact': contact, 'password': password})
    profile = await asyncio.to_thread(client.ok, '/api/store/profile-orders?token=' + logged_in['token'])
    assert profile['orders'] == [] and profile['web_balance_history'] == []
    assert profile['referral']['balance_rub'] == 0
    assert not profile['balance']['balance_mode_enabled']
    assert not profile['balance']['balance_autodebit_enabled']


async def test_qa_preflight_uses_confirmed_email_for_existing_phone(account_server, tmp_path, monkeypatch):
    from sqlalchemy import select
    from webstore.models import WebAccount

    _, paths, qa = await prepare_qa(account_server, tmp_path, monkeypatch)
    _, factory = account_server
    async with factory() as session:
        account = await session.scalar(select(WebAccount).where(WebAccount.contact == qa['contact']))
        account.contact = '+71234567890'
        await session.commit()
    preflight_qa(paths, qa)
    with pytest.raises(SafetyError, match='no longer an active login'):
        preflight_qa(paths, {**qa, 'contact': '+71234567890'})
    monkeypatch.setattr(routes, '_fetch_balance_profile', AsyncMock(return_value={'balance_rub': 0, 'balance_mode_enabled': False, 'balance_autodebit_enabled': False}))
    monkeypatch.setattr(routes, '_fetch_balance_history', AsyncMock(return_value=[]))
    result = await asyncio.to_thread(run_smoke, account_server[0], 'test-bridge-secret', paths, qa)
    assert all(result.values())


async def test_qa_preflight_accepts_legacy_schema_without_email_table(account_server, tmp_path, monkeypatch):
    _, paths, qa = await prepare_qa(account_server, tmp_path, monkeypatch)
    with sqlite3.connect(paths['web']) as connection:
        connection.execute('DROP TABLE web_emails')
    preflight_qa(paths, qa)


async def test_smoke_rejects_login_to_a_different_profile(account_server, tmp_path, monkeypatch):
    url, paths, qa = await prepare_qa(account_server, tmp_path, monkeypatch)
    original_call = Client.call

    def wrong_profile(self, path, body=None, internal=False):
        status, data = original_call(self, path, body, internal)
        if path == '/api/store/auth/login' and status == 200:
            data['token'] = 'wrong-profile'
        return status, data

    monkeypatch.setattr(Client, 'call', wrong_profile)
    with pytest.raises(SafetyError, match='Dedicated QA profile is invalid'):
        await asyncio.to_thread(run_smoke, url, 'test-bridge-secret', paths, qa)
    with sqlite3.connect(paths['web']) as connection:
        assert connection.execute('SELECT count(*) FROM web_profile_link_codes').fetchone()[0] == 0
        assert connection.execute('SELECT count(*) FROM web_accounts').fetchone()[0] == 1
