from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import sys
import threading
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import web
from sqlalchemy import select

from test_webstore_telegram_linking import db_engine, db_session_factory, patched_webstore, _Request
from webstore import otp_auth, routes
from webstore.models import (
    SupportRecoveryAudit,
    SupportRecoveryRequest,
    SupportMessage,
    WebAccount,
    WebBalanceAccount,
    WebEmail,
    WebOrder,
    WebProfileLink,
    WebSession,
)

pytestmark = pytest.mark.asyncio


def payload(response):
    return json.loads(response.text)


def request(body=None, *, cookies=None, headers=None, path_args=None, query=None):
    value = _Request(_json_body=body or {}, cookies=cookies or {})
    value.headers = headers or {}
    value.match_info = path_args or {}
    value.rel_url = SimpleNamespace(query=query or {})
    return value


def mailed_code(sender):
    return re.search(r"Ваш код: (\d{6})", sender.await_args.args[2]).group(1)


async def call(coro):
    try:
        return await coro
    except web.HTTPException as exc:
        return exc


async def login_agent(patched_webstore):
    routes.settings.support_admin_password = "test-support-password"
    response = await routes.handle_support_admin_login(request({"password": "test-support-password"}))
    return payload(response)["token"]


def telegram_login_body(*, telegram_id=123456789, bot_token="test-bot-token"):
    body = {
        "id": str(telegram_id),
        "first_name": "Verified",
        "username": "verified_admin",
        "auth_date": str(int(time.time())),
    }
    check = "\n".join(f"{key}={value}" for key, value in sorted(body.items()))
    body["hash"] = hmac.new(hashlib.sha256(bot_token.encode()).digest(), check.encode(), hashlib.sha256).hexdigest()
    return body


async def login_telegram_agent(patched_webstore, monkeypatch):
    monkeypatch.setattr(routes.settings, "admin_bot_token", "test-bot-token")
    monkeypatch.setattr(routes.settings, "support_agent_ids", [123456789])
    response = await routes.handle_support_admin_login(request(telegram_login_body()))
    assert response.status == 200, response.text
    return payload(response)["token"]


def configure_fake_yookassa(
    monkeypatch,
    *,
    customer: str | None = "+71234567890",
    started: threading.Event | None = None,
    delay: float = 0,
    status="succeeded",
    paid=True,
    amount="499.00",
    currency="RUB",
    order_id="order-legacy-1",
    returned_id=None,
):
    class Payment:
        @classmethod
        def find_one(cls, payment_id):
            if started:
                started.set()
            if delay:
                time.sleep(delay)
            return SimpleNamespace(
                id=returned_id or payment_id,
                status=status,
                paid=paid,
                amount=SimpleNamespace(value=amount, currency=currency),
                metadata={"order_id": order_id},
                receipt=SimpleNamespace(customer=SimpleNamespace(phone=customer)),
            )

    monkeypatch.setattr(routes.settings, "yookassa_shop_id", "test-shop")
    monkeypatch.setattr(routes.settings, "yookassa_secret_key", "test-secret")
    monkeypatch.setitem(sys.modules, "yookassa", SimpleNamespace(
        Configuration=SimpleNamespace(account_id=None, secret_key=None),
        Payment=Payment,
    ))


async def seed_legacy_account(factory, *, trusted=True, with_telegram=False):
    async with factory() as session:
        session.add(WebAccount(
            contact="+71234567890",
            profile_token="legacy-phone",
            password_hash=routes._hash_password("old-password"),
        ))
        session.add(WebBalanceAccount(
            profile_token="legacy-phone",
            contact="+71234567890",
            balance_rub=321,
            ref_code="legacy-ref",
            balance_mode_enabled=1,
            balance_autodebit_enabled=1,
            next_daily_charge_at=datetime(2026, 1, 2),
        ))
        if with_telegram:
            session.add(WebProfileLink(
                profile_token="legacy-phone",
                contact="+71234567890",
                telegram_id=777000111,
                telegram_username="legacy_user",
            ))
        session.add(WebOrder(
            order_id="order-legacy-1",
            profile_token="legacy-phone",
            contact="+71234567890",
            tariff_key="vpn_30",
            tariff_label="VPN 30",
            days=30,
            amount_rub=499,
            status="delivered",
            yookassa_payment_id="payment-legacy-1" if trusted else None,
            paid_at=datetime.utcnow() if trusted else None,
            subscription_url="https://vpn.example/sub/legacy-1",
        ))
        await session.commit()


async def create_recovery_ticket():
    response = await routes.handle_support_new_ticket(request({
        "recovery": {
            "old_contact": "+7 123 456 7890",
            "reference": "order-legacy-1",
            "new_email": "recovery@example.com",
            "details": "Дата и сумма платежа известны поддержке.",
        }
    }))
    assert response.status == 200
    return payload(response)


@pytest.mark.parametrize(
    ("provider_customer", "manual_review"),
    [("81234567890", False), (None, True)],
    ids=["provider-contact", "provider-contact-missing"],
)
async def test_support_recovery_preserves_profile_and_rejects_replay(
    patched_webstore, monkeypatch, db_session_factory, provider_customer, manual_review
):
    sender = AsyncMock()
    monkeypatch.setattr(otp_auth, "send_email", sender)
    configure_fake_yookassa(monkeypatch, customer=provider_customer)
    await seed_legacy_account(db_session_factory, with_telegram=not manual_review)
    old_login = await routes.handle_auth_login(request({"contact": "+71234567890", "password": "old-password"}))
    old_cookie = {routes._AUTH_COOKIE: old_login.cookies[routes._AUTH_COOKIE].value}
    ticket = await create_recovery_ticket()
    ticket_token = ticket["token"]
    unauthorized = await call(routes.handle_support_admin_recovery_candidates(
        request(path_args={"ticket_id": str(ticket["ticket_id"])}),
    ))
    assert unauthorized.status == 401
    agent_token = await login_telegram_agent(patched_webstore, monkeypatch)
    headers = {"X-Support-Agent-Token": agent_token}

    candidates = await routes.handle_support_admin_recovery_candidates(request(headers=headers, path_args={"ticket_id": str(ticket["ticket_id"])}))
    candidate_data = payload(candidates)
    assert candidate_data["status"] == "pending"
    assert candidate_data["candidates"][0]["trusted_payment"] is True

    approval_body = {
        "profile_token": "legacy-phone",
        "order_id": "order-legacy-1",
        "verification_source": "yookassa_api",
        "verification_note": "Платёж найден у провайдера, статус succeeded и сумма совпадает.",
    }
    if manual_review:
        pending = await routes.handle_support_admin_recovery_approve(request(
            approval_body,
            headers=headers,
            path_args={"ticket_id": str(ticket["ticket_id"])}
        ))
        assert pending.status == 403
        pending_data = payload(pending)
        assert pending_data["recovery"]["payment_check"]["manual_review_required"] is True
        approval_body.update({
            "verification_source": "yookassa_manual",
            "manual_basis": "account_history",
            "verification_note": "История заказов и активная подписка сопоставлены с обращением клиента.",
        })
    approved = await routes.handle_support_admin_recovery_approve(request(
        approval_body,
        headers=headers,
        path_args={"ticket_id": str(ticket["ticket_id"])}
    ))
    assert approved.status == 200, approved.text
    assert payload(approved)["recovery"]["status"] == "approved"
    assert payload(approved)["recovery"]["profile_token"] == "legacy-phone"

    started = await otp_auth.start(request({"purpose": "support", "ticket_token": ticket_token}))
    assert started.status == 200
    started_data = payload(started)
    code = mailed_code(sender)
    verified = await otp_auth.verify(request({
        "challenge": started_data["challenge"],
        "code": code,
        "email": "recovery@example.com",
        "purpose": "support",
        "support_recovery_id": started_data["support_recovery_id"],
    }))
    assert verified.status == 200 and payload(verified)["next"] == "password"
    retry_start = await otp_auth.start(request({"purpose": "support", "ticket_token": ticket_token}))
    assert retry_start.status == 409
    grant_cookie = {otp_auth._GRANT_COOKIE: verified.cookies[otp_auth._GRANT_COOKIE].value}
    reset = await otp_auth.reset(request({"password": "new-password", "password2": "new-password"}, cookies=grant_cookie))
    assert reset.status == 200
    assert not payload(await routes.handle_auth_me(request(cookies=old_cookie)))["authenticated"]
    new_cookie = {routes._AUTH_COOKIE: reset.cookies[routes._AUTH_COOKIE].value}
    profile = payload(await routes.handle_profile_orders(request(cookies=new_cookie, query={"token": "legacy-phone"})))
    assert profile["contact"] == "recovery@example.com"
    assert profile["email_confirmed"] is True
    assert profile["referral"]["ref_code"] == "legacy-ref"
    assert profile["balance"]["balance_rub"] == 321
    assert profile["balance"]["balance_mode_enabled"] is True
    assert profile["balance"]["balance_autodebit_enabled"] is True
    assert profile["orders"][0]["order_id"] == "order-legacy-1"
    assert payload(await routes.handle_auth_login(request({"contact": "recovery@example.com", "password": "new-password"})))['ok'] is True

    replay = await otp_auth.reset(request({"password": "another-password", "password2": "another-password"}, cookies=grant_cookie))
    assert replay.status == 400
    second_approve = await routes.handle_support_admin_recovery_approve(request({
        "profile_token": "legacy-phone",
        "order_id": "order-legacy-1",
        "verification_source": "yookassa_api",
        "verification_note": "Повторная попытка не должна сработать.",
    }, headers=headers, path_args={"ticket_id": str(ticket["ticket_id"])}))
    assert second_approve.status == 409
    async with db_session_factory() as session:
        recovery = await session.scalar(select(SupportRecoveryRequest).where(SupportRecoveryRequest.ticket_id == ticket["ticket_id"]))
        assert recovery.status == "completed"
        audits = (await session.scalars(select(SupportRecoveryAudit).where(SupportRecoveryAudit.request_id == recovery.id))).all()
        expected_actions = ["approved", "otp_verified", "completed"]
        if manual_review:
            expected_actions.insert(0, "payment_check_failed")
        assert [audit.action for audit in audits] == expected_actions
        assert [audit.order_id for audit in audits] == ["order-legacy-1"] * len(expected_actions)
        approved_audit = audits[1] if manual_review else audits[0]
        assert approved_audit.agent_telegram_id == 123456789
        assert approved_audit.verification_source == ("yookassa_manual" if manual_review else "yookassa_api")
        assert approved_audit.order_id == "order-legacy-1"
        assert approved_audit.payment_id == "payment-legacy-1"
        assert approved_audit.payment_status == "succeeded"
        assert approved_audit.payment_currency == "RUB"
        assert approved_audit.payment_amount_rub == 499
        assert approved_audit.claimant_evidence_type == ("manual:account_history" if manual_review else "provider_customer_contact")
        assert await session.scalar(select(WebEmail.email).where(WebEmail.profile_token == "legacy-phone")) == "recovery@example.com"
        assert await session.scalar(select(WebSession.token_hash).where(WebSession.profile_token == "legacy-phone"))
        assert len((await session.scalars(select(WebAccount))).all()) == 1
        balance = await session.get(WebBalanceAccount, "legacy-phone")
        assert balance.next_daily_charge_at == datetime(2026, 1, 2)
        link = await session.get(WebProfileLink, "legacy-phone")
        if manual_review:
            assert link is None
        else:
            assert link.telegram_id == 777000111


async def test_subscription_link_or_order_without_trusted_payment_cannot_approve(patched_webstore, monkeypatch, db_session_factory):
    await seed_legacy_account(db_session_factory, trusted=False)
    ticket = await create_recovery_ticket()
    agent_token = await login_telegram_agent(patched_webstore, monkeypatch)
    response = await routes.handle_support_admin_recovery_approve(request({
        "profile_token": "legacy-phone",
        "order_id": "order-legacy-1",
        "verification_source": "yookassa_api",
        "verification_note": "Проверка только по ссылке не должна быть достаточной.",
    }, headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket["ticket_id"])}))
    assert response.status == 403
    status = payload(await routes.handle_support_recovery_status(request(path_args={"token": ticket["token"]})))
    assert status["status"] == "pending"
    denied = await routes.handle_support_admin_recovery_deny(request(
        {"reason": "Провайдер не подтвердил оплату."},
        headers={"X-Support-Agent-Token": agent_token},
        path_args={"ticket_id": str(ticket["ticket_id"])},
    ))
    assert denied.status == 200
    assert payload(denied)["recovery"]["status"] == "denied"
    replay = await routes.handle_support_admin_recovery_deny(request(
        {"reason": "Повторный отказ не должен сработать."},
        headers={"X-Support-Agent-Token": agent_token},
        path_args={"ticket_id": str(ticket["ticket_id"])},
    ))
    assert replay.status == 409


async def test_shared_password_support_session_cannot_approve_but_can_reply(patched_webstore, monkeypatch, db_session_factory):
    configure_fake_yookassa(monkeypatch)
    await seed_legacy_account(db_session_factory)
    ticket = await create_recovery_ticket()
    agent_token = await login_agent(patched_webstore)
    headers = {"X-Support-Agent-Token": agent_token}
    denied = await routes.handle_support_admin_recovery_approve(request({
        "profile_token": "legacy-phone",
        "order_id": "order-legacy-1",
        "verification_source": "yookassa_api",
        "verification_note": "Платёж подтверждён через YooKassa.",
    }, headers=headers, path_args={"ticket_id": str(ticket["ticket_id"])}))
    assert denied.status == 403
    me = await routes.handle_support_admin_me(request(headers=headers))
    assert payload(me)["auth_method"] == "password"
    assert payload(me)["can_approve_recovery"] is False
    message = await routes.handle_support_admin_send_message(request(
        {"text": "Проверяем обращение вручную."},
        headers=headers,
        path_args={"ticket_id": str(ticket["ticket_id"])},
    ))
    assert message.status == 200


async def test_telegram_admin_identity_is_signed_and_allowlisted(patched_webstore, monkeypatch):
    monkeypatch.setattr(routes.settings, "admin_bot_token", "test-bot-token")
    monkeypatch.setattr(routes.settings, "support_agent_ids", [123456789])
    unauthorized = await routes.handle_support_admin_login(request(telegram_login_body(telegram_id=987654321)))
    assert unauthorized.status == 403
    forged = telegram_login_body()
    forged["id"] = "987654321"
    invalid_signature = await routes.handle_support_admin_login(request(forged))
    assert invalid_signature.status == 403


async def test_support_admin_login_is_rate_limited(patched_webstore):
    routes.settings.support_admin_password = "test-support-password"
    responses = [await routes.handle_support_admin_login(request(
        {"password": "wrong-password"},
        headers={"X-Real-IP": "198.51.100.55"},
    )) for _ in range(13)]
    assert [response.status for response in responses[:12]] == [403] * 12
    assert responses[-1].status == 429


async def test_provider_mismatch_stays_pending_and_is_audited(patched_webstore, monkeypatch, db_session_factory):
    configure_fake_yookassa(monkeypatch, customer="+79990000000")
    await seed_legacy_account(db_session_factory)
    ticket = await create_recovery_ticket()
    agent_token = await login_telegram_agent(patched_webstore, monkeypatch)
    response = await routes.handle_support_admin_recovery_approve(request({
        "profile_token": "legacy-phone",
        "order_id": "order-legacy-1",
        "verification_source": "yookassa_api",
        "verification_note": "Проверка контакта плательщика не совпала.",
    }, headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket["ticket_id"])}))
    assert response.status == 403
    response_data = payload(response)
    assert response_data["recovery"]["payment_check"]["manual_review_required"] is True
    assert response_data["recovery"]["payment_check"]["claimant_evidence_type"] == "provider_contact_mismatch"
    status = payload(await routes.handle_support_recovery_status(request(path_args={"token": ticket["token"]})))
    assert status["status"] == "pending"
    approved = await routes.handle_support_admin_recovery_approve(request({
        "profile_token": "legacy-phone",
        "order_id": "order-legacy-1",
        "verification_source": "yookassa_manual",
        "manual_basis": "account_history",
        "verification_note": "История заказов и активная подписка сопоставлены с обращением клиента.",
    }, headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket["ticket_id"])}))
    assert approved.status == 200, approved.text
    assert payload(approved)["recovery"]["payment_check"]["source"] == "yookassa_manual"
    assert payload(approved)["recovery"]["payment_check"]["claimant_evidence_type"] == "manual:account_history"
    async with db_session_factory() as session:
        recovery = await session.scalar(select(SupportRecoveryRequest).where(SupportRecoveryRequest.ticket_id == ticket["ticket_id"]))
        assert recovery.payment_check_status == "verified"
        audit = await session.scalar(select(SupportRecoveryAudit).where(
            SupportRecoveryAudit.request_id == recovery.id,
            SupportRecoveryAudit.action == "payment_check_failed",
        ))
        assert audit.agent_telegram_id == 123456789
        assert audit.order_id == "order-legacy-1"
        assert audit.payment_id == "payment-legacy-1"
        assert audit.payment_status == "succeeded"
        assert audit.payment_amount_rub == 499
        approved_audit = await session.scalar(select(SupportRecoveryAudit).where(
            SupportRecoveryAudit.request_id == recovery.id,
            SupportRecoveryAudit.action == "approved",
        ))
        assert approved_audit.verification_source == "yookassa_manual"
        assert approved_audit.claimant_evidence_type == "manual:account_history"


@pytest.mark.parametrize("provider_kwargs", [
    {"status": "pending"},
    {"paid": False},
    {"amount": "400.00"},
    {"currency": "USD"},
    {"order_id": "other-order"},
    {"returned_id": "other-payment"},
])
async def test_provider_payment_fields_must_match_order(patched_webstore, monkeypatch, db_session_factory, provider_kwargs):
    configure_fake_yookassa(monkeypatch, **provider_kwargs)
    await seed_legacy_account(db_session_factory)
    ticket = await create_recovery_ticket()
    agent_token = await login_telegram_agent(patched_webstore, monkeypatch)
    response = await routes.handle_support_admin_recovery_approve(request({
        "profile_token": "legacy-phone",
        "order_id": "order-legacy-1",
        "verification_source": "yookassa_api",
        "verification_note": "Проверяем статус, сумму, валюту и идентификатор платежа.",
    }, headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket["ticket_id"])}))
    assert response.status == 403
    assert payload(response)["status"] == "manual_review"
    manual_response = await routes.handle_support_admin_recovery_approve(request({
        "profile_token": "legacy-phone",
        "order_id": "order-legacy-1",
        "verification_source": "yookassa_manual",
        "manual_basis": "account_history",
        "verification_note": "Поля платежа не подтверждены, ручное решение не должно обходить проверку.",
    }, headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket["ticket_id"])}))
    assert manual_response.status == 403
    status = payload(await routes.handle_support_recovery_status(request(path_args={"token": ticket["token"]})))
    assert status["status"] == "pending"


async def test_provider_unavailable_does_not_approve(patched_webstore, monkeypatch, db_session_factory):
    monkeypatch.setattr(routes.settings, "yookassa_shop_id", "")
    monkeypatch.setattr(routes.settings, "yookassa_secret_key", "")
    await seed_legacy_account(db_session_factory)
    ticket = await create_recovery_ticket()
    agent_token = await login_telegram_agent(patched_webstore, monkeypatch)
    response = await routes.handle_support_admin_recovery_approve(request({
        "profile_token": "legacy-phone",
        "order_id": "order-legacy-1",
        "verification_source": "yookassa_api",
        "verification_note": "Провайдер недоступен, автоматического одобрения нет.",
    }, headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket["ticket_id"])}))
    assert response.status == 503
    assert payload(response)["status"] == "provider_unavailable"


async def test_recovery_cannot_replace_existing_confirmed_email(patched_webstore, monkeypatch, db_session_factory):
    await seed_legacy_account(db_session_factory)
    async with db_session_factory() as session:
        session.add(WebEmail(email="already@example.com", profile_token="legacy-phone"))
        await session.commit()
    ticket = await create_recovery_ticket()
    configure_fake_yookassa(monkeypatch)
    agent_token = await login_telegram_agent(patched_webstore, monkeypatch)
    response = await routes.handle_support_admin_recovery_approve(request({
        "profile_token": "legacy-phone",
        "order_id": "order-legacy-1",
        "verification_source": "yookassa_api",
        "verification_note": "Профиль уже имеет подтверждённый email.",
    }, headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket["ticket_id"])}))
    assert response.status == 409


async def test_parallel_approvals_are_single_use(patched_webstore, monkeypatch, db_session_factory):
    await seed_legacy_account(db_session_factory)
    ticket = await create_recovery_ticket()
    configure_fake_yookassa(monkeypatch)
    agent_token = await login_telegram_agent(patched_webstore, monkeypatch)
    body = {
        "profile_token": "legacy-phone",
        "order_id": "order-legacy-1",
        "verification_source": "yookassa_api",
        "verification_note": "Платёж подтверждён по доверенной записи.",
    }
    responses = await asyncio.gather(*(
        routes.handle_support_admin_recovery_approve(request(body, headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket["ticket_id"])}))
        for _ in range(2)
    ))
    assert sorted(response.status for response in responses) == [200, 409], [response.text for response in responses]


async def test_slow_provider_does_not_hold_sqlite_write_lock(patched_webstore, monkeypatch, db_session_factory):
    provider_started = threading.Event()
    configure_fake_yookassa(monkeypatch, started=provider_started, delay=0.5)
    monkeypatch.setattr(routes, "_notify_support_agents", AsyncMock())
    await seed_legacy_account(db_session_factory)
    ticket = await create_recovery_ticket()
    agent_token = await login_telegram_agent(patched_webstore, monkeypatch)
    approval = asyncio.create_task(routes.handle_support_admin_recovery_approve(request({
        "profile_token": "legacy-phone",
        "order_id": "order-legacy-1",
        "verification_source": "yookassa_api",
        "verification_note": "Медленная проверка платежа не должна удерживать write-lock.",
    }, headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket["ticket_id"])})))
    assert await asyncio.to_thread(provider_started.wait, 2)

    async def write_during_provider_call():
        async with db_session_factory() as session:
            session.add(SupportMessage(
                ticket_id=ticket["ticket_id"],
                sender="agent",
                text="Параллельная запись не должна ждать YooKassa.",
                created_at=datetime.utcnow(),
            ))
            await session.commit()

    writer = asyncio.create_task(write_during_provider_call())
    await asyncio.wait_for(asyncio.shield(writer), timeout=0.25)
    approved = await approval
    assert approved.status == 200, approved.text


async def test_order_change_during_provider_check_cannot_approve(patched_webstore, monkeypatch, db_session_factory):
    provider_started = threading.Event()
    configure_fake_yookassa(monkeypatch, started=provider_started, delay=0.2)
    monkeypatch.setattr(routes, "_notify_support_agents", AsyncMock())
    await seed_legacy_account(db_session_factory)
    ticket = await create_recovery_ticket()
    agent_token = await login_telegram_agent(patched_webstore, monkeypatch)
    approval = asyncio.create_task(routes.handle_support_admin_recovery_approve(request({
        "profile_token": "legacy-phone",
        "order_id": "order-legacy-1",
        "verification_source": "yookassa_api",
        "verification_note": "Проверяем, что изменение заказа отменяет устаревшее решение.",
    }, headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket["ticket_id"])})))
    assert await asyncio.to_thread(provider_started.wait, 2)
    async with db_session_factory() as session:
        order = await session.scalar(select(WebOrder).where(WebOrder.order_id == "order-legacy-1"))
        order.amount_rub = 500
        await session.commit()
    response = await approval
    assert response.status == 409
    assert payload(response)["status"] == "stale"
    status = payload(await routes.handle_support_recovery_status(request(path_args={"token": ticket["token"]})))
    assert status["status"] == "pending"


async def test_recovery_ticket_creation_is_rate_limited(patched_webstore):
    responses = [await routes.handle_support_new_ticket(request({
        "recovery": {
            "old_contact": "+7 123 456 7890",
            "reference": "order-legacy-1",
            "new_email": "rate-limit@example.com",
        }
    })) for _ in range(4)]
    assert [response.status for response in responses] == [200, 200, 200, 429]


async def test_approved_recovery_expires_before_otp(patched_webstore, monkeypatch, db_session_factory):
    await seed_legacy_account(db_session_factory)
    ticket = await create_recovery_ticket()
    configure_fake_yookassa(monkeypatch)
    agent_token = await login_telegram_agent(patched_webstore, monkeypatch)
    approved = await routes.handle_support_admin_recovery_approve(request({
        "profile_token": "legacy-phone",
        "order_id": "order-legacy-1",
        "verification_source": "yookassa_api",
        "verification_note": "Платёж подтверждён до истечения разрешения.",
    }, headers={"X-Support-Agent-Token": agent_token}, path_args={"ticket_id": str(ticket["ticket_id"])}))
    assert approved.status == 200
    async with db_session_factory() as session:
        recovery = await session.scalar(select(SupportRecoveryRequest).where(SupportRecoveryRequest.ticket_id == ticket["ticket_id"]))
        recovery.approval_expires_at = datetime.utcnow() - timedelta(seconds=1)
        await session.commit()
    status = payload(await routes.handle_support_recovery_status(request(path_args={"token": ticket["token"]})))
    assert status["status"] == "expired"
    started = await otp_auth.start(request({"purpose": "support", "ticket_token": ticket["token"]}))
    assert started.status == 409


async def test_support_recovery_ui_copy_and_protection_card(patched_webstore):
    recovery_page = await routes.handle_support_page(request(query={"flow": "recovery"}))
    assert "Старый номер телефона или контакт" in recovery_page.text
    assert "Новый email" in recovery_page.text
    assert "Не указывайте пароль" in recovery_page.text
    profile_page = await routes.handle_profile_page(request())
    assert "Нет доступа к почте или Telegram?" in profile_page.text
    assert "Защитите свой аккаунт" in profile_page.text
