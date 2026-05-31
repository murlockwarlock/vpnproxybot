from __future__ import annotations

from bot.utils import texts


def test_core_client_texts_avoid_stale_brand_and_forbidden_terms():
    snippets = [
        texts.WELCOME,
        texts.WELCOME_BACK,
        texts.WELCOME_RENEW,
        texts.HELP,
        texts.PROFILE,
        texts.PROFILE_RENEW_HINT,
        texts.PAYMENT_STARS_INVOICE_TITLE,
        texts.PAYMENT_STARS_INVOICE_DESC,
        texts.PAYMENT_CANCELLED,
    ]
    forbidden = ["darimiru", "vpn", "впн", "туннел", "анонимайзер", "прокси"]

    normalized = "\n".join(snippets).lower()

    for term in forbidden:
        assert term not in normalized
