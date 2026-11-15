"""Tests for adapt_subscription_proxy.py."""

from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from bot.services.adapt_subscription_proxy import (
    _ADAPT_UPSTREAM_BASE,
    build_adapt_mirror_url,
)


# ── build_adapt_mirror_url ────────────────────────────────────────────────────

def test_build_adapt_mirror_url_with_settings(tmp_path):
    """Should produce branded URL when settings are configured."""
    with patch("bot.services.adapt_subscription_proxy.settings") as mock_settings:
        mock_settings.base_webhook_url = "https://darimiru.ru"
        mock_settings.subscription_base_url = "https://darimiru.ru"
        mock_settings.webhook_path_prefix = "/vpnbot"
        uuid = "my-adapt-uuid"
        url = build_adapt_mirror_url(uuid)
    assert url == "https://darimiru.ru/vpnbot/adapt-sub/my-adapt-uuid"


def test_build_adapt_mirror_url_empty_base():
    """Falls back to upstream URL when base is not configured."""
    with patch("bot.services.adapt_subscription_proxy.settings") as mock_settings:
        mock_settings.base_webhook_url = ""
        mock_settings.subscription_base_url = ""
        mock_settings.webhook_path_prefix = ""
        uuid = "my-adapt-uuid"
        url = build_adapt_mirror_url(uuid)
    assert url == f"{_ADAPT_UPSTREAM_BASE}/my-adapt-uuid"


def test_build_adapt_mirror_url_empty_uuid():
    """Falls back when UUID is empty."""
    with patch("bot.services.adapt_subscription_proxy.settings") as mock_settings:
        mock_settings.base_webhook_url = "https://darimiru.ru"
        mock_settings.subscription_base_url = "https://darimiru.ru"
        mock_settings.webhook_path_prefix = "/vpnbot"
        url = build_adapt_mirror_url("")
    assert url == f"{_ADAPT_UPSTREAM_BASE}/"


def test_build_adapt_mirror_url_no_prefix():
    """Works without a prefix."""
    with patch("bot.services.adapt_subscription_proxy.settings") as mock_settings:
        mock_settings.base_webhook_url = "https://darimiru.ru"
        mock_settings.subscription_base_url = "https://darimiru.ru"
        mock_settings.webhook_path_prefix = ""
        url = build_adapt_mirror_url("uuid-123")
    assert url == "https://darimiru.ru/adapt-sub/uuid-123"


def test_build_adapt_mirror_url_base_already_includes_prefix():
    """Avoids double prefix insertion."""
    with patch("bot.services.adapt_subscription_proxy.settings") as mock_settings:
        mock_settings.base_webhook_url = "https://darimiru.ru/vpnbot"
        mock_settings.subscription_base_url = "https://darimiru.ru"
        mock_settings.webhook_path_prefix = "/vpnbot"
        url = build_adapt_mirror_url("uuid-456")
    # base already ends with /vpnbot, so prefix_clean should not be appended again
    assert url == "https://darimiru.ru/vpnbot/adapt-sub/uuid-456"
    assert url.count("/vpnbot") == 1


# ── inject_adapt_devices_manager ──────────────────────────────────────────────

def test_inject_adapt_devices_manager_with_devices():
    from bot.services.adapt_subscription_proxy import inject_adapt_devices_manager

    base_html = """<!DOCTYPE html>
<html>
<head>
<style>
body { background: #000; }
</style>
</head>
<body>
<div class="main">
    <div class="card">
        <div class="card-label">Устройства</div>
        <div class="devices-row">
            <span class="devices-count">1</span>
        </div>
    </div>
    <div class="card">Happ</div>
</div>
<div class="footer"></div>
</body>
</html>""".encode("utf-8")

    devices = [
        {
            "id": 42705,
            "name": "SM-A546E",
            "device_os": "Android",
            "os_version": "15",
            "ip_address": "127.0.0.1",
            "first_seen": "2026-09-26T06:13:00.784446",
            "last_seen": "2026-09-26T07:13:53.753405",
        }
    ]

    out_bytes = inject_adapt_devices_manager(base_html, "test-uuid-1234", devices)
    out_html = out_bytes.decode("utf-8")

    assert "devices-manager-card" in out_html
    assert "dev-header" in out_html
    assert "Устройства" in out_html
    assert "SM-A546E" in out_html
    assert "Android • 15 • 127.0.0.1" in out_html
    assert "Первый вход:" in out_html
    assert "Последний вход:" in out_html
    assert "promptDeleteDev(42705" in out_html
    assert "devModalOverlay" in out_html
    assert "/devices/delete" in out_html
    assert "quick-actions-card" in out_html
    assert "Продлить подписку" in out_html
    assert "Привязать Telegram" in out_html
    assert "quickReferralAuthenticated" in out_html
    assert "quickReferralAnonymous" in out_html
    assert out_html.index("quick-actions-card") < out_html.index('card-label">Устройства')
    assert "/renew?key=test-uuid-1234" in out_html


def test_inject_adapt_devices_manager_empty():
    from bot.services.adapt_subscription_proxy import inject_adapt_devices_manager

    base_html = """<!DOCTYPE html><html><head><style></style></head><body>
<div class="main">
    <div class="card">
        <div class="card-label">Устройства</div>
        <div>0</div>
    </div>
</div>
<div class="footer"></div>
</body></html>""".encode("utf-8")

    out_bytes = inject_adapt_devices_manager(base_html, "test-uuid-empty", [])
    out_html = out_bytes.decode("utf-8")

    assert "devices-manager-card" in out_html
    assert "Нет подключенных устройств" in out_html
    assert "dev-count-pill\">0</span>" in out_html
    assert out_html.index("quick-actions-card") < out_html.index('card-label">Устройства')
