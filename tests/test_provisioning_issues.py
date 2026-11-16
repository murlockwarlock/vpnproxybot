import pytest

from bot.services.provisioning_issues import (
    AccessProvisionError,
    build_internal_access_error,
    build_vhq_access_error,
    describe_access_provision_result,
    recommend_access_provision_action,
    safe_access_provision_reason,
)


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


@pytest.mark.parametrize(
    "raw_message",
    [
        '{"token":"synthetic-secret"}',
        '{"access_token":"synthetic-secret"}',
        '{"token": "synthetic-secret with spaces"}',
        "Authorization: Bearer synthetic-bearer",
        "https://supplier.invalid/error?token=synthetic-secret",
        '{"details":"' + ("x" * 500) + '","token":"synthetic-secret"}',
        "line one\nline two\nsecret: synthetic-secret",
    ],
)
def test_safe_access_provision_reason_fails_closed_for_untrusted_responses(raw_message):
    issue = AccessProvisionError(
        provider="adapt",
        code="adapt_upgrade_failed",
        client_message="x",
        admin_message="Adapt upgrade failed",
        status=503,
        raw_message=raw_message,
    )

    reason = safe_access_provision_reason(issue)

    assert "synthetic-secret" not in reason
    assert "synthetic-bearer" not in reason
    assert "supplier.invalid" not in reason
    assert len(reason) <= 240
    assert reason == "Поставщик не подтвердил улучшение подписки. HTTP 503"


def test_safe_access_provision_reason_preserves_allowlisted_provider_reason():
    issue = AccessProvisionError(
        provider="adapt",
        code="adapt_upgrade_failed",
        client_message="x",
        admin_message="Adapt upgrade failed",
        status=503,
        raw_message="Service temporarily unavailable",
    )

    assert safe_access_provision_reason(issue) == "Service temporarily unavailable"


def test_post_mutation_without_confirmation_requires_provider_reconciliation():
    issue = AccessProvisionError(
        provider="vhq",
        code="vhq_missing_config",
        client_message="x",
        admin_message="VHQ response did not contain config_url",
        stage="post_mutation",
    )

    assert describe_access_provision_result(issue) == (
        "Запрос к поставщику выполнялся, итог операции требует сверки"
    )
    assert recommend_access_provision_action(issue) == "Не повторяйте действие до проверки"
