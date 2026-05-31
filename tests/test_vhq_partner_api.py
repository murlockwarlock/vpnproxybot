from __future__ import annotations

import pytest

import bot.services.vhq_partner_api as vhq_module
from bot.services.vhq_partner_api import VHQPartnerAPI, VHQPartnerAPIError

pytestmark = pytest.mark.asyncio


class _FakeResponse:
    def __init__(self, *, status: int, payload):
        self.status = status
        self._payload = payload

    async def json(self, content_type=None):
        return self._payload

    async def text(self):
        if isinstance(self._payload, str):
            return self._payload
        return str(self._payload)


class _FakeRequestContext:
    def __init__(self, response: _FakeResponse):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeClientSession:
    def __init__(self, *, response_map, seen):
        self._response_map = response_map
        self._seen = seen

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def request(self, method, url, json=None, headers=None):
        action = url.split("?action=", 1)[1]
        self._seen.append(
            {
                "method": method,
                "url": url,
                "action": action,
                "headers": headers or {},
                "json": json,
            }
        )
        return _FakeRequestContext(self._response_map[action])


def _patch_client_session(monkeypatch, *, response_map, seen):
    def factory(*args, **kwargs):
        return _FakeClientSession(response_map=response_map, seen=seen)

    monkeypatch.setattr(vhq_module.aiohttp, "ClientSession", factory)


async def test_vhq_partner_api_get_balance_and_buy_flow(monkeypatch):
    seen = []
    _patch_client_session(
        monkeypatch,
        seen=seen,
        response_map={
            "balance": _FakeResponse(
                status=200,
                payload={"balance": 15000, "currency": "RUB", "pricing": {"lite": [], "basic": []}},
            ),
            "buy": _FakeResponse(
                status=200,
                payload={
                    "order_id": "uuid-1",
                    "config_url": "https://example.com/config",
                    "tier": "lite",
                    "days": 7,
                    "charged": 59,
                    "balance_remaining": 14941,
                    "created_at": "2026-04-13T12:00:00Z",
                },
            ),
            "orders": _FakeResponse(status=200, payload={"orders": []}),
        },
    )

    client = VHQPartnerAPI(api_key="test-key", base_url="https://example.com/partner-api")

    balance = await client.get_balance()
    order = await client.buy(tier="Lite", days=7)
    orders = await client.get_orders()

    assert balance["balance"] == 15000
    assert order["charged"] == 59
    assert orders["orders"] == []
    assert seen == [
        {
            "method": "GET",
            "url": "https://example.com/partner-api?action=balance",
            "action": "balance",
            "headers": {"X-API-Key": "test-key"},
            "json": None,
        },
        {
            "method": "POST",
            "url": "https://example.com/partner-api?action=buy",
            "action": "buy",
            "headers": {"X-API-Key": "test-key", "Content-Type": "application/json"},
            "json": {"tier": "lite", "days": 7},
        },
        {
            "method": "GET",
            "url": "https://example.com/partner-api?action=orders",
            "action": "orders",
            "headers": {"X-API-Key": "test-key"},
            "json": None,
        },
    ]


async def test_vhq_partner_api_extracts_subscription_url_from_branded_url():
    payload = {
        "order_id": "uuid-2",
        "tier": "lite",
        "days": 1,
        "charged": 1,
        "balance_remaining": 999,
        "branded_url": "https://example.com/branded",
        "created_at": "2026-04-15T18:44:31.874Z",
    }

    assert VHQPartnerAPI.extract_subscription_url(payload) == "https://example.com/branded"


async def test_vhq_partner_api_rejects_unsupported_duration_locally():
    client = VHQPartnerAPI(api_key="test-key", base_url="https://example.com/partner-api")

    with pytest.raises(VHQPartnerAPIError, match="Unsupported VHQ duration"):
        await client.buy(tier="lite", days=180)


async def test_vhq_partner_api_surfaces_remote_errors(monkeypatch):
    seen = []
    _patch_client_session(
        monkeypatch,
        seen=seen,
        response_map={
            "buy": _FakeResponse(status=402, payload={"error": "Insufficient balance"}),
        },
    )

    client = VHQPartnerAPI(api_key="test-key", base_url="https://example.com/partner-api")
    with pytest.raises(VHQPartnerAPIError, match="Insufficient balance"):
        await client.buy(tier="basic", days=30)
