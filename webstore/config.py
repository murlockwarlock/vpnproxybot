"""Web store configuration."""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

_DEFAULT_ENV_FILE = Path(__file__).resolve().parent.parent / ".env.webstore"
_CUSTOM_ENV_FILE = os.getenv("WEBSTORE_ENV_FILE", "").strip()
_ENV_FILE = Path(_CUSTOM_ENV_FILE) if _CUSTOM_ENV_FILE else _DEFAULT_ENV_FILE
if _ENV_FILE.exists():
    load_dotenv(_ENV_FILE)
else:
    load_dotenv()

_BLOCKED_ADMIN_IDS = {979514796}


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return int(raw.strip())
    except (TypeError, ValueError):
        return default


def _env_list(name: str) -> list[str]:
    raw = os.getenv(name, "")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        return float(raw.strip())
    except (TypeError, ValueError):
        return default


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


@dataclass
class WebStoreSettings:
    # Marzban API (local on NL server)
    marzban_api_url: str = field(
        default_factory=lambda: os.getenv("MARZBAN_API_URL", "http://127.0.0.1:7000")
    )
    marzban_username: str = field(
        default_factory=lambda: os.getenv("MARZBAN_USERNAME", "")
    )
    marzban_password: str = field(
        default_factory=lambda: os.getenv("MARZBAN_PASSWORD", "")
    )

    # Subscription URL base for generated links
    subscription_base_url: str = field(
        default_factory=lambda: os.getenv("SUBSCRIPTION_BASE_URL", "https://darimiru.ru")
    )
    subscription_sub_path: str = field(
        default_factory=lambda: os.getenv("SUBSCRIPTION_SUB_PATH", "")
    )
    bot_webhook_path_prefix: str = field(
        default_factory=lambda: os.getenv("WEBHOOK_PATH_PREFIX", "/vpnbot")
    )
    vhq_subscription_proxy_base_url: str = field(
        default_factory=lambda: os.getenv(
            "VHQ_SUBSCRIPTION_PROXY_BASE_URL",
            os.getenv("SUBSCRIPTION_BASE_URL", "https://darimiru.ru"),
        )
    )
    vhq_subscription_proxy_secret: str = field(
        default_factory=lambda: os.getenv(
            "VHQ_SUBSCRIPTION_PROXY_SECRET",
            os.getenv("BRIDGE_SHARED_SECRET", ""),
        )
    )

    # YooKassa (empty = disabled, payment buttons greyed out)
    yookassa_shop_id: str = field(
        default_factory=lambda: os.getenv("YOOKASSA_SHOP_ID", "")
    )
    yookassa_secret_key: str = field(
        default_factory=lambda: os.getenv("YOOKASSA_SECRET_KEY", "")
    )

    # Web server
    host: str = field(default_factory=lambda: os.getenv("WEBSTORE_HOST", "127.0.0.1"))
    port: int = field(default_factory=lambda: int(os.getenv("WEBSTORE_PORT", "8900")))

    # Database
    database_path: str = field(
        default_factory=lambda: os.getenv("WEBSTORE_DB", "webstore.db")
    )

    # Site
    site_name: str = field(
        default_factory=lambda: os.getenv("SITE_NAME", "Ускоритель ДариМир")
    )
    channel_url: str = field(
        default_factory=lambda: os.getenv("CHANNEL_URL", "https://t.me/darimiru_bot")
    )
    vk_url: str = field(
        default_factory=lambda: os.getenv("VK_URL", "")
    )
    bot_url: str = field(
        default_factory=lambda: os.getenv("BOT_URL", "https://t.me/darimiru_bot")
    )
    support_url: str = field(
        default_factory=lambda: os.getenv("SUPPORT_URL", "https://t.me/darimiru_support")
    )
    site_logo_path: str = field(
        default_factory=lambda: os.getenv("SITE_LOGO_PATH", str(Path(__file__).resolve().parent.parent / "svg.jpeg"))
    )
    site_favicon_path: str = field(
        default_factory=lambda: os.getenv("SITE_FAVICON_PATH", os.getenv("SITE_LOGO_PATH", str(Path(__file__).resolve().parent.parent / "svg.jpeg")))
    )

    # Admin notifications via Telegram (darimiru bot token)
    admin_bot_token: str = field(
        default_factory=lambda: os.getenv("ADMIN_BOT_TOKEN", "")
    )
    admin_ids: list[int] = field(default_factory=lambda: _env_admin_ids("ADMIN_IDS"))
    bridge_shared_secret: str = field(
        default_factory=lambda: os.getenv("BRIDGE_SHARED_SECRET", "")
    )
    referral_bridge_url: str = field(
        default_factory=lambda: os.getenv("REFERRAL_BRIDGE_URL", "http://45.92.174.214:8080/vpnbot")
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
    referral_commission_percent: float = field(
        default_factory=lambda: float(os.getenv("REFERRAL_COMMISSION_PERCENT", "40"))
    )
    adapt_api_id: str = field(
        default_factory=lambda: os.getenv("ADAPT_API_ID", "")
    )
    adapt_api_key: str = field(
        default_factory=lambda: os.getenv("ADAPT_API_KEY", "")
    )
    bot_db_path: str = field(
        default_factory=lambda: os.getenv("WEBSTORE_BOT_DB_PATH", "")
    )
    telegram_link_ttl_minutes: int = field(
        default_factory=lambda: int(os.getenv("TELEGRAM_LINK_TTL_MINUTES", "15"))
    )
    demo_key_enabled: bool = field(
        default_factory=lambda: os.getenv("DEMO_KEY_ENABLED", "1") == "1"
    )
    demo_key_days: int = field(
        default_factory=lambda: int(os.getenv("DEMO_KEY_DAYS", "7"))
    )
    extra_device_price_rub: int = field(
        default_factory=lambda: int(os.getenv("EXTRA_DEVICE_PRICE_RUB", "50"))
    )
    extra_device_max: int = field(
        default_factory=lambda: int(os.getenv("EXTRA_DEVICE_MAX", "5"))
    )
    # Support chat
    support_agent_ids: list[int] = field(
        default_factory=lambda: _env_admin_ids("SUPPORT_AGENT_IDS", os.getenv("ADMIN_IDS", ""))
    )
    telegram_bot_name: str = field(
        default_factory=lambda: os.getenv("TELEGRAM_BOT_NAME", "")
    )
    support_admin_password: str = field(
        default_factory=lambda: os.getenv("SUPPORT_ADMIN_PASSWORD", "")
    )
    adapt_demo_enabled: bool = field(
        default_factory=lambda: os.getenv("ADAPT_DEMO_ENABLED", "1" if "darimiru.ru" in os.getenv("SUBSCRIPTION_BASE_URL", "") else "0") == "1"
    )
    adapt_demo_plan_uuid: str = field(
        default_factory=lambda: os.getenv("ADAPT_DEMO_PLAN_UUID", "00cce2fe-ee55-4c0d-8bfa-9e6e47cd99a4").strip()
    )


    @property
    def yookassa_enabled(self) -> bool:
        return bool(self.yookassa_shop_id and self.yookassa_secret_key)

    @property
    def bot_handle(self) -> str:
        parsed = urlparse(self.bot_url.strip())
        candidate = (parsed.path or "").strip("/").split("/", 1)[0]
        if not candidate:
            return "@bot"
        return candidate if candidate.startswith("@") else f"@{candidate}"


settings = WebStoreSettings()

# Tariffs available on the web store
# key, label, days, price_rub, description, badge (optional)
_MARZBAN_FEATURES = [
    "3 сервера: Эстония, Германия, Нидерланды",
    "9 вариантов подключения на выбор",
    "3 устройства в тарифе",
    "Можно докупить дополнительные устройства",
]

_VHQ_LITE_FEATURES = [
    "Серверы с обходом блокировок",
    "Несколько вариантов подключения",
    "3 устройства в тарифе",
]

_VHQ_PREMIUM_FEATURES = [
    "До 70 серверов по всему миру",
    "Приоритетный доступ к каналам",
    "Самый быстрый вариант при отключениях",
    "3 устройства в тарифе",
]

ADAPT_BYPASS_TRAFFIC_GB = _env_int("WEBSTORE_ADAPT_BYPASS_TRAFFIC_GB", 39)


def _adapt_description() -> str:
    return (
        "С обходом блокировок на мобильном интернете. "
        f"{ADAPT_BYPASS_TRAFFIC_GB} ГБ трафика на обходах, без лимита на обычных серверах."
    )


def _adapt_features() -> list[str]:
    return [
        f"Серверы ОБХОД — {ADAPT_BYPASS_TRAFFIC_GB} ГБ в месяц (работают при глушении мобильного)",
        "Обычные серверы — без лимита трафика",
        "3 устройства в тарифе",
        "Можно докупить дополнительные устройства",
    ]


def _parse_device_count(label: str, default_devices: int) -> int:
    match = re.search(r"(\d+)\s*(?:📱|устройств)", label)
    if match:
        return int(match.group(1))
    return default_devices


def _label_family(label: str) -> str:
    normalized = label.lower().replace("ё", "е")
    if "преми" in normalized or "максим" in normalized:
        return "premium"
    if "базов" in normalized:
        return "basic"
    if "лайт" in normalized:
        return "light"
    return ""


def _is_darimiru_catalog() -> bool:
    return urlparse(settings.subscription_base_url).hostname == "darimiru.ru"


def _provider_description(provider: str, label: str = "", vhq_tier: str = "") -> str:
    family = _label_family(label)
    if _is_darimiru_catalog():
        if provider == "marzban" and family == "light":
            return "Стабильная работа основных приложений и сервисов на Wi-Fi и мобильном интернете без блокировок. Без лимитов. Подключение до 3-х устройств."
        if family == "basic":
            devices = _parse_device_count(label, 1)
            device_word = "устройство" if devices == 1 else "устройства" if devices < 5 else "устройств"
            return (
                "Быстрый VPN для любых задач. "
                "Обходы ⚡️ — используйте при глушении мобильного интернета (лимит на трафик указан в названии тарифа и действует только на обходы, на основные локации ограничений нет). "
                f"Подключение на {devices} {device_word}."
            )
        if family == "premium" or (provider == "vhq" and vhq_tier == "basic"):
            devices = _parse_device_count(label, 3)
            device_word = "устройство" if devices == 1 else "устройства" if devices < 5 else "устройств"
            return (
                "Максимальная стабильность и комфорт для активного использования без ограничений. Самые быстрые и надёжные серверы. "
                f"Подключение на {devices} {device_word}."
            )
    if provider == "adapt":
        return _adapt_description()
    if provider == "marzban":
        return "Ускорение всех сервисов. Стабильная работа на wifi и с мобильного интернета, когда нет блокировок"
    if provider == "vhq" and vhq_tier == "basic":
        return "Максимум возможностей по обходу блокировок и отключений. До 70 серверов и самый быстрый вариант на все случаи жизни."
    if "1 день" in label:
        return "С обходом блокировок на мобильном интернете. Тест на 1 день, один раз на пользователя."
    if family == "premium":
        return "Максимум возможностей по обходу блокировок и отключений. До 70 серверов и самый быстрый вариант на все случаи жизни."
    return "С обходом блокировок на мобильном интернете"


def _provider_features(provider: str, label: str = "", vhq_tier: str = "") -> list[str]:
    family = _label_family(label)
    if _is_darimiru_catalog():
        if provider == "marzban" and family == "light":
            return [
                "4 сервера 🌐",
                "3 устройства 📱📱📱",
                "Без лимитов",
                "Стабильная работа основных сервисов",
            ]
        if family == "basic":
            devices = _parse_device_count(label, 1)
            device_text = f"{devices} {'устройство' if devices == 1 else 'устройства' if devices < 5 else 'устройств'} 📱"
            return [
                "50 серверов 🌐 + 50 обходов ⚡️",
                device_text,
                "Без лимита на основные сервера",
            ]
        if family == "premium" or (provider == "vhq" and vhq_tier == "basic"):
            devices = _parse_device_count(label, 3)
            device_text = f"{devices} {'устройство' if devices == 1 else 'устройства' if devices < 5 else 'устройств'} 📱"
            return [
                "80 серверов 🌐 + 80 обходов ⚡️",
                device_text,
                "Безлимитный трафик на ⚡️ серверах",
            ]
    if provider == "adapt":
        return _adapt_features()
    if provider == "marzban":
        return _MARZBAN_FEATURES
    if provider == "vhq" and vhq_tier == "basic":
        return _VHQ_PREMIUM_FEATURES
    if family == "premium":
        return _VHQ_PREMIUM_FEATURES
    return _VHQ_LITE_FEATURES


def _tariff_badge(label: str, days: int, provider: str, vhq_tier: str = "") -> str:
    label_norm = label.lower()
    if provider == "vhq" and days == 1 and "базовый" in label_norm:
        return "1 раз"
    if provider == "vhq" and (vhq_tier == "basic" or "премиум" in label_norm):
        return "Топ"
    return ""


def _normalize_key_part(value: str) -> str:
    value = value.lower().replace("ё", "е")
    value = re.sub(r"[^a-zа-я0-9]+", "_", value)
    return value.strip("_") or "tariff"


def _webstore_key(label: str, days: int, provider: str, vhq_tier: str = "") -> str:
    label_norm = label.lower().replace("ё", "е")
    if provider == "vhq" and vhq_tier == "basic":
        return f"premium_{days}"
    if provider == "vhq" and vhq_tier == "lite":
        return f"basic_{days}"
    if "базов" in label_norm:
        return f"basic_{days}"
    if "преми" in label_norm:
        return f"premium_{days}"
    if "лайт" in label_norm or provider == "marzban":
        return f"vpn_{days}"
    if provider == "adapt":
        return f"adapt_{days}"
    return f"{_normalize_key_part(label)}_{days}"


def _provider_from_bot_tariff(
    label: str,
    days: int,
    price_rub: int,
    adapt_plan_uuid: str,
    vhq_tier: str = "",
) -> tuple[str, str | None, str | None]:
    if adapt_plan_uuid:
        return "adapt", None, adapt_plan_uuid
    vhq_tier = vhq_tier.strip().lower()
    if vhq_tier in {"lite", "basic"}:
        return "vhq", vhq_tier, None
    label_norm = label.lower().replace("ё", "е")
    if days == 1 and price_rub == 10:
        return "vhq", "lite", None
    if days == 30 and price_rub == 399 and "премиум" in label_norm:
        return "vhq", "basic", None
    # Tariffs synced from the bot DB default to Marzban unless they explicitly
    # carry an Adapt UUID or match the Darimiru VHQ exceptions above.
    return "marzban", None, None


def _sqlite_path_from_url(url: str) -> Path | None:
    for prefix in ("sqlite+aiosqlite:///", "sqlite:///"):
        if url.startswith(prefix):
            raw = url.removeprefix(prefix)
            return Path(raw) if raw.startswith("/") else (_ENV_FILE.parent / raw)
    return None


def _bot_db_path() -> Path | None:
    if settings.bot_db_path:
        return Path(settings.bot_db_path)
    database_url = os.getenv("DATABASE_URL", "").strip()
    if database_url:
        candidate = _sqlite_path_from_url(database_url)
        if candidate and candidate.exists():
            return candidate
    for name in ("darimiru_bot.db", "vpn_bot.db"):
        candidate = _ENV_FILE.parent / name
        if candidate.exists():
            return candidate
    return None


def _load_bot_db_tariffs() -> list[dict] | None:
    if not _env_bool("WEBSTORE_SYNC_TARIFFS_FROM_BOT_DB", True):
        return None
    path = _bot_db_path()
    if not path or not path.exists():
        return None

    rows: list[sqlite3.Row]
    try:
        con = sqlite3.connect(str(path))
        con.row_factory = sqlite3.Row
        try:
            columns = {row[1] for row in con.execute("PRAGMA table_info(tariffs)").fetchall()}
            adapt_expr = "COALESCE(adapt_plan_uuid, '')" if "adapt_plan_uuid" in columns else "''"
            vhq_expr = "COALESCE(vhq_tier, '')" if "vhq_tier" in columns else "''"
            rows = con.execute(
                f"""
                SELECT id, label, days, price_rub, price_stars, tariff_type,
                       is_active, COALESCE(is_admin_only, 0) AS is_admin_only,
                       {adapt_expr} AS adapt_plan_uuid,
                       {vhq_expr} AS vhq_tier
                FROM tariffs
                WHERE is_active = 1 AND COALESCE(is_admin_only, 0) = 0
                ORDER BY price_rub, days, id
                """
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error:
        return None

    tariffs: list[dict] = []
    seen_keys: set[str] = set()
    for row in rows:
        if str(row["tariff_type"]) not in {"VPN", "BOTH"}:
            continue
        label = str(row["label"])
        days = int(row["days"] or 0)
        price_rub = int(row["price_rub"] or 0)
        if days <= 0 or price_rub <= 0:
            continue
        provider, vhq_tier, adapt_plan_uuid = _provider_from_bot_tariff(
            label,
            days,
            price_rub,
            str(row["adapt_plan_uuid"] or "").strip(),
            str(row["vhq_tier"] or "").strip(),
        )
        key = _webstore_key(label, days, provider, vhq_tier or "")
        if key in seen_keys:
            key = f"{key}_{int(row['id'])}"
        seen_keys.add(key)
        tariff = {
            "key": key,
            "label": label,
            "days": days,
            "price_rub": price_rub,
            "description": _provider_description(provider, label, vhq_tier or ""),
            "badge": _tariff_badge(label, days, provider, vhq_tier or ""),
            "provider": provider,
            "features": _provider_features(provider, label, vhq_tier or ""),
        }
        if vhq_tier:
            tariff["vhq_tier"] = vhq_tier
        if adapt_plan_uuid:
            tariff["adapt_plan_uuid"] = adapt_plan_uuid
        tariffs.append(tariff)
    return tariffs or None

_adapt_basic_30_uuid = os.getenv("ADAPT_PLAN_UUID_BASIC_30", "").strip()
_static_basic_30_provider = "adapt" if _adapt_basic_30_uuid else "vhq"

TARIFFS = [
    {
        "key": "vpn_30",
        "label": "Лайт (1 месяц)",
        "days": 30,
        "price_rub": _env_int("WEBSTORE_TARIFF_VPN_30_PRICE_RUB", 95),
        "description": "Ускорение всех сервисов. Стабильная работа на wifi и с мобильного интернета, когда нет блокировок",
        "badge": "",
        "provider": "marzban",
        "features": _MARZBAN_FEATURES,
    },
    {
        "key": "basic_1",
        "label": "Базовый (1 день)",
        "days": 1,
        "price_rub": _env_int("WEBSTORE_TARIFF_BASIC_1_PRICE_RUB", 10),
        "description": "С обходом блокировок на мобильном интернете. Тест на 1 день, один раз на пользователя.",
        "badge": "1 раз",
        "provider": "vhq",
        "vhq_tier": "lite",
        "features": _VHQ_LITE_FEATURES,
    },
    {
        "key": "basic_7",
        "label": "Базовый (7 дней)",
        "days": 7,
        "price_rub": _env_int("WEBSTORE_TARIFF_BASIC_7_PRICE_RUB", 59),
        "description": "С обходом блокировок на мобильном интернете",
        "badge": "",
        "provider": "vhq",
        "vhq_tier": "lite",
        "features": _VHQ_LITE_FEATURES,
    },
    {
        "key": "basic_30",
        "label": "Базовый (1 месяц)",
        "days": 30,
        "price_rub": _env_int("WEBSTORE_TARIFF_ADAPT_30_PRICE_RUB" if _adapt_basic_30_uuid else "WEBSTORE_TARIFF_BASIC_30_PRICE_RUB", 249),
        "description": _provider_description(_static_basic_30_provider, "Базовый (1 месяц)"),
        "badge": "",
        "provider": _static_basic_30_provider,
        **({"adapt_plan_uuid": _adapt_basic_30_uuid} if _adapt_basic_30_uuid else {"vhq_tier": "lite"}),
        "features": _provider_features(_static_basic_30_provider, "Базовый (1 месяц)"),
    },
    {
        "key": "premium_30",
        "label": "Премиум (1 месяц)",
        "days": 30,
        "price_rub": _env_int("WEBSTORE_TARIFF_PREMIUM_30_PRICE_RUB", 399),
        "description": "Максимум возможностей по обходу блокировок и отключений. До 70 серверов и самый быстрый вариант на все случаи жизни.",
        "badge": "Топ",
        "provider": "vhq",
        "vhq_tier": "basic",
        "features": _VHQ_PREMIUM_FEATURES,
    },
]

if _env_bool("WEBSTORE_TARIFF_VPN_7_ENABLED"):
    TARIFFS.insert(0, {
        "key": "vpn_7",
        "label": "7 дней",
        "days": 7,
        "price_rub": _env_int("WEBSTORE_TARIFF_VPN_7_PRICE_RUB", 23),
        "description": "Ускорение всех сервисов. Стабильная работа на wifi и с мобильного интернета, когда нет блокировок",
        "badge": "",
        "provider": "marzban",
        "features": _MARZBAN_FEATURES,
    })

_synced_tariffs = _load_bot_db_tariffs()
if _synced_tariffs:
    TARIFFS = _synced_tariffs

_enabled_tariff_keys = set(_env_list("WEBSTORE_ENABLED_TARIFF_KEYS"))
if _enabled_tariff_keys:
    TARIFFS = [t for t in TARIFFS if str(t.get("key")) in _enabled_tariff_keys]

TARIFFS_BY_KEY = {t["key"]: t for t in TARIFFS}


def get_store_tariffs() -> list[dict]:
    """Return the current public tariff catalog.

    In production this reads the bot DB on every call, so changes made in the
    Telegram admin panel are reflected by the web store without a restart.
    """
    tariffs = _load_bot_db_tariffs()
    if tariffs is None:
        tariffs = list(TARIFFS)
    if _enabled_tariff_keys:
        tariffs = [t for t in tariffs if str(t.get("key")) in _enabled_tariff_keys]
    return tariffs


def get_store_tariffs_by_key() -> dict[str, dict]:
    return {str(t["key"]): t for t in get_store_tariffs()}
