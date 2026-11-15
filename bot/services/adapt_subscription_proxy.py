"""Helpers for exposing Adapt subscription URLs through our own mirror URL.

Adapt subscription URLs look like:
  https://network-api.adaptgroup.app/sub/{uuid}

We proxy them through our domain as:
  https://darimiru.ru/vpnbot/adapt-sub/{uuid}

The UUID is validated against the adapt_subscriptions table on each request,
ensuring we only proxy UUIDs we own.
"""

from __future__ import annotations

from html import escape
from typing import Any, Mapping
from urllib.parse import quote

import aiohttp

from bot.config import settings
from bot.services.subscription_payload_localizer import localize_subscription_payload

_TIMEOUT = aiohttp.ClientTimeout(total=15)
_ADAPT_UPSTREAM_BASE = "https://network-api.adaptgroup.app/sub"

_PASSTHROUGH_HEADERS = {
    "content-type",
    "cache-control",
    "etag",
    "last-modified",
    "subscription-userinfo",
    "profile-update-interval",
    "profile-title",
    "support-url",
    "profile-web-page-url",
    "announce",
    "announce-url",
    "sub-info-text",
}
_BLOCKED_FORWARD_HEADERS = {
    "host",
    "connection",
    "content-length",
    "transfer-encoding",
    "accept-encoding",
    "if-none-match",
    "if-modified-since",
}


def _header_text(value: str) -> str:
    import base64

    encoded = base64.b64encode(value.encode("utf-8")).decode("ascii")
    return f"base64:{encoded}"


def _build_route_base() -> str:
    adapt_base = getattr(settings, "adapt_subscription_base_url", None)
    if isinstance(adapt_base, str) and adapt_base.strip():
        base = adapt_base.strip().rstrip("/")
        prefix = str(getattr(settings, "webhook_path_prefix", None) or "/vpnbot").strip()
        prefix_clean = "/vpnbot" if (not prefix or prefix.startswith("/bot_")) else f"/{prefix.strip('/')}"
        return base if (prefix_clean and base.endswith(prefix_clean)) else f"{base}{prefix_clean}"

    base = str(
        getattr(settings, "base_webhook_url", None)
        or getattr(settings, "subscription_base_url", None)
        or ""
    ).strip().rstrip("/")

    raw_prefix = getattr(settings, "webhook_path_prefix", None)
    prefix = str(raw_prefix).strip() if isinstance(raw_prefix, str) else "/vpnbot"
    prefix_clean = f"/{prefix.strip('/')}" if prefix else ""
    if not base:
        return ""
    return base if (prefix_clean and base.endswith(prefix_clean)) else f"{base}{prefix_clean}"


def build_adapt_mirror_url(adapt_uuid: str) -> str:
    """Build branded subscription URL pointing to our preferred delivery proxy endpoint."""
    route_base = _build_route_base()
    if not route_base or not adapt_uuid:
        return f"{_ADAPT_UPSTREAM_BASE}/{adapt_uuid}"
    return f"{route_base}/adapt-sub/{adapt_uuid.strip()}"


def get_adapt_display_key(subscription: Any) -> str | None:
    """Return the preferred darimiru.xyz delivery URL for an Adapt subscription."""
    from bot.services.adapt_routing import get_adapt_uuid_from_subscription
    uuid = get_adapt_uuid_from_subscription(subscription)
    if uuid:
        return build_adapt_mirror_url(uuid)
    return None


async def fetch_adapt_mirror_payload(
    adapt_uuid: str,
    *,
    request_headers: Mapping[str, str] | None = None,
    query_params: Mapping[str, str] | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    """Fetch the upstream Adapt subscription response and return (status, body, headers).

    We pass through relevant headers from the upstream response.
    """
    upstream_url = f"{_ADAPT_UPSTREAM_BASE}/{adapt_uuid}"
    forward_headers: dict[str, str] = {}
    if request_headers:
        for k, v in request_headers.items():
            if k.lower() not in _BLOCKED_FORWARD_HEADERS:
                forward_headers[k] = v

    async with aiohttp.ClientSession(timeout=_TIMEOUT) as http:
        async with http.get(upstream_url, headers=forward_headers, params=query_params) as resp:
            body = await resp.read()
            original_body = body
            if resp.status == 200:
                content_type = resp.headers.get("content-type", "").lower()
                if "text/html" in content_type:
                    body = body.replace(b'<div class="footer">', b'<div class="footer" style="display:none !important;">')
                    if settings.subscription_profile_title:
                        body = body.replace(b"DARIMIR API", settings.subscription_profile_title.encode("utf-8"))

                    devices: list[dict[str, Any]] = []
                    try:
                        import asyncio
                        from bot.services.adapt_api import AdaptAPI
                        api = AdaptAPI()
                        if api.enabled:
                            devices = await asyncio.wait_for(api.get_devices(adapt_uuid), timeout=3.0)
                    except Exception as exc:
                        import logging
                        logging.getLogger(__name__).warning("Failed to preload devices for adapt HTML uuid=%s: %s", adapt_uuid, exc)

                    body = inject_adapt_devices_manager(body, adapt_uuid, devices)
                body = localize_subscription_payload(body)

            out_headers: dict[str, str] = {}
            for h in _PASSTHROUGH_HEADERS:
                val = resp.headers.get(h) or resp.headers.get(h.title())
                if val:
                    out_headers[h] = val
            if body != original_body:
                out_headers.pop("etag", None)
                out_headers.pop("last-modified", None)
                out_headers["cache-control"] = "no-cache"
                content_type = out_headers.get("content-type", "")
                if content_type.lower().startswith("text/html") and "charset=" not in content_type.lower():
                    out_headers["content-type"] = "text/html; charset=utf-8"
            # Override branding headers with our domain settings
            if settings.subscription_profile_title:
                out_headers["profile-title"] = _header_text(settings.subscription_profile_title)
            if settings.subscription_support_url:
                out_headers["support-url"] = settings.subscription_support_url
            if settings.subscription_profile_web_page_url:
                out_headers["profile-web-page-url"] = settings.subscription_profile_web_page_url
            if settings.subscription_announce:
                import base64
                out_headers["announce"] = (
                    "base64:" + base64.b64encode(settings.subscription_announce.encode()).decode()
                )
            if settings.subscription_announce_url:
                out_headers["announce-url"] = settings.subscription_announce_url
            return resp.status, body, out_headers


def _format_device_dt_msk(val: Any) -> str:
    if not val:
        return "—"
    try:
        from datetime import datetime, timezone, timedelta
        if isinstance(val, (int, float)):
            ts = float(val)
            if ts > 10_000_000_000:
                ts /= 1000
            dt = datetime.fromtimestamp(ts, tz=timezone.utc)
        elif isinstance(val, str):
            clean = val.strip()
            if clean.isdigit():
                ts = float(clean)
                if ts > 10_000_000_000:
                    ts /= 1000
                dt = datetime.fromtimestamp(ts, tz=timezone.utc)
            else:
                dt = datetime.fromisoformat(clean.replace("Z", "+00:00"))
        elif isinstance(val, datetime):
            dt = val
        else:
            return "—"

        msk = timezone(timedelta(hours=3))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(msk).strftime("%d.%m.%Y, %H:%M")
    except Exception:
        return "—"


def _render_device_item_html(dev: dict[str, Any]) -> str:
    import html
    dev_id = dev.get("id") or dev.get("device_id")
    if dev_id is None:
        return ""
    name = (
        dev.get("name")
        or dev.get("client_name")
        or dev.get("device_model")
        or f"Устройство {dev_id}"
    )
    device_os = dev.get("device_os") or dev.get("os") or "Устройство"
    os_version = dev.get("os_version") or ""
    ip_addr = dev.get("ip_address") or dev.get("last_ip") or dev.get("ip") or "127.0.0.1"

    specs_parts = [p for p in (device_os, os_version, ip_addr) if p]
    specs_line = " • ".join(html.escape(str(p)) for p in specs_parts)

    first_seen_str = _format_device_dt_msk(dev.get("first_seen"))
    last_seen_str = _format_device_dt_msk(dev.get("last_seen") or dev.get("last_login") or dev.get("online_at"))

    safe_name = html.escape(str(name))
    safe_name_js = str(name).replace("\\", "\\\\").replace("'", "\\'").replace('"', '&quot;')

    return f"""
    <div class="dev-item" id="dev-item-{dev_id}">
        <div class="dev-icon-box">
            <svg class="dev-screen-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
                <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>
                <line x1="8" y1="21" x2="16" y2="21"/>
                <line x1="12" y1="17" x2="12" y2="21"/>
            </svg>
        </div>
        <div class="dev-details">
            <div class="dev-name">{safe_name}</div>
            <div class="dev-specs">{specs_line}</div>
            <div class="dev-dates">
                <div>Первый вход: {first_seen_str}</div>
                <div>Последний вход: {last_seen_str}</div>
            </div>
        </div>
        <button type="button" class="dev-del-btn" onclick="promptDeleteDev({dev_id}, '{safe_name_js}')" title="Удалить устройство">
            <svg class="dev-trash-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8">
                <polyline points="3 6 5 6 21 6"/>
                <path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                <line x1="10" y1="11" x2="10" y2="17"/>
                <line x1="14" y1="11" x2="14" y2="17"/>
            </svg>
        </button>
    </div>
    """


def _render_quick_actions_html(adapt_uuid: str) -> str:
    renew_url = f"/renew?key={quote(str(adapt_uuid), safe='')}"
    return f"""
    <div class="card quick-actions-card" id="quick-actions-card">
        <div class="quick-actions-heading">Быстрые действия</div>
        <div class="quick-actions-grid">
            <a class="quick-action" href="/profile">Мой профиль</a>
            <a class="quick-action" href="{escape(renew_url, quote=True)}">Продлить подписку</a>
            <a class="quick-action" id="quickTelegramAction" href="/profile#telegram">Привязать Telegram</a>
            <a class="quick-action" id="quickReferralAction" href="/profile">Пригласить друга</a>
            <a class="quick-action" href="/support">Поддержка</a>
        </div>
        <div class="quick-telegram-note" id="quickTelegramNote" hidden>
            <strong>Telegram ещё не привязан к профилю Webstore.</strong>
            <span>Пока Telegram не подключён, бот и Webstore могут отображаться отдельно.<br>Привяжите Telegram, чтобы видеть один и тот же профиль и связанные возможности в обоих местах.</span>
        </div>
        <div class="quick-telegram-status" id="quickTelegramStatus" hidden>Telegram подключён</div>
        <div class="quick-referral-anonymous" id="quickReferralAnonymous">
            <strong>Хотите пригласить друга и получать бонусы?</strong>
            <span>Откройте Мой профиль или войдите в Webstore.</span>
            <div class="quick-referral-actions">
                <a href="/profile">Мой профиль</a>
                <a href="/profile">Войти</a>
                <a href="/buy#referral-program">Подробнее о реферальной системе</a>
            </div>
        </div>
        <div class="quick-referral-authenticated" id="quickReferralAuthenticated" hidden>
            <strong>Ваша реферальная ссылка</strong>
            <div class="quick-referral-link-row">
                <span id="quickReferralLink"></span>
                <button type="button" id="quickReferralCopy">Копировать ссылку</button>
            </div>
            <span class="quick-referral-copy-status" id="quickReferralCopyStatus" role="status" aria-live="polite"></span>
        </div>
        <div class="quick-referral-error" id="quickReferralError" hidden>Не удалось загрузить реферальную информацию. Откройте профиль и попробуйте ещё раз.</div>
    </div>
    """


def inject_adapt_devices_manager(body: bytes, adapt_uuid: str, devices: list[dict[str, Any]]) -> bytes:
    """Inject interactive Adapt devices management section into the subscription HTML."""
    try:
        html_text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body

    dev_items_html = "".join(_render_device_item_html(d) for d in devices if isinstance(d, dict))
    empty_style = 'style="display:none;"' if devices else ""
    devices_count = len(devices)

    dev_css = """
    .quick-actions-card {
        background: linear-gradient(135deg, rgba(19, 138, 140, 0.2), rgba(255, 255, 255, 0.04));
        border: 1px solid rgba(255, 255, 255, 0.14);
        margin-bottom: 16px;
    }
    .quick-actions-heading {
        color: #ffffff;
        font-size: 16px;
        font-weight: 700;
        margin-bottom: 14px;
    }
    .quick-actions-grid {
        display: grid;
        grid-template-columns: repeat(5, minmax(0, 1fr));
        gap: 8px;
    }
    .quick-action,
    .quick-referral-actions a {
        display: inline-flex;
        align-items: center;
        justify-content: center;
        min-height: 40px;
        padding: 8px 10px;
        border: 1px solid rgba(255, 255, 255, 0.16);
        border-radius: 10px;
        background: rgba(255, 255, 255, 0.07);
        color: #ffffff;
        font-size: 12.5px;
        font-weight: 600;
        line-height: 1.25;
        text-align: center;
        text-decoration: none;
        transition: background 0.2s ease, border-color 0.2s ease;
    }
    .quick-action:hover,
    .quick-referral-actions a:hover {
        background: rgba(255, 255, 255, 0.14);
        border-color: rgba(255, 255, 255, 0.3);
    }
    .quick-telegram-note,
    .quick-telegram-status,
    .quick-referral-anonymous,
    .quick-referral-authenticated,
    .quick-referral-error {
        margin-top: 14px;
        padding: 12px 14px;
        border-radius: 12px;
        color: rgba(255, 255, 255, 0.72);
        background: rgba(0, 0, 0, 0.14);
        font-size: 12.5px;
        line-height: 1.5;
    }
    .quick-telegram-note strong,
    .quick-referral-anonymous strong,
    .quick-referral-authenticated strong {
        display: block;
        color: #ffffff;
        margin-bottom: 3px;
    }
    .quick-telegram-note span,
    .quick-referral-anonymous span,
    .quick-referral-copy-status {
        display: block;
    }
    .quick-referral-actions {
        display: flex;
        flex-wrap: wrap;
        gap: 8px;
        margin-top: 10px;
    }
    .quick-referral-actions a {
        min-height: 34px;
        font-size: 12px;
    }
    .quick-referral-link-row {
        display: flex;
        align-items: stretch;
        gap: 8px;
        margin-top: 8px;
    }
    .quick-referral-link-row span {
        min-width: 0;
        flex: 1;
        padding: 9px 10px;
        border: 1px solid rgba(255, 255, 255, 0.14);
        border-radius: 9px;
        color: #ffffff;
        font-family: monospace;
        font-size: 11px;
        overflow-wrap: anywhere;
    }
    .quick-referral-link-row button {
        flex: 0 0 auto;
        min-height: 38px;
        padding: 8px 12px;
        border: 1px solid rgba(255, 255, 255, 0.18);
        border-radius: 9px;
        background: rgba(255, 255, 255, 0.12);
        color: #ffffff;
        font: inherit;
        font-size: 12px;
        font-weight: 600;
        cursor: pointer;
    }
    .quick-referral-copy-status {
        min-height: 18px;
        margin-top: 6px;
        color: #9ee6bd;
    }
    @media (max-width: 720px) {
        .quick-actions-grid {
            grid-template-columns: repeat(2, minmax(0, 1fr));
        }
        .quick-referral-link-row {
            flex-direction: column;
        }
        .quick-referral-link-row button {
            width: 100%;
        }
    }

    /* Adapt Devices Manager */
    .dev-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        border-bottom: 1px solid rgba(255, 255, 255, 0.08);
        padding-bottom: 12px;
        margin-bottom: 14px;
    }
    .dev-header-title {
        color: #ffffff;
        font-size: 15px;
        font-weight: 600;
        display: flex;
        align-items: center;
        gap: 8px;
    }
    .dev-header-icon {
        width: 18px;
        height: 18px;
        color: rgba(255, 255, 255, 0.7);
    }
    .dev-count-pill {
        background: rgba(255, 255, 255, 0.12);
        color: #ffffff;
        font-size: 12px;
        font-weight: 700;
        padding: 2px 8px;
        border-radius: 10px;
    }
    .dev-items-list {
        display: flex;
        flex-direction: column;
        gap: 10px;
    }
    .dev-item {
        background: rgba(255, 255, 255, 0.03);
        border: 1px solid rgba(255, 255, 255, 0.07);
        border-radius: 16px;
        padding: 16px;
        display: flex;
        align-items: center;
        gap: 14px;
        transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
    }
    .dev-item.deleting {
        opacity: 0;
        transform: scale(0.95);
        max-height: 0;
        padding-top: 0;
        padding-bottom: 0;
        margin-bottom: 0;
        overflow: hidden;
        border-color: transparent;
    }
    .dev-icon-box {
        width: 44px;
        height: 44px;
        border-radius: 12px;
        background: rgba(255, 255, 255, 0.05);
        border: 1px solid rgba(255, 255, 255, 0.08);
        display: flex;
        align-items: center;
        justify-content: center;
        flex-shrink: 0;
    }
    .dev-screen-svg {
        width: 22px;
        height: 22px;
        color: rgba(255, 255, 255, 0.7);
    }
    .dev-details {
        flex: 1;
        min-width: 0;
    }
    .dev-name {
        font-size: 14.5px;
        font-weight: 600;
        color: #ffffff;
        letter-spacing: -0.2px;
        word-break: break-word;
        margin-bottom: 3px;
    }
    .dev-specs {
        font-size: 12.5px;
        color: rgba(255, 255, 255, 0.45);
        line-height: 1.4;
        word-break: break-all;
    }
    .dev-dates {
        margin-top: 5px;
        font-size: 11.5px;
        color: rgba(255, 255, 255, 0.35);
        line-height: 1.45;
    }
    .dev-del-btn {
        width: 38px;
        height: 38px;
        border-radius: 10px;
        background: rgba(255, 255, 255, 0.04);
        border: 1px solid rgba(255, 255, 255, 0.08);
        color: rgba(255, 255, 255, 0.4);
        display: flex;
        align-items: center;
        justify-content: center;
        cursor: pointer;
        flex-shrink: 0;
        transition: all 0.2s ease;
    }
    .dev-del-btn:hover {
        background: rgba(239, 68, 68, 0.15);
        border-color: rgba(239, 68, 68, 0.3);
        color: #ef4444;
    }
    .dev-trash-svg {
        width: 17px;
        height: 17px;
    }
    .dev-empty-box {
        background: rgba(255, 255, 255, 0.02);
        border: 1px dashed rgba(255, 255, 255, 0.1);
        border-radius: 16px;
        padding: 24px 16px;
        text-align: center;
    }
    .dev-empty-icon {
        font-size: 26px;
        margin-bottom: 8px;
        opacity: 0.6;
    }
    .dev-empty-title {
        font-size: 14px;
        font-weight: 600;
        color: #ffffff;
        margin-bottom: 4px;
    }
    .dev-empty-desc {
        font-size: 12.5px;
        color: rgba(255, 255, 255, 0.4);
        line-height: 1.45;
        max-width: 320px;
        margin: 0 auto;
    }
    .dev-modal-overlay {
        position: fixed;
        top: 0;
        left: 0;
        right: 0;
        bottom: 0;
        background: rgba(0, 0, 0, 0.75);
        backdrop-filter: blur(8px);
        display: none;
        align-items: center;
        justify-content: center;
        padding: 20px;
        z-index: 10000;
    }
    .dev-modal-overlay.open {
        display: flex;
    }
    .dev-modal-card {
        background: #141416;
        border: 1px solid rgba(255, 255, 255, 0.12);
        border-radius: 20px;
        padding: 24px;
        max-width: 360px;
        width: 100%;
        box-shadow: 0 20px 40px rgba(0, 0, 0, 0.5);
    }
    .dev-modal-title {
        font-size: 17px;
        font-weight: 700;
        color: #ffffff;
        margin-bottom: 8px;
    }
    .dev-modal-text {
        font-size: 13.5px;
        color: rgba(255, 255, 255, 0.6);
        line-height: 1.5;
        margin-bottom: 20px;
    }
    .dev-modal-device-badge {
        color: #ffffff;
        font-weight: 600;
        word-break: break-word;
    }
    .dev-modal-actions {
        display: flex;
        gap: 10px;
    }
    .dev-modal-btn {
        flex: 1;
        padding: 12px;
        border-radius: 12px;
        font-family: inherit;
        font-size: 14px;
        font-weight: 600;
        cursor: pointer;
        border: none;
        transition: all 0.2s ease;
    }
    .dev-btn-cancel {
        background: rgba(255, 255, 255, 0.08);
        color: #ffffff;
    }
    .dev-btn-cancel:hover {
        background: rgba(255, 255, 255, 0.14);
    }
    .dev-btn-confirm {
        background: #ef4444;
        color: #ffffff;
    }
    .dev-btn-confirm:hover {
        background: #dc2626;
    }
    .dev-toast {
        position: fixed;
        bottom: 24px;
        left: 50%;
        transform: translateX(-50%) translateY(50px);
        background: #1e1e24;
        border: 1px solid rgba(255, 255, 255, 0.15);
        color: #ffffff;
        padding: 12px 20px;
        border-radius: 14px;
        font-size: 13.5px;
        font-weight: 500;
        box-shadow: 0 10px 30px rgba(0,0,0,0.5);
        opacity: 0;
        pointer-events: none;
        transition: all 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        z-index: 10001;
    }
    .dev-toast.show {
        transform: translateX(-50%) translateY(0);
        opacity: 1;
    }
    """

    dev_card_html = f"""
    <div class="card" id="devices-manager-card">
        <div class="dev-header">
            <div class="dev-header-title">
                <svg class="dev-header-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2">
                    <rect x="2" y="3" width="20" height="14" rx="2" ry="2"/>
                    <line x1="8" y1="21" x2="16" y2="21"/>
                    <line x1="12" y1="17" x2="12" y2="21"/>
                </svg>
                <span>Устройства</span>
            </div>
            <span class="dev-count-pill" id="dev-count-pill">{devices_count}</span>
        </div>

        <div class="dev-items-list" id="dev-items-list">
            {dev_items_html}
        </div>
        <div class="dev-empty-box" id="dev-empty-box" {empty_style}>
            <div class="dev-empty-icon">🖥</div>
            <div class="dev-empty-title">Нет подключенных устройств</div>
            <div class="dev-empty-desc">Устройства появятся здесь автоматически после первого подключения к VPN по вашей ссылке.</div>
        </div>
    </div>
    """

    quick_actions_script = """
    <script>
    (function() {
        var telegramAction = document.getElementById('quickTelegramAction');
        var telegramNote = document.getElementById('quickTelegramNote');
        var telegramStatus = document.getElementById('quickTelegramStatus');
        var referralAction = document.getElementById('quickReferralAction');
        var anonymousReferral = document.getElementById('quickReferralAnonymous');
        var authenticatedReferral = document.getElementById('quickReferralAuthenticated');
        var referralError = document.getElementById('quickReferralError');
        var referralLink = document.getElementById('quickReferralLink');
        var referralCopy = document.getElementById('quickReferralCopy');
        var referralCopyStatus = document.getElementById('quickReferralCopyStatus');
        var profileToken = '';

        function showReferralError() {
            if (referralError) referralError.hidden = false;
        }

        async function startTelegramLink() {
            if (!profileToken || !telegramAction) return;
            telegramAction.textContent = 'Готовим ссылку...';
            telegramAction.setAttribute('aria-disabled', 'true');
            try {
                var response = await fetch('/api/store/profile-link-init', {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    credentials: 'same-origin',
                    body: JSON.stringify({token: profileToken})
                });
                var data = await response.json();
                if (!response.ok || !data.link_url) throw new Error('link');
                window.location.href = data.link_url;
            } catch (_) {
                telegramAction.textContent = 'Не удалось начать привязку';
                telegramAction.removeAttribute('aria-disabled');
            }
        }

        async function copyReferralLink() {
            if (!referralLink || !referralLink.textContent) return;
            try {
                await navigator.clipboard.writeText(referralLink.textContent);
            } catch (_) {
                var input = document.createElement('textarea');
                input.value = referralLink.textContent;
                input.setAttribute('readonly', '');
                input.style.position = 'fixed';
                input.style.opacity = '0';
                document.body.appendChild(input);
                input.select();
                document.execCommand('copy');
                input.remove();
            }
            if (referralCopyStatus) referralCopyStatus.textContent = 'Ссылка скопирована';
            window.setTimeout(function() {
                if (referralCopyStatus) referralCopyStatus.textContent = '';
            }, 2200);
        }

        async function loadProfileContext() {
            try {
                var authResponse = await fetch('/api/store/auth/me', {
                    credentials: 'same-origin',
                    headers: {'Accept': 'application/json'}
                });
                var auth = await authResponse.json();
                if (!authResponse.ok || !auth.authenticated || !auth.profile_token) return;
                profileToken = auth.profile_token;
                var profileResponse = await fetch('/api/store/profile-orders', {
                    credentials: 'same-origin',
                    headers: {'Accept': 'application/json'}
                });
                var profile = await profileResponse.json();
                if (!profileResponse.ok) {
                    showReferralError();
                    return;
                }

                if (profile.telegram_link) {
                    if (telegramAction) {
                        telegramAction.textContent = 'Telegram подключён';
                        telegramAction.href = '/profile#telegram';
                    }
                    if (telegramStatus) telegramStatus.hidden = false;
                } else {
                    if (telegramNote) telegramNote.hidden = false;
                    if (telegramAction) {
                        telegramAction.href = '#';
                        telegramAction.addEventListener('click', function(event) {
                            event.preventDefault();
                            startTelegramLink();
                        });
                    }
                }

                var siteUrl = profile.referral && profile.referral.site_url;
                if (!siteUrl || !referralLink || !authenticatedReferral || !anonymousReferral) {
                    showReferralError();
                    return;
                }
                referralLink.textContent = siteUrl;
                authenticatedReferral.hidden = false;
                anonymousReferral.hidden = true;
                if (referralAction) referralAction.href = '#quickReferralAuthenticated';
            } catch (_) {
                showReferralError();
            }
        }

        if (referralCopy) referralCopy.addEventListener('click', copyReferralLink);
        loadProfileContext();
    })();
    </script>
    """

    dev_modal_and_script = f"""
    <div class="dev-modal-overlay" id="devModalOverlay" onclick="if(event.target===this)closeDevModal()">
        <div class="dev-modal-card">
            <div class="dev-modal-title">Удалить устройство?</div>
            <div class="dev-modal-text">
                Устройство <span class="dev-modal-device-badge" id="devModalDeviceName"></span> будет отключено от подписки.
            </div>
            <div class="dev-modal-actions">
                <button type="button" class="dev-modal-btn dev-btn-cancel" onclick="closeDevModal()">Отмена</button>
                <button type="button" class="dev-modal-btn dev-btn-confirm" id="devModalConfirmBtn">Удалить</button>
            </div>
        </div>
    </div>
    <div class="dev-toast" id="devToast"></div>

    <script>
    (function() {{
        var currentDevId = null;

        window.promptDeleteDev = function(deviceId, deviceName) {{
            currentDevId = deviceId;
            document.getElementById('devModalDeviceName').textContent = '«' + (deviceName || 'Устройство') + '»';
            document.getElementById('devModalOverlay').classList.add('open');
        }};

        window.closeDevModal = function() {{
            document.getElementById('devModalOverlay').classList.remove('open');
            currentDevId = null;
            var btn = document.getElementById('devModalConfirmBtn');
            btn.textContent = 'Удалить';
            btn.disabled = false;
        }};

        var confirmBtn = document.getElementById('devModalConfirmBtn');
        if (confirmBtn) {{
            confirmBtn.addEventListener('click', function() {{
                if (!currentDevId) return;
                var btn = this;
                btn.textContent = 'Удаление...';
                btn.disabled = true;

                var baseUrl = window.location.pathname.replace(/\\/+$/, '');
                var deleteUrl = baseUrl + '/devices/delete';

                fetch(deleteUrl, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ device_id: currentDevId }})
                }})
                .then(function(res) {{ return res.json(); }})
                .then(function(data) {{
                    if (data && data.ok) {{
                        var el = document.getElementById('dev-item-' + currentDevId);
                        if (el) {{
                            el.classList.add('deleting');
                            setTimeout(function() {{
                                if (el.parentNode) el.parentNode.removeChild(el);
                                checkDevEmpty();
                            }}, 350);
                        }}
                        showDevToast('✅ Устройство успешно удалено');
                        closeDevModal();
                    }} else {{
                        showDevToast('❌ ' + ((data && data.error) || 'Ошибка удаления'));
                        btn.textContent = 'Удалить';
                        btn.disabled = false;
                    }}
                }})
                .catch(function() {{
                    showDevToast('❌ Ошибка связи с сервером');
                    btn.textContent = 'Удалить';
                    btn.disabled = false;
                }});
            }});
        }}

        function checkDevEmpty() {{
            var items = document.querySelectorAll('.dev-item');
            var pill = document.getElementById('dev-count-pill');
            if (pill) pill.textContent = items.length;

            var topCount = document.querySelector('.devices-count');
            if (topCount) topCount.textContent = items.length;

            var emptyBox = document.getElementById('dev-empty-box');
            if (items.length === 0 && emptyBox) {{
                emptyBox.style.display = 'block';
            }}
        }}

        function showDevToast(msg) {{
            var toast = document.getElementById('devToast');
            if (!toast) return;
            toast.textContent = msg;
            toast.classList.add('show');
            setTimeout(function() {{
                toast.classList.remove('show');
            }}, 3000);
        }}
    }})();
    </script>
    """

    if "</style>" in html_text:
        html_text = html_text.replace("</style>", f"\n{dev_css}\n</style>", 1)
    elif "</head>" in html_text:
        html_text = html_text.replace("</head>", f"<style>\n{dev_css}\n</style>\n</head>", 1)

    quick_actions_html = _render_quick_actions_html(adapt_uuid)
    marker = '<div class="card-label">Устройства</div>'
    idx = html_text.find(marker)
    if 'id="quick-actions-card"' not in html_text:
        if idx != -1:
            card_start = html_text.rfind('<div class="card"', 0, idx)
            if card_start == -1:
                card_start = idx
            html_text = html_text[:card_start] + quick_actions_html + "\n    " + html_text[card_start:]
            idx = html_text.find(marker)
        else:
            footer_idx = html_text.find('<div class="footer"')
            if footer_idx != -1:
                html_text = html_text[:footer_idx] + quick_actions_html + "\n" + html_text[footer_idx:]
            else:
                html_text += quick_actions_html

    if idx != -1:
        next_card = html_text.find('<div class="card"', idx)
        if next_card != -1:
            html_text = html_text[:next_card] + dev_card_html + "\n    " + html_text[next_card:]
        else:
            footer_idx = html_text.find('<div class="footer"')
            if footer_idx != -1:
                main_end = html_text.rfind('</div>', 0, footer_idx)
                if main_end != -1:
                    html_text = html_text[:main_end] + dev_card_html + "\n" + html_text[main_end:]
                else:
                    html_text = html_text[:footer_idx] + dev_card_html + "\n" + html_text[footer_idx:]
            else:
                html_text += dev_card_html
    else:
        footer_idx = html_text.find('<div class="footer"')
        if footer_idx != -1:
            main_end = html_text.rfind('</div>', 0, footer_idx)
            if main_end != -1:
                html_text = html_text[:main_end] + dev_card_html + "\n" + html_text[main_end:]
            else:
                html_text = html_text[:footer_idx] + dev_card_html + "\n" + html_text[footer_idx:]
        else:
            html_text += dev_card_html

    if "</body>" in html_text:
        html_text = html_text.replace("</body>", f"\n{quick_actions_script}\n{dev_modal_and_script}\n</body>", 1)
    else:
        html_text += quick_actions_script + dev_modal_and_script

    return html_text.encode("utf-8")
