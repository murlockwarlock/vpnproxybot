"""Database models for the VPN bot."""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


# ── Enums ─────────────────────────────────────────────

class SubStatus(str, enum.Enum):
    ACTIVE = "active"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"


class PaymentMethod(str, enum.Enum):
    STARS = "stars"
    TELEGRAM = "telegram"   # Native Telegram Pay (card via Telegram API)
    YOOKASSA = "yookassa"
    ROBOKASSA = "robokassa"
    MANUAL = "manual"
    BALANCE = "balance"


class BalanceTransactionKind(str, enum.Enum):
    TOPUP = "topup"
    DAILY_CHARGE = "daily_charge"
    REFERRAL_BONUS = "referral_bonus"
    CASHBACK = "cashback"
    DEVICE_SLOT_PURCHASE = "device_slot_purchase"
    MANUAL_ADJUSTMENT = "manual_adjustment"
    REFUND = "refund"
    MIGRATION = "migration"


class BalanceDirection(str, enum.Enum):
    CREDIT = "credit"
    DEBIT = "debit"


class Platform(str, enum.Enum):
    ANDROID = "android"
    IOS = "ios"
    MAC = "mac"
    WINDOWS = "windows"
    ANDROID_TV = "android_tv"


class TariffType(str, enum.Enum):
    VPN = "vpn"
    TG_PROXY = "tg_proxy"
    BOTH = "both"


class PartnerPlatform(str, enum.Enum):
    YOUTUBE = "youtube"
    INSTAGRAM = "instagram"
    TELEGRAM = "telegram"
    TIKTOK = "tiktok"
    OTHER = "other"


class PartnerPayoutStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class PartnerApplicationStatus(str, enum.Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


# ── Models ────────────────────────────────────────────

class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    platform: Mapped[Platform | None] = mapped_column(Enum(Platform), nullable=True)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    referral_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    referred_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    referral_balance: Mapped[float] = mapped_column(Float, default=0.0)
    balance_rub: Mapped[float] = mapped_column(Float, default=0.0)
    balance_autodebit_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    balance_mode_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    balance_grace_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_daily_charge_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_daily_charge_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    balance_warning_for_charge_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    bonus_days: Mapped[int] = mapped_column(Integer, default=0)
    partner_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("partners.id"), nullable=True)
    partner_link_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("partner_links.id"), nullable=True)
    ad_source: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ad_source_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # Multi-level referral chain tracking.
    # referral_root_partner_id: the partner whose post/link originated this user's chain.
    # referral_depth: how many hops from the partner link (1=direct, 2=invited by level-1 user, …).
    referral_root_partner_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("partners.id"), nullable=True)
    referral_depth: Mapped[int] = mapped_column(Integer, default=0)
    # Which tariff to use for daily balance charges (nullable = global rate + Marzban provider).
    daily_charge_tariff_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("tariffs.id"), nullable=True)

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="user", lazy="selectin")
    payments: Mapped[list["Payment"]] = relationship(back_populates="user", lazy="selectin")
    proxy_accounts: Mapped[list["ProxyAccount"]] = relationship(back_populates="user", lazy="selectin")


class Server(Base):
    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    host: Mapped[str] = mapped_column(String(128), nullable=False)
    # Marzban API credentials
    api_url: Mapped[str | None] = mapped_column(String(256), nullable=True) # e.g. https://panel.domain.com:8000
    api_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    api_password: Mapped[str | None] = mapped_column(String(128), nullable=True)

    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    ssh_user: Mapped[str] = mapped_column(String(32), default="root")
    ssh_key_path: Mapped[str] = mapped_column(String(256), default="~/.ssh/id_rsa")
    location: Mapped[str] = mapped_column(String(64), nullable=False)  # "Amsterdam"
    country_emoji: Mapped[str] = mapped_column(String(8), default="🌍")  # "🇳🇱"
    protocol: Mapped[str] = mapped_column(String(32), default="Marzban")
    max_clients: Mapped[int] = mapped_column(Integer, default=50)
    current_clients: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    subscriptions: Mapped[list["Subscription"]] = relationship(back_populates="server", lazy="selectin")


class ProxyAccount(Base):
    __tablename__ = "proxy_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"), nullable=False)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"), nullable=True)
    marzban_username: Mapped[str] = mapped_column(String(128), nullable=False)
    sub_url: Mapped[str] = mapped_column(Text, nullable=False)
    device_limit: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    user: Mapped["User"] = relationship(back_populates="proxy_accounts")
    server: Mapped["Server"] = relationship()
    subscription: Mapped["Subscription"] = relationship()


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"), nullable=False)
    tariff_months: Mapped[int] = mapped_column(Integer, nullable=False)
    tariff_days: Mapped[int] = mapped_column(Integer, default=0)
    billing_mode: Mapped[str] = mapped_column(String(16), default="tariff")
    status: Mapped[SubStatus] = mapped_column(Enum(SubStatus), default=SubStatus.ACTIVE)
    device_slots: Mapped[int] = mapped_column(Integer, default=1)
    vpn_key: Mapped[str | None] = mapped_column(Text, nullable=True)
    client_name: Mapped[str] = mapped_column(String(64), nullable=False)
    platform: Mapped[Platform] = mapped_column(Enum(Platform), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)

    # FK to the Tariff that created/last-renewed this subscription (nullable for legacy rows).
    tariff_id: Mapped[int | None] = mapped_column(ForeignKey("tariffs.id"), nullable=True)

    user: Mapped["User"] = relationship(back_populates="subscriptions")
    server: Mapped["Server"] = relationship(back_populates="subscriptions")
    payment: Mapped["Payment"] = relationship(back_populates="subscription", uselist=False)
    tariff: Mapped["Tariff | None"] = relationship(foreign_keys=[tariff_id])


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    subscription_id: Mapped[int | None] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=True
    )
    amount: Mapped[int] = mapped_column(Integer, nullable=False)  # in Stars or kopecks
    currency: Mapped[str] = mapped_column(String(8), default="XTR")  # XTR = Stars
    method: Mapped[PaymentMethod] = mapped_column(Enum(PaymentMethod), nullable=False)
    status: Mapped[PaymentStatus] = mapped_column(
        Enum(PaymentStatus), default=PaymentStatus.PENDING
    )
    provider_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discount_applied: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    
    tariff_id: Mapped[int | None] = mapped_column(ForeignKey("tariffs.id"), nullable=True)
    platform: Mapped[str | None] = mapped_column(String(32), nullable=True)
    provisioning_operation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    provisioning_baseline_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    provisioning_baseline_plan_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    provisioning_failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)

    user: Mapped["User"] = relationship(back_populates="payments")
    subscription: Mapped["Subscription"] = relationship(back_populates="payment")
    tariff: Mapped["Tariff | None"] = relationship(foreign_keys=[tariff_id])


class Mailing(Base):
    __tablename__ = "mailings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_file_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # photo/video
    media_position: Mapped[str] = mapped_column(String(16), default="media_top")
    buttons_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of button configs
    target_audience: Mapped[str] = mapped_column(String(32), nullable=False)
    creator_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/sending/completed/failed
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    start_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)


class ReferralConfig(Base):
    __tablename__ = "referral_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)   # singleton pk=1
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    commission_percent: Mapped[float] = mapped_column(Float, default=10.0)
    # Bonus-days referral settings
    btn_name: Mapped[str] = mapped_column(String(64), default="👥 Пригласить друзей")
    sub_btn_name: Mapped[str] = mapped_column(String(64), default="🤝 Бонус за приглашение")
    bonus_days_referrer: Mapped[int] = mapped_column(Integer, default=1)
    bonus_days_referral: Mapped[int] = mapped_column(Integer, default=1)
    pay_bonus_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    pay_bonus_days: Mapped[int] = mapped_column(Integer, default=1)
    pay_bonus_first_only: Mapped[bool] = mapped_column(Boolean, default=True)


class ReferralEarning(Base):
    __tablename__ = "referral_earnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    referred_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)
    amount_rub: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReferralPaymentLog(Base):
    """Tracks all payments made by referred users (for turnover stats and pay bonus)."""
    __tablename__ = "referral_payment_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    referred_user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    paid_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BalanceTransaction(Base):
    __tablename__ = "balance_transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    kind: Mapped[BalanceTransactionKind] = mapped_column(
        Enum(BalanceTransactionKind), nullable=False
    )
    direction: Mapped[BalanceDirection] = mapped_column(
        Enum(BalanceDirection), nullable=False
    )
    amount_rub: Mapped[float] = mapped_column(Float, nullable=False)
    balance_after_rub: Mapped[float] = mapped_column(Float, nullable=False)
    description: Mapped[str | None] = mapped_column(String(255), nullable=True)
    source_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, index=True)


class BalanceTopUp(Base):
    __tablename__ = "balance_topups"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True, index=True)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    profile_token: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    amount_rub: Mapped[float] = mapped_column(Float, nullable=False)
    cashback_amount_rub: Mapped[float] = mapped_column(Float, default=0.0)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    provider_payment_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WebReferralEarning(Base):
    __tablename__ = "web_referral_earnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    referrer_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    web_order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    ref_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    buyer_contact: Mapped[str | None] = mapped_column(String(256), nullable=True)
    payment_amount_rub: Mapped[float] = mapped_column(Float, nullable=False)
    earning_amount_rub: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class WebPartnerEarning(Base):
    __tablename__ = "web_partner_earnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    partner_link_id: Mapped[int | None] = mapped_column(ForeignKey("partner_links.id"), nullable=True, index=True)
    web_order_id: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    ref_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    buyer_contact: Mapped[str | None] = mapped_column(String(256), nullable=True)
    tariff_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payment_amount_rub: Mapped[float] = mapped_column(Float, nullable=False)
    earning_amount_rub: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RobokassaPayment(Base):
    __tablename__ = "robokassa_payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    server_id: Mapped[int] = mapped_column(ForeignKey("servers.id"), nullable=False)
    tariff_idx: Mapped[int] = mapped_column(Integer, nullable=True)         # legacy
    tariff_id: Mapped[int | None] = mapped_column(ForeignKey("tariffs.id"), nullable=True)  # new DB tariff
    platform: Mapped[str] = mapped_column(String(16), nullable=False)   # "android" / "ios"
    amount: Mapped[float] = mapped_column(nullable=False)
    discount_applied: Mapped[float] = mapped_column(Float, default=0.0)
    status: Mapped[str] = mapped_column(String(16), default="pending")  # pending/completed/failed
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RAGConfig(Base):
    __tablename__ = "rag_config"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)   # singleton pk=1
    system_prompt: Mapped[str] = mapped_column(Text, default="Ты - умный и вежливый AI-помощник сервиса. Помогай пользователям по вопросам подключения и оплаты, используя загруженные базы знаний.")
    temperature: Mapped[float] = mapped_column(Float, default=0.7)


class RAGDocument(Base):
    __tablename__ = "rag_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    filename: Mapped[str] = mapped_column(String(256), nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ── New: DB-managed tariffs ───────────────────────────

class Tariff(Base):
    """VPN subscription tariff stored in the database (managed via admin panel)."""
    __tablename__ = "tariffs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    days: Mapped[int] = mapped_column(Integer, nullable=False)          # duration in days
    label: Mapped[str] = mapped_column(String(64), nullable=False)      # e.g. "1 месяц"
    price_rub: Mapped[int] = mapped_column(Integer, nullable=False)     # price in rubles
    price_stars: Mapped[int] = mapped_column(Integer, default=0)        # Telegram Stars price
    tariff_type: Mapped[TariffType] = mapped_column(
        Enum(TariffType), default=TariffType.VPN, nullable=False,
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # If True, tariff is hidden from regular buy flow — visible only in admin key issuance
    is_admin_only: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    # Adapt Group integration: if set, this tariff is fulfilled via Adapt API
    adapt_plan_uuid: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Adapt plan cost in USD for profit / upgrade math
    adapt_cost_price_usd: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Number of devices this tariff includes (None = use default included_devices_per_sub)
    device_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # VHQ integration: explicit tier (lite/basic). Keeps provider stable when label changes.
    vhq_tier: Mapped[str | None] = mapped_column(String(16), nullable=True)


class BotSettings(Base):
    """Key-value settings store for bot-wide configuration."""
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text, nullable=False)

    # Known keys:
    # stars_enabled            "1" / "0"
    # max_devices_per_sub      "3"
    # daily_charge_rub         "3.17"
    # extra_device_price_rub   "50"
    # extra_device_price_stars "50"
    # demo_key_enabled         "1" / "0"  — выдавать бесплатный ключ новым пользователям
    # demo_key_days            "3"         — срок демо-доступа в днях
    # notify_admins_payment    "1" / "0"  — уведомлять всех админов о каждой оплате
    # followup_enabled         "1" / "0"  — авторассылка тем, кто взял демо и не купил
    # followup_days            "4"         — через сколько дней после демо
    # followup_text            "..."       — текст догоняющей рассылки (HTML)
    # followup_media_file_id   "..."       — telegram file_id медиа (опционально)
    # followup_media_type      "photo"/"video"


class PlatformGuide(Base):
    """Media and text setup guide per platform."""
    __tablename__ = "platform_guides"

    platform: Mapped[str] = mapped_column(String(32), primary_key=True)  # android/ios/mac/windows/android_tv
    guide_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_file_id: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)  # photo/video/album
    buttons_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FollowUpCampaign(Base):
    """A follow-up mailing campaign sent N days after demo to non-paying users."""
    __tablename__ = "followup_campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    days_after_demo: Mapped[int] = mapped_column(Integer, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    media_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    buttons_json: Mapped[str | None] = mapped_column(Text, nullable=True)  # JSON list of button configs
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class FollowUpLog(Base):
    """Tracks which campaign was sent to which user."""
    __tablename__ = "followup_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("followup_campaigns.id"), nullable=True, index=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class SubscriptionNotificationLog(Base):
    """Tracks one-off notifications sent for a subscription."""
    __tablename__ = "subscription_notification_logs"
    __table_args__ = (
        UniqueConstraint("subscription_id", "notification_code", name="uq_subscription_notification"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(ForeignKey("subscriptions.id"), nullable=False, index=True)
    notification_code: Mapped[str] = mapped_column(String(32), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class RecurringPaymentProfile(Base):
    """Recurring billing consent and the next planned charge date."""
    __tablename__ = "recurring_payment_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"), nullable=True, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="unknown")
    provider_customer_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    provider_payment_method_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    payment_method_label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    tariff_id: Mapped[int | None] = mapped_column(ForeignKey("tariffs.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    consent_granted: Mapped[bool] = mapped_column(Boolean, default=False)
    next_charge_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_charge_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    payment_attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_payment_attempt: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )

    user: Mapped["User"] = relationship()
    subscription: Mapped["Subscription"] = relationship()
    tariff: Mapped["Tariff"] = relationship()


class RecurringNotificationLog(Base):
    """Tracks recurring charge reminders to avoid duplicate warnings."""
    __tablename__ = "recurring_notification_logs"
    __table_args__ = (
        UniqueConstraint("recurring_profile_id", "notification_code", name="uq_recurring_notification"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    recurring_profile_id: Mapped[int] = mapped_column(
        ForeignKey("recurring_payment_profiles.id"),
        nullable=False,
        index=True,
    )
    notification_code: Mapped[str] = mapped_column(String(32), nullable=False)
    sent_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MTProtoAccount(Base):
    """MTProto proxy secret for a user - provides Telegram proxy access."""
    __tablename__ = "mtproto_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    subscription_id: Mapped[int | None] = mapped_column(ForeignKey("subscriptions.id"), nullable=True)
    secret: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    label: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user: Mapped["User"] = relationship()
    subscription: Mapped["Subscription"] = relationship()


class Feedback(Base):
    """User feedback sent via the bot."""
    __tablename__ = "feedbacks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_file_id: Mapped[str | None] = mapped_column(String(256), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class AppLease(Base):
    """Distributed lease used for leader election and short-lived processing locks."""
    __tablename__ = "app_leases"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lease_until: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
    )


class AdTrackingLink(Base):
    """Managed ad deep link shown in admin for Telegram Ads and manual campaigns."""
    __tablename__ = "ad_tracking_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(128), nullable=False)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    source_kind: Mapped[str] = mapped_column(String(32), nullable=False, default="custom")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ── Partners ─────────────────────────────────────────

class Partner(Base):
    """Blogger / affiliate partner."""
    __tablename__ = "partners"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, unique=True)
    contact_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    commission_percent: Mapped[float] = mapped_column(Float, default=0.0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    valid_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    welcome_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    audience_discount_percent: Mapped[float] = mapped_column(Float, default=0.0)
    audience_bonus_days: Mapped[int] = mapped_column(Integer, default=0)
    payouts_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    min_payout: Mapped[float] = mapped_column(Float, default=1000.0)
    partner_balance: Mapped[float] = mapped_column(Float, default=0.0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    links: Mapped[list["PartnerLink"]] = relationship(back_populates="partner", lazy="selectin")


class PartnerLink(Base):
    """Tracking link per platform for a partner."""
    __tablename__ = "partner_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    platform: Mapped[PartnerPlatform] = mapped_column(Enum(PartnerPlatform), nullable=False)
    label: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    partner: Mapped["Partner"] = relationship(back_populates="links")


class PartnerEarning(Base):
    """Commission earnings log for a partner."""
    __tablename__ = "partner_earnings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False)
    payment_id: Mapped[int | None] = mapped_column(ForeignKey("payments.id"), nullable=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class PartnerPayout(Base):
    """Partner payout request and processing log."""
    __tablename__ = "partner_payouts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    partner_id: Mapped[int] = mapped_column(ForeignKey("partners.id"), nullable=False, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[PartnerPayoutStatus] = mapped_column(
        Enum(PartnerPayoutStatus),
        default=PartnerPayoutStatus.PENDING,
        nullable=False,
    )
    admin_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)


class PartnerApplication(Base):
    """User-submitted application to become a partner."""
    __tablename__ = "partner_applications"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(128), default="")
    contact_info: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[PartnerApplicationStatus] = mapped_column(
        Enum(PartnerApplicationStatus),
        default=PartnerApplicationStatus.PENDING,
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    admin_comment: Mapped[str | None] = mapped_column(Text, nullable=True)


# ── Adapt Group integration ───────────────────────────

class AdaptSubscription(Base):
    """Adapt Group VPN subscription details linked to a bot Subscription."""
    __tablename__ = "adapt_subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id"), nullable=False, unique=True, index=True
    )
    adapt_uuid: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    adapt_plan_uuid: Mapped[str] = mapped_column(String(64), nullable=False)
    is_frozen: Mapped[bool] = mapped_column(Boolean, default=False)
    frozen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    traffic_limit_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    subscription: Mapped["Subscription"] = relationship()
