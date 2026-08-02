"""Tests for AdaptAPI HTTP client (bot/services/adapt_api.py)."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.services.adapt_api import (
    AdaptAPI,
    AdaptAPIError,
    can_upgrade_after_minimum_custom_renew,
)


def _make_api(api_id=12, api_key="testkey", base_url="https://test.adapt.example"):
    return AdaptAPI(api_id=api_id, api_key=api_key, base_url=base_url)


def _mock_response(status: int, data: dict):
    """Build a mock aiohttp response context manager."""
    resp = AsyncMock()
    resp.status = status
    resp.json = AsyncMock(return_value=data)
    resp.read = AsyncMock(return_value=json.dumps(data).encode())
    resp.text = AsyncMock(return_value=json.dumps(data))
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


def _mock_session(response):
    session = MagicMock()
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    session.post = MagicMock(return_value=response)
    return session


# ── enabled property ──────────────────────────────────────────────────────────

def test_enabled_with_credentials():
    api = _make_api()
    assert api.enabled is True


def test_disabled_without_credentials():
    api = AdaptAPI(api_id=0, api_key="", base_url="https://x.com")
    assert api.enabled is False


def test_minimum_custom_renew_upgrade_requires_positive_delta():
    current = {"uuid": "old", "price_usd": 1.0, "days": 10}

    assert can_upgrade_after_minimum_custom_renew(
        current,
        {"uuid": "higher", "price_usd": 0.31, "days": 7},
    ) is True
    assert can_upgrade_after_minimum_custom_renew(
        current,
        {"uuid": "lower", "price_usd": 0.30, "days": 7},
    ) is False


def test_disabled_raises_on_post():
    api = AdaptAPI(api_id=0, api_key="", base_url="https://x.com")
    with pytest.raises(AdaptAPIError, match="not configured"):
        import asyncio
        asyncio.get_event_loop().run_until_complete(api._post("/any", {}))


# ── list_plans ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_plans_success():
    payload = {"plans": [{"uuid": "abc", "name": "Basic", "days": 30}]}
    resp = _mock_response(200, payload)
    session = _mock_session(resp)
    api = _make_api()
    with patch("aiohttp.ClientSession", return_value=session):
        plans = await api.list_plans()
    assert plans == payload["plans"]


@pytest.mark.asyncio
async def test_list_plans_empty():
    resp = _mock_response(200, {})
    session = _mock_session(resp)
    api = _make_api()
    with patch("aiohttp.ClientSession", return_value=session):
        plans = await api.list_plans()
    assert plans == []


# ── create_subscription ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_subscription_success():
    payload = {
        "subscription_uuid": "sub-uuid-1",
        "subscription_url": "https://adapt.example/sub/sub-uuid-1",
        "plan_uuid": "plan-1",
        "end_date": "2026-06-01T00:00:00Z",
        "days": 30,
        "devices": 3,
        "traffic_limit_bytes": None,
        "balance": 10.0,
    }
    resp = _mock_response(200, payload)
    session = _mock_session(resp)
    api = _make_api()
    with patch("aiohttp.ClientSession", return_value=session):
        result = await api.create_subscription("plan-1", external_user_id="user123")
    assert result["subscription_uuid"] == "sub-uuid-1"
    # Verify body sent to API
    call_args = session.post.call_args
    body = call_args[1]["json"] if "json" in call_args[1] else call_args[0][1]
    assert body["plan_uuid"] == "plan-1"
    assert body["external_user_id"] == "user123"
    assert body["api_key_id"] == 12


@pytest.mark.asyncio
async def test_create_subscription_api_error_4xx():
    resp = _mock_response(400, {"detail": "Invalid plan UUID"})
    session = _mock_session(resp)
    api = _make_api()
    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(AdaptAPIError) as exc_info:
            await api.create_subscription("bad-plan")
    assert exc_info.value.status == 400
    assert "Invalid plan UUID" in str(exc_info.value)


@pytest.mark.asyncio
async def test_create_subscription_success_false():
    resp = _mock_response(200, {"success": False, "message": "Insufficient balance"})
    session = _mock_session(resp)
    api = _make_api()
    with patch("aiohttp.ClientSession", return_value=session):
        with pytest.raises(AdaptAPIError, match="Insufficient balance"):
            await api.create_subscription("plan-1")


# ── renew_subscription ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_renew_subscription_success():
    payload = {"subscription_uuid": "sub-1", "end_date": "2026-07-01T00:00:00Z"}
    resp = _mock_response(200, payload)
    session = _mock_session(resp)
    api = _make_api()
    with patch("aiohttp.ClientSession", return_value=session):
        result = await api.renew_subscription("sub-1")
    assert result["end_date"] == "2026-07-01T00:00:00Z"


@pytest.mark.asyncio
async def test_custom_renew_rejects_days_below_live_provider_minimum():
    api = _make_api()

    with pytest.raises(ValueError, match="at least 3 days"):
        await api.renew_subscription_custom("sub-1", 1)


@pytest.mark.asyncio
async def test_custom_renew_sends_live_provider_minimum():
    resp = _mock_response(200, {"subscription_uuid": "sub-1"})
    session = _mock_session(resp)
    api = _make_api()

    with patch("aiohttp.ClientSession", return_value=session):
        await api.renew_subscription_custom("sub-1", 3)

    assert session.post.call_args.kwargs["json"]["custom_days"] == 3


# ── freeze / unfreeze ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_freeze_subscription():
    payload = {"subscription_uuid": "sub-1", "frozen_at": "2026-04-01T00:00:00Z"}
    resp = _mock_response(200, payload)
    session = _mock_session(resp)
    api = _make_api()
    with patch("aiohttp.ClientSession", return_value=session):
        result = await api.freeze_subscription("sub-1")
    assert result["frozen_at"] == "2026-04-01T00:00:00Z"
    call_body = session.post.call_args[1]["json"]
    assert call_body["subscription_uuid"] == "sub-1"


@pytest.mark.asyncio
async def test_unfreeze_subscription():
    payload = {"subscription_uuid": "sub-1", "end_date": "2026-08-01T00:00:00Z"}
    resp = _mock_response(200, payload)
    session = _mock_session(resp)
    api = _make_api()
    with patch("aiohttp.ClientSession", return_value=session):
        result = await api.unfreeze_subscription("sub-1")
    assert "end_date" in result


# ── upgrade ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_upgrade_subscription():
    payload = {"upgrade_price": 2.5, "devices": 5, "end_date": "2026-07-01T00:00:00Z"}
    resp = _mock_response(200, payload)
    session = _mock_session(resp)
    api = _make_api()
    with patch("aiohttp.ClientSession", return_value=session):
        result = await api.upgrade_subscription("sub-1", "plan-premium")
    assert result["upgrade_price"] == 2.5
    call_body = session.post.call_args[1]["json"]
    assert call_body["new_plan_uuid"] == "plan-premium"


# ── traffic purchase ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_purchase_traffic():
    payload = {"total_price": 1.0, "additional_bytes": 10_737_418_240}
    resp = _mock_response(200, payload)
    session = _mock_session(resp)
    api = _make_api()
    with patch("aiohttp.ClientSession", return_value=session):
        result = await api.purchase_traffic("sub-1", 10)
    assert result["total_price"] == 1.0
    call_body = session.post.call_args[1]["json"]
    assert call_body["gb_amount"] == 10


# ── get_status ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_status():
    payload = {
        "subscription_uuid": "sub-1",
        "is_active": True,
        "is_frozen": False,
        "end_date": "2026-07-01T00:00:00Z",
        "used_traffic_bytes": 1024,
        "traffic_limit_bytes": None,
    }
    resp = _mock_response(200, payload)
    session = _mock_session(resp)
    api = _make_api()
    with patch("aiohttp.ClientSession", return_value=session):
        result = await api.get_status("sub-1")
    assert result["is_active"] is True


# ── get_devices ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_devices():
    payload = {"devices": [{"id": 1, "name": "iPhone"}]}
    resp = _mock_response(200, payload)
    session = _mock_session(resp)
    api = _make_api()
    with patch("aiohttp.ClientSession", return_value=session):
        devices = await api.get_devices("sub-1")
    assert len(devices) == 1
    assert devices[0]["name"] == "iPhone"


# ── auth header ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_auth_header_set():
    payload = {"subscription_uuid": "x"}
    resp = _mock_response(200, payload)
    session = _mock_session(resp)
    api = _make_api(api_key="supersecret")
    with patch("aiohttp.ClientSession", return_value=session):
        await api.create_subscription("plan-1")
    call_kwargs = session.post.call_args[1]
    assert call_kwargs["headers"]["X-Api-Key"] == "supersecret"


# ── get_balance ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_balance_success():
    payload = {"success": True, "balance": 15.5, "currency": "USD"}
    resp = _mock_response(200, payload)
    session = _mock_session(resp)
    api = _make_api()
    with patch("aiohttp.ClientSession", return_value=session):
        result = await api.get_balance()
    assert result["balance"] == 15.5
    assert result["currency"] == "USD"
    call_body = session.post.call_args[1]["json"]
    assert call_body["api_key_id"] == 12
