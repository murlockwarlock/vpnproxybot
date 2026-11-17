"""Targeted regression tests for admin and webstore client search by links/UUIDs/tokens.

Covers end-to-end search behavior across `bot/handlers/admin.py` and `webstore/routes.py`:
1. New (`darimiru.xyz`) and old (`darimiru.ru`) Adapt mirror URLs.
2. Original Adapt supplier URLs (`network-api.adaptgroup.app/sub/<uuid>`).
3. VHQ links: `/vhq-sub/<token>` (subscription ref, web order ref, upstream mirror) and original supplier URLs.
4. Strict case sensitivity of VHQ base64 tokens and upstream keys.
5. Pure UUIDs (lower/upper), `adapt_<uuid>`, and URLs with query parameters, fragments, or trailing `/`.
6. Zero false positives (prefix extensions, generic/truncated URLs, shared proxy host with different secret).
7. Clean handling of 0 matches and ambiguous (multi-client) results without crashing or picking wrong user.
8. Preservation of admin & internal bridge secret authorization in bot and webstore.
9. Non-regression check for `docs/adapt-openapi.json` and `AdaptAPI.upgrade_subscription`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from aiohttp.test_utils import make_mocked_request
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from bot.handlers import admin as admin_handler
from bot.models import (
    AdaptSubscription,
    Base as BotBase,
    Platform,
    ProxyAccount,
    Server,
    SubStatus,
    Subscription,
    User,
)
from bot.services.adapt_api import AdaptAPI
from bot.services.vhq_subscription_proxy import (
    build_vhq_mirror_url as build_bot_vhq_mirror_url,
    build_vhq_subscription_ref_url,
)
from webstore import routes as webstore_routes
from webstore.models import (
    Base as WebBase,
    WebAccount,
    WebOrder,
    WebProfileLink,
    WebTelegramItem,
)
from webstore.vhq_proxy import (
    build_vhq_mirror_url as build_web_vhq_mirror_url,
    build_vhq_web_order_ref_url,
)

pytestmark = pytest.mark.asyncio

TEST_BRIDGE_SECRET = "test-internal-bridge-secret-search-4"
TEST_VHQ_SECRET = "test-vhq-proxy-secret-search-4"

UUID_CLIENT_A = "07dfbbe7-2b62-4cad-9a78-9fdacc9d8e4a"
UUID_CLIENT_B = "11111111-2222-4333-8444-555555555555"
UUID_MISSING = "99999999-8888-4777-8666-555555555555"


class _DummyState:
    def __init__(self) -> None:
        self.data: dict = {}
        self.current_state = None
        self.cleared = False

    async def update_data(self, **kwargs) -> None:
        self.data.update(kwargs)

    async def get_data(self) -> dict:
        return dict(self.data)

    async def set_state(self, state) -> None:
        self.current_state = state

    async def clear(self) -> None:
        self.cleared = True
        self.current_state = None
        self.data.clear()


def _make_message(text: str, user_id: int = 7001):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        text=text,
        answer=AsyncMock(),
    )


def _make_callback(data: str, user_id: int = 7001):
    return SimpleNamespace(
        from_user=SimpleNamespace(id=user_id),
        data=data,
        message=SimpleNamespace(edit_text=AsyncMock(), answer=AsyncMock()),
        answer=AsyncMock(),
    )


def _flip_first_alpha_case(s: str) -> str:
    chars = list(s)
    for idx, ch in enumerate(chars):
        if ch.isalpha():
            chars[idx] = ch.lower() if ch.isupper() else ch.upper()
            return "".join(chars)
    raise AssertionError(f"No alphabetic character to flip in {s!r}")


@pytest_asyncio.fixture
async def search_env(monkeypatch):
    bot_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    web_engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    async with bot_engine.begin() as conn:
        await conn.run_sync(BotBase.metadata.create_all)
    async with web_engine.begin() as conn:
        await conn.run_sync(WebBase.metadata.create_all)

    bot_session_factory = async_sessionmaker(bot_engine, expire_on_commit=False)
    web_session_factory = async_sessionmaker(web_engine, expire_on_commit=False)

    monkeypatch.setattr(admin_handler, "async_session", bot_session_factory)
    monkeypatch.setattr("bot.services.admin_audit.async_session", bot_session_factory)
    monkeypatch.setattr(webstore_routes, "async_session", web_session_factory)

    monkeypatch.setattr(admin_handler.settings, "admin_ids", [7001])
    monkeypatch.setattr(admin_handler.settings, "webstore_bridge_secret", TEST_BRIDGE_SECRET)
    monkeypatch.setattr(admin_handler.settings, "vhq_subscription_proxy_secret", TEST_VHQ_SECRET, raising=False)
    monkeypatch.setattr(admin_handler.settings, "webstore_public_enabled", True)

    monkeypatch.setattr(webstore_routes.settings, "bridge_shared_secret", TEST_BRIDGE_SECRET)
    monkeypatch.setattr(webstore_routes.settings, "vhq_subscription_proxy_secret", TEST_VHQ_SECRET)

    # Wire bot -> webstore lookup directly in-memory (no network I/O)
    async def _inprocess_fetch_webstore_profiles(query: str) -> list[dict]:
        from urllib.parse import urlencode
        req = make_mocked_request(
            "GET",
            f"/api/store/internal/admin-client-lookup?{urlencode({'q': query})}",
            headers={"X-Internal-Secret": TEST_BRIDGE_SECRET},
        )
        resp = await webstore_routes.handle_internal_admin_client_lookup(req)
        if resp.status == 404:
            return []
        payload = json.loads(resp.text)
        if resp.status != 200:
            raise RuntimeError(str(payload.get("error") or resp.status))
        return list(payload.get("profiles") or [])

    monkeypatch.setattr(admin_handler, "_fetch_webstore_search_profiles", _inprocess_fetch_webstore_profiles)

    now = datetime.utcnow()
    async with bot_session_factory() as s:
        srv = Server(id=1, name="NL-1", host="127.0.0.1", location="NL", is_active=True)

        # User 1: Adapt client whose DB vpn_key still holds old darimiru.ru domain
        u_adapt_a = User(id=1, telegram_id=109049579, username="adapt_friend", full_name="Adapt Friend")
        sub_adapt_a = Subscription(
            id=162,
            user_id=1,
            server_id=1,
            tariff_months=12,
            client_name=f"adapt_{UUID_CLIENT_A}",
            vpn_key=f"https://darimiru.ru/vpnbot/adapt-sub/{UUID_CLIENT_A}",
            platform=Platform.ANDROID,
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=365),
        )
        asub_a = AdaptSubscription(
            subscription_id=162,
            adapt_uuid=UUID_CLIENT_A,
            adapt_plan_uuid="plan-1y-1d",
        )

        # User 2: Second Adapt client (stored with original network-api URL)
        u_adapt_b = User(id=2, telegram_id=202020202, username="adapt_other", full_name="Adapt Other")
        sub_adapt_b = Subscription(
            id=163,
            user_id=2,
            server_id=1,
            tariff_months=1,
            client_name=f"adapt_{UUID_CLIENT_B}",
            vpn_key=f"https://network-api.adaptgroup.app/sub/{UUID_CLIENT_B}",
            platform=Platform.IOS,
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=30),
        )
        asub_b = AdaptSubscription(
            subscription_id=163,
            adapt_uuid=UUID_CLIENT_B,
            adapt_plan_uuid="plan-1m-3d",
        )

        # User 3 & User 4: VHQ clients with case-distinct keys + prefix-related keys
        u_vhq_upper = User(id=3, telegram_id=303030303, username="vhq_upper", full_name="VHQ Upper")
        sub_vhq_upper = Subscription(
            id=201,
            user_id=3,
            server_id=1,
            tariff_months=1,
            client_name="vhq_ord_10",
            vhq_order_id="ord_10",
            vpn_key="https://sub.vhq-connect.xyz/KeyAbCdEf100",
            platform=Platform.ANDROID,
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=30),
        )

        u_vhq_lower = User(id=4, telegram_id=404040404, username="vhq_lower", full_name="VHQ Lower")
        sub_vhq_lower = Subscription(
            id=202,
            user_id=4,
            server_id=1,
            tariff_months=1,
            client_name="vhq_ord_109",
            vhq_order_id="ord_109",
            vpn_key="https://sub.vhq-connect.xyz/keyabcdef100",
            platform=Platform.ANDROID,
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=30),
        )

        u_vhq_prefix_ext = User(id=5, telegram_id=505050505, username="vhq_longer", full_name="VHQ Longer")
        sub_vhq_prefix_ext = Subscription(
            id=203,
            user_id=5,
            server_id=1,
            tariff_months=1,
            client_name="vhq_ord_200",
            vhq_order_id="ord_200",
            vpn_key="https://sub.vhq-connect.xyz/KeyAbCdEf1009",
            platform=Platform.ANDROID,
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=30),
        )

        # User 6 & User 7: Marzban / Proxy domain migration + query secret isolation
        u_marzban = User(id=6, telegram_id=606060606, username="marz_client", full_name="Marzban Client")
        sub_marzban = Subscription(
            id=301,
            user_id=6,
            server_id=1,
            tariff_months=1,
            client_name="mrz_606",
            vpn_key="https://darimiru.ru/sub/MrzTokAbC777",
            platform=Platform.WINDOWS,
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=30),
        )
        proxy_a = ProxyAccount(
            id=1,
            user_id=6,
            server_id=1,
            marzban_username="prx_606",
            sub_url="https://t.me/proxy?server=198.51.100.10&port=443&secret=ddaaaa1111",
        )

        u_marzban_ext = User(id=7, telegram_id=707070707, username="marz_longer", full_name="Marzban Longer")
        sub_marzban_ext = Subscription(
            id=302,
            user_id=7,
            server_id=1,
            tariff_months=1,
            client_name="mrz_707",
            vpn_key="https://darimiru.ru/sub/MrzTokAbC777999",
            platform=Platform.WINDOWS,
            status=SubStatus.ACTIVE,
            expires_at=now + timedelta(days=30),
        )
        proxy_b = ProxyAccount(
            id=2,
            user_id=7,
            server_id=1,
            marzban_username="prx_707",
            sub_url="https://t.me/proxy?server=198.51.100.10&port=443&secret=ddbbbb2222",
        )

        s.add_all([
            srv,
            u_adapt_a, sub_adapt_a, asub_a,
            u_adapt_b, sub_adapt_b, asub_b,
            u_vhq_upper, sub_vhq_upper,
            u_vhq_lower, sub_vhq_lower,
            u_vhq_prefix_ext, sub_vhq_prefix_ext,
            u_marzban, sub_marzban, proxy_a,
            u_marzban_ext, sub_marzban_ext, proxy_b,
        ])
        await s.commit()

    # Populate webstore DB with standalone web-only clients + linked items
    web_vhq_ref_url = build_vhq_web_order_ref_url(
        "WEB-VHQ-1",
        public_base_url="https://darimiru.ru",
        path_prefix="/vpnbot",
        secret=TEST_VHQ_SECRET,
    )
    async with web_session_factory() as ws:
        ws.add_all([
            WebAccount(contact="web_adapt@example.com", profile_token="tok_web_adapt_aaaa111122223333", password_hash="x"),
            WebOrder(
                order_id="WEB-ADAPT-1",
                contact="web_adapt@example.com",
                email="web_adapt@example.com",
                tariff_key="adapt_basic",
                tariff_label="Adapt 30d",
                days=30,
                amount_rub=299,
                status="delivered",
                profile_token="tok_web_adapt_aaaa111122223333",
                marzban_username=f"adapt_{UUID_CLIENT_B}",
                subscription_url=f"https://darimiru.ru/vpnbot/adapt-sub/{UUID_CLIENT_B}",
            ),
            WebAccount(contact="web_vhq_upper@example.com", profile_token="tok_web_vhq_up_bbbb111122223333", password_hash="x"),
            WebOrder(
                order_id="WEB-VHQ-1",
                contact="web_vhq_upper@example.com",
                email="web_vhq_upper@example.com",
                tariff_key="vhq_basic",
                tariff_label="VHQ Basic 30d",
                days=30,
                amount_rub=359,
                status="delivered",
                profile_token="tok_web_vhq_up_bbbb111122223333",
                subscription_url=web_vhq_ref_url,
                vhq_order_id="web_vhq_ord_55",
                vhq_upstream_url="https://sub.vhq-connect.xyz/WebVhqKeyAbC55",
            ),
            WebAccount(contact="web_vhq_lower@example.com", profile_token="tok_web_vhq_lo_cccc111122223333", password_hash="x"),
            WebOrder(
                order_id="WEB-VHQ-2",
                contact="web_vhq_lower@example.com",
                email="web_vhq_lower@example.com",
                tariff_key="vhq_basic",
                tariff_label="VHQ Basic 30d",
                days=30,
                amount_rub=359,
                status="delivered",
                profile_token="tok_web_vhq_lo_cccc111122223333",
                subscription_url="https://sub.vhq-connect.xyz/webvhqkeyabc55",
                vhq_order_id="web_vhq_ord_559",
                vhq_upstream_url="https://sub.vhq-connect.xyz/webvhqkeyabc55",
            ),
            WebProfileLink(
                profile_token="tok_linked_tg_dddd111122223333",
                telegram_id=303030303,
                telegram_username="vhq_upper",
                telegram_full_name="VHQ Upper",
                contact="linked@example.com",
            ),
            WebTelegramItem(
                profile_token="tok_linked_tg_dddd111122223333",
                telegram_id=303030303,
                item_type="vpn",
                external_id="sub_201",
                title="VHQ Basic",
                key_value=build_vhq_subscription_ref_url(
                    201,
                    public_base_url="https://darimiru.ru",
                    path_prefix="/vpnbot",
                    secret=TEST_VHQ_SECRET,
                ),
                provider="vhq",
                status="active",
            ),
        ])
        await ws.commit()

    yield {
        "bot_session_factory": bot_session_factory,
        "web_session_factory": web_session_factory,
        "web_vhq_ref_url": web_vhq_ref_url,
    }

    await bot_engine.dispose()
    await web_engine.dispose()


async def _call_webstore_lookup(query: str, *, secret: str | None = TEST_BRIDGE_SECRET):
    from urllib.parse import urlencode
    headers = {"X-Internal-Secret": secret} if secret is not None else {}
    req = make_mocked_request(
        "GET",
        f"/api/store/internal/admin-client-lookup?{urlencode({'q': query})}",
        headers=headers,
    )
    return await webstore_routes.handle_internal_admin_client_lookup(req)


# ── 1 & 2 & 5: Adapt mirror links (new/old), upstream URL, UUID, query/slash ──


@pytest.mark.parametrize(
    "query_str",
    [
        f"https://darimiru.xyz/vpnbot/adapt-sub/{UUID_CLIENT_A}",
        f"https://darimiru.ru/vpnbot/adapt-sub/{UUID_CLIENT_A}",
        f"https://network-api.adaptgroup.app/sub/{UUID_CLIENT_A}",
        f"https://darimiru.xyz/vpnbot/adapt-sub/{UUID_CLIENT_A}/",
        f"https://darimiru.xyz/vpnbot/adapt-sub/{UUID_CLIENT_A}/?utm_source=tg&v=2#frag",
        f"https://network-api.adaptgroup.app/sub/{UUID_CLIENT_A}?client=clash",
        UUID_CLIENT_A,
        UUID_CLIENT_A.upper(),
        f"adapt_{UUID_CLIENT_A}",
    ],
)
async def test_adapt_links_and_uuid_find_exact_client_via_handlers(search_env, monkeypatch, query_str):
    # 1. Unified search message handler
    msg = _make_message(query_str)
    state = _DummyState()
    await admin_handler.admin_unified_search_process(msg, state)

    msg.answer.assert_awaited_once()
    rendered = msg.answer.await_args.args[0]
    assert "Найдено: <b>1</b>" in rendered
    assert "109049579" in rendered
    assert "202020202" not in rendered
    assert len(state.data["unified_results"]) == 1
    assert state.data["unified_results"][0]["telegram_id"] == 109049579

    # 2. Direct user-by-link handler
    shown_users = []

    async def _fake_show(target_msg, user_obj):
        shown_users.append(user_obj.telegram_id)

    monkeypatch.setattr(admin_handler, "_show_user_info", _fake_show)
    link_msg = _make_message(query_str)
    link_state = _DummyState()
    await admin_handler.admin_user_by_link_process(link_msg, link_state)
    assert shown_users == [109049579]
    assert link_state.cleared is True


@pytest.mark.parametrize(
    "query_str",
    [
        f"https://darimiru.xyz/vpnbot/adapt-sub/{UUID_CLIENT_B}",
        f"https://darimiru.ru/vpnbot/adapt-sub/{UUID_CLIENT_B}",
        f"https://network-api.adaptgroup.app/sub/{UUID_CLIENT_B}/?x=1",
        UUID_CLIENT_B,
        UUID_CLIENT_B.upper(),
    ],
)
async def test_webstore_adapt_lookup_across_domains_and_formats(search_env, query_str):
    resp = await _call_webstore_lookup(query_str)
    assert resp.status == 200
    profiles = json.loads(resp.text)["profiles"]
    assert [p["profile_token"] for p in profiles] == ["tok_web_adapt_aaaa111122223333"]


# ── 3 & 4: VHQ mirror links (/vhq-sub/), upstream URLs, and base64 case ───────


async def test_vhq_mirror_and_original_links_bot_and_webstore(search_env):
    sub_ref_xyz = build_vhq_subscription_ref_url(
        201, public_base_url="https://darimiru.xyz", path_prefix="/vpnbot", secret=TEST_VHQ_SECRET
    )
    sub_ref_ru = build_vhq_subscription_ref_url(
        201, public_base_url="https://darimiru.ru", path_prefix="/vpnbot", secret=TEST_VHQ_SECRET
    )
    mirror_upstream_xyz = build_bot_vhq_mirror_url(
        "https://sub.vhq-connect.xyz/KeyAbCdEf100",
        public_base_url="https://darimiru.xyz",
        path_prefix="/vpnbot",
        secret=TEST_VHQ_SECRET,
        order_id="ord_10",
    )

    for q in [
        sub_ref_xyz,
        f"{sub_ref_xyz}/?client=singbox#main",
        sub_ref_ru,
        mirror_upstream_xyz,
        "https://sub.vhq-connect.xyz/KeyAbCdEf100",
        "https://sub.vhq-connect.xyz/KeyAbCdEf100/?flag=v2ray",
    ]:
        results, warning = await admin_handler._run_unified_user_search(q)
        assert warning is None
        assert [r["telegram_id"] for r in results] == [303030303], f"Unexpected results for {q}: {results}"

    # Webstore VHQ order lookup: by new .xyz web_order_ref, old .ru web_order_ref, upstream mirror, and raw supplier URL
    web_ref_xyz = build_vhq_web_order_ref_url(
        "WEB-VHQ-1",
        public_base_url="https://darimiru.xyz",
        path_prefix="/vpnbot",
        secret=TEST_VHQ_SECRET,
    )
    web_mirror_xyz = build_web_vhq_mirror_url(
        "https://sub.vhq-connect.xyz/WebVhqKeyAbC55",
        public_base_url="https://darimiru.xyz",
        path_prefix="/vpnbot",
        secret=TEST_VHQ_SECRET,
        order_id="WEB-VHQ-1",
    )
    for wq in [
        web_ref_xyz,
        f"{web_ref_xyz}/?foo=bar",
        search_env["web_vhq_ref_url"],
        web_mirror_xyz,
        "https://sub.vhq-connect.xyz/WebVhqKeyAbC55",
        "https://sub.vhq-connect.xyz/WebVhqKeyAbC55/?client=clash",
    ]:
        resp = await _call_webstore_lookup(wq)
        assert resp.status == 200, f"Expected 200 for {wq}, got {resp.status}"
        profiles = json.loads(resp.text)["profiles"]
        assert [p["profile_token"] for p in profiles] == ["tok_web_vhq_up_bbbb111122223333"]


async def test_vhq_base64_token_and_key_case_sensitivity(search_env):
    # 1. Flipping case inside base64 payload of /vhq-sub/<payload>.<sig> MUST fail closed (0 matches)
    valid_bot_ref = build_vhq_subscription_ref_url(
        201, public_base_url="https://darimiru.xyz", path_prefix="/vpnbot", secret=TEST_VHQ_SECRET
    )
    prefix, token = valid_bot_ref.rsplit("/vhq-sub/", 1)
    payload_b64, sig = token.rsplit(".", 1)
    corrupted_bot_ref = f"{prefix}/vhq-sub/{_flip_first_alpha_case(payload_b64)}.{sig}"

    results, _ = await admin_handler._run_unified_user_search(corrupted_bot_ref)
    assert results == []

    # Same for webstore order whose exact valid /vhq-sub/ URL IS stored in WebOrder.subscription_url!
    stored_web_ref = search_env["web_vhq_ref_url"]
    w_prefix, w_token = stored_web_ref.rsplit("/vhq-sub/", 1)
    w_payload_b64, w_sig = w_token.rsplit(".", 1)
    corrupted_web_ref = f"{w_prefix}/vhq-sub/{_flip_first_alpha_case(w_payload_b64)}.{w_sig}"

    resp_corrupted = await _call_webstore_lookup(corrupted_web_ref)
    assert resp_corrupted.status == 404

    # 2. Upstream VHQ URLs differing ONLY in ASCII letter case must resolve strictly to their own client
    res_upper, _ = await admin_handler._run_unified_user_search("https://sub.vhq-connect.xyz/KeyAbCdEf100")
    assert [r["telegram_id"] for r in res_upper] == [303030303]

    res_lower, _ = await admin_handler._run_unified_user_search("https://sub.vhq-connect.xyz/keyabcdef100")
    assert [r["telegram_id"] for r in res_lower] == [404040404]

    resp_w_upper = await _call_webstore_lookup("https://sub.vhq-connect.xyz/WebVhqKeyAbC55")
    assert [p["profile_token"] for p in json.loads(resp_w_upper.text)["profiles"]] == [
        "tok_web_vhq_up_bbbb111122223333"
    ]

    resp_w_lower = await _call_webstore_lookup("https://sub.vhq-connect.xyz/webvhqkeyabc55")
    assert [p["profile_token"] for p in json.loads(resp_w_lower.text)["profiles"]] == [
        "tok_web_vhq_lo_cccc111122223333"
    ]


# ── 6: False-positive & prefix-collision prevention ───────────────────────────


async def test_no_false_positives_on_prefixes_generic_urls_or_shared_proxy_host(search_env):
    # Marzban new domain URL matches exact token (606060606) and NEVER prefix extension MrzTokAbC777999 (707070707)
    res_mrz, _ = await admin_handler._run_unified_user_search("https://darimiru.xyz/sub/MrzTokAbC777/?foo=1")
    assert [r["telegram_id"] for r in res_mrz] == [606060606]

    # VHQ order prefix ord_10 / vhq_ord_10 must NOT return ord_109 (404040404)
    res_ord, _ = await admin_handler._run_unified_user_search("vhq_ord_10")
    assert [r["telegram_id"] for r in res_ord] == [303030303]

    # Shared TG proxy server IP with distinct secret must return ONLY owner of that secret
    res_prx, _ = await admin_handler._run_unified_user_search(
        "https://t.me/proxy?server=198.51.100.10&port=443&secret=ddaaaa1111"
    )
    assert [r["telegram_id"] for r in res_prx] == [606060606]

    # Generic, incomplete, or corrupted links MUST return 0 matches (never dump all subscriptions)
    for bad_query in [
        "https://darimiru.xyz/",
        "https://darimiru.xyz/vpnbot",
        "https://darimiru.xyz/vpnbot/adapt-sub/",
        f"https://darimiru.xyz/vpnbot/adapt-sub/{UUID_CLIENT_A[:18]}",
        f"https://darimiru.xyz/vpnbot/adapt-sub/{UUID_CLIENT_A}EXTRA",
        "https://network-api.adaptgroup.app/sub/",
        "https://darimiru.xyz/vpnbot/vhq-sub/",
        "https://darimiru.xyz/vpnbot/vhq-sub/not-a-valid-token",
        "https://sub.vhq-connect.xyz/",
        "https://t.me/proxy",
    ]:
        bot_res, _ = await admin_handler._run_unified_user_search(bad_query)
        assert bot_res == [], f"Expected 0 bot matches for {bad_query!r}, got {bot_res}"
        web_resp = await _call_webstore_lookup(bad_query)
        assert web_resp.status == 404, f"Expected 404 for {bad_query!r}, got {web_resp.status}"


# ── 7: No-match and Ambiguous-result handling ─────────────────────────────────


async def test_no_match_and_ambiguous_results_behavior(search_env, monkeypatch):
    # 1. Unknown valid UUID -> clean 0 matches in unified search, link search, and webstore
    msg_none = _make_message(f"https://darimiru.xyz/vpnbot/adapt-sub/{UUID_MISSING}")
    state_none = _DummyState()
    await admin_handler.admin_unified_search_process(msg_none, state_none)
    rendered_none = msg_none.answer.await_args.args[0]
    assert "Найдено: <b>0</b>" in rendered_none
    assert "Совпадений нет." in rendered_none

    link_none = _make_message(f"https://darimiru.xyz/vpnbot/adapt-sub/{UUID_MISSING}")
    link_state_none = _DummyState()
    await admin_handler.admin_user_by_link_process(link_none, link_state_none)
    assert "не найден" in link_none.answer.await_args.args[0]
    assert link_state_none.cleared is True

    web_none = await _call_webstore_lookup(f"https://darimiru.xyz/vpnbot/adapt-sub/{UUID_MISSING}")
    assert web_none.status == 404

    # 2. Ambiguous shared key in legacy DB: must NOT crash with MultipleResultsFound or silently open 1 user
    shared_key = "https://darimiru.ru/sub/SharedLegacyKey999"
    async with search_env["bot_session_factory"]() as s:
        s.add_all([
            Subscription(
                id=901,
                user_id=1,
                server_id=1,
                tariff_months=1,
                client_name="shared_1",
                vpn_key=shared_key,
                platform=Platform.ANDROID,
                status=SubStatus.ACTIVE,
                expires_at=datetime.utcnow() + timedelta(days=10),
            ),
            Subscription(
                id=902,
                user_id=2,
                server_id=1,
                tariff_months=1,
                client_name="shared_2",
                vpn_key=shared_key,
                platform=Platform.IOS,
                status=SubStatus.ACTIVE,
                expires_at=datetime.utcnow() + timedelta(days=10),
            ),
        ])
        await s.commit()

    shown_single = AsyncMock()
    monkeypatch.setattr(admin_handler, "_show_user_info", shown_single)

    ambig_link_msg = _make_message("https://darimiru.xyz/sub/SharedLegacyKey999")
    ambig_link_state = _DummyState()
    await admin_handler.admin_user_by_link_process(ambig_link_msg, ambig_link_state)

    shown_single.assert_not_awaited()
    ambig_rendered = ambig_link_msg.answer.await_args.args[0]
    assert "Найдено: <b>2</b>" in ambig_rendered
    assert "Найдено несколько клиентов" in ambig_rendered
    assert "109049579" in ambig_rendered
    assert "202020202" in ambig_rendered


# ── 8: Authorization & access control preservation ────────────────────────────


async def test_admin_and_webstore_authorization_preserved(search_env):
    unauth_uid = 888888

    cb_search = _make_callback("adm_user_search", user_id=unauth_uid)
    st_cb = _DummyState()
    await admin_handler.admin_unified_search_start(cb_search, st_cb)
    cb_search.message.edit_text.assert_not_awaited()
    assert st_cb.current_state is None

    msg_search = _make_message(f"https://darimiru.xyz/vpnbot/adapt-sub/{UUID_CLIENT_A}", user_id=unauth_uid)
    st_msg = _DummyState()
    await admin_handler.admin_unified_search_process(msg_search, st_msg)
    msg_search.answer.assert_not_awaited()
    assert st_msg.data == {}

    msg_link = _make_message(f"https://darimiru.xyz/vpnbot/adapt-sub/{UUID_CLIENT_A}", user_id=unauth_uid)
    st_link = _DummyState()
    await admin_handler.admin_user_by_link_process(msg_link, st_link)
    msg_link.answer.assert_not_awaited()

    # Webstore internal lookup rejects missing and wrong X-Internal-Secret
    resp_missing = await _call_webstore_lookup(UUID_CLIENT_B, secret=None)
    assert resp_missing.status == 403

    resp_wrong = await _call_webstore_lookup(UUID_CLIENT_B, secret="wrong-secret")
    assert resp_wrong.status == 403


# ── 9: Adapt OpenAPI spec & client non-regression ─────────────────────────────


async def test_adapt_openapi_spec_and_upgrade_client_unchanged(monkeypatch):
    spec_path = Path(__file__).resolve().parent.parent / "docs" / "adapt-openapi.json"
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    assert spec["openapi"].startswith("3.")
    assert "/subs/upgrade" in spec["paths"]
    assert "/subs/renew/custom" in spec["paths"]
    assert "/balance/check" in spec["paths"]

    api = AdaptAPI(api_id=42, api_key="secret-key", base_url="https://adapt.example")
    mock_post = AsyncMock(return_value={"success": True, "devices": 3, "upgrade_price": "2.3000"})
    monkeypatch.setattr(api, "_post", mock_post)

    result = await api.upgrade_subscription(UUID_CLIENT_A, "plan-3dev-uuid")
    assert result["devices"] == 3
    mock_post.assert_awaited_once_with(
        "/subs/upgrade",
        {"subscription_uuid": UUID_CLIENT_A, "new_plan_uuid": "plan-3dev-uuid"},
    )
