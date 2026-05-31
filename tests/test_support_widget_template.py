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


def test_deploy_webstore_proxies_support_chat_routes():
    deploy_script = (ROOT / "deploy_webstore.py").read_text(encoding="utf-8")
    assert "location /support" in deploy_script
    assert "location /api/support/" in deploy_script
    assert "location /ws/support/" in deploy_script
    assert 'proxy_set_header Connection "upgrade";' in deploy_script
