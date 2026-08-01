"""Database models for the web store."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import BigInteger, Boolean, Column, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class WebOrder(Base):
    __tablename__ = "web_orders"

    id = Column(Integer, primary_key=True, autoincrement=True)
    order_id = Column(String(64), unique=True, nullable=False, index=True)
    contact = Column(String(256), nullable=True)
    email = Column(String(256), nullable=True)
    tariff_key = Column(String(32), nullable=False)
    tariff_label = Column(String(64), nullable=False)
    days = Column(Integer, nullable=False)
    amount_rub = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    yookassa_payment_id = Column(String(128), nullable=True)
    marzban_username = Column(String(128), nullable=True)
    subscription_url = Column(Text, nullable=True)
    ip_address = Column(String(45), nullable=True)
    ref_source = Column(String(128), nullable=True)
    entry_referrer = Column(String(512), nullable=True)   # document.referrer at order time
    entry_url = Column(String(512), nullable=True)        # page URL with UTM at order time
    referrer_telegram_id = Column(BigInteger, nullable=True, index=True)
    referral_status = Column(String(16), nullable=True)
    referral_credited_at = Column(DateTime, nullable=True)
    original_amount_rub = Column(Integer, nullable=True)
    bonus_applied_rub = Column(Integer, nullable=True, default=0)
    bonus_spent_at = Column(DateTime, nullable=True)
    access_expires_at = Column(DateTime, nullable=True)
    profile_token = Column(String(64), nullable=True, index=True)
    purchase_action = Column(String(16), nullable=False, default="new")
    target_order_id = Column(String(64), nullable=True, index=True)
    failure_message = Column(String(255), nullable=True)
    failure_code = Column(String(64), nullable=True)
    failure_reason = Column(Text, nullable=True)
    fulfillment_attempts = Column(Integer, nullable=False, default=0)
    next_fulfillment_retry_at = Column(DateTime, nullable=True, index=True)
    last_fulfillment_attempt_at = Column(DateTime, nullable=True)
    target_snapshot_expires_at = Column(DateTime, nullable=True)
    target_snapshot_plan_uuid = Column(String(64), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    paid_at = Column(DateTime, nullable=True)
    delivered_at = Column(DateTime, nullable=True)


class WebNotificationOutbox(Base):
    """Persistent Telegram notifications for payment-critical events."""

    __tablename__ = "web_notification_outbox"

    id = Column(Integer, primary_key=True, autoincrement=True)
    dedupe_key = Column(String(160), unique=True, nullable=False, index=True)
    recipient_id = Column(BigInteger, nullable=False, index=True)
    event = Column(String(32), nullable=False)
    text = Column(Text, nullable=False)
    status = Column(String(16), nullable=False, default="pending", index=True)
    attempts = Column(Integer, nullable=False, default=0)
    next_attempt_at = Column(DateTime, nullable=False, default=datetime.utcnow, index=True)
    last_attempt_at = Column(DateTime, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    sent_at = Column(DateTime, nullable=True)


class WebAccount(Base):
    __tablename__ = "web_accounts"

    id = Column(Integer, primary_key=True, autoincrement=True)
    contact = Column(String(256), unique=True, nullable=False, index=True)
    profile_token = Column(String(64), unique=True, nullable=False, index=True)
    password_hash = Column(String(256), nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_login_at = Column(DateTime, nullable=True)


class WebProfileLink(Base):
    __tablename__ = "web_profile_links"

    profile_token = Column(String(64), primary_key=True)
    contact = Column(String(256), nullable=True)
    telegram_id = Column(BigInteger, nullable=False, unique=True, index=True)
    telegram_username = Column(String(64), nullable=True)
    telegram_full_name = Column(String(128), nullable=True)
    linked_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_synced_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class WebProfileLinkCode(Base):
    __tablename__ = "web_profile_link_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(64), unique=True, nullable=False, index=True)
    profile_token = Column(String(64), nullable=False, index=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    consumed_at = Column(DateTime, nullable=True)


class WebTelegramAuthCode(Base):
    __tablename__ = "web_telegram_auth_codes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    code = Column(String(64), unique=True, nullable=False, index=True)
    profile_token = Column(String(64), nullable=True, index=True)
    telegram_id = Column(BigInteger, nullable=True, index=True)
    telegram_username = Column(String(64), nullable=True)
    telegram_full_name = Column(String(128), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    consumed_at = Column(DateTime, nullable=True)


class WebTelegramItem(Base):
    __tablename__ = "web_telegram_items"
    __table_args__ = (
        UniqueConstraint("profile_token", "item_type", "external_id", name="uq_web_telegram_item"),
    )

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_token = Column(String(64), nullable=False, index=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    item_type = Column(String(16), nullable=False)
    external_id = Column(String(64), nullable=False)
    title = Column(String(128), nullable=False)
    subtitle = Column(String(256), nullable=True)
    key_value = Column(Text, nullable=True)
    provider = Column(String(16), nullable=True)
    adapt_plan_uuid = Column(String(64), nullable=True)
    status = Column(String(16), nullable=False, default="active")
    device_slots = Column(Integer, nullable=True)
    expires_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class WebBalanceTopUp(Base):
    __tablename__ = "web_balance_topups"

    id = Column(Integer, primary_key=True, autoincrement=True)
    topup_id = Column(String(64), unique=True, nullable=False, index=True)
    profile_token = Column(String(64), nullable=False, index=True)
    telegram_id = Column(BigInteger, nullable=True, index=True)
    amount_rub = Column(Integer, nullable=False)
    status = Column(String(16), nullable=False, default="pending")
    yookassa_payment_id = Column(String(128), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)


class WebBalanceAccount(Base):
    __tablename__ = "web_balance_accounts"

    profile_token = Column(String(64), primary_key=True)
    contact = Column(String(256), nullable=True)
    ref_code = Column(String(32), unique=True, nullable=False, index=True)
    balance_rub = Column(Integer, nullable=False, default=0)
    total_earned_rub = Column(Integer, nullable=False, default=0)
    total_spent_rub = Column(Integer, nullable=False, default=0)
    balance_mode_enabled = Column(Integer, nullable=False, default=0)
    balance_autodebit_enabled = Column(Integer, nullable=False, default=0)
    balance_grace_until = Column(DateTime, nullable=True)
    last_daily_charge_at = Column(DateTime, nullable=True)
    next_daily_charge_at = Column(DateTime, nullable=True)
    balance_warning_for_charge_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    updated_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class WebBalanceTransaction(Base):
    __tablename__ = "web_balance_transactions"

    id = Column(Integer, primary_key=True, autoincrement=True)
    profile_token = Column(String(64), nullable=False, index=True)
    amount_rub = Column(Integer, nullable=False)
    direction = Column(String(16), nullable=False)
    kind = Column(String(32), nullable=False)
    balance_after_rub = Column(Integer, nullable=False)
    description = Column(String(255), nullable=True)
    source_id = Column(String(64), nullable=True, index=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


# ---------------------------------------------------------------------------
# Support chat
# ---------------------------------------------------------------------------

class SupportTicket(Base):
    __tablename__ = "support_tickets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    token = Column(String(64), unique=True, nullable=False, index=True)
    client_contact = Column(String(256), nullable=True)
    client_messenger = Column(String(32), nullable=True)   # telegram / vk / phone / other
    status = Column(String(16), nullable=False, default="open")  # open / resolved
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    last_message_at = Column(DateTime, nullable=True)
    resolved_at = Column(DateTime, nullable=True)


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id = Column(Integer, primary_key=True, autoincrement=True)
    ticket_id = Column(Integer, ForeignKey("support_tickets.id"), nullable=False, index=True)
    sender = Column(String(16), nullable=False)            # client / agent
    agent_telegram_id = Column(BigInteger, nullable=True)
    agent_name = Column(String(128), nullable=True)
    text = Column(Text, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)


class SupportAgentSession(Base):
    __tablename__ = "support_agent_sessions"

    token = Column(String(64), primary_key=True)
    telegram_id = Column(BigInteger, nullable=False, index=True)
    telegram_username = Column(String(64), nullable=True)
    telegram_full_name = Column(String(128), nullable=True)
    expires_at = Column(DateTime, nullable=False)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
