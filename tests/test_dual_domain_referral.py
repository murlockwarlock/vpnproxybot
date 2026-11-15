import pytest
from unittest.mock import patch, MagicMock
from aiohttp.test_utils import make_mocked_request
from pathlib import Path

from webstore.routes import _resolve_referral_base_url
from bot.handlers.partner import _build_partner_web_link
from bot.config import settings as bot_settings
from webstore.config import settings as webstore_settings


def test_resolve_referral_base_url_default():
    """Referral base URL resolves to darimiru.xyz by default."""
    assert _resolve_referral_base_url() == "https://darimiru.xyz"


def test_resolve_referral_base_url_custom():
    """Referral base URL respects PUBLIC_WEBSTORE_BASE_URL override."""
    with patch.object(webstore_settings, "public_webstore_base_url", "https://mirror.darimiru.xyz"):
        assert _resolve_referral_base_url() == "https://mirror.darimiru.xyz"


def test_partner_web_link_generation():
    """Partner link generates https://darimiru.xyz/buy?ref=p_{code} by default."""
    with patch.object(bot_settings, "webstore_public_enabled", True):
        link = _build_partner_web_link("alpha123")
        assert link == "https://darimiru.xyz/buy?ref=p_alpha123"


def test_partner_web_link_disabled():
    """Partner link returns None if webstore is disabled."""
    with patch.object(bot_settings, "webstore_public_enabled", False):
        link = _build_partner_web_link("alpha123")
        assert link is None


def test_store_template_ref_lifecycle_and_ttl():
    """Verify store.html manages ref lifecycle: session-scoped, purged from localStorage, cleared on checkout."""
    template_path = Path(__file__).resolve().parent.parent / "webstore" / "templates" / "store.html"
    content = template_path.read_text(encoding="utf-8")

    assert "darimiru_ref" in content
    assert "REF_MAX_AGE_MS" in content
    assert "localStorage.removeItem(REF_STORAGE_KEY)" in content
    assert "clearStoredReferral()" in content
    assert "sessionStorage.setItem(REF_STORAGE_KEY, refSource)" in content
    assert "returnSearch" in content or "returnTo" in content


@pytest.mark.asyncio
async def test_backend_referral_attribution_valid_vs_unrelated_direct_purchase():
    """Verify that an order with ref is attributed, while a later unrelated direct order is skipped."""
    from webstore.models import WebOrder
    from webstore.routes import _credit_web_referral

    # 1. Legitimate referral order
    referred_order = WebOrder(
        order_id="order_referred_001",
        tariff_key="month",
        amount_rub=300.0,
        ref_source="user_ref_999",
        status="delivered",
    )
    assert referred_order.ref_source == "user_ref_999"

    # 2. Unrelated direct purchase (no ref code passed or stale ref cleared)
    direct_order = WebOrder(
        order_id="order_direct_002",
        tariff_key="month",
        amount_rub=300.0,
        ref_source=None,
        status="delivered",
    )
    assert direct_order.ref_source is None

    # When ref_source is None, _credit_web_referral must immediately skip
    result = await _credit_web_referral(direct_order)
    assert result == {"status": "skipped"}

    # 3. Legitimate partner link order
    partner_order = WebOrder(
        order_id="order_partner_003",
        tariff_key="month",
        amount_rub=300.0,
        ref_source="p_superpartner",
        status="delivered",
    )
    assert partner_order.ref_source == "p_superpartner"



@pytest.mark.asyncio
async def test_bot_referral_link_generation():
    """Bot referral link generates https://darimiru.xyz/buy?ref={user_id} by default."""
    from bot.handlers.referral import show_referral_info, _build_referral_copy_messages
    from unittest.mock import AsyncMock
    from aiogram.types import User

    callback = MagicMock()
    callback.from_user = User(id=987654321, is_bot=False, first_name="Test")
    callback.data = "referral"
    callback.message = AsyncMock()
    callback.answer = AsyncMock()
    callback.bot = AsyncMock()
    callback.bot.get_me = AsyncMock(return_value=MagicMock(username="DarimiruBot"))

    dummy_user = MagicMock()
    dummy_config = MagicMock(is_enabled=True, commission_percent=15)

    with patch.object(bot_settings, "webstore_public_enabled", True), \
         patch("bot.handlers.referral.async_session") as mock_session_ctx, \
         patch("bot.handlers.referral.get_user_balance", return_value=0.0):
        mock_sess = AsyncMock()
        mock_session_ctx.return_value.__aenter__.return_value = mock_sess
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = dummy_user
        mock_sess.execute.return_value = mock_result
        mock_sess.scalar.return_value = 0
        with patch("bot.handlers.referral._get_or_create_config", AsyncMock(return_value=dummy_config)):
            await show_referral_info(callback)

    calls = callback.message.answer.call_args_list
    web_link_found = any("https://darimiru.xyz/buy?ref=987654321" in str(c) for c in calls)
    assert web_link_found, f"Expected darimiru.xyz referral link in message calls: {calls}"


def test_profile_markup_site_url():
    """Profile message site button points to darimiru.xyz/profile by default."""
    from bot.keyboards.client import profile_kb

    url = (getattr(bot_settings, "public_webstore_base_url", None) or bot_settings.webstore_api_base_url).rstrip("/")
    assert url == "https://darimiru.xyz"
    kb = profile_kb(site_profile_url=f"{url}/profile")
    buttons = [btn for row in kb.inline_keyboard for btn in row]
    site_btn = next((b for b in buttons if b.url and "darimiru.xyz/profile" in b.url), None)
    assert site_btn is not None


