from unittest.mock import Mock

import pytest

from webstore import telegram_notify


def test_webstore_telegram_connector_uses_configured_proxy(monkeypatch):
    connector = Mock()
    factory = Mock(return_value=connector)
    monkeypatch.setattr(telegram_notify.settings, "telegram_proxy", "socks5://proxy.example:1080")
    monkeypatch.setattr(telegram_notify, "_proxy_connector_from_url", factory)

    assert telegram_notify._telegram_connector() is connector
    factory.assert_called_once_with("socks5://proxy.example:1080")


def test_webstore_telegram_connector_uses_direct_connection_without_proxy(monkeypatch):
    connector = Mock()
    factory = Mock(return_value=connector)
    monkeypatch.setattr(telegram_notify.settings, "telegram_proxy", "")
    monkeypatch.setattr(telegram_notify.aiohttp, "TCPConnector", factory)

    assert telegram_notify._telegram_connector() is connector
    factory.assert_called_once_with()


@pytest.mark.asyncio
async def test_notification_transport_error_does_not_break_payment_flow(monkeypatch):
    monkeypatch.setattr(
        telegram_notify,
        "_telegram_connector",
        Mock(side_effect=RuntimeError("proxy unavailable")),
    )

    delivered = await telegram_notify.send_telegram_notifications(
        "123:token",
        [1001, 1002],
        "Оплата получена",
    )

    assert delivered == 0
