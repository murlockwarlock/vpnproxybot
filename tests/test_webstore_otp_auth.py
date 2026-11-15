import asyncio
import hashlib
import hmac
import json
import re
from datetime import datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from test_webstore_telegram_linking import db_engine, db_session_factory, patched_webstore, _Request
from webstore import otp_auth, routes
from webstore.models import WebAccount, WebBalanceAccount, WebEmail, WebOrder, WebOtpChallenge, WebOtpMail, WebProfileLink

pytestmark = pytest.mark.asyncio


def req(body=None, cookies=None):
    return _Request(_json_body=body or {}, cookies=cookies or {})


def payload(response):
    return json.loads(response.text)


def mailed_code(sender):
    message = sender.await_args.args[2]
    return re.search(r'Ваш код: (\d{6})', message).group(1)


async def test_registration_code_lock_resend_and_single_use(patched_webstore, monkeypatch):
    sender = AsyncMock()
    monkeypatch.setattr(otp_auth, 'send_email', sender)
    start = await otp_auth.start(req({'purpose': 'register', 'email': 'user@example.com', 'password': 'password-one', 'password2': 'password-one'}))
    assert start.status == 200 and sender.await_count == 1
    challenge, code = payload(start)['challenge'], mailed_code(sender)
    digest = hashlib.sha256(challenge.encode()).hexdigest()
    async with patched_webstore() as session:
        record = await session.get(WebOtpChallenge, digest)
        key = hmac.new(b'test-bridge-secret', b'webstore-email-otp-v1', hashlib.sha256).digest()
        expected = hmac.new(key, '\0'.join((digest, 'register', 'user@example.com', code)).encode(), hashlib.sha256).hexdigest()
        assert record.code_digest == expected and record.code_digest != hashlib.sha256(code.encode()).hexdigest()
        assert record.password_hash != 'password-one' and (record.expires_at - datetime.utcnow()).total_seconds() <= 600
        assert not await session.scalar(select(WebAccount))
    for _ in range(5):
        wrong = '000000' if code != '000000' else '000001'
        assert (await otp_auth.verify(req({'challenge': challenge, 'code': wrong}))).status == 400
    assert (await otp_auth.verify(req({'challenge': challenge, 'code': code}))).status == 400
    assert payload(await otp_auth.status(req({'challenge': challenge})))['locked']
    assert (await otp_auth.resend(req({'challenge': challenge}))).status == 429
    async with patched_webstore() as session:
        event = await session.scalar(select(WebOtpMail))
        event.created_at = datetime.utcnow() - timedelta(seconds=61)
        await session.commit()
    assert (await otp_auth.resend(req({'challenge': challenge}))).status == 200
    new_code = mailed_code(sender)
    assert new_code != code
    assert (await otp_auth.verify(req({'challenge': challenge, 'code': code}))).status == 400
    verified = await otp_auth.verify(req({'challenge': challenge, 'code': new_code}))
    assert verified.status == 200 and payload(verified)['next'] == 'profile'
    cookie = {routes._AUTH_COOKIE: verified.cookies[routes._AUTH_COOKIE].value}
    assert payload(await routes.handle_auth_me(req(cookies=cookie)))['contact'] == 'user@example.com'
    assert (await otp_auth.verify(req({'challenge': challenge, 'code': new_code}))).status == 400
    async with patched_webstore() as session:
        assert (await session.get(WebEmail, 'user@example.com')).profile_token == routes._make_profile_token('user@example.com')


async def test_reset_revokes_sessions_and_preserves_phone_profile(patched_webstore, monkeypatch):
    sender = AsyncMock()
    monkeypatch.setattr(otp_auth, 'send_email', sender)
    async with patched_webstore() as session:
        session.add(WebAccount(contact='+71234567890', profile_token='legacy', password_hash=routes._hash_password('old-password')))
        session.add(WebEmail(email='owner@example.com', profile_token='legacy'))
        session.add(WebBalanceAccount(profile_token='legacy', contact='+71234567890', balance_rub=123, ref_code='original-ref'))
        session.add(WebOrder(order_id='original-order', profile_token='legacy', tariff_key='vpn', tariff_label='VPN', days=30, amount_rub=100, status='delivered'))
        session.add(WebProfileLink(profile_token='legacy', telegram_id=12345))
        await session.commit()
    login = await routes.handle_auth_login(req({'contact': 'owner@example.com', 'password': 'old-password'}))
    old_cookie = {routes._AUTH_COOKIE: login.cookies[routes._AUTH_COOKIE].value}
    start = await otp_auth.start(req({'purpose': 'reset', 'email': 'owner@example.com'}))
    challenge, code = payload(start)['challenge'], mailed_code(sender)
    verified = await otp_auth.verify(req({'challenge': challenge, 'code': code}))
    assert verified.status == 200 and payload(verified)['next'] == 'password'
    grant = verified.cookies[otp_auth._GRANT_COOKIE]
    assert grant['httponly'] and grant['secure'] and grant['samesite'] == 'Lax'
    assert (await otp_auth.verify(req({'challenge': challenge, 'code': code}))).status == 400
    cookies = {otp_auth._GRANT_COOKIE: grant.value}
    assert (await otp_auth.reset(req({'password': 'short', 'password2': 'short'}, cookies))).status == 400
    reset = await otp_auth.reset(req({'password': 'new-password', 'password2': 'new-password'}, cookies))
    assert reset.status == 200
    assert not payload(await routes.handle_auth_me(req(cookies=old_cookie)))['authenticated']
    new_cookie = {routes._AUTH_COOKIE: reset.cookies[routes._AUTH_COOKIE].value}
    assert payload(await routes.handle_auth_me(req(cookies=new_cookie)))['profile_token'] == 'legacy'
    assert (await otp_auth.reset(req({'password': 'another-password', 'password2': 'another-password'}, cookies))).status == 400
    assert (await routes.handle_auth_login(req({'contact': 'owner@example.com', 'password': 'old-password'}))).status == 401
    assert (await routes.handle_auth_login(req({'contact': 'owner@example.com', 'password': 'new-password'}))).status == 200
    async with patched_webstore() as session:
        balance = await session.get(WebBalanceAccount, 'legacy')
        assert (balance.balance_rub, balance.ref_code) == (123, 'original-ref')
        assert (await session.scalar(select(WebOrder))).order_id == 'original-order'
        assert (await session.get(WebProfileLink, 'legacy')).telegram_id == 12345


async def test_missing_account_and_smtp_failure_do_not_expose_account(patched_webstore, monkeypatch, caplog):
    sender = AsyncMock()
    monkeypatch.setattr(otp_auth, 'send_email', sender)
    async with patched_webstore() as session:
        session.add(WebAccount(contact='known@example.com', profile_token='known', password_hash=routes._hash_password('password-one')))
        await session.commit()
    known = await otp_auth.start(req({'purpose': 'reset', 'email': 'known@example.com'}))
    unknown = await otp_auth.start(req({'purpose': 'reset', 'email': 'unknown@example.com'}))
    assert known.status == unknown.status == 200
    assert payload(known).keys() == payload(unknown).keys() and sender.await_count == 2
    code = mailed_code(sender)
    assert (await otp_auth.verify(req({'challenge': payload(unknown)['challenge'], 'code': code}))).status == 400
    sender.side_effect = OSError('private transport details')
    failed = await otp_auth.start(req({'purpose': 'reset', 'email': 'third@example.com'}))
    assert failed.status == 503 and 'не удалось' in payload(failed)['error'].lower()
    assert 'private transport details' not in caplog.text and code not in caplog.text
    async with patched_webstore() as session:
        assert not await session.scalar(select(WebOtpChallenge).where(WebOtpChallenge.email == 'third@example.com'))


async def test_cooldown_and_five_per_hour_are_atomic(patched_webstore, monkeypatch):
    sender = AsyncMock()
    monkeypatch.setattr(otp_auth, 'send_email', sender)
    body = {'purpose': 'reset', 'email': 'same@example.com'}
    results = await asyncio.gather(*(otp_auth.start(req(body)) for _ in range(4)))
    assert sorted(response.status for response in results) == [200, 429, 429, 429]
    assert sender.await_count == 1
    for expected in (2, 3, 4, 5):
        async with patched_webstore() as session:
            events = (await session.scalars(select(WebOtpMail))).all()
            for event in events:
                event.created_at -= timedelta(seconds=61)
            await session.commit()
        assert (await otp_auth.start(req(body))).status == 200
        assert sender.await_count == expected
    async with patched_webstore() as session:
        events = (await session.scalars(select(WebOtpMail))).all()
        for event in events:
            event.created_at -= timedelta(seconds=61)
        await session.commit()
    blocked = await otp_auth.start(req(body))
    assert blocked.status == 429 and payload(blocked)['retry_after'] > 0 and sender.await_count == 5


async def test_register_and_reset_have_separate_cooldowns_and_limits(patched_webstore, monkeypatch):
    sender = AsyncMock()
    monkeypatch.setattr(otp_auth, 'send_email', sender)
    email = 'purpose-cooldown@example.com'
    register = await otp_auth.start(req({'purpose': 'register', 'email': email, 'password': 'password-one', 'password2': 'password-one'}))
    assert register.status == 200
    reset = await otp_auth.start(req({'purpose': 'reset', 'email': email}))
    assert reset.status == 200
    blocked = await otp_auth.start(req({'purpose': 'reset', 'email': email}))
    assert blocked.status == 429


async def test_concurrent_code_consumption_is_single_use(patched_webstore, monkeypatch):
    sender = AsyncMock()
    monkeypatch.setattr(otp_auth, 'send_email', sender)
    started = await otp_auth.start(req({'purpose': 'register', 'email': 'race@example.com', 'password': 'password-one', 'password2': 'password-one'}))
    body = {'challenge': payload(started)['challenge'], 'code': mailed_code(sender)}
    responses = await asyncio.gather(*(otp_auth.verify(req(body)) for _ in range(4)))
    assert sorted(response.status for response in responses) == [200, 400, 400, 400]
    async with patched_webstore() as session:
        assert len((await session.scalars(select(WebAccount).where(WebAccount.contact == 'race@example.com'))).all()) == 1


async def test_code_from_latest_email_recovers_stale_browser_challenge(patched_webstore, monkeypatch):
    sender = AsyncMock()
    monkeypatch.setattr(otp_auth, 'send_email', sender)
    body = {'purpose': 'register', 'email': 'stale@example.com', 'password': 'password-one', 'password2': 'password-one'}
    first = await otp_auth.start(req(body))
    first_challenge = payload(first)['challenge']
    async with patched_webstore() as session:
        event = await session.scalar(select(WebOtpMail))
        event.created_at -= timedelta(seconds=61)
        await session.commit()
    second = await otp_auth.start(req(body))
    second_code = mailed_code(sender)
    recovered = await otp_auth.verify(req({
        'challenge': first_challenge,
        'code': second_code,
        'email': 'stale@example.com',
        'purpose': 'register',
    }))
    assert recovered.status == 200 and payload(recovered)['next'] == 'profile'
    assert (await otp_auth.verify(req({
        'challenge': payload(second)['challenge'],
        'code': second_code,
        'email': 'stale@example.com',
        'purpose': 'register',
    }))).status == 400


async def test_reset_code_from_latest_email_recovers_stale_challenge(patched_webstore, monkeypatch):
    sender = AsyncMock()
    monkeypatch.setattr(otp_auth, 'send_email', sender)
    async with patched_webstore() as session:
        session.add(WebAccount(contact='stale-reset@example.com', profile_token='stale-reset', password_hash=routes._hash_password('password-one')))
        session.add(WebEmail(email='stale-reset@example.com', profile_token='stale-reset'))
        await session.commit()
    body = {'purpose': 'reset', 'email': 'stale-reset@example.com'}
    first = await otp_auth.start(req(body))
    first_challenge = payload(first)['challenge']
    async with patched_webstore() as session:
        event = await session.scalar(select(WebOtpMail))
        event.created_at -= timedelta(seconds=61)
        await session.commit()
    second = await otp_auth.start(req(body))
    second_code = mailed_code(sender)
    recovered = await otp_auth.verify(req({
        'challenge': first_challenge,
        'code': second_code,
        'email': 'stale-reset@example.com',
        'purpose': 'reset',
    }))
    assert recovered.status == 200 and payload(recovered)['next'] == 'password'


async def test_existing_email_registration_explains_recovery(patched_webstore, monkeypatch):
    sender = AsyncMock()
    monkeypatch.setattr(otp_auth, 'send_email', sender)
    old_hash = routes._hash_password('old-password')
    async with patched_webstore() as session:
        session.add(WebAccount(contact='already@example.com', profile_token='already', password_hash=old_hash))
        await session.commit()
    started = await otp_auth.start(req({
        'purpose': 'register', 'email': 'already@example.com',
        'password': 'new-password', 'password2': 'new-password',
    }))
    verified = await otp_auth.verify(req({
        'challenge': payload(started)['challenge'],
        'code': mailed_code(sender),
        'email': 'already@example.com',
        'purpose': 'register',
    }))
    assert verified.status == 409
    assert payload(verified)['next'] == 'login'
    assert 'уже зарегистрирован' in payload(verified)['error']
    assert (await otp_auth.verify(req({
        'challenge': payload(started)['challenge'],
        'code': mailed_code(sender),
        'email': 'already@example.com',
        'purpose': 'register',
    }))).status == 400
    async with patched_webstore() as session:
        account = await session.scalar(select(WebAccount).where(WebAccount.contact == 'already@example.com'))
        assert account.password_hash == old_hash


async def test_expired_code_and_legacy_email_reset(patched_webstore, monkeypatch):
    sender = AsyncMock()
    monkeypatch.setattr(otp_auth, 'send_email', sender)
    async with patched_webstore() as session:
        session.add(WebAccount(contact='old@example.com', profile_token='old-profile', password_hash=routes._hash_password('old-password')))
        await session.commit()
    started = await otp_auth.start(req({'purpose': 'reset', 'email': 'old@example.com'}))
    challenge, code = payload(started)['challenge'], mailed_code(sender)
    async with patched_webstore() as session:
        item = await session.get(WebOtpChallenge, hashlib.sha256(challenge.encode()).hexdigest())
        item.expires_at = datetime.utcnow() - timedelta(seconds=1)
        await session.commit()
    assert (await otp_auth.status(req({'challenge': challenge}))).status == 410
    assert (await otp_auth.verify(req({'challenge': challenge, 'code': code}))).status == 400
    async with patched_webstore() as session:
        event = await session.scalar(select(WebOtpMail))
        event.created_at -= timedelta(seconds=61)
        await session.commit()
    resent = await otp_auth.resend(req({'challenge': challenge}))
    assert resent.status == 200
    verified = await otp_auth.verify(req({'challenge': challenge, 'code': mailed_code(sender)}))
    grant = verified.cookies[otp_auth._GRANT_COOKIE].value
    assert (await otp_auth.reset(req({'password': 'new-password', 'password2': 'new-password'}, {otp_auth._GRANT_COOKIE: grant}))).status == 200
    async with patched_webstore() as session:
        assert (await session.get(WebEmail, 'old@example.com')).profile_token == 'old-profile'
