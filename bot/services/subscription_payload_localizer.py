from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any
from urllib.parse import quote, unquote, urlsplit, urlunsplit


_DISPLAY_LABEL_REPLACEMENTS = (
    (re.compile(r"subscription\s+expired", re.IGNORECASE), "Подписка истекла"),
    (re.compile(r"contact\s+support", re.IGNORECASE), "Связаться с поддержкой"),
)
_DISPLAY_KEYS = {
    "comment",
    "label",
    "name",
    "ps",
    "remark",
    "remarks",
    "tag",
    "title",
}


def _translate_display_label(value: str) -> str:
    translated = value
    for pattern, replacement in _DISPLAY_LABEL_REPLACEMENTS:
        translated = pattern.sub(replacement, translated)
    return translated


def _localize_uri_line(line: str) -> tuple[str, bool]:
    newline = ""
    content = line
    if line.endswith("\r\n"):
        content, newline = line[:-2], "\r\n"
    elif line.endswith(("\r", "\n")):
        content, newline = line[:-1], line[-1]

    if "://" not in content:
        translated = _translate_display_label(content.strip())
        if translated == content.strip() or not content.strip():
            return line, False
        leading = content[: len(content) - len(content.lstrip())]
        trailing = content[len(content.rstrip()):]
        return f"{leading}{translated}{trailing}{newline}", True

    scheme, _, uri_payload = content.partition("://")
    if scheme.lower() == "vmess" and uri_payload and "#" not in uri_payload:
        decoded = _decode_base64_text(uri_payload)
        if decoded:
            payload, variant = decoded
            try:
                decoded_text = payload.decode("utf-8")
            except UnicodeDecodeError:
                decoded_text = ""
            localized, changed = _localize_json_text(decoded_text) if decoded_text else (decoded_text, False)
            if changed:
                encoded = _encode_base64_text(localized.encode("utf-8"), variant, uri_payload)
                return f"{scheme}://{encoded}{newline}", True

    try:
        parsed = urlsplit(content)
    except ValueError:
        return line, False

    fragment = unquote(parsed.fragment)
    if not fragment:
        return line, False
    translated = _translate_display_label(fragment)
    if translated == fragment:
        return line, False

    rebuilt = urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, parsed.query, quote(translated, safe=""))
    )
    return f"{rebuilt}{newline}", True


def _localize_uri_text(text: str) -> tuple[str, bool]:
    lines = text.splitlines(keepends=True)
    if not lines:
        return text, False
    localized_lines = []
    changed = False
    for line in lines:
        localized, line_changed = _localize_uri_line(line)
        localized_lines.append(localized)
        changed = changed or line_changed
    return "".join(localized_lines), changed


def _localize_json_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        result = {}
        for key, item in value.items():
            if isinstance(item, str) and str(key).lower() in _DISPLAY_KEYS:
                localized = _translate_display_label(item)
                result[key] = localized
                changed = changed or localized != item
            elif isinstance(item, (dict, list)):
                localized, item_changed = _localize_json_value(item)
                result[key] = localized
                changed = changed or item_changed
            else:
                result[key] = item
        return result, changed
    if isinstance(value, list):
        result = []
        changed = False
        for item in value:
            if isinstance(item, (dict, list)):
                localized, item_changed = _localize_json_value(item)
                result.append(localized)
                changed = changed or item_changed
            else:
                result.append(item)
        return result, changed
    return value, False


def _localize_json_text(text: str) -> tuple[str, bool]:
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return text, False
    localized, changed = _localize_json_value(value)
    if not changed:
        return text, False
    return json.dumps(localized, ensure_ascii=False, separators=(",", ":")), True


def _decode_base64_text(text: str) -> tuple[bytes, str] | None:
    compact = "".join(text.split())
    if len(compact) < 8 or len(compact) % 4 == 1:
        return None
    try:
        if re.fullmatch(r"[A-Za-z0-9+/]*={0,2}", compact):
            return base64.b64decode(compact + "=" * (-len(compact) % 4), validate=True), "standard"
        if re.fullmatch(r"[A-Za-z0-9_-]*={0,2}", compact):
            return base64.urlsafe_b64decode(compact + "=" * (-len(compact) % 4)), "urlsafe"
    except (binascii.Error, ValueError):
        return None
    return None


def _encode_base64_text(payload: bytes, variant: str, original: str) -> str:
    if variant == "urlsafe":
        encoded = base64.urlsafe_b64encode(payload).decode("ascii")
    else:
        encoded = base64.b64encode(payload).decode("ascii")
    if "=" not in "".join(original.split()):
        encoded = encoded.rstrip("=")
    return encoded


def _localize_payload_text(text: str, *, allow_base64: bool) -> tuple[str, bool]:
    localized, changed = _localize_uri_text(text)
    if changed:
        return localized, True

    localized, changed = _localize_json_text(text)
    if changed:
        return localized, True

    if not allow_base64:
        return text, False
    decoded = _decode_base64_text(text)
    if not decoded:
        return text, False
    payload, variant = decoded
    try:
        decoded_text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return text, False
    localized_payload, changed = _localize_payload_text(decoded_text, allow_base64=False)
    if not changed:
        return text, False
    encoded = _encode_base64_text(localized_payload.encode("utf-8"), variant, text)
    prefix = text[: len(text) - len(text.lstrip())]
    suffix = text[len(text.rstrip()):]
    return f"{prefix}{encoded}{suffix}", True


def localize_subscription_payload(body: bytes) -> bytes:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return body
    localized, changed = _localize_payload_text(text, allow_base64=True)
    return localized.encode("utf-8") if changed else body
