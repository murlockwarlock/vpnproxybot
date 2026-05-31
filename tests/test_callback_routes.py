from __future__ import annotations

import re
from pathlib import Path


def test_literal_callback_data_have_matching_handlers():
    bot_dir = Path(__file__).resolve().parents[1] / "bot"
    source = "\n".join(path.read_text(encoding="utf-8") for path in bot_dir.rglob("*.py"))

    literal_callbacks = set(re.findall(r'callback_data="([A-Za-z0-9_:-]+)"', source))
    exact_handlers = set(re.findall(r'F\.data == "([A-Za-z0-9_:-]+)"', source))

    in_set_handlers = set()
    for match in re.finditer(r'F\.data\.in_\(\{([^}]*)\}\)', source, re.S):
        in_set_handlers.update(re.findall(r'"([A-Za-z0-9_:-]+)"', match.group(1)))

    startswith_handlers = re.findall(r'F\.data\.startswith\("([A-Za-z0-9_:-]+)"\)', source)
    regexp_handlers = [re.compile(pattern) for pattern in re.findall(r'F\.data\.regexp\(r"([^"]+)"\)', source)]

    # Intentionally inert buttons.
    allowed_without_handler = {"ignore"}

    unknown_callbacks = []
    for callback_data in sorted(literal_callbacks):
        has_handler = (
            callback_data in exact_handlers
            or callback_data in in_set_handlers
            or callback_data in allowed_without_handler
            or any(callback_data.startswith(prefix) for prefix in startswith_handlers)
            or any(pattern.search(callback_data) for pattern in regexp_handlers)
        )
        if not has_handler:
            unknown_callbacks.append(callback_data)

    assert unknown_callbacks == []
