"""Bot configuration - reads from .env and provides settings + tariff definitions."""

from __future__ import annotations

import os
import socket
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
_BLOCKED_ADMIN_IDS = {979514796}


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_admin_ids(name: str, default: str = "") -> list[int]:
    raw = os.getenv(name, default)
    ids: list[int] = []
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        try:
            admin_id = int(item)
        except ValueError:
            continue
        if admin_id in _BLOCKED_ADMIN_IDS or admin_id in ids:
            continue
        ids.append(admin_id)
    return ids


def _default_instance_id() -> str:
    return os.getenv("INSTANCE_ID", f"{socket.gethostname()}-{os.getpid()}")


def _sanitize_client_prefix(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9]+", "", value.strip().lower())
    return cleaned[:12]


def _default_bot_client_prefix() -> str:
    explicit = os.getenv("BOT_CLIENT_PREFIX", "")
    if explicit.strip():
        sanitized = _sanitize_client_prefix(explicit)
        if sanitized:
            return sanitized

    token = os.getenv("BOT_TOKEN", "")
    if token:
        return "b" + hashlib.sha1(token.encode()).hexdigest()[:8]
    return "bot"


def _default_support_username() -> str:
    raw = os.getenv("SUPPORT_USERNAME", "gleosky").strip()
    if not raw:
        return "@gleosky"
    return raw if raw.startswith("@") else f"@{raw}"


def _default_subscription_support_url() -> str:
    raw = os.getenv("SUB_SUPPORT_URL", "").strip()
    if raw:
        return raw
    username = _default_support_username().lstrip("@")
    return f"https://t.me/{username}" if username else ""


def _normalize_mtproto_label(value: str) -> str:
    """Normalize labels loaded from JSON/.env, including surrogate-pair emoji."""
    if not value:
        return value
    try:
        return value.encode("utf-16", "surrogatepass").decode("utf-16")
    except UnicodeError:
        return value




@dataclass
class Settings:
    # Telegram
    bot_token: str = field(default_factory=lambda: os.getenv("BOT_TOKEN", ""))
    bot_client_prefix: str = field(default_factory=_default_bot_client_prefix)
    admin_ids: list[int] = field(default_factory=lambda: _env_admin_ids("ADMIN_IDS"))
    owner_ids: list[int] = field(
        default_factory=lambda: _env_admin_ids("OWNER_IDS", os.getenv("ADMIN_IDS", ""))
    )
    support_agent_ids: list[int] = field(
        default_factory=lambda: _env_admin_ids("SUPPORT_AGENT_IDS", os.getenv("ADMIN_IDS", ""))
    )
    support_username: str = field(default_factory=_default_support_username)

    # Database
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "DATABASE_URL", f"sqlite+aiosqlite:///{BASE_DIR / 'vpn_bot.db'}"
        )
    )
    backup_dir: str = field(
        default_factory=lambda: os.getenv("BACKUP_DIR", str(BASE_DIR / "backups"))
    )
    backup_extra_sqlite_dbs: str = field(
        default_factory=lambda: os.getenv("BACKUP_EXTRA_SQLITE_DBS", "")
    )
    run_daily_backup: bool = field(
        default_factory=lambda: _env_bool("RUN_DAILY_BACKUP", False)
    )
    backup_retention_days: int = field(
        default_factory=lambda: int(os.getenv("BACKUP_RETENTION_DAYS", "5"))
    )
    backup_hour: int = field(
        default_factory=lambda: int(os.getenv("BACKUP_HOUR", "3"))
    )
    backup_minute: int = field(
        default_factory=lambda: int(os.getenv("BACKUP_MINUTE", "0"))
    )

    # Telegram Pay (native card via Telegram's payment API)
    telegram_payment_provider_token: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_PAYMENT_PROVIDER_TOKEN", "")
    )

    # YooKassa
    yookassa_shop_id: str = field(default_factory=lambda: os.getenv("YOOKASSA_SHOP_ID", ""))
    yookassa_secret_key: str = field(default_factory=lambda: os.getenv("YOOKASSA_SECRET_KEY", ""))
    yookassa_save_payment_method: bool = field(
        default_factory=lambda: _env_bool("YOOKASSA_SAVE_PAYMENT_METHOD", False)
    )
    recurring_payments_enabled: bool = field(
        default_factory=lambda: _env_bool("RECURRING_PAYMENTS_ENABLED", False)
    )

    # Robokassa
    robokassa_merchant_login: str = field(
        default_factory=lambda: os.getenv("ROBOKASSA_MERCHANT_LOGIN", "")
    )
    robokassa_password_1: str = field(
        default_factory=lambda: os.getenv("ROBOKASSA_PASSWORD_1", "")
    )
    robokassa_password_2: str = field(
        default_factory=lambda: os.getenv("ROBOKASSA_PASSWORD_2", "")
    )

    # Payment webhook server
    app_port: int = field(
        default_factory=lambda: int(os.getenv("APP_PORT", "8080"))
    )
    base_webhook_url: str = field(
        default_factory=lambda: os.getenv("BASE_WEBHOOK_URL", "")
    )
    webhook_path_prefix: str = field(
        default_factory=lambda: os.getenv("WEBHOOK_PATH_PREFIX", "/vpnbot")
    )

    # Mode
    mock_mode: bool = field(
        default_factory=lambda: os.getenv("MOCK_MODE", "true").lower() == "true"
    )
    log_level: str = field(default_factory=lambda: os.getenv("LOG_LEVEL", "INFO"))

    # Custom subscription URL path (replaces /sub/ in Marzban URLs)
    # Used with nginx proxy to override profile-title header per bot instance
    subscription_sub_path: str = field(
        default_factory=lambda: os.getenv("SUBSCRIPTION_SUB_PATH", "")
    )

    # Custom base URL for subscription links (replaces server api_url domain)
    # e.g. "https://darimiru.ru" — subscription links become https://darimiru.ru/sub/<token>
    subscription_base_url: str = field(
        default_factory=lambda: os.getenv("SUBSCRIPTION_BASE_URL", "")
    )
    subscription_profile_title: str = field(
        default_factory=lambda: os.getenv("SUB_PROFILE_TITLE", "Ускоритель интернета")
    )
    subscription_support_url: str = field(
        default_factory=_default_subscription_support_url
    )
    subscription_profile_web_page_url: str = field(
        default_factory=lambda: os.getenv("SUB_PROFILE_WEB_PAGE_URL", os.getenv("SUBSCRIPTION_BASE_URL", ""))
    )
    subscription_announce: str = field(
        default_factory=lambda: os.getenv("SUB_ANNOUNCE", "")
    )
    subscription_announce_url: str = field(
        default_factory=lambda: os.getenv("SUB_ANNOUNCE_URL", "")
    )
    subscription_update_interval_hours: int = field(
        default_factory=lambda: int(os.getenv("SUB_UPDATE_INTERVAL", "12"))
    )

    # AI (DeepSeek RAG)
    deepseek_api_key: str = field(default_factory=lambda: os.getenv("DEEPSEEK_API_KEY", ""))

    # MTProto Proxy (legacy single-server)
    mtproto_host: str = field(default_factory=lambda: os.getenv("MTPROTO_HOST", ""))
    mtproto_port: int = field(default_factory=lambda: int(os.getenv("MTPROTO_PORT", "4430")))
    mtproto_ssh_user: str = field(default_factory=lambda: os.getenv("MTPROTO_SSH_USER", "root"))
    mtproto_ssh_password: str = field(default_factory=lambda: os.getenv("MTPROTO_SSH_PASSWORD", ""))

    # MTProto Proxy servers (JSON list)
    # Format: [{"host":"1.2.3.4","port":4430,"ssh_user":"root","ssh_password":"xxx","label":"NL"}]
    mtproto_servers_json: str = field(default_factory=lambda: os.getenv("MTPROTO_SERVERS", ""))
    webstore_api_base_url: str = field(
        default_factory=lambda: os.getenv("WEBSTORE_API_BASE_URL", "https://darimiru.ru")
    )
    webstore_bridge_secret: str = field(
        default_factory=lambda: os.getenv("WEBSTORE_BRIDGE_SECRET", "")
    )
    webstore_public_enabled: bool = field(
        default_factory=lambda: bool(os.getenv("WEBSTORE_BRIDGE_SECRET", "").strip())
    )
    vhq_partner_api_url: str = field(
        default_factory=lambda: os.getenv(
            "VHQ_PARTNER_API_URL",
            "https://yhmaeogxdxqszffrbjui.supabase.co/functions/v1/partner-api",
        )
    )
    vhq_partner_api_key: str = field(
        default_factory=lambda: os.getenv("VHQ_PARTNER_API_KEY", "")
    )

    # Adapt Group VPN API
    adapt_api_id: int = field(
        default_factory=lambda: int(os.getenv("ADAPT_API_ID", "0"))
    )
    adapt_api_key: str = field(
        default_factory=lambda: os.getenv("ADAPT_API_KEY", "")
    )
    adapt_webhook_secret: str = field(
        default_factory=lambda: os.getenv("ADAPT_WEBHOOK_SECRET", "")
    )
    adapt_base_url: str = field(
        default_factory=lambda: os.getenv(
            "ADAPT_BASE_URL", "https://network-api.adaptgroup.app"
        )
    )

    adapt_demo_enabled: bool = field(
        default_factory=lambda: _env_bool("ADAPT_DEMO_ENABLED", False)
    )
    adapt_demo_plan_uuid: str = field(
        default_factory=lambda: os.getenv("ADAPT_DEMO_PLAN_UUID", "00cce2fe-ee55-4c0d-8bfa-9e6e47cd99a4")
    )
    adapt_min_balance: float = field(
        default_factory=lambda: float(os.getenv("ADAPT_MIN_BALANCE", "5.0"))
    )

    @property
    def mtproto_servers(self) -> list[dict]:
        """Return list of MTProto proxy servers. Falls back to single legacy server."""
        if self.mtproto_servers_json:
            import json
            servers = json.loads(self.mtproto_servers_json)
            for server in servers:
                label = server.get("label")
                if isinstance(label, str):
                    server["label"] = _normalize_mtproto_label(label)
            return servers
        if self.mtproto_host:
            return [{
                "host": self.mtproto_host,
                "port": self.mtproto_port,
                "ssh_user": self.mtproto_ssh_user,
                "ssh_password": self.mtproto_ssh_password,
                "label": "NL",
            }]
        return []

    # Cluster / multi-instance mode
    instance_id: str = field(default_factory=_default_instance_id)
    core_leader_lease_name: str = field(
        default_factory=lambda: os.getenv("CORE_LEADER_LEASE_NAME", "bot-core-leader")
    )
    leader_lease_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("LEADER_LEASE_TTL_SECONDS", "30"))
    )
    leader_renew_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("LEADER_RENEW_INTERVAL_SECONDS", "10"))
    )
    leader_retry_interval_seconds: int = field(
        default_factory=lambda: int(os.getenv("LEADER_RETRY_INTERVAL_SECONDS", "5"))
    )
    webhook_lock_ttl_seconds: int = field(
        default_factory=lambda: int(os.getenv("WEBHOOK_LOCK_TTL_SECONDS", "120"))
    )
    webhook_lock_wait_seconds: int = field(
        default_factory=lambda: int(os.getenv("WEBHOOK_LOCK_WAIT_SECONDS", "15"))
    )
    run_telegram_polling: bool = field(
        default_factory=lambda: _env_bool("RUN_TELEGRAM_POLLING", True)
    )
    run_scheduler: bool = field(
        default_factory=lambda: _env_bool("RUN_SCHEDULER", True)
    )
    run_mailing_worker: bool = field(
        default_factory=lambda: _env_bool("RUN_MAILING_WORKER", True)
    )
    run_payment_webhook: bool = field(
        default_factory=lambda: _env_bool("RUN_PAYMENT_WEBHOOK", True)
    )

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    @property
    def notification_recipient_ids(self) -> list[int]:
        return sorted(set(self.admin_ids) | set(self.owner_ids) | set(self.support_agent_ids))


settings = Settings()
