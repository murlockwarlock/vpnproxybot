"""Tests for Adapt webhook and subscription proxy routes in bot/webhooks.py."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _make_request(path: str, body: dict | bytes, headers: dict | None = None) -> MagicMock:
    """Build a minimal aiohttp Request mock."""
    req = MagicMock()
    req.match_info = {"uuid": path.split("/")[-1]}
    if isinstance(body, dict):
        raw = json.dumps(body).encode()
    else:
        raw = body
    req.read = AsyncMock(return_value=raw)
    req.headers = {**(headers or {}), "Content-Type": "application/json"}
    req.method = "POST" if isinstance(body, (dict, bytes)) else "GET"
    return req


def _hmac_sig(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


# ── _adapt_notify_user (smoke test) ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_adapt_notify_user_sends_message():
    from bot.webhooks import _adapt_notify_user

    bot_mock = AsyncMock()
    bot_mock.send_message = AsyncMock()

    app = {"bot": bot_mock}

    user = MagicMock()
    user.telegram_id = 12345
    sub = MagicMock()
    adapt_record = MagicMock()

    await _adapt_notify_user(
        event="subs.expires_in_24_hours",
        user=user,
        subscription=sub,
        adapt_record=adapt_record,
        app=app,
    )

    bot_mock.send_message.assert_awaited_once_with(12345, "⚠️ Ваш доступ истекает завтра. Продлите его сейчас.")


# ── handle_adapt_webhook: missing signature ──────────────────────────────────

@pytest.mark.asyncio
async def test_adapt_webhook_no_signature_skip_when_no_secret():
    """If ADAPT_WEBHOOK_SECRET is empty, webhook should still be processed."""
    from aiohttp import web
    from bot.webhooks import handle_adapt_webhook

    body = json.dumps({
        "event": "subs.expired",
        "subscription_uuid": "sub-123",
        "external_user_id": "99999",
    }).encode()

    req = MagicMock()
    req.read = AsyncMock(return_value=body)
    req.headers = {}

    with (
        patch("bot.webhooks.settings") as mock_settings,
        patch("bot.webhooks.async_session") as mock_session_cm,
    ):
        mock_settings.adapt_webhook_secret = ""
        mock_settings.bot_token = "fake-token"

        session_mock = AsyncMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)
        session_mock.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        mock_session_cm.return_value = session_mock

        response = await handle_adapt_webhook(req)

    assert response.status == 200


@pytest.mark.asyncio
async def test_adapt_webhook_wrong_signature_returns_403():
    """When secret is configured, wrong HMAC returns 403."""
    from bot.webhooks import handle_adapt_webhook

    body = b'{"event":"subs.expired"}'
    req = MagicMock()
    req.read = AsyncMock(return_value=body)
    req.headers = {"X-Webhook-Signature": "wrong-sig"}

    with patch("bot.webhooks.settings") as mock_settings:
        mock_settings.adapt_webhook_secret = "my-secret"

        response = await handle_adapt_webhook(req)

    assert response.status == 403


@pytest.mark.asyncio
async def test_adapt_webhook_correct_signature_processes():
    """Correct HMAC signature allows processing."""
    from bot.webhooks import handle_adapt_webhook

    secret = "my-secret"
    body = json.dumps({
        "event": "subs.status_warning",
        "subscription_uuid": "sub-abc",
        "external_user_id": "111222",
        "days_remaining": 3,
    }).encode()
    sig = _hmac_sig(secret, body)

    req = MagicMock()
    req.read = AsyncMock(return_value=body)
    req.headers = {"X-Webhook-Signature": sig}

    with (
        patch("bot.webhooks.settings") as mock_settings,
        patch("bot.webhooks.async_session") as mock_session_cm,
        patch("bot.webhooks._adapt_notify_user", new=AsyncMock()),
    ):
        mock_settings.adapt_webhook_secret = secret
        mock_settings.bot_token = "token"

        session_mock = AsyncMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)
        session_mock.execute = AsyncMock(return_value=MagicMock(scalar_one_or_none=MagicMock(return_value=None)))
        mock_session_cm.return_value = session_mock

        response = await handle_adapt_webhook(req)

    assert response.status == 200


# ── handle_adapt_subscription_proxy: unknown UUID ────────────────────────────

@pytest.mark.asyncio
async def test_adapt_sub_proxy_unknown_uuid_returns_404():
    from bot.webhooks import handle_adapt_subscription_proxy

    req = MagicMock()
    req.match_info = {"uuid": "unknown-uuid"}
    req.headers = {}
    req.method = "GET"

    with (
        patch("bot.webhooks.async_session") as mock_session_cm,
    ):
        session_mock = AsyncMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=None)
        session_mock.execute = AsyncMock(return_value=result_mock)
        mock_session_cm.return_value = session_mock

        response = await handle_adapt_subscription_proxy(req)

    assert response.status == 404


@pytest.mark.asyncio
async def test_adapt_sub_proxy_known_uuid_fetches_upstream():
    from bot.webhooks import handle_adapt_subscription_proxy
    from bot.models import AdaptSubscription

    uuid = "known-uuid"
    adapt_record = MagicMock(spec=AdaptSubscription)
    adapt_record.adapt_uuid = uuid

    req = MagicMock()
    req.match_info = {"uuid": uuid}
    req.headers = {"user-agent": "TestClient/1.0"}
    req.method = "GET"

    upstream_body = base64.b64encode(b"vless://...").decode()

    with (
        patch("bot.webhooks.async_session") as mock_session_cm,
        patch("bot.webhooks.fetch_adapt_mirror_payload", new=AsyncMock(
            return_value=(200, upstream_body.encode(), {"content-type": "text/plain"})
        )),
    ):
        session_mock = AsyncMock()
        session_mock.__aenter__ = AsyncMock(return_value=session_mock)
        session_mock.__aexit__ = AsyncMock(return_value=False)
        result_mock = MagicMock()
        result_mock.scalar_one_or_none = MagicMock(return_value=adapt_record)
        session_mock.execute = AsyncMock(return_value=result_mock)
        mock_session_cm.return_value = session_mock

        response = await handle_adapt_subscription_proxy(req)

    assert response.status == 200
