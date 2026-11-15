import pytest
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from aiohttp.test_utils import make_mocked_request

from webstore.routes import (
    _resolve_payment_base_url,
    _TRUSTED_PAYMENT_DOMAINS,
)
from bot.services.vhq_subscription_proxy import get_subscription_display_key


def test_trusted_payment_domains():
    assert "darimiru.ru" in _TRUSTED_PAYMENT_DOMAINS
    assert "darimiru.xyz" in _TRUSTED_PAYMENT_DOMAINS


def test_resolve_payment_base_url_darimiru_xyz():
    # 1. From Origin
    req_xyz_origin = make_mocked_request("POST", "/api/store/create-order", headers={"Origin": "https://darimiru.xyz"})
    assert _resolve_payment_base_url(req_xyz_origin) == "https://darimiru.xyz"

    # 2. From Referer
    req_xyz_referer = make_mocked_request("POST", "/api/store/create-order", headers={"Referer": "https://darimiru.xyz/buy"})
    assert _resolve_payment_base_url(req_xyz_referer) == "https://darimiru.xyz"

    # 3. From Host
    req_xyz_host = make_mocked_request("POST", "/api/store/create-order", headers={"Host": "darimiru.xyz"})
    assert _resolve_payment_base_url(req_xyz_host) == "https://darimiru.xyz"


def test_resolve_payment_base_url_darimiru_ru():
    req_ru_origin = make_mocked_request("POST", "/api/store/create-order", headers={"Origin": "https://darimiru.ru"})
    assert _resolve_payment_base_url(req_ru_origin) == "https://darimiru.ru"

    req_ru_host = make_mocked_request("POST", "/api/store/create-order", headers={"Host": "darimiru.ru"})
    assert _resolve_payment_base_url(req_ru_host) == "https://darimiru.ru"


def test_resolve_payment_base_url_rejects_untrusted_host():
    req_evil = make_mocked_request("POST", "/api/store/create-order", headers={"Host": "evil.com", "Origin": "https://evil.com"})
    resolved = _resolve_payment_base_url(req_evil)
    assert "evil.com" not in resolved
    assert resolved == "https://darimiru.ru" or "darimiru" in resolved


def test_admin_manual_delivery_normalizes_adapt_key_to_backup_domain():
    adapt_uuid = "3093bb7e-632d-4ea9-8ffa-afbd0a6a85a9"
    sub = SimpleNamespace(
        vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{adapt_uuid}",
        client_name=f"adapt_{adapt_uuid}",
    )
    display_key = get_subscription_display_key(sub)
    assert display_key == f"https://darimiru.xyz/vpnbot/adapt-sub/{adapt_uuid}"


def test_admin_manual_delivery_preserves_vhq_and_marzban_keys():
    vhq_sub = SimpleNamespace(
        vpn_key="https://darimiru.ru/vpnbot/vhq-sub/abc-123",
        client_name="vhq_abc123",
    )
    vhq_display = get_subscription_display_key(vhq_sub)
    assert "darimiru.xyz" not in vhq_display
    assert "adapt-sub" not in vhq_display

    marzban_sub = SimpleNamespace(
        vpn_key="vless://user@1.2.3.4:443?security=reality#mykey",
        client_name="marzban_user",
    )
    assert get_subscription_display_key(marzban_sub) == "vless://user@1.2.3.4:443?security=reality#mykey"


@pytest.mark.asyncio
async def test_resolve_payment_base_url_free_or_balance_checkout_redirect():
    # Verify that redirect_url for zero-price checkout on darimiru.xyz points to darimiru.xyz
    req_xyz = make_mocked_request("POST", "/api/store/create-order", headers={"Origin": "https://darimiru.xyz"})
    base = _resolve_payment_base_url(req_xyz)
    order_id = "test_order_xyz_123"
    redirect_url = f"{base}/pay/success?order_id={order_id}"
    assert redirect_url == f"https://darimiru.xyz/pay/success?order_id={order_id}"

    # And for darimiru.ru
    req_ru = make_mocked_request("POST", "/api/store/create-order", headers={"Origin": "https://darimiru.ru"})
    base_ru = _resolve_payment_base_url(req_ru)
    assert f"{base_ru}/pay/success?order_id={order_id}" == f"https://darimiru.ru/pay/success?order_id={order_id}"
