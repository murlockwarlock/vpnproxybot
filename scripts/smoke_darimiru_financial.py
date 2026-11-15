from __future__ import annotations

import contextlib
import base64
import hashlib
import hmac
from http.cookiejar import CookieJar, DefaultCookiePolicy
import json
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlparse
from urllib.request import HTTPCookieProcessor, HTTPRedirectHandler, Request, build_opener, urlopen

from scripts.darimiru_deploy_safety import require, readonly, daily_snapshot


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Client:
    def __init__(self, base, secret):
        parsed = urlparse(base)
        require(parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', '::1', 'localhost'), 'Smoke requires a local HTTP origin')
        self.base = base.rstrip('/')
        self.secret = secret
        self.opener = build_opener(HTTPCookieProcessor(CookieJar(DefaultCookiePolicy(secure_protocols=('http', 'https')))), NoRedirect())

    def call(self, path, body=None, internal=False):
        require(path.startswith('/') and not path.startswith('//'), 'Invalid smoke API path')
        headers = {'Content-Type': 'application/json'}
        if internal:
            headers['X-Internal-Secret'] = self.secret
        request = Request(self.base + path, data=None if body is None else json.dumps(body).encode(), headers=headers)
        try:
            with self.opener.open(request, timeout=15) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, {}

    def ok(self, path, body=None, internal=False):
        status, data = self.call(path, body, internal)
        require(status == 200, 'Financial smoke API request failed')
        return data


def assert_empty_profile(data):
    require(data.get('orders') == [] and not data.get('telegram_items'), 'QA profile must have no subscriptions')
    require(data.get('web_balance_history') == [], 'QA profile has financial history')
    referral = data.get('referral') or {}
    require(referral.get('balance_rub') == 0 and referral.get('ref_code'), 'QA referral balance must be zero')
    require(parse_qs(urlparse(referral.get('site_url', '')).query).get('ref') == [referral['ref_code']], 'Referral URL is invalid')
    require(not (data.get('balance') or {}).get('can_enable_daily_billing'), 'Public daily billing enable control exposed')
    require(not (data.get('balance') or {}).get('can_manage_daily_billing'), 'Public daily billing management exposed')
    return referral['ref_code']


def validate_qa_user(bot_db, web_db, telegram_id, profile_token):
    require(isinstance(telegram_id, int) and telegram_id > 0, 'A genuine dedicated Telegram QA user is required')
    with contextlib.closing(readonly(bot_db)) as connection:
        row = connection.execute('SELECT id,balance_rub,referral_balance,balance_mode_enabled,balance_autodebit_enabled FROM users WHERE telegram_id=?', (telegram_id,)).fetchone()
        require(row is not None and not any(row[1:]), 'Telegram QA user is missing or has an active balance/mode')
        for table in ('subscriptions', 'balance_topups', 'balance_transactions', 'payments'):
            require(not connection.execute(f'SELECT 1 FROM "{table}" WHERE user_id=? LIMIT 1', (row[0],)).fetchone(), 'Telegram QA user has subscriptions or financial history')
    with contextlib.closing(readonly(web_db)) as connection:
        row = connection.execute('SELECT profile_token FROM web_profile_links WHERE telegram_id=?', (telegram_id,)).fetchone()
        require(row is None or row[0] == profile_token, 'Telegram QA user belongs to another Webstore profile')
        require(not connection.execute('SELECT 1 FROM web_balance_transfers WHERE profile_token=? LIMIT 1', (profile_token,)).fetchone(), 'QA profile has balance transfers')
        own_link = connection.execute('SELECT telegram_id FROM web_profile_links WHERE profile_token=?', (profile_token,)).fetchone()
        require(own_link is None or own_link[0] == telegram_id, 'Dedicated QA profile belongs to another Telegram identity')
        require(not connection.execute('SELECT 1 FROM web_telegram_items WHERE profile_token=? LIMIT 1', (profile_token,)).fetchone(), 'QA profile has Telegram subscriptions')



def preflight_qa(paths, qa):
    with contextlib.closing(readonly(paths['web'])) as connection:
        contact = qa['contact'].strip().lower()
        has_emails = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='web_emails'").fetchone()
        confirmed = connection.execute('SELECT profile_token FROM web_emails WHERE email=?', (contact,)).fetchone() if has_emails else None
        if confirmed:
            row = connection.execute('SELECT profile_token,password_hash FROM web_accounts WHERE profile_token=?', (confirmed[0],)).fetchone()
        else:
            row = connection.execute('SELECT profile_token,password_hash FROM web_accounts WHERE contact=?', (contact,)).fetchone()
            if row and has_emails:
                linked = connection.execute('SELECT email FROM web_emails WHERE profile_token=?', (row[0],)).fetchone()
                require(not linked, 'Dedicated QA contact is no longer an active login')
        require(row is not None, 'Create a dedicated Webstore QA account before deployment')
        try:
            scheme, rounds, salt, expected = row[1].split('$')
            require(scheme == 'pbkdf2_sha256' and 1 <= int(rounds) <= 1000000, 'Unsupported QA password hash')
            actual = hashlib.pbkdf2_hmac('sha256', qa['password'].encode(), base64.urlsafe_b64decode(salt + '=' * (-len(salt) % 4)), int(rounds))
            require(hmac.compare_digest(actual, base64.urlsafe_b64decode(expected + '=' * (-len(expected) % 4))), 'Dedicated QA credentials do not match')
        except (ValueError, TypeError):
            require(False, 'Invalid QA password configuration')
        token = row[0]
        require(not connection.execute('SELECT 1 FROM web_orders WHERE profile_token=? LIMIT 1', (token,)).fetchone(), 'Dedicated Webstore QA account has orders')
        balance = connection.execute('SELECT balance_rub,balance_mode_enabled,balance_autodebit_enabled FROM web_balance_accounts WHERE profile_token=?', (token,)).fetchone()
        require(balance == (0, 0, 0), 'Dedicated Webstore QA balance or mode is unsafe')
        require(not connection.execute('SELECT 1 FROM web_balance_transactions WHERE profile_token=? LIMIT 1', (token,)).fetchone(), 'Dedicated Webstore QA account has financial history')
    validate_qa_user(paths['bot'], paths['web'], qa['telegram_id'], token)
    return token

def run_smoke(web_base, secret, paths, qa):
    before = {kind: daily_snapshot(path, kind, active_only=True, configuration_only=True) for kind, path in paths.items()}
    qa_contact, qa_password = qa.get('contact'), qa.get('password')
    require(qa_contact and qa_password, 'Dedicated Webstore QA credentials are required')
    expected_token = preflight_qa(paths, qa)
    qa_client = Client(web_base, secret)
    require(qa_client.call('/api/store/auth/login', {'contact': qa_contact, 'password': qa_password + 'wrong'})[0] == 401, 'Invalid password accepted')
    qa_login = qa_client.ok('/api/store/auth/login', {'contact': qa_contact, 'password': qa_password})
    qa_token = qa_login.get('token')
    require(qa_token and qa_token == expected_token, 'Dedicated QA profile is invalid')
    me = qa_client.ok('/api/store/auth/me')
    require(me.get('authenticated') is True and me.get('profile_token') == qa_token, 'QA session is invalid')
    qa_ref = assert_empty_profile(qa_client.ok('/api/store/profile-orders?token=' + qa_token))
    qa_client.ok('/api/store/auth/logout', {})
    require(qa_client.ok('/api/store/auth/me').get('authenticated') is False, 'Logout failed')
    qa_login = qa_client.ok('/api/store/auth/login', {'contact': qa_contact, 'password': qa_password})
    require(qa_login.get('token') == qa_token, 'QA profile changed after login')
    require(assert_empty_profile(qa_client.ok('/api/store/profile-orders?token=' + qa_token)) == qa_ref, 'Referral code changed after login')
    validate_qa_user(paths['bot'], paths['web'], qa.get('telegram_id'), qa_token)
    init = qa_client.ok('/api/store/profile-link-init', {'token': qa_token})
    start = parse_qs(urlparse(init.get('link_url', '')).query).get('start', [''])[0]
    require(start.startswith('web_') and len(start) > 4, 'Telegram linking deep link is invalid')
    qa_client.ok('/api/store/internal/telegram-link-claim', {'code': start[4:], 'telegram_id': qa['telegram_id'], 'items': []}, internal=True)
    linked = qa_client.ok('/api/store/profile-orders?token=' + qa_token)
    require(assert_empty_profile(linked) == qa_ref, 'Referral code changed after Telegram linking')
    require(str((linked.get('telegram_link') or {}).get('telegram_id')) == str(qa['telegram_id']), 'Telegram linking did not persist')
    require(qa_client.call('/api/store/balance-toggle', {'token': qa_token, 'enabled': True})[0] == 403, 'Public daily billing enable was accepted')
    with contextlib.closing(readonly(paths['web'])) as connection:
        row = connection.execute('SELECT consumed_at FROM web_profile_link_codes WHERE code=? AND profile_token=?', (start[4:], qa_token)).fetchone()
        require(row is not None and row[0] is not None, 'Link claim did not consume the QA code')
    for kind, path in paths.items():
        after = daily_snapshot(path, kind, active_only=True, configuration_only=True)
        require(after == before[kind], 'Existing active daily settings changed during smoke')
    with contextlib.closing(readonly(paths['web'])) as connection:
        require(connection.execute('SELECT balance_mode_enabled,balance_autodebit_enabled FROM web_balance_accounts WHERE profile_token=?', (qa_token,)).fetchone() == (0, 0), 'QA profile enabled daily billing')
    return {'qa_without_orders': True, 'login_logout': True, 'referral': True, 'telegram_linking': True}


def run_readonly_smoke(web_base, secret):
    client = Client(web_base, secret)
    auth_status, auth_data = client.call('/api/store/auth/me')
    require(auth_status == 200 and auth_data.get('authenticated') is False, 'Unauthenticated auth health failed')
    require(client.call('/api/store/profile-orders')[0] in (400, 401), 'Unauthenticated profile lookup was accepted')
    require(client.call('/api/store/balance-toggle', {'enabled': True})[0] in (400, 401), 'Unauthenticated billing toggle was accepted')
    with urlopen(Request(web_base.rstrip('/') + '/'), timeout=10) as response:
        require(response.status == 200, 'Webstore home health failed')
    with urlopen(Request(web_base.rstrip('/') + '/profile'), timeout=10) as response:
        page = response.read().decode('utf-8', 'replace')
        require(response.status == 200 and 'referralBox' in page and 'Пригласить друга' in page, 'Profile referral UI health failed')
    return {'readonly_auth': True, 'readonly_profile': True, 'readonly_referral_ui': True}
