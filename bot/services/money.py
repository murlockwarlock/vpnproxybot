"""Canonical RUB money conversions for payment-critical paths."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import SupportsFloat


RUB_QUANTUM = Decimal("0.01")
MIN_YOOKASSA_KOPECKS = 1000


def _as_decimal(value: Decimal | SupportsFloat | str | int) -> Decimal:
    try:
        decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"Invalid RUB amount: {value!r}") from exc
    if not decimal_value.is_finite():
        raise ValueError(f"Invalid non-finite RUB amount: {value!r}")
    return decimal_value


def rub_to_kopecks(value: Decimal | SupportsFloat | str | int) -> int:
    """Convert RUB to integer kopecks using one explicit half-up rule."""
    normalized = _as_decimal(value).quantize(RUB_QUANTUM, rounding=ROUND_HALF_UP)
    return int(normalized * 100)


def kopecks_to_decimal(value: int) -> Decimal:
    """Convert integer kopecks to a two-decimal Decimal RUB value."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("Kopecks must be an integer")
    return (Decimal(value) / Decimal(100)).quantize(RUB_QUANTUM)


def kopecks_to_rub_string(value: int) -> str:
    """Return the exact two-decimal provider representation for kopecks."""
    return f"{kopecks_to_decimal(value):.2f}"


def kopecks_to_rub_float(value: int) -> float:
    """Return a legacy float boundary value derived from canonical kopecks."""
    return float(kopecks_to_decimal(value))


def canonical_discount_and_payable(
    purchase_price: Decimal | SupportsFloat | str | int,
    balance: Decimal | SupportsFloat | str | int,
    *,
    minimum_external_kopecks: int = 0,
) -> tuple[int, int, int]:
    """Return ``(purchase, discount, payable)`` in canonical kopecks.

    The discount is capped at the purchase price and adjusted downward when a
    provider minimum must be preserved. No float participates in the result.
    """
    purchase_kopecks = max(0, rub_to_kopecks(purchase_price))
    balance_kopecks = max(0, rub_to_kopecks(balance))
    discount_kopecks = min(balance_kopecks, purchase_kopecks)
    payable_kopecks = purchase_kopecks - discount_kopecks
    if purchase_kopecks >= minimum_external_kopecks and 0 < payable_kopecks < minimum_external_kopecks:
        payable_kopecks = minimum_external_kopecks
        discount_kopecks = max(0, purchase_kopecks - payable_kopecks)
    return purchase_kopecks, discount_kopecks, payable_kopecks
