from __future__ import annotations

from pathlib import Path

import pytest

from bot.config import settings as bot_settings
from bot.models import Server
from bot.services import proxy_manager


class _FakeMarzbanAPI:
    def __init__(self, server):
        self.server = server

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def get_user(self, username: str):
        return {"subscription_url": "/sub/test-token"}


@pytest.mark.asyncio
async def test_get_subscription_link_rewrites_to_instance_domain_and_path(monkeypatch):
    server = Server(name="NL", host="192.0.2.10", api_url="https://panel.example.com:8443")
    monkeypatch.setattr(proxy_manager, "MarzbanAPI", _FakeMarzbanAPI)
    monkeypatch.setattr(bot_settings, "subscription_sub_path", "s")
    monkeypatch.setattr(bot_settings, "subscription_base_url", "https://loonapie.xyz")

    link = await proxy_manager.get_subscription_link(server, "tg1_1")

    assert link == "https://loonapie.xyz/s/test-token"


@pytest.mark.asyncio
async def test_get_subscription_link_keeps_default_sub_path_when_not_overridden(monkeypatch):
    server = Server(name="NL2", host="198.51.100.20", api_url="https://panel.example.com:8443")
    monkeypatch.setattr(proxy_manager, "MarzbanAPI", _FakeMarzbanAPI)
    monkeypatch.setattr(bot_settings, "subscription_sub_path", "")
    monkeypatch.setattr(bot_settings, "subscription_base_url", "https://darimiru.ru")

    link = await proxy_manager.get_subscription_link(server, "tg1_1")

    assert link == "https://darimiru.ru/sub/test-token"


def test_webstore_templates_have_no_hardcoded_darimiru_branding():
    templates_dir = Path(__file__).resolve().parents[1] / "webstore" / "templates"
    checked = ["store.html", "profile.html", "success.html"]

    for template_name in checked:
        html = (templates_dir / template_name).read_text(encoding="utf-8")
        assert "@darimiru_bot" not in html
        assert "https://darimiru.ru" not in html
        assert "darimiru_support" not in html
