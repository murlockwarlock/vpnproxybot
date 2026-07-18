"""Shared predicates for demo vs paid subscription rows."""

from __future__ import annotations

from sqlalchemy import and_, or_


def is_demo_subscription_row(sub) -> bool:
    """Return True only for a real demo row."""
    return str(getattr(sub, "billing_mode", "") or "") == "demo"


def paid_access_clause(model):
    """SQLAlchemy clause for rows that should be treated as non-demo access."""
    return model.billing_mode != "demo"


def demo_access_clause(model):
    """SQLAlchemy clause for real demo rows."""
    return model.billing_mode == "demo"
