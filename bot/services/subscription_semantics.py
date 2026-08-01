"""Shared predicates for demo vs paid subscription rows."""

from __future__ import annotations

from sqlalchemy import and_, or_


def is_demo_subscription_row(sub) -> bool:
    """Return True only for a real demo row."""
    return str(getattr(sub, "billing_mode", "") or "") == "demo"


def is_adapt_trial_tariff(tariff) -> bool:
    """Return True for a short Adapt plan used as introductory access."""
    return bool(
        tariff
        and str(getattr(tariff, "adapt_plan_uuid", "") or "").strip()
        and 0 < int(getattr(tariff, "days", 0) or 0) <= 7
    )


def is_adapt_trial_subscription(sub) -> bool:
    """Recognise explicit demos and legacy 1–7 day Adapt subscriptions."""
    return is_demo_subscription_row(sub) or is_adapt_trial_tariff(getattr(sub, "tariff", None))


def paid_access_clause(model):
    """SQLAlchemy clause for rows that should be treated as non-demo access."""
    return model.billing_mode != "demo"


def demo_access_clause(model):
    """SQLAlchemy clause for real demo rows."""
    return model.billing_mode == "demo"
