"""Regression tests for P0-2: block unpaid Adapt traffic callbacks while keeping AdaptAPI.purchase_traffic."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from bot.handlers.profile import adapt_buy_traffic, adapt_traffic
from bot.models import (
    AdaptSubscription,
    Base,
    Platform,
    Server,
    SubStatus,
    Subscription,
    User,
)
from bot.services.adapt_api import AdaptAPI

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def bot_db():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield factory
    await engine.dispose()


async def test_legacy_adapt_traffic_menu_callback_blocks_unpaid_flow():
    """adapt_traffic:<sub_id> must reject legacy button and never render adapt_buy_traffic callbacks."""
    callback = SimpleNamespace(
        data="adapt_traffic:42",
        from_user=SimpleNamespace(id=700001),
        answer=AsyncMock(),
        message=SimpleNamespace(
            answer=AsyncMock(),
            edit_text=AsyncMock(),
        ),
    )

    with patch.object(AdaptAPI, "purchase_traffic", new_callable=AsyncMock) as mock_purchase:
        await adapt_traffic(callback)

    assert mock_purchase.await_count == 0
    callback.answer.assert_awaited_once_with("Эта кнопка устарела", show_alert=True)
    assert callback.message.edit_text.await_count == 0
    callback.message.answer.assert_awaited_once()


@pytest.mark.parametrize("gb_payload", ["10", "100", "10000", "invalid_payload"])
async def test_legacy_adapt_buy_traffic_callback_never_calls_supplier(bot_db, gb_payload: str):
    """Crafted adapt_buy_traffic:<sub_id>:<gb> by subscription owner must never spend supplier funds."""
    now = datetime.utcnow()
    async with bot_db() as session:
        user = User(id=1, telegram_id=700002, username="owner_user")
        server = Server(
            id=1,
            name="Adapt Placeholder",
            host="adapt.test",
            location="NL",
            api_url="https://adapt.test",
            is_active=True,
        )
        sub = Subscription(
            id=77,
            user_id=1,
            server_id=1,
            tariff_months=0,
            tariff_days=3,
            vpn_key="https://mirror.test/adapt/sub77",
            client_name="adapt_trial_77",
            platform=Platform.ANDROID,
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=3),
        )
        adapt_rec = AdaptSubscription(
            subscription_id=77,
            adapt_uuid="00000000-0000-0000-0000-000000000077",
            adapt_plan_uuid="plan-trial",
        )
        session.add_all([user, server, sub, adapt_rec])
        await session.commit()

    callback = SimpleNamespace(
        data=f"adapt_buy_traffic:77:{gb_payload}",
        from_user=SimpleNamespace(id=700002),
        answer=AsyncMock(),
        message=SimpleNamespace(answer=AsyncMock()),
    )

    with (
        patch("bot.handlers.profile.async_session", bot_db),
        patch.object(AdaptAPI, "purchase_traffic", new_callable=AsyncMock) as mock_purchase,
    ):
        await adapt_buy_traffic(callback)

    assert mock_purchase.await_count == 0
    callback.answer.assert_awaited_once_with("Эта кнопка устарела", show_alert=True)
    callback.message.answer.assert_awaited_once()


async def test_adapt_api_purchase_traffic_method_is_preserved_for_future_paid_flow():
    """AdaptAPI.purchase_traffic must remain intact for future verified-payment feature."""
    api = AdaptAPI(api_key="test-key", base_url="https://api.adapt.test")
    with patch.object(api, "_post", new=AsyncMock(return_value={"total_price": "1.50", "additional_bytes": 10737418240})) as mock_post:
        res = await api.purchase_traffic("sub-uuid-1", 10)

    mock_post.assert_awaited_once_with(
        "/subs/traffic",
        {"subscription_uuid": "sub-uuid-1", "gb_amount": 10},
    )
    assert res["total_price"] == "1.50"
