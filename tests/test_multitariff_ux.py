"""Tests for multi-tariff UX: renewal choice, daily charge tariff, referral block in profile."""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from sqlalchemy.ext.asyncio import AsyncSession


# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_tariff(
    tid: int,
    label: str,
    price: float = 249.0,
    days: int = 30,
    is_active: bool = True,
    adapt_plan_uuid: str | None = None,
):
    t = MagicMock()
    t.id = tid
    t.label = label
    t.price_rub = price
    t.days = days
    t.is_active = is_active
    t.is_admin_only = False
    t.adapt_plan_uuid = adapt_plan_uuid
    return t


def _make_subscription(
    sid: int,
    tariff_id: int | None,
    client_name: str = "tg123_1",
    status: str = "active",
    expires_days: int = 15,
):
    sub = MagicMock()
    sub.id = sid
    sub.tariff_id = tariff_id
    sub.client_name = client_name
    sub.status = MagicMock(value=status)
    sub.expires_at = datetime.utcnow() + timedelta(days=expires_days)
    return sub


def _make_user(uid: int = 1, daily_charge_tariff_id: int | None = None, referred_by: int | None = None):
    u = MagicMock()
    u.id = uid
    u.telegram_id = 100000 + uid
    u.daily_charge_tariff_id = daily_charge_tariff_id
    u.referral_balance = 0.0
    u.referred_by = referred_by
    u.balance_mode_enabled = False
    u.balance_autodebit_enabled = False
    return u


# ── renewal_choice_kb ─────────────────────────────────────────────────────────

class TestRenewalChoiceKb:
    def test_empty_options_returns_markup_with_buy_button(self):
        from bot.keyboards.client import renewal_choice_kb
        kb = renewal_choice_kb([])
        # Should still return an InlineKeyboardMarkup
        assert kb is not None
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [b.callback_data for b in all_buttons]
        assert "buy" in callbacks or any("back" in c for c in callbacks)

    def test_single_option_shows_tariff_button(self):
        from bot.keyboards.client import renewal_choice_kb
        kb = renewal_choice_kb([(1, "Лайт", 4.17)])
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [b.callback_data for b in all_buttons]
        assert "renew_tariff_1" in callbacks

    def test_multiple_options_shows_all_tariff_buttons(self):
        from bot.keyboards.client import renewal_choice_kb
        options = [(1, "Лайт", 4.17), (2, "Базовый", 8.30)]
        kb = renewal_choice_kb(options)
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [b.callback_data for b in all_buttons]
        assert "renew_tariff_1" in callbacks
        assert "renew_tariff_2" in callbacks

    def test_new_tariff_button_present(self):
        from bot.keyboards.client import renewal_choice_kb
        kb = renewal_choice_kb([(1, "Лайт", 4.17)])
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        texts = [b.text for b in all_buttons]
        assert any("Новый" in t or "Купить" in t for t in texts)

    def test_back_button_present(self):
        from bot.keyboards.client import renewal_choice_kb
        kb = renewal_choice_kb([(1, "Лайт", 4.17)])
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [b.callback_data for b in all_buttons]
        assert any(c in ("profile", "back_main") for c in callbacks)


# ── profile_kb with renewal_options ──────────────────────────────────────────

class TestProfileKbRenewalOptions:
    def test_profile_kb_single_tariff_uses_buy_callback(self):
        from bot.keyboards.client import profile_kb
        kb = profile_kb(renewal_options=[(1, "Лайт", 4.17)])
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [b.callback_data for b in all_buttons if b.callback_data]
        assert "buy" in callbacks
        assert "renewal_choice" not in callbacks

    def test_profile_kb_multiple_tariffs_uses_purchase_action_callback(self):
        from bot.keyboards.client import profile_kb
        options = [(1, "Лайт", 4.17), (2, "Базовый", 8.30)]
        kb = profile_kb(renewal_options=options, has_daily_charge_tariff_choice=True)
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [b.callback_data for b in all_buttons if b.callback_data]
        assert "buy" in callbacks
        assert "renewal_choice" not in callbacks

    def test_profile_kb_daily_charge_button_when_multiple_tariffs(self):
        from bot.keyboards.client import profile_kb
        options = [(1, "Лайт", 4.17), (2, "Базовый", 8.30)]
        kb = profile_kb(
            renewal_options=options,
            has_daily_charge_tariff_choice=True,
            balance_mode_enabled=True,
        )
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [b.callback_data for b in all_buttons if b.callback_data]
        assert "daily_charge_tariff_choice" in callbacks

    def test_profile_kb_no_daily_charge_button_when_single_tariff(self):
        from bot.keyboards.client import profile_kb
        kb = profile_kb(renewal_options=[(1, "Лайт", 4.17)], has_daily_charge_tariff_choice=False)
        all_buttons = [btn for row in kb.inline_keyboard for btn in row]
        callbacks = [b.callback_data for b in all_buttons if b.callback_data]
        assert "daily_charge_tariff_choice" not in callbacks


# ── _build_renewal_options ────────────────────────────────────────────────────

class TestBuildRenewalOptions:
    @pytest.mark.asyncio
    async def test_returns_empty_for_user_with_no_subs(self):
        from bot.handlers.profile import _build_renewal_options

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[]))))
        )

        user = _make_user()
        result = await _build_renewal_options(session, user)
        assert result == []

    @pytest.mark.asyncio
    async def test_deduplicates_same_tariff_id(self):
        from bot.handlers.profile import _build_renewal_options

        tariff = _make_tariff(1, "Лайт")
        subs = [
            _make_subscription(1, tariff_id=1),
            _make_subscription(2, tariff_id=1),
        ]

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=subs))))
        )
        session.get = AsyncMock(return_value=tariff)

        user = _make_user()
        result = await _build_renewal_options(session, user)
        assert len(result) == 1
        assert result[0][0] == 1

    @pytest.mark.asyncio
    async def test_returns_multiple_tariff_options(self):
        from bot.handlers.profile import _build_renewal_options

        tariff1 = _make_tariff(1, "Лайт", price=95.0, days=30)
        tariff2 = _make_tariff(2, "Базовый", price=249.0, days=30)
        subs = [
            _make_subscription(1, tariff_id=1),
            _make_subscription(2, tariff_id=2, client_name="adapt_abc"),
        ]

        call_count = [0]

        async def get_tariff(model, tid):
            call_count[0] += 1
            return tariff1 if tid == 1 else tariff2

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=subs))))
        )
        session.get = get_tariff

        user = _make_user()
        result = await _build_renewal_options(session, user)
        assert len(result) == 2
        tariff_ids = {r[0] for r in result}
        assert 1 in tariff_ids
        assert 2 in tariff_ids

    @pytest.mark.asyncio
    async def test_excludes_inactive_tariffs(self):
        from bot.handlers.profile import _build_renewal_options

        inactive_tariff = _make_tariff(5, "Старый", is_active=False)
        subs = [_make_subscription(1, tariff_id=5)]

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=subs))))
        )
        session.get = AsyncMock(return_value=inactive_tariff)

        user = _make_user()
        result = await _build_renewal_options(session, user)
        assert result == []

    @pytest.mark.asyncio
    async def test_daily_rate_calculated_correctly(self):
        from bot.handlers.profile import _build_renewal_options

        tariff = _make_tariff(1, "Базовый", price=249.0, days=30)
        subs = [_make_subscription(1, tariff_id=1)]

        session = AsyncMock(spec=AsyncSession)
        session.execute = AsyncMock(
            return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=subs))))
        )
        session.get = AsyncMock(return_value=tariff)

        user = _make_user()
        result = await _build_renewal_options(session, user)
        assert len(result) == 1
        tid, label, rate = result[0]
        assert label == "Базовый"
        assert abs(rate - round(249.0 / 30, 2)) < 0.01


# ── _provider_label ───────────────────────────────────────────────────────────

class TestProviderLabel:
    def test_adapt_subscription_returns_bazoviy(self):
        from bot.handlers.profile import _provider_label
        sub = MagicMock()
        with patch("bot.handlers.profile.is_adapt_subscription", return_value=True), \
             patch("bot.handlers.profile.is_vhq_subscription", return_value=False):
            label = _provider_label(sub)
        assert label == "Базовый"

    def test_vhq_subscription_uses_public_label(self):
        from bot.handlers.profile import _provider_label
        sub = MagicMock()
        with patch("bot.handlers.profile.is_adapt_subscription", return_value=False), \
             patch("bot.handlers.profile.is_vhq_subscription", return_value=True):
            label = _provider_label(sub)
        assert label == "Премиум"

    def test_marzban_subscription_returns_lait(self):
        from bot.handlers.profile import _provider_label
        sub = MagicMock()
        with patch("bot.handlers.profile.is_adapt_subscription", return_value=False), \
             patch("bot.handlers.profile.is_vhq_subscription", return_value=False):
            label = _provider_label(sub)
        assert label == "Лайт"


# ── renew_tariff_ callback handler in buy.py ──────────────────────────────────

class TestRenewTariffHandler:
    @pytest.mark.asyncio
    async def test_renew_tariff_unknown_id_answers_alert(self):
        from bot.handlers.buy import renew_tariff

        callback = AsyncMock()
        callback.data = "renew_tariff_9999"
        callback.from_user.id = 100

        with patch("bot.handlers.buy.async_session") as mock_session_ctx:
            mock_sess = AsyncMock()
            mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
            mock_sess.__aexit__ = AsyncMock(return_value=False)
            mock_sess.get = AsyncMock(return_value=None)  # tariff not found
            mock_sess.scalar = AsyncMock(return_value=None)
            mock_session_ctx.return_value = mock_sess

            await renew_tariff(callback)

        callback.answer.assert_called_once_with("Тариф не найден", show_alert=True)

    @pytest.mark.asyncio
    async def test_renew_tariff_inactive_tariff_answers_alert(self):
        from bot.handlers.buy import renew_tariff

        tariff = _make_tariff(3, "Старый", is_active=False)
        callback = AsyncMock()
        callback.data = "renew_tariff_3"
        callback.from_user.id = 100

        with patch("bot.handlers.buy.async_session") as mock_session_ctx:
            mock_sess = AsyncMock()
            mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
            mock_sess.__aexit__ = AsyncMock(return_value=False)
            mock_sess.get = AsyncMock(return_value=tariff)
            mock_sess.scalar = AsyncMock(return_value=_make_user())
            mock_session_ctx.return_value = mock_sess

            await renew_tariff(callback)

        callback.answer.assert_called_once_with("Этот тариф больше недоступен", show_alert=True)


# ── set_daily_tariff_ callback handler in profile.py ─────────────────────────

class TestSetDailyTariffHandler:
    @pytest.mark.asyncio
    async def test_set_daily_tariff_saves_tariff_id(self):
        from bot.handlers.profile import set_daily_tariff

        tariff = _make_tariff(2, "Базовый")
        user = _make_user(daily_charge_tariff_id=None)

        callback = AsyncMock()
        callback.data = "set_daily_tariff_2"
        callback.from_user.id = user.telegram_id

        with patch("bot.handlers.profile.async_session") as mock_session_ctx, \
             patch("bot.handlers.profile.show_profile", new_callable=AsyncMock) as mock_show:
            mock_sess = AsyncMock()
            mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
            mock_sess.__aexit__ = AsyncMock(return_value=False)
            mock_sess.scalar = AsyncMock(return_value=user)
            mock_sess.get = AsyncMock(return_value=tariff)
            mock_sess.commit = AsyncMock()
            mock_session_ctx.return_value = mock_sess

            await set_daily_tariff(callback)

        assert user.daily_charge_tariff_id == 2
        mock_sess.commit.assert_called_once()
        callback.answer.assert_called_once()

    @pytest.mark.asyncio
    async def test_set_daily_tariff_inactive_shows_alert(self):
        from bot.handlers.profile import set_daily_tariff

        tariff = _make_tariff(5, "Старый", is_active=False)
        user = _make_user()

        callback = AsyncMock()
        callback.data = "set_daily_tariff_5"
        callback.from_user.id = user.telegram_id

        with patch("bot.handlers.profile.async_session") as mock_session_ctx:
            mock_sess = AsyncMock()
            mock_sess.__aenter__ = AsyncMock(return_value=mock_sess)
            mock_sess.__aexit__ = AsyncMock(return_value=False)
            mock_sess.scalar = AsyncMock(return_value=user)
            mock_sess.get = AsyncMock(return_value=tariff)
            mock_session_ctx.return_value = mock_sess

            await set_daily_tariff(callback)

        callback.answer.assert_called_once_with("Тариф недоступен", show_alert=True)


# ── _format_proxy_links is importable ────────────────────────────────────────

class TestFormatProxyLinksImport:
    def test_import_succeeds(self):
        from bot.services.subscription_service import _format_proxy_links
        assert callable(_format_proxy_links)

    def test_empty_secret_returns_empty_string(self):
        from bot.services.subscription_service import _format_proxy_links
        with patch("bot.services.subscription_service._format_proxy_links.__module__", create=True):
            pass  # just test it doesn't raise on import

        with patch("bot.services.mtproto_manager.build_all_proxy_links", return_value=[]):
            result = _format_proxy_links("abc123secret")
        assert result == ""

    def test_links_formatted_as_html(self):
        from bot.services.subscription_service import _format_proxy_links
        links = [("🇳🇱 NL", "https://t.me/proxy?server=1.2.3.4&port=443&secret=abc")]
        with patch("bot.services.mtproto_manager.build_all_proxy_links", return_value=links):
            result = _format_proxy_links("abc123secret")
        assert '<a href=' in result
        assert "🇳🇱 NL" in result
