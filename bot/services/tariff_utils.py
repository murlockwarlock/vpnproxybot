"""Helpers for formatting tariff durations and labels."""

from __future__ import annotations


def _plural(value: int, one: str, few: str, many: str) -> str:
    mod10 = value % 10
    mod100 = value % 100
    if mod10 == 1 and mod100 != 11:
        return one
    if 2 <= mod10 <= 4 and not 12 <= mod100 <= 14:
        return few
    return many


def format_duration_days(days: int) -> str:
    if days <= 0:
        return "N/A"
    if days == 365:
        return "1 год"
    if days % 30 == 0:
        months = days // 30
        return f"{months} {_plural(months, 'мес.', 'мес.', 'мес.')}"
    return f"{days} {_plural(days, 'день', 'дня', 'дней')}"


def format_subscription_duration(*, tariff_days: int | None, tariff_months: int | None) -> str:
    if tariff_days and tariff_days > 0:
        return format_duration_days(int(tariff_days))
    if tariff_months and tariff_months > 0:
        return f"{int(tariff_months)} мес."
    return "N/A"
