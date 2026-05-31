"""Helpers for turning supplier/runtime failures into actionable issue objects."""

from __future__ import annotations

from dataclasses import dataclass


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

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.admin_message)


def build_internal_access_error(
    *,
    provider: str,
    code: str,
    admin_message: str,
    client_message: str | None = None,
    status: int | None = None,
    raw_message: str | None = None,
) -> AccessProvisionError:
    return AccessProvisionError(
        provider=provider,
        code=code,
        client_message=client_message or _CLIENT_DELAY_TEXT,
        admin_message=admin_message,
        status=status,
        raw_message=raw_message,
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
            admin_message=f"VHQ auth/config error: {normalized or f'HTTP {status}'}{context_suffix}",
            client_message=(
                "Оплата прошла, но автоматическая выдача временно недоступна. "
                "Мы уже получили уведомление и исправляем проблему."
            ),
        )

    if status == 400:
        return build_internal_access_error(
            provider="vhq",
            code="vhq_request_invalid",
            status=status,
            raw_message=normalized,
            admin_message=f"VHQ request mismatch: {normalized or f'HTTP {status}'}{context_suffix}",
            client_message=(
                "Оплата прошла, но автоматическая выдача временно недоступна. "
                "Мы уже получили уведомление и проверяем настройки."
            ),
        )

    if status == 402 or "insufficient balance" in lowered:
        return build_internal_access_error(
            provider="vhq",
            code="vhq_balance",
            status=status,
            raw_message=normalized,
            admin_message=f"VHQ partner balance issue: {normalized or 'Insufficient balance'}{context_suffix}",
        )

    if status == 502 or "supplier error" in lowered:
        return build_internal_access_error(
            provider="vhq",
            code="vhq_supplier",
            status=status,
            raw_message=normalized,
            admin_message=f"VHQ supplier error: {normalized or f'HTTP {status}'}{context_suffix}",
        )

    if "config_url" in lowered:
        return build_internal_access_error(
            provider="vhq",
            code="vhq_missing_config",
            status=status,
            raw_message=normalized,
            admin_message=f"VHQ returned no config_url: {normalized or 'missing config_url'}{context_suffix}",
        )

    return build_internal_access_error(
        provider="vhq",
        code="vhq_unknown",
        status=status,
        raw_message=normalized,
        admin_message=f"VHQ unexpected error: {normalized or f'HTTP {status or 0}'}{context_suffix}",
    )
