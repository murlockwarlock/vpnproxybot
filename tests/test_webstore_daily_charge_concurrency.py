from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from test_webstore_telegram_linking import (
    db_engine,
    db_session_factory,
    patched_webstore,
    _MockClientSession,
    _MockResponse,
)
from webstore import routes, scheduler
from webstore.models import WebBalanceAccount, WebBalanceTransaction, WebBalanceTransfer, WebOrder

pytestmark = pytest.mark.asyncio


async def create_daily_account(factory):
    due = datetime.utcnow() - timedelta(minutes=5)
    async with factory() as session:
        session.add(WebBalanceAccount(
            profile_token="daily-concurrency", ref_code="daily-concurrency", balance_rub=100,
            balance_mode_enabled=1, balance_autodebit_enabled=1, next_daily_charge_at=due,
        ))
        session.add(WebOrder(
            order_id="daily-access", profile_token="daily-concurrency", contact="daily@example.com",
            tariff_key="vpn_30", tariff_label="Доступ", days=30, amount_rub=100,
            status="delivered", marzban_username="daily_access",
            subscription_url="https://example.test/sub/daily", access_expires_at=due,
        ))
        await session.commit()
    return due


async def extend_access(order, expires_at):
    order.access_expires_at = expires_at
    return True


async def test_daily_scheduler_and_wbtr_serialize_across_processes(patched_webstore, monkeypatch, tmp_path):
    due = await create_daily_account(patched_webstore)
    release = tmp_path / "release-provider"
    effects = tmp_path / "provider-effects"
    worker = tmp_path / "daily_worker.py"
    worker.write_text('''
import asyncio
from datetime import datetime
from pathlib import Path
import sys
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from webstore import routes, scheduler

async def main():
    engine = create_async_engine("sqlite+aiosqlite:///" + sys.argv[1])
    factory = async_sessionmaker(engine, expire_on_commit=False)
    routes.async_session = scheduler.async_session = factory
    async def rate():
        return 10.0
    async def extend(order, expires_at):
        Path(sys.argv[3]).write_text(expires_at.isoformat())
        print("provider-started", flush=True)
        async def wait_release():
            while not Path(sys.argv[2]).exists():
                await asyncio.sleep(0.01)
        await asyncio.wait_for(wait_release(), 15)
        order.access_expires_at = expires_at
        return True
    scheduler._get_web_daily_charge_rub = rate
    scheduler._ensure_web_access_until = extend
    try:
        await scheduler.process_web_balance_daily_charges()
    finally:
        await engine.dispose()
asyncio.run(main())
''')
    database = str(patched_webstore.kw["bind"].url.database)
    process = await asyncio.create_subprocess_exec(
        sys.executable, str(worker), database, str(release), str(effects),
        cwd=str(Path(__file__).resolve().parents[1]),
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    tasks = []
    try:
        assert await asyncio.wait_for(process.stdout.readline(), 10) == b"provider-started\n"
        attempted = asyncio.Event()
        real_lock = routes._acquire_profile_lock

        @asynccontextmanager
        async def observed_lock(token):
            attempted.set()
            async with real_lock(token):
                yield

        monkeypatch.setattr(scheduler, "_acquire_profile_lock", observed_lock)
        monkeypatch.setattr(scheduler, "_get_web_daily_charge_rub", AsyncMock(return_value=10.0))
        provider = AsyncMock(side_effect=AssertionError("A second scheduler must not extend the same due period"))
        monkeypatch.setattr(scheduler, "_ensure_web_access_until", provider)
        credits = []

        def credit(url, json, **kwargs):
            credits.append(json)
            return _MockResponse({"status": "credited"})

        monkeypatch.setattr(routes.aiohttp, "ClientSession", lambda **kwargs: _MockClientSession(post_handler=credit))
        daily = asyncio.create_task(scheduler.process_web_balance_daily_charges())
        tasks.append(daily)
        await asyncio.wait_for(attempted.wait(), 3)

        async def migrate():
            async with patched_webstore() as session:
                return await routes._migrate_web_balance_to_telegram(session, "daily-concurrency", 123)

        transfer = asyncio.create_task(migrate())
        tasks.append(transfer)
        await asyncio.sleep(0.1)
        assert not daily.done() and not transfer.done()
        async with patched_webstore() as session:
            session.add(WebBalanceAccount(profile_token="independent", ref_code="independent", balance_rub=7))
            await asyncio.wait_for(session.commit(), 2)
        release.touch()
        await asyncio.wait_for(asyncio.gather(*tasks), 10)
        assert await asyncio.wait_for(process.wait(), 5) == 0, (await process.stderr.read()).decode()
        provider.assert_not_called()
        assert len(credits) == 1 and credits[0]["telegram_id"] == 123 and credits[0]["amount_rub"] == 90
        async with patched_webstore() as session:
            account = await session.get(WebBalanceAccount, "daily-concurrency")
            order = await session.scalar(select(WebOrder).where(WebOrder.order_id == "daily-access"))
            transactions = (await session.execute(select(WebBalanceTransaction).where(WebBalanceTransaction.profile_token == "daily-concurrency"))).scalars().all()
            durable = await session.scalar(select(WebBalanceTransfer))
            assert account.balance_rub == 0 and account.total_spent_rub == 100
            assert account.balance_mode_enabled == account.balance_autodebit_enabled == 1
            assert account.next_daily_charge_at > datetime.utcnow()
            assert account.last_daily_charge_at is not None
            assert order.access_expires_at == account.next_daily_charge_at == datetime.fromisoformat(effects.read_text())
            assert sorted((tx.kind, tx.amount_rub) for tx in transactions) == [("balance_migration", 90), ("daily_charge", 10)]
            assert next(tx for tx in transactions if tx.kind == "daily_charge").source_id == due.isoformat()
            assert durable.status == "completed" and durable.amount_rub == 90
    finally:
        release.touch()
        if process.returncode is None:
            process.kill()
            await process.wait()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.parametrize("change", ["balance", "rescheduled", "disabled"])
async def test_daily_scheduler_rereads_profile_after_lock(patched_webstore, monkeypatch, change):
    await create_daily_account(patched_webstore)
    monkeypatch.setattr(scheduler, "_get_web_daily_charge_rub", AsyncMock(return_value=10.0))
    provider = AsyncMock(side_effect=extend_access)
    monkeypatch.setattr(scheduler, "_ensure_web_access_until", provider)
    attempted = asyncio.Event()
    real_lock = routes._acquire_profile_lock

    @asynccontextmanager
    async def observed_lock(token):
        attempted.set()
        async with real_lock(token):
            yield

    monkeypatch.setattr(scheduler, "_acquire_profile_lock", observed_lock)
    async with real_lock("daily-concurrency"):
        task = asyncio.create_task(scheduler.process_web_balance_daily_charges())
        await asyncio.wait_for(attempted.wait(), 3)
        async with patched_webstore() as session:
            account = await session.get(WebBalanceAccount, "daily-concurrency")
            if change == "balance":
                account.balance_rub = 150
            elif change == "rescheduled":
                account.next_daily_charge_at = datetime.utcnow() + timedelta(days=1)
            else:
                account.balance_autodebit_enabled = 0
            await session.commit()
    await asyncio.wait_for(task, 5)
    async with patched_webstore() as session:
        account = await session.get(WebBalanceAccount, "daily-concurrency")
        transactions = (await session.execute(select(WebBalanceTransaction))).scalars().all()
        if change == "balance":
            assert account.balance_rub == 140 and account.total_spent_rub == 10
            assert len(transactions) == 1
            provider.assert_awaited_once()
        else:
            assert account.balance_rub == 100 and account.total_spent_rub == 0
            assert transactions == []
            provider.assert_not_called()
        assert account.balance_mode_enabled == 1
        assert account.balance_autodebit_enabled == (0 if change == "disabled" else 1)


async def test_existing_daily_billing_continues_and_retry_does_not_charge_twice(patched_webstore, monkeypatch):
    await create_daily_account(patched_webstore)
    monkeypatch.setattr(scheduler, "_get_web_daily_charge_rub", AsyncMock(return_value=10.0))
    provider = AsyncMock(side_effect=extend_access)
    monkeypatch.setattr(scheduler, "_ensure_web_access_until", provider)
    for cycle in range(2):
        if cycle:
            async with patched_webstore() as session:
                account = await session.get(WebBalanceAccount, "daily-concurrency")
                account.next_daily_charge_at = datetime.utcnow() - timedelta(minutes=1)
                await session.commit()
        await scheduler.process_web_balance_daily_charges()
        await scheduler.process_web_balance_daily_charges()
        async with patched_webstore() as session:
            account = await session.get(WebBalanceAccount, "daily-concurrency")
            order = await session.scalar(select(WebOrder).where(WebOrder.order_id == "daily-access"))
            transactions = (await session.execute(select(WebBalanceTransaction))).scalars().all()
            assert account.balance_rub == 100 - 10 * (cycle + 1)
            assert account.total_spent_rub == 10 * (cycle + 1)
            assert account.balance_mode_enabled == account.balance_autodebit_enabled == 1
            assert account.next_daily_charge_at == order.access_expires_at > datetime.utcnow()
            assert len(transactions) == cycle + 1
            assert len({tx.source_id for tx in transactions}) == cycle + 1
    assert provider.await_count == 2


async def test_cancelled_daily_charge_rolls_back_and_can_retry(patched_webstore, monkeypatch):
    await create_daily_account(patched_webstore)
    monkeypatch.setattr(scheduler, "_get_web_daily_charge_rub", AsyncMock(return_value=10.0))
    started = asyncio.Event()

    async def delayed_provider(order, expires_at):
        started.set()
        await asyncio.Future()

    monkeypatch.setattr(scheduler, "_ensure_web_access_until", delayed_provider)
    task = asyncio.create_task(scheduler.process_web_balance_daily_charges())
    await asyncio.wait_for(started.wait(), 3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    async with patched_webstore() as session:
        account = await session.get(WebBalanceAccount, "daily-concurrency")
        assert account.balance_rub == 100 and account.total_spent_rub == 0
        assert account.balance_mode_enabled == account.balance_autodebit_enabled == 1
        assert not (await session.execute(select(WebBalanceTransaction))).scalars().all()
    monkeypatch.setattr(scheduler, "_ensure_web_access_until", extend_access)
    await scheduler.process_web_balance_daily_charges()
    async with patched_webstore() as session:
        account = await session.get(WebBalanceAccount, "daily-concurrency")
        assert account.balance_rub == 90 and account.total_spent_rub == 10
        assert account.balance_mode_enabled == account.balance_autodebit_enabled == 1
        assert len((await session.execute(select(WebBalanceTransaction))).scalars().all()) == 1
