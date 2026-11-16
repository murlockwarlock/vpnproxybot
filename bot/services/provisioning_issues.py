"""Helpers for turning supplier/runtime failures into actionable issue objects."""

from __future__ import annotations

from dataclasses import dataclass
import re


_CLIENT_DELAY_TEXT = (
    "Оплата прошла, но выдача доступа задержалась. "
    "Мы уже получили уведомление и проверяем проблему."
)


@dataclass(slots=True)
class AccessProvisionError(RuntimeError):
    provider: str
    code: str
    client_message: str
    admin_message: str
    status: int | None = None
    raw_message: str | None = None
    stage: str = "provisioning"            # "pre_mutation" | "post_mutation" | "unknown"
    confirmed_rejection: bool = False      # True if provider definitely rejected before mutation
    safe_retry_eligible: bool = False      # True if safe to retry without double charge risk

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.admin_message)


_TRANSIENT_PROVIDER_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504})
_SENSITIVE_FIELD_RE = re.compile(
    r"(?i)\b(?:api[_ -]?key|access[_ -]?token|refresh[_ -]?token|token|secret|password|authorization|cookie)\b"
    r"\s*[:=]"
)
_BEARER_RE = re.compile(r"(?i)\b(?:bearer|basic)\b")
_URL_RE = re.compile(r"(?i)https?://")
_UNSAFE_DELIMITER_RE = re.compile(r"[{}\[\]\"'`]")
_KNOWN_SAFE_REASONS = {
    "service temporarily unavailable": "Service temporarily unavailable",
}
_DEFAULT_REASON_BY_CODE = {
    "adapt_api_402": "Поставщик отклонил запрос из-за недостатка средств.",
    "vhq_balance": "Поставщик отклонил запрос из-за недостатка средств.",
    "vhq_auth": "Поставщик отклонил запрос из-за ошибки авторизации.",
    "vhq_request_invalid": "Поставщик отклонил запрос как некорректный.",
    "adapt_upgrade_failed": "Поставщик не подтвердил улучшение подписки.",
    "adapt_renew_failed": "Поставщик не подтвердил продление подписки.",
}


def describe_access_provision_result(issue: AccessProvisionError) -> str:
    """Return a safe operational classification for an admin-facing report."""
    if issue.confirmed_rejection:
        return "Подтверждённый отказ поставщика до изменения"
    if issue.status in _TRANSIENT_PROVIDER_STATUSES:
        return "Временная недоступность поставщика; результат запроса не подтверждён"
    return "Запрос к поставщику выполнялся, итог операции требует сверки"


def recommend_access_provision_action(issue: AccessProvisionError) -> str:
    """Return a safe next step without implying that a supplier retry is harmless."""
    if issue.confirmed_rejection and issue.safe_retry_eligible:
        return "Повторить можно после устранения причины"
    if issue.confirmed_rejection:
        return "Не повторять до проверки параметров и причины отказа"
    if issue.status in _TRANSIENT_PROVIDER_STATUSES:
        return "Не повторять вслепую; проверить поставщика, затем решить о повторе"
    return "Не повторяйте действие до проверки"


def _safe_reason_fallback(issue: AccessProvisionError) -> str:
    reason = _DEFAULT_REASON_BY_CODE.get(issue.code) or f"Операция {issue.code} не подтверждена"
    if issue.status is not None:
        return f"{reason} HTTP {issue.status}"
    return reason


def _extract_known_safe_reason(value: str) -> str | None:
    if not value or any(char in value for char in "\r\n"):
        return None
    if (
        _UNSAFE_DELIMITER_RE.search(value)
        or _URL_RE.search(value)
        or _SENSITIVE_FIELD_RE.search(value)
        or _BEARER_RE.search(value)
    ):
        return None
    normalized = " ".join(value.split()).casefold()
    for known_reason, display_reason in _KNOWN_SAFE_REASONS.items():
        if known_reason in normalized:
            return display_reason
    return None


def safe_access_provision_reason(issue: AccessProvisionError, *, limit: int = 240) -> str:
    """Return only an allowlisted provider reason or a code/status fallback."""
    for candidate in (issue.raw_message, issue.admin_message):
        safe_reason = _extract_known_safe_reason(str(candidate or "").strip())
        if safe_reason:
            return safe_reason
    return _safe_reason_fallback(issue)[:limit]


def build_internal_access_error(
    *,
    provider: str,
    code: str,
    admin_message: str,
    client_message: str | None = None,
    status: int | None = None,
    raw_message: str | None = None,
    stage: str = "unknown",
    confirmed_rejection: bool = False,
    safe_retry_eligible: bool = False,
) -> AccessProvisionError:
    return AccessProvisionError(
        provider=provider,
        code=code,
        client_message=client_message or _CLIENT_DELAY_TEXT,
        admin_message=admin_message,
        status=status,
        raw_message=raw_message,
        stage=stage,
        confirmed_rejection=confirmed_rejection,
        safe_retry_eligible=safe_retry_eligible,
    )


def build_vhq_access_error(
    *,
    status: int | None,
    message: str,
    context: str | None = None,
) -> AccessProvisionError:
    normalized = (message or "").strip()
    lowered = normalized.lower()
    context_suffix = f" | {context}" if context else ""

    if status in {401, 403}:
        return build_internal_access_error(
            provider="vhq",
            code="vhq_auth",
            status=status,
            raw_message=normalized,
            stage="pre_mutation",
            confirmed_rejection=True,
            safe_retry_eligible=True,
            admin_message=f"VHQ auth/config error: {normalized or f'HTTP {status}'}{context_suffix}",
            client_message=(
                "Оплата прошла, но автоматическая выдача временно недоступна. "
                "Мы уже получили уведомление и исправляем проблему."
            ),
        )

    if status == 402 or "insufficient balance" in lowered or "недостаточно средств" in lowered:
        return build_internal_access_error(
            provider="vhq",
            code="vhq_balance",
            status=status,
            raw_message=normalized,
            stage="pre_mutation",
            confirmed_rejection=True,
            safe_retry_eligible=True,
            admin_message=f"VHQ partner balance issue: {normalized or 'Insufficient balance'}{context_suffix}",
            client_message=(
                "Оплата прошла, но выдача доступа задержалась. "
                "Мы уже получили уведомление и проверяем пополнение."
            ),
        )

    if status == 400:
        return build_internal_access_error(
            provider="vhq",
            code="vhq_request_invalid",
            status=status,
            raw_message=normalized,
            stage="pre_mutation",
            confirmed_rejection=True,
            safe_retry_eligible=False,
            admin_message=f"VHQ request mismatch: {normalized or f'HTTP {status}'}{context_suffix}",
            client_message=(
                "Оплата прошла, но автоматическая выдача временно недоступна. "
                "Мы уже получили уведомление и проверяем настройки."
            ),
        )

    if status == 502 or "supplier error" in lowered:
        return build_internal_access_error(
            provider="vhq",
            code="vhq_supplier",
            status=status,
            raw_message=normalized,
            stage="unknown",
            confirmed_rejection=False,
            safe_retry_eligible=False,
            admin_message=f"VHQ supplier error: {normalized or f'HTTP {status}'}{context_suffix}",
        )

    if "config_url" in lowered:
        return build_internal_access_error(
            provider="vhq",
            code="vhq_missing_config",
            status=status,
            raw_message=normalized,
            stage="post_mutation",
            confirmed_rejection=False,
            safe_retry_eligible=False,
            admin_message=f"VHQ returned no config_url: {normalized or 'missing config_url'}{context_suffix}",
        )

    return build_internal_access_error(
        provider="vhq",
        code="vhq_unknown",
        status=status,
        raw_message=normalized,
        stage="unknown",
        confirmed_rejection=False,
        safe_retry_eligible=False,
        admin_message=f"VHQ unexpected error: {normalized or f'HTTP {status or 0}'}{context_suffix}",
    )


def build_adapt_access_error(
    *,
    status: int | None,
    message: str,
    context: str | None = None,
    code: str | None = None,
) -> AccessProvisionError:
    normalized = (message or "").strip()
    context_suffix = f" | {context}" if context else ""
    if status == 402:
        return build_internal_access_error(
            provider="adapt",
            code="adapt_api_402",
            status=status,
            raw_message=normalized,
            stage="pre_mutation",
            confirmed_rejection=True,
            safe_retry_eligible=True,
            admin_message=(
                f"Adapt partner balance issue: {normalized or 'Insufficient balance'}"
                f"{context_suffix}"
            ),
            client_message=(
                "Оплата прошла, но выдача доступа задержалась. "
                "Мы автоматически продолжим после пополнения."
            ),
        )
    confirmed_rejection = status is not None and 400 <= status < 500 and status not in _TRANSIENT_PROVIDER_STATUSES
    safe_retry_eligible = confirmed_rejection and status in {401, 402, 403}
    issue_code = code or f"adapt_api_{status or 'error'}"
    if confirmed_rejection:
        issue_stage = "pre_mutation"
    else:
        issue_stage = "unknown"
    return build_internal_access_error(
        provider="adapt",
        code=issue_code,
        status=status,
        raw_message=normalized,
        stage=issue_stage,
        confirmed_rejection=confirmed_rejection,
        safe_retry_eligible=safe_retry_eligible,
        admin_message=(
            f"Adapt API error: {normalized or f'HTTP {status or 0}'}"
            f"{context_suffix}"
        ),
    )
