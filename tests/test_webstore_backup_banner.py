import pytest
from aiohttp import web
from pathlib import Path
from bs4 import BeautifulSoup

from webstore.routes import handle_store_page


@pytest.mark.asyncio
async def test_webstore_homepage_contains_backup_domain_banner_and_modal():
    template_path = Path(__file__).resolve().parents[1] / "webstore" / "templates" / "store.html"
    content = template_path.read_text(encoding="utf-8")

    soup = BeautifulSoup(content, "html.parser")

    # 1. Banner exists and has correct attributes
    banner = soup.find(id="backupDomainBanner")
    assert banner is not None, "Backup domain banner element #backupDomainBanner missing"
    assert "backup-domain-banner" in banner.get("class", [])
    assert banner.get("role") == "region"

    # 2. Placement: immediately below topbar, above hero
    page = soup.find("div", class_="page")
    assert page is not None
    children = [child for child in page.children if child.name]
    topbar_idx = next(i for i, c in enumerate(children) if "topbar" in c.get("class", []))
    banner_idx = next(i for i, c in enumerate(children) if c.get("id") == "backupDomainBanner")
    hero_idx = next(i for i, c in enumerate(children) if "hero" in c.get("class", []))
    assert topbar_idx < banner_idx < hero_idx, "Banner must be placed between topbar and hero"

    # 3. Required banner text and elements
    banner_text = banner.get_text()
    assert "Резервная ссылка для HAPP / INCY" in banner_text
    assert "Если основная ссылка не работает или не обновляется, воспользуйтесь альтернативной." in banner_text

    cta_btn = banner.find(id="backupBannerCta")
    assert cta_btn is not None
    assert "Получить резервную ссылку" in cta_btn.get_text()

    close_btn = banner.find(id="backupBannerClose")
    assert close_btn is not None
    assert close_btn.get_text().strip() == "✕"

    # 4. Modal elements exist
    modal_overlay = soup.find(id="backupLinkOverlay")
    assert modal_overlay is not None
    modal_close = modal_overlay.find(id="backupLinkModalClose")
    assert modal_close is not None

    loading_box = modal_overlay.find(id="backupLoading")
    assert loading_box is not None

    content_box = modal_overlay.find(id="backupContent")
    assert content_box is not None
    assert modal_overlay.find(id="backupList") is not None

    empty_box = modal_overlay.find(id="backupEmpty")
    assert empty_box is not None
    assert "Активных подписок не найдено" in empty_box.get_text()

    error_box = modal_overlay.find(id="backupError")
    assert error_box is not None

    # 5. Script logic verified
    assert "darimiru_backup_banner_dismissed_v1" in content
    assert "initBackupDomainBanner" in content
    assert "dismissBackupDomainBanner" in content
    assert "openBackupLinkModal" in content
    assert "copyBackupUrl" in content
    assert "https://darimiru.xyz/vpnbot/adapt-sub/" in content


@pytest.mark.asyncio
async def test_handle_store_page_serves_banner_in_html(monkeypatch):
    from unittest.mock import MagicMock
    from aiohttp.test_utils import make_mocked_request
    from webstore.models import WebOrder
    from webstore.routes import _display_order_subscription_url

    req = make_mocked_request("GET", "/buy")
    resp = await handle_store_page(req)
    assert resp.status == 200
    assert resp.content_type == "text/html"
    body = resp.text
    assert "backupDomainBanner" in body
    assert "backupLinkOverlay" in body
    assert "Резервная ссылка для HAPP / INCY" in body
    assert "darimiru.xyz" in body

    # Verify Adapt web order URL display normalization
    adapt_order = WebOrder(
        order_id="test_ord_1",
        status="delivered",
        subscription_url="https://darimiru.ru/vpnbot/adapt-sub/11111111-2222-3333-4444-555555555555",
        marzban_username="adapt_11111111-2222-3333-4444-555555555555",
    )
    displayed = _display_order_subscription_url(adapt_order)
    assert displayed == "https://darimiru.xyz/vpnbot/adapt-sub/11111111-2222-3333-4444-555555555555"
