"""Shared predicates for demo vs paid subscription rows."""

from __future__ import annotations

from sqlalchemy import and_, or_


def is_demo_subscription_row(sub) -> bool:
    """Return True only for a real demo row, not a paid row reusing a demo key."""
    client_name = str(getattr(sub, "client_name", "") or "")
    if not client_name.endswith("_demo"):
        return False
    if getattr(sub, "tariff_id", None) is not None:
        return False
    if str(getattr(sub, "billing_mode", "") or "") == "balance":
        return False
    return True


def paid_access_clause(model):
    """SQLAlchemy clause for rows that should be treated as non-demo access."""
    return or_(
        model.client_name.notlike("%_demo"),
        model.tariff_id.is_not(None),
        model.billing_mode == "balance",
    )


def demo_access_clause(model):
    """SQLAlchemy clause for real demo rows."""
    return and_(
        model.client_name.like("%_demo"),
        model.tariff_id.is_(None),
        model.billing_mode != "balance",
    )
