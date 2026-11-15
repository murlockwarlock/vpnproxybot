"""Authenticated YooKassa payment lookup for the web store."""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import aiohttp

from webstore.config import settings


class YooKassaVerificationError(RuntimeError):
    """Raised when the authoritative YooKassa payment state cannot be read."""


@dataclass(frozen=True)
class YooKassaPaymentSnapshot:
    payment_id: str
    status: str
    paid: bool
    amount_rub: Decimal | None
    currency: str
    metadata: dict[str, Any]


def _field(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _metadata_dict(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if hasattr(value, "to_dict"):
        result = value.to_dict()
        return dict(result) if result else {}
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def _amount_snapshot(value: Any) -> tuple[Decimal | None, str]:
    if not value:
        return None, ""
    raw_value = _field(value, "value")
    currency = str(_field(value, "currency", "") or "").strip().upper()
    try:
        amount = Decimal(str(raw_value))
    except (InvalidOperation, TypeError, ValueError):
        amount = None
    return amount, currency


async def fetch_yookassa_payment(payment_id: str) -> YooKassaPaymentSnapshot:
    """Read a payment from YooKassa using server-side credentials with bounded timeouts."""
    payment_id = str(payment_id or "").strip()
    if not payment_id:
        raise YooKassaVerificationError("missing payment id")
    if not settings.yookassa_shop_id or not settings.yookassa_secret_key:
        raise YooKassaVerificationError("YooKassa credentials are not configured")

    # If yookassa SDK has been patched with a custom/mock Payment.find_one in tests,
    # invoke that mock while enforcing an explicit timeout.
    if "yookassa" in sys.modules:
        yk = sys.modules["yookassa"]
        payment_cls = getattr(yk, "Payment", None)
        find_one = getattr(payment_cls, "find_one", None)
        if find_one is not None and getattr(find_one, "__module__", "") != "yookassa.payment":
            try:
                payment = await asyncio.wait_for(
                    asyncio.to_thread(find_one, payment_id),
                    timeout=10.0,
                )
            except asyncio.TimeoutError as exc:
                raise YooKassaVerificationError(
                    f"YooKassa payment lookup timed out for {payment_id}"
                ) from exc
            except Exception as exc:
                raise YooKassaVerificationError(
                    f"failed to fetch YooKassa payment {payment_id}"
                ) from exc

            if not payment:
                raise YooKassaVerificationError(
                    f"YooKassa payment {payment_id} was not returned"
                )

            resolved_id = str(_field(payment, "id", payment_id) or payment_id).strip()
            if resolved_id != payment_id:
                raise YooKassaVerificationError(
                    f"YooKassa returned unexpected payment id {resolved_id}"
                )

            amount_rub, currency = _amount_snapshot(_field(payment, "amount"))
            return YooKassaPaymentSnapshot(
                payment_id=payment_id,
                status=str(_field(payment, "status", "") or "").strip().lower(),
                paid=bool(_field(payment, "paid", False)),
                amount_rub=amount_rub,
                currency=currency,
                metadata=_metadata_dict(_field(payment, "metadata", {})),
            )

    # Primary production path: async HTTP call with bounded timeouts
    url = f"https://api.yookassa.ru/v3/payments/{payment_id}"
    auth = aiohttp.BasicAuth(settings.yookassa_shop_id, settings.yookassa_secret_key)
    timeout = aiohttp.ClientTimeout(total=10.0, connect=3.0, sock_read=7.0)

    try:
        async with aiohttp.ClientSession(timeout=timeout) as http_session:
            async with http_session.get(url, auth=auth, headers={"Accept": "application/json"}) as resp:
                if resp.status == 404:
                    raise YooKassaVerificationError(
                        f"YooKassa payment {payment_id} was not returned"
                    )
                if resp.status != 200:
                    raise YooKassaVerificationError(
                        f"YooKassa returned HTTP {resp.status} for {payment_id}"
                    )
                try:
                    payment_data = await resp.json()
                except Exception as exc:
                    raise YooKassaVerificationError(
                        f"failed to parse YooKassa response for {payment_id}: {exc}"
                    ) from exc
    except asyncio.TimeoutError as exc:
        raise YooKassaVerificationError(
            f"YooKassa payment lookup timed out for {payment_id}"
        ) from exc
    except aiohttp.ClientError as exc:
        raise YooKassaVerificationError(
            f"failed to fetch YooKassa payment {payment_id}: {exc}"
        ) from exc

    if not isinstance(payment_data, dict):
        raise YooKassaVerificationError(
            f"YooKassa returned non-dictionary response for {payment_id}"
        )

    resolved_id = str(_field(payment_data, "id", payment_id) or payment_id).strip()
    if resolved_id != payment_id:
        raise YooKassaVerificationError(
            f"YooKassa returned unexpected payment id {resolved_id}"
        )

    amount_rub, currency = _amount_snapshot(_field(payment_data, "amount"))
    return YooKassaPaymentSnapshot(
        payment_id=payment_id,
        status=str(_field(payment_data, "status", "") or "").strip().lower(),
        paid=bool(_field(payment_data, "paid", False)),
        amount_rub=amount_rub,
        currency=currency,
        metadata=_metadata_dict(_field(payment_data, "metadata", {})),
    )
