from __future__ import annotations

import types

import pytest

from bot.services import payment_service

pytestmark = pytest.mark.asyncio


class _FakePaymentApi:
    last_payload = None

    @classmethod
    def create(cls, payload):
        cls.last_payload = payload
        return types.SimpleNamespace(
            id="yk_test_payment",
            confirmation=types.SimpleNamespace(confirmation_url="https://pay.test/confirm"),
        )


class _FakeYooKassa(types.SimpleNamespace):
    Configuration = types.SimpleNamespace(account_id=None, secret_key=None)
    Payment = _FakePaymentApi


async def test_create_yookassa_payment_saves_method_when_enabled(monkeypatch):
    monkeypatch.setattr(payment_service.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(payment_service.settings, "yookassa_secret_key", "secret")
    monkeypatch.setattr(payment_service.settings, "yookassa_save_payment_method", True)
    monkeypatch.setitem(__import__("sys").modules, "yookassa", _FakeYooKassa())

    await payment_service.create_yookassa_payment(
        user_id=1,
        tariff_id=2,
        platform="ios",
        chat_id=3,
        return_url="https://example.com/return",
        tariff_label="1 месяц",
        price_rub=95.0,
    )

    assert _FakePaymentApi.last_payload["save_payment_method"] is True


async def test_create_yookassa_payment_skips_method_save_when_disabled(monkeypatch):
    monkeypatch.setattr(payment_service.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(payment_service.settings, "yookassa_secret_key", "secret")
    monkeypatch.setattr(payment_service.settings, "yookassa_save_payment_method", False)
    monkeypatch.setitem(__import__("sys").modules, "yookassa", _FakeYooKassa())

    await payment_service.create_yookassa_payment(
        user_id=1,
        tariff_id=2,
        platform="ios",
        chat_id=3,
        return_url="https://example.com/return",
        tariff_label="1 месяц",
        price_rub=95.0,
    )

    assert "save_payment_method" not in _FakePaymentApi.last_payload


async def test_create_yookassa_payment_skips_method_save_when_recurring_disabled(monkeypatch):
    monkeypatch.setattr(payment_service.settings, "yookassa_shop_id", "shop")
    monkeypatch.setattr(payment_service.settings, "yookassa_secret_key", "secret")
    monkeypatch.setattr(payment_service.settings, "yookassa_save_payment_method", True)
    monkeypatch.setattr(payment_service.settings, "recurring_payments_enabled", False)
    monkeypatch.setitem(__import__("sys").modules, "yookassa", _FakeYooKassa())

    await payment_service.create_yookassa_payment(
        user_id=1,
        tariff_id=2,
        platform="ios",
        chat_id=3,
        return_url="https://example.com/return",
        tariff_label="1 месяц",
        price_rub=95.0,
    )

    assert "save_payment_method" not in _FakePaymentApi.last_payload

