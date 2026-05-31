"""Tests for adapt_routing.py helpers."""

from __future__ import annotations

import pytest

from bot.services.adapt_routing import (
    ADAPT_CLIENT_PREFIX,
    build_adapt_client_name,
    get_adapt_uuid_from_subscription,
    is_adapt_subscription,
    is_adapt_tariff,
)


# ── Fake model stubs ──────────────────────────────────────────────────────────

class _Tariff:
    def __init__(self, adapt_plan_uuid=None):
        self.adapt_plan_uuid = adapt_plan_uuid


class _Sub:
    def __init__(self, client_name=""):
        self.client_name = client_name


# ── is_adapt_tariff ───────────────────────────────────────────────────────────

def test_is_adapt_tariff_with_uuid():
    t = _Tariff(adapt_plan_uuid="plan-abc-123")
    assert is_adapt_tariff(t) is True


def test_is_adapt_tariff_none():
    t = _Tariff(adapt_plan_uuid=None)
    assert is_adapt_tariff(t) is False


def test_is_adapt_tariff_empty_string():
    t = _Tariff(adapt_plan_uuid="")
    assert is_adapt_tariff(t) is False


def test_is_adapt_tariff_no_attribute():
    class Bare:
        pass
    assert is_adapt_tariff(Bare()) is False


# ── is_adapt_subscription ─────────────────────────────────────────────────────

def test_is_adapt_subscription_with_prefix():
    s = _Sub(client_name="adapt_some-uuid-here")
    assert is_adapt_subscription(s) is True


def test_is_adapt_subscription_marzban():
    s = _Sub(client_name="tg12345_1")
    assert is_adapt_subscription(s) is False


def test_is_adapt_subscription_vhq():
    s = _Sub(client_name="vhq_some-uuid")
    assert is_adapt_subscription(s) is False


def test_is_adapt_subscription_empty():
    s = _Sub(client_name="")
    assert is_adapt_subscription(s) is False


def test_is_adapt_subscription_none_client_name():
    s = _Sub(client_name=None)
    assert is_adapt_subscription(s) is False


# ── get_adapt_uuid_from_subscription ─────────────────────────────────────────

def test_get_adapt_uuid_from_subscription_valid():
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    s = _Sub(client_name=f"adapt_{uuid}")
    result = get_adapt_uuid_from_subscription(s)
    assert result == uuid


def test_get_adapt_uuid_from_subscription_not_adapt():
    s = _Sub(client_name="tg12345_1")
    assert get_adapt_uuid_from_subscription(s) is None


def test_get_adapt_uuid_from_subscription_only_prefix():
    s = _Sub(client_name="adapt_")
    result = get_adapt_uuid_from_subscription(s)
    assert result is None


# ── build_adapt_client_name ───────────────────────────────────────────────────

def test_build_adapt_client_name():
    uuid = "test-uuid"
    name = build_adapt_client_name(uuid)
    assert name == f"adapt_{uuid}"
    assert name.startswith(ADAPT_CLIENT_PREFIX)


def test_build_adapt_client_name_truncated():
    long_uuid = "x" * 100
    name = build_adapt_client_name(long_uuid)
    assert len(name) <= 64
    assert name.startswith(ADAPT_CLIENT_PREFIX)
