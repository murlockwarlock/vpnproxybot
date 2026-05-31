"""Helpers for legal document links shown in the bot."""

from __future__ import annotations

from bot.models import BotSettings


LEGAL_DOCS = {
    "policy": ("legal_policy_url", "Политика конфиденциальности"),
    "agree": ("legal_agree_url", "Пользовательское соглашение"),
    "oferta": ("legal_oferta_url", "Публичная оферта"),
}


async def get_legal_doc_url(session, doc_code: str) -> str:
    key = LEGAL_DOCS[doc_code][0]
    row = await session.get(BotSettings, key)
    return row.value.strip() if row and row.value else ""


async def get_all_legal_doc_urls(session) -> dict[str, str]:
    result: dict[str, str] = {}
    for code in LEGAL_DOCS:
        result[code] = await get_legal_doc_url(session, code)
    return result


def build_legal_notice(urls: dict[str, str]) -> str:
    parts: list[str] = []
    if urls.get("oferta"):
        parts.append(f'<a href="{urls["oferta"]}">Офертой</a>')
    if urls.get("agree"):
        parts.append(f'<a href="{urls["agree"]}">соглашением</a>')
    if urls.get("policy"):
        parts.append(f'<a href="{urls["policy"]}">политикой</a>')

    if not parts:
        return ""

    if len(parts) == 1:
        docs_text = parts[0]
    elif len(parts) == 2:
        docs_text = " и ".join(parts)
    else:
        docs_text = ", ".join(parts[:-1]) + " и " + parts[-1]

    return (
        "\n"
        "Нажимая кнопку оплаты, вы соглашаетесь с "
        f"{docs_text}."
    )
