from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent


def _template(name: str) -> str:
    return (ROOT / "webstore" / "templates" / name).read_text(encoding="utf-8")


def test_support_widget_loads_iframe_explicitly():
    for template_name in ("store.html", "profile.html"):
        html = _template(template_name)
        assert 'id="supportFrame" src=""' not in html
        assert 'id="supportFrame" title="Чат с поддержкой"' in html
        assert "frame.dataset.loaded !== '1'" in html
        assert "frame.src = '/support?widget=1'" in html


def test_support_widget_has_mobile_layout():
    for template_name in ("store.html", "profile.html"):
        html = _template(template_name)
        assert "@media (max-width: 640px)" in html
        assert "height: min(680px, 100dvh)" in html


def test_support_page_can_reset_closed_ticket_when_enabled():
    html = _template("support.html")
    assert 'id="resolvedActions"' in html
    assert 'onclick="startNewTicket()"' in html
    assert "localStorage.removeItem(STORAGE_KEY)" in html
    assert 'document.getElementById("newTicketScreen").style.display = "flex"' in html
    assert 'document.getElementById("resolvedActions").style.display = "block"' in html


def test_support_page_has_messenger_contact_buttons_below_start_chat():
    html = _template("support.html")
    start_chat_pos = html.index('id="startChatBtn"')
    contacts_pos = html.index("Связаться с нами:")

    assert contacts_pos > start_chat_pos
    assert 'href="https://t.me/darimiru_support"' in html
    assert 'href="https://vk.me/id1115218916"' in html
    assert 'href="https://max.ru/u/f9LHodD0cOLRekkBBJUG4DVfHlbqzm1ZEE1C6fp6cGKYXHwcI83LikdFndI"' in html


def test_support_page_has_requested_messenger_options():
    html = _template("support.html")
    select = html[html.index('<select id="messengerInput">'):html.index("</select>")]

    expected_options = (
        '<option value="">— не указано —</option>',
        '<option value="telegram">Telegram</option>',
        '<option value="vk">ВКонтакте</option>',
        '<option value="max">MAX</option>',
        '<option value="whatsapp">WhatsApp</option>',
        '<option value="email">Email</option>',
        '<option value="other">Другой</option>',
    )
    positions = [select.index(option) for option in expected_options]

    assert positions == sorted(positions)
    assert '<option value="phone">' not in select


def test_deploy_webstore_proxies_support_chat_routes():
    deploy_script = (ROOT / "deploy_webstore.py").read_text(encoding="utf-8")
    assert "location /support" in deploy_script
    assert "location /api/support/" in deploy_script
    assert "location /ws/support/" in deploy_script
    assert 'proxy_set_header Connection "upgrade";' in deploy_script
