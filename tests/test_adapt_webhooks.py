"""Tests for Adapt webhook and subscription proxy routes in bot/webhooks.py."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from datetime import datetime
from types import SimpleNamespace
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


@pytest.mark.asyncio
async def test_adapt_expired_notification_contains_subscription_details_and_renew_button():
    from bot.webhooks import _adapt_notify_user

    bot_mock = AsyncMock()
    sub = SimpleNamespace(
        id=180,
        expires_at=datetime(2026, 8, 18),
        tariff_days=7,
        device_slots=5,
        adapt_current_tariff=SimpleNamespace(label="Базовый · 14 дней", days=14),
    )

    await _adapt_notify_user(
        event="subs.expired_24_hours_ago",
        user=SimpleNamespace(telegram_id=12345),
        subscription=sub,
        adapt_record=SimpleNamespace(adapt_plan_uuid="canonical-plan"),
        app={"bot": bot_mock},
    )

    message = bot_mock.send_message.await_args
    assert "❌ <b>Подписка истекла</b>" in message.args[1]
    assert "Базовый · 14 дней" in message.args[1]
    assert "Срок тарифа: <b>14 дн.</b>" in message.args[1]
    assert "Устройств: <b>5</b>" in message.args[1]
    assert "Действовала до: <b>18.08.2026</b>" in message.args[1]
    button = message.kwargs["reply_markup"].inline_keyboard[0][0]
    assert button.text == "↻ Продлить"
    assert button.callback_data == "purchase_renew_180"


# ── handle_adapt_webhook: signature verification ──────────────────────────────

@pytest.mark.asyncio
async def test_adapt_webhook_no_secret_fails_closed_returns_403():
    """If ADAPT_WEBHOOK_SECRET is empty, webhook must fail closed and return 403."""
    from bot.webhooks import handle_adapt_webhook

    body = json.dumps({
        "event": "subs.expired",
        "subscription_uuid": "sub-123",
        "external_user_id": "99999",
    }).encode()

    req = MagicMock()
    req.read = AsyncMock(return_value=body)
    req.headers = {"X-Webhook-Signature": "some-sig"}

    with patch("bot.webhooks.settings") as mock_settings:
        mock_settings.adapt_webhook_secret = ""

        response = await handle_adapt_webhook(req)

    assert response.status == 403


@pytest.mark.asyncio
async def test_adapt_webhook_missing_signature_returns_403():
    """When secret is configured but X-Webhook-Signature header is missing, returns 403."""
    from bot.webhooks import handle_adapt_webhook

    body = b'{"event":"subs.expired"}'
    req = MagicMock()
    req.read = AsyncMock(return_value=body)
    req.headers = {}

    with patch("bot.webhooks.settings") as mock_settings:
        mock_settings.adapt_webhook_secret = "my-secret"

        response = await handle_adapt_webhook(req)

    assert response.status == 403


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
        "data": {
            "subscription_uuid": "sub-abc",
            "external_user_id": "111222",
            "days_remaining": 3,
        },
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


@pytest.mark.asyncio
async def test_adapt_expired_webhook_marks_active_subscription_expired():
    """The provider expiry event with valid signature must update the local status and return 200."""
    from bot.models import SubStatus
    from bot.webhooks import handle_adapt_webhook

    secret = "my-secret"
    body = json.dumps({
        "event": "subs.expired",
        "data": {
            "subscription_uuid": "sub-expired",
            "external_user_id": "12345",
        },
    }).encode()
    sig = _hmac_sig(secret, body)
    req = MagicMock()
    req.read = AsyncMock(return_value=body)
    req.headers = {"X-Webhook-Signature": sig}

    adapt_record = MagicMock(subscription_id=77)
    subscription = MagicMock(status=SubStatus.ACTIVE)
    session_mock = AsyncMock()
    session_mock.__aenter__ = AsyncMock(return_value=session_mock)
    session_mock.__aexit__ = AsyncMock(return_value=False)
    session_mock.execute = AsyncMock(
        return_value=MagicMock(
            scalar_one_or_none=MagicMock(return_value=adapt_record)
        )
    )
    session_mock.get = AsyncMock(return_value=subscription)

    with (
        patch("bot.webhooks.settings") as mock_settings,
        patch("bot.webhooks.async_session") as mock_session_cm,
    ):
        mock_settings.adapt_webhook_secret = secret
        mock_settings.webstore_bridge_secret = ""
        mock_session_cm.return_value = session_mock

        response = await handle_adapt_webhook(req)

    assert response.status == 200
    assert subscription.status == SubStatus.EXPIRED
    session_mock.commit.assert_awaited_once()


# ── handle_adapt_subscription_proxy: invalid UUID format ─────────────────────

@pytest.mark.asyncio
async def test_adapt_sub_proxy_invalid_uuid_format_returns_404():
    from bot.webhooks import handle_adapt_subscription_proxy

    req = MagicMock()
    req.match_info = {"uuid": "not-a-valid-uuid"}
    req.headers = {}
    req.method = "GET"

    response = await handle_adapt_subscription_proxy(req)

    assert response.status == 404


@pytest.mark.asyncio
async def test_adapt_sub_proxy_valid_uuid_fetches_upstream():
    from bot.webhooks import handle_adapt_subscription_proxy

    uuid = "8c3c5da9-d89a-4d05-90da-57108d6d56c6"

    req = MagicMock()
    req.match_info = {"uuid": uuid}
    req.headers = {"user-agent": "TestClient/1.0"}
    req.method = "GET"

    upstream_body = base64.b64encode(b"vless://...").decode()

    with patch("bot.webhooks.fetch_adapt_mirror_payload", new=AsyncMock(
        return_value=(200, upstream_body.encode(), {"content-type": "text/plain"})
    )):
        response = await handle_adapt_subscription_proxy(req)

    assert response.status == 200


@pytest.mark.asyncio
async def test_adapt_sub_devices_list():
    import json
    from bot.webhooks import handle_adapt_subscription_devices

    uuid = "8c3c5da9-d89a-4d05-90da-57108d6d56c6"
    req = MagicMock()
    req.match_info = {"uuid": uuid}

    mock_devices = [{"id": 101, "name": "Phone 1"}]
    with patch("bot.services.adapt_api.AdaptAPI.get_devices", new=AsyncMock(return_value=mock_devices)):
        response = await handle_adapt_subscription_devices(req)

    assert response.status == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["ok"] is True
    assert payload["devices"] == mock_devices


@pytest.mark.asyncio
async def test_adapt_sub_device_delete():
    import json
    from bot.webhooks import handle_adapt_subscription_device_delete

    uuid = "8c3c5da9-d89a-4d05-90da-57108d6d56c6"
    req = MagicMock()
    req.match_info = {"uuid": uuid}
    req.json = AsyncMock(return_value={"device_id": 101})

    with patch("bot.services.adapt_api.AdaptAPI.delete_device", new=AsyncMock(return_value=True)):
        response = await handle_adapt_subscription_device_delete(req)

    assert response.status == 200
    payload = json.loads(response.body.decode("utf-8"))
    assert payload["ok"] is True


@pytest.mark.asyncio
async def test_adapt_sub_device_delete_invalid_uuid():
    from bot.webhooks import handle_adapt_subscription_device_delete

    req = MagicMock()
    req.match_info = {"uuid": "invalid-uuid"}

    response = await handle_adapt_subscription_device_delete(req)
    assert response.status == 400
