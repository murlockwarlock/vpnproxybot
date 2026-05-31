from bot.services.provisioning_issues import build_internal_access_error, build_vhq_access_error


def test_vhq_balance_issue_maps_to_customer_safe_message():
    issue = build_vhq_access_error(
        status=402,
        message="Insufficient balance",
        context="order_id=abc123",
    )

    assert issue.provider == "vhq"
    assert issue.code == "vhq_balance"
    assert "выдача доступа задержалась" in issue.client_message.lower()
    assert "balance" in issue.admin_message.lower()


def test_vhq_auth_issue_maps_to_config_problem():
    issue = build_vhq_access_error(
        status=403,
        message="Invalid or inactive API key",
        context="user_id=1",
    )

    assert issue.code == "vhq_auth"
    assert "автоматическая выдача временно недоступна" in issue.client_message.lower()
    assert "auth" in issue.admin_message.lower()


def test_internal_issue_preserves_custom_message():
    issue = build_internal_access_error(
        provider="marzban",
        code="marzban_runtime",
        admin_message="runtime failure",
        client_message="Сервис временно недоступен.",
    )

    assert issue.provider == "marzban"
    assert issue.code == "marzban_runtime"
    assert issue.client_message == "Сервис временно недоступен."
    assert issue.admin_message == "runtime failure"
