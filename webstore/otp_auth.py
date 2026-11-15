import hashlib
import hmac
import logging
import math
import secrets
from datetime import datetime, timedelta

from aiohttp import web
from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError

from webstore.email_auth import send_email
from webstore.models import (
    SupportRecoveryAudit, SupportRecoveryRequest, SupportTicket,
    WebAccount, WebEmail, WebEmailToken, WebOtpChallenge, WebOtpGrant,
    WebOtpMail, WebSession, WebTelegramAuthCode,
)

logger = logging.getLogger(__name__)
_GRANT_COOKIE = 'webstore_otp_reset'
_TTL = timedelta(minutes=10)
_COOLDOWN = timedelta(seconds=60)
_HOURLY_LIMIT = 5


def _routes():
    from webstore import routes
    return routes


def _key():
    secret = _routes().settings.bridge_shared_secret
    return hmac.new(secret.encode(), b'webstore-email-otp-v1', hashlib.sha256).digest() if secret else None


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _address_digest(key, email):
    return hmac.new(key, ('email:' + email).encode(), hashlib.sha256).hexdigest()


def _code_digest(key, challenge_hash, purpose, email, code):
    value = '\0'.join((challenge_hash, purpose, email, code))
    return hmac.new(key, value.encode(), hashlib.sha256).hexdigest()


async def _resolve_reset_profile(session, email):
    confirmed = await session.get(WebEmail, email)
    if confirmed:
        return confirmed.profile_token
    account = await session.scalar(select(WebAccount).where(WebAccount.contact == email))
    if account and not await session.scalar(select(WebEmail.email).where(WebEmail.profile_token == account.profile_token)):
        return account.profile_token
    return None


async def _body(request):
    try:
        value = await request.json()
        return value if isinstance(value, dict) else {}
    except Exception:
        return {}


def _invalid():
    return web.json_response({'error': 'Неверный или истёкший код'}, status=400)


async def _retry_after(session, address_digest, now, purpose=None):
    filters = [WebOtpMail.address_digest == address_digest, WebOtpMail.created_at > now - timedelta(hours=1)]
    if purpose:
        filters.append(WebOtpMail.purpose == purpose)
    last, oldest, count = (await session.execute(select(
        func.max(WebOtpMail.created_at), func.min(WebOtpMail.created_at), func.count(WebOtpMail.id)
    ).where(*filters))).one()
    cooldown = max(0, math.ceil((last + _COOLDOWN - now).total_seconds())) if last else 0
    hourly = max(0, math.ceil((oldest + timedelta(hours=1) - now).total_seconds())) if count >= _HOURLY_LIMIT else 0
    return max(cooldown, hourly)


async def _reserve(session, key, email, now, purpose):
    address = _address_digest(key, email)
    delay = await _retry_after(session, address, now, purpose)
    if delay:
        return delay
    session.add(WebOtpMail(address_digest=address, created_at=now, purpose=purpose))
    return 0


async def _deliver(email, code):
    try:
        await send_email(email, 'Darimiru: код подтверждения',
                         f'Ваш код: {code}\n\nКод действует 10 минут. Если вы не запрашивали его, проигнорируйте письмо.')
        return True
    except Exception:
        logger.error('OTP email delivery failed')
        return False


async def start(request):
    r = _routes()
    key = _key()
    if not key:
        return web.json_response({'error': 'Отправка кодов временно недоступна'}, status=503)
    body = await _body(request)
    purpose = str(body.get('purpose') or '')
    email = r._normalize_contact(str(body.get('email') or ''))
    password = str(body.get('password') or '')
    ticket_token = str(body.get('ticket_token') or '').strip()
    if purpose not in ('register', 'reset', 'support'):
        return web.json_response({'error': 'Введите корректный email'}, status=400)
    if purpose != 'support' and not r._valid_email(email):
        return web.json_response({'error': 'Введите корректный email'}, status=400)
    if purpose == 'support' and (not ticket_token or len(ticket_token) > 128):
        return web.json_response({'error': 'Обращение в поддержку не найдено'}, status=400)
    if purpose == 'register' and (len(password) < 8 or len(password) > 1024 or password != str(body.get('password2') or '')):
        return web.json_response({'error': 'Проверьте пароль (от 8 знаков)'}, status=400)
    if not await r._allow_auth_attempt('otp-start-ip', r._get_client_ip(request), 20, 15):
        return web.json_response({'error': 'Слишком много попыток. Попробуйте позже.'}, status=429)
    challenge = secrets.token_urlsafe(32)
    challenge_hash = _digest(challenge)
    code = f'{secrets.randbelow(1000000):06d}'
    now = datetime.utcnow()
    password_hash = r._hash_password(password) if purpose == 'register' else None
    async with r.async_session() as session:
        await session.execute(text('BEGIN IMMEDIATE'))
        await session.execute(delete(WebOtpChallenge).where(WebOtpChallenge.expires_at <= now))
        await session.execute(delete(WebOtpGrant).where(WebOtpGrant.expires_at <= now))
        await session.execute(delete(WebOtpMail).where(WebOtpMail.created_at <= now - timedelta(hours=1)))
        support_request = None
        support_recovery_id = None
        if purpose == 'support':
            support_request = await session.scalar(
                select(SupportRecoveryRequest)
                .join(SupportTicket, SupportTicket.id == SupportRecoveryRequest.ticket_id)
                .where(SupportTicket.token == ticket_token)
            )
            if (
                not support_request
                or support_request.status != 'approved'
                or not support_request.profile_token
                or support_request.otp_verified_at
                or not support_request.approval_expires_at
                or support_request.approval_expires_at <= now
            ):
                await session.rollback()
                return web.json_response({'error': 'Разрешение на восстановление недействительно'}, status=409)
            account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == support_request.profile_token))
            current_email = await session.scalar(
                select(WebEmail.email).where(WebEmail.profile_token == support_request.profile_token)
            )
            if not account or current_email:
                await session.rollback()
                return web.json_response({'error': 'Восстановление уже выполнено или недоступно'}, status=409)
            email = support_request.new_email
            support_recovery_id = support_request.id
        delay = await _reserve(session, key, email, now, purpose)
        if delay:
            await session.rollback()
            return web.json_response({'error': 'Подождите перед повторной отправкой', 'retry_after': delay}, status=429)
        profile = (
            support_request.profile_token
            if support_request
            else await _resolve_reset_profile(session, email) if purpose == 'reset' else None
        )
        if purpose == 'register':
            existing = await session.scalar(select(WebAccount.id).where(WebAccount.contact == email))
            if existing or await session.get(WebEmail, email):
                password_hash = None
        session.add(WebOtpChallenge(token_hash=challenge_hash, code_digest=_code_digest(key, challenge_hash, purpose, email, code),
                                    purpose=purpose, email=email, profile_token=profile, password_hash=password_hash,
                                    support_recovery_id=support_recovery_id, attempts=0, expires_at=now + _TTL))
        if support_request:
            support_request.otp_started_at = now
        await session.commit()
    if not await _deliver(email, code):
        async with r.async_session() as session:
            await session.execute(delete(WebOtpChallenge).where(WebOtpChallenge.token_hash == challenge_hash))
            await session.commit()
        return web.json_response({'error': 'Не удалось отправить письмо. Попробуйте позже.', 'retry_after': 60}, status=503)
    payload = {'ok': True, 'challenge': challenge, 'retry_after': 60, 'expires_in': 600}
    if support_recovery_id:
        payload['support_recovery_id'] = support_recovery_id
    return web.json_response(payload)


async def status(request):
    r = _routes()
    key = _key()
    body = await _body(request)
    challenge = str(body.get('challenge') or '')
    if not key or not challenge or len(challenge) > 128:
        return _invalid()
    async with r.async_session() as session:
        item = await session.get(WebOtpChallenge, _digest(challenge))
        if not item:
            return _invalid()
        now = datetime.utcnow()
        if item.expires_at <= now:
            return web.json_response({'error': 'Код истёк', 'expired': True}, status=410)
        retry = await _retry_after(session, _address_digest(key, item.email), now, item.purpose)
        return web.json_response({'ok': True, 'email': item.email, 'purpose': item.purpose, 'locked': item.attempts >= 5,
                                  'retry_after': retry, 'expires_in': max(0, math.ceil((item.expires_at - now).total_seconds()))})


async def resend(request):
    r = _routes()
    key = _key()
    body = await _body(request)
    challenge = str(body.get('challenge') or '')
    if not key or not challenge or len(challenge) > 128:
        return _invalid()
    if not await r._allow_auth_attempt('otp-resend-ip', r._get_client_ip(request), 20, 15):
        return web.json_response({'error': 'Слишком много попыток. Попробуйте позже.'}, status=429)
    challenge_hash = _digest(challenge)
    code = f'{secrets.randbelow(1000000):06d}'
    now = datetime.utcnow()
    async with r.async_session() as session:
        await session.execute(text('BEGIN IMMEDIATE'))
        item = await session.get(WebOtpChallenge, challenge_hash)
        if not item:
            await session.rollback()
            return _invalid()
        if item.purpose == 'support':
            support_request = await session.get(SupportRecoveryRequest, item.support_recovery_id)
            if (
                not support_request
                or support_request.status != 'approved'
                or support_request.otp_verified_at
                or not support_request.approval_expires_at
                or support_request.approval_expires_at <= now
            ):
                await session.rollback()
                return _invalid()
        delay = await _reserve(session, key, item.email, now, item.purpose)
        if delay:
            await session.rollback()
            return web.json_response({'error': 'Подождите перед повторной отправкой', 'retry_after': delay}, status=429)
        email = item.email
        while hmac.compare_digest(item.code_digest, _code_digest(key, challenge_hash, item.purpose, email, code)):
            code = f'{secrets.randbelow(1000000):06d}'
        item.code_digest = _code_digest(key, challenge_hash, item.purpose, email, code)
        item.expires_at = now + _TTL
        item.attempts = 0
        await session.commit()
    if not await _deliver(email, code):
        async with r.async_session() as session:
            await session.execute(delete(WebOtpChallenge).where(WebOtpChallenge.token_hash == challenge_hash))
            await session.commit()
        return web.json_response({'error': 'Не удалось отправить письмо. Попробуйте позже.', 'retry_after': 60}, status=503)
    return web.json_response({'ok': True, 'retry_after': 60, 'expires_in': 600})


async def verify(request):
    r = _routes()
    key = _key()
    body = await _body(request)
    challenge = str(body.get('challenge') or '')
    code = str(body.get('code') or '')
    email_hint = r._normalize_contact(str(body.get('email') or ''))
    purpose_hint = str(body.get('purpose') or '')
    if not key or not challenge or len(challenge) > 128 or len(code) != 6 or not code.isascii() or not code.isdigit():
        return _invalid()
    if not await r._allow_auth_attempt('otp-verify-ip', r._get_client_ip(request), 60, 15):
        return web.json_response({'error': 'Слишком много попыток. Попробуйте позже.'}, status=429)
    if r._valid_email(email_hint) and purpose_hint in ('register', 'reset', 'support') and not await r._allow_auth_attempt('otp-verify-email', email_hint, 20, 15):
        return web.json_response({'error': 'Слишком много попыток. Попробуйте позже.'}, status=429)
    challenge_hash = _digest(challenge)
    now = datetime.utcnow()
    async with r.async_session() as session:
        await session.execute(text('BEGIN IMMEDIATE'))
        item = await session.get(WebOtpChallenge, challenge_hash)
        matched_hash = challenge_hash
        valid_item = bool(item and item.expires_at > now and item.attempts < 5)
        expected = _code_digest(key, challenge_hash, item.purpose, item.email, code) if valid_item else ''
        code_matches = valid_item and hmac.compare_digest(item.code_digest, expected)
        if not code_matches and r._valid_email(email_hint) and purpose_hint in ('register', 'reset', 'support'):
            candidates_query = select(WebOtpChallenge).where(
                func.lower(WebOtpChallenge.email) == email_hint,
                WebOtpChallenge.purpose == purpose_hint,
                WebOtpChallenge.expires_at > now,
                WebOtpChallenge.attempts < 5,
            )
            recovery_hint = body.get('support_recovery_id')
            if purpose_hint == 'support' and str(recovery_hint or '').isdigit():
                candidates_query = candidates_query.where(WebOtpChallenge.support_recovery_id == int(recovery_hint))
            candidates = (await session.scalars(candidates_query.order_by(WebOtpChallenge.expires_at.desc()).limit(3))).all()
            for candidate in candidates:
                candidate_expected = _code_digest(key, candidate.token_hash, candidate.purpose, candidate.email, code)
                if hmac.compare_digest(candidate.code_digest, candidate_expected):
                    item = candidate
                    matched_hash = candidate.token_hash
                    code_matches = True
                    break
        if not item or item.expires_at <= now or item.attempts >= 5:
            await session.rollback()
            return _invalid()
        if not code_matches:
            item.attempts += 1
            await session.commit()
            return _invalid()
        purpose, email, profile, password_hash = item.purpose, item.email, item.profile_token, item.password_hash
        if purpose == 'reset':
            current = await _resolve_reset_profile(session, email)
            if not profile or current != profile:
                item.attempts += 1
                await session.commit()
                return _invalid()
            grant = secrets.token_urlsafe(32)
            session.add(WebOtpGrant(token_hash=_digest(grant), email=email, profile_token=profile, purpose='reset', expires_at=now + _TTL))
            await session.execute(delete(WebOtpChallenge).where(WebOtpChallenge.token_hash == matched_hash))
            await session.commit()
            response = web.json_response({'ok': True, 'next': 'password'})
            response.set_cookie(_GRANT_COOKIE, grant, max_age=600, path='/api/store/auth/otp/reset',
                                secure=True, httponly=True, samesite='Lax')
            return response
        if purpose == 'support':
            support_request = await session.get(SupportRecoveryRequest, item.support_recovery_id)
            if (
                not support_request
                or support_request.status != 'approved'
                or support_request.profile_token != profile
                or support_request.new_email != email
                or not support_request.approval_expires_at
                or support_request.approval_expires_at <= now
            ):
                item.attempts += 1
                await session.commit()
                return _invalid()
            grant = secrets.token_urlsafe(32)
            support_request.otp_verified_at = now
            session.add(WebOtpGrant(token_hash=_digest(grant), email=email, profile_token=profile,
                                    purpose='support', support_recovery_id=support_request.id,
                                    expires_at=now + _TTL))
            session.add(SupportRecoveryAudit(
                request_id=support_request.id,
                ticket_id=support_request.ticket_id,
                action='otp_verified',
                profile_token=profile,
                order_id=support_request.matched_order_id,
                details='Email OTP подтверждён владельцем обращения',
                created_at=now,
            ))
            await session.execute(delete(WebOtpChallenge).where(WebOtpChallenge.token_hash == matched_hash))
            await session.commit()
            response = web.json_response({'ok': True, 'next': 'password'})
            response.set_cookie(_GRANT_COOKIE, grant, max_age=600, path='/api/store/auth/otp/reset',
                                secure=True, httponly=True, samesite='Lax')
            return response
        existing = await session.scalar(select(WebAccount.id).where(WebAccount.contact == email))
        confirmed_email = await session.get(WebEmail, email)
        profile = r._make_profile_token(email)
        if not password_hash or existing or confirmed_email or await session.scalar(
                select(WebAccount.id).where(WebAccount.profile_token == profile)):
            if existing or confirmed_email:
                await session.execute(delete(WebOtpChallenge).where(WebOtpChallenge.token_hash == matched_hash))
                await session.commit()
                return web.json_response({
                    'error': 'Этот email уже зарегистрирован. Восстановите пароль или войдите в профиль.',
                    'next': 'login',
                }, status=409)
            item.attempts += 1
            await session.commit()
            return _invalid()
        session_token = secrets.token_urlsafe(32)
        try:
            session.add(WebAccount(contact=email, profile_token=profile, password_hash=password_hash, last_login_at=now))
            session.add(WebEmail(email=email, profile_token=profile))
            await r._get_or_create_web_balance_account(session, profile, email)
            await session.execute(delete(WebOtpChallenge).where(WebOtpChallenge.email == email))
            session.add(WebSession(token_hash=_digest(session_token), profile_token=profile,
                                   method='password', expires_at=now + timedelta(seconds=r._AUTH_COOKIE_MAX_AGE)))
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return _invalid()
    response = web.json_response({'ok': True, 'next': 'profile'})
    r._set_profile_cookie(response, session_token)
    return response


async def reset(request):
    r = _routes()
    body = await _body(request)
    password = str(body.get('password') or '')
    if len(password) < 8 or len(password) > 1024 or password != str(body.get('password2') or ''):
        return web.json_response({'error': 'Проверьте пароль (от 8 знаков)'}, status=400)
    grant = request.cookies.get(_GRANT_COOKIE, '')
    if not grant or len(grant) > 128:
        return _invalid()
    if not await r._allow_auth_attempt('otp-reset-ip', r._get_client_ip(request), 30, 15):
        return web.json_response({'error': 'Слишком много попыток. Попробуйте позже.'}, status=429)
    now = datetime.utcnow()
    session_token = secrets.token_urlsafe(32)
    password_hash = r._hash_password(password)
    async with r.async_session() as session:
        await session.execute(text('BEGIN IMMEDIATE'))
        item = await session.get(WebOtpGrant, _digest(grant))
        if not item or item.expires_at <= now:
            await session.rollback()
            return _invalid()
        email, profile = item.email, item.profile_token
        account = await session.scalar(select(WebAccount).where(WebAccount.profile_token == profile))
        grant_purpose = item.purpose
        support_request = await session.get(SupportRecoveryRequest, item.support_recovery_id) if grant_purpose == 'support' else None
        current = await _resolve_reset_profile(session, email)
        if grant_purpose == 'support':
            valid_support = (
                support_request
                and support_request.status == 'approved'
                and support_request.profile_token == profile
                and support_request.new_email == email
                and support_request.approval_expires_at
                and support_request.approval_expires_at > now
            )
            if not valid_support:
                await session.rollback()
                return _invalid()
        elif not account or current != profile:
            await session.rollback()
            return _invalid()
        if not account:
            await session.rollback()
            return _invalid()
        confirmed = await session.get(WebEmail, email)
        if confirmed and confirmed.profile_token != profile:
            await session.rollback()
            return _invalid()
        if not confirmed:
            session.add(WebEmail(email=email, profile_token=profile))
        account.password_hash = password_hash
        await session.execute(delete(WebSession).where(WebSession.profile_token == profile))
        await session.execute(delete(WebTelegramAuthCode).where(WebTelegramAuthCode.profile_token == profile))
        await session.execute(delete(WebEmailToken).where(WebEmailToken.profile_token == profile))
        await session.execute(delete(WebOtpChallenge).where((WebOtpChallenge.profile_token == profile) | (WebOtpChallenge.email == email)))
        await session.execute(delete(WebOtpGrant).where(WebOtpGrant.profile_token == profile))
        if support_request:
            support_request.status = 'completed'
            support_request.completed_at = now
            session.add(SupportRecoveryAudit(
                request_id=support_request.id,
                ticket_id=support_request.ticket_id,
                action='completed',
                profile_token=profile,
                order_id=support_request.matched_order_id,
                details='Пароль установлен после подтверждения email OTP',
                created_at=now,
            ))
        session.add(WebSession(token_hash=_digest(session_token), profile_token=profile, method='password',
                               expires_at=now + timedelta(seconds=r._AUTH_COOKIE_MAX_AGE)))
        try:
            await session.commit()
        except IntegrityError:
            await session.rollback()
            return _invalid()
    response = web.json_response({'ok': True, 'next': 'profile'})
    response.del_cookie(_GRANT_COOKIE, path='/api/store/auth/otp/reset')
    r._set_profile_cookie(response, session_token)
    return response
