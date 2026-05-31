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
