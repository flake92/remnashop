from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import HTTPException, status

from src.application.dto import PaymentResultDto, PriceDetailsDto
from src.application.services.payment_idempotency import PaymentOperationStart
from src.core.enums import Currency, PaymentGatewayType
from src.web.endpoints.public.subscription import purchase_subscription
from src.web.schemas import PaymentInitResponse, PurchaseRequest


class FakePaymentIdempotency:
    def __init__(self) -> None:
        self.response: Optional[dict[str, Any]] = None
        self.started = PaymentOperationStart(operation_id=1, provider_key="provider-key-1")
        self.unknown_calls = 0
        self.abandon_calls = 0

    async def start(self, **kwargs: Any) -> PaymentOperationStart:
        if self.response is None:
            return self.started
        return PaymentOperationStart(
            operation_id=self.started.operation_id,
            provider_key=self.started.provider_key,
            replay_response=self.response,
        )

    async def mark_processing(self, operation_id: int) -> None:
        return None

    async def complete(self, operation_id: int, response: dict[str, Any]) -> None:
        self.response = response

    async def mark_unknown(self, operation_id: int) -> None:
        self.unknown_calls += 1

    async def abandon(self, operation_id: int) -> None:
        self.abandon_calls += 1


def build_purchase_call(
    monkeypatch: Any,
    create_payment: AsyncMock,
    idempotency_key: Optional[str],
) -> tuple[Any, dict[str, Any], Any, FakePaymentIdempotency]:
    duration = SimpleNamespace(days=30, get_price=lambda currency: Decimal(0))
    plan = SimpleNamespace(
        public_code="basic",
        get_duration=lambda days: duration if days == 30 else None,
    )
    gateway = SimpleNamespace(
        type=PaymentGatewayType.YOOKASSA,
        currency=Currency.RUB,
        is_active=True,
        settings=SimpleNamespace(is_configured=True),
    )
    process_payment = SimpleNamespace(system=AsyncMock())
    idempotency = FakePaymentIdempotency()
    monkeypatch.setattr(
        "src.web.endpoints.public.subscription.PlanSnapshotDto.from_plan",
        lambda plan, days: SimpleNamespace(id=1, duration=days),
    )
    handler = purchase_subscription.__dishka_orig_func__  # type: ignore[attr-defined]
    kwargs = {
        "body": PurchaseRequest(
            plan_code="basic",
            duration_days=30,
            gateway_type=PaymentGatewayType.YOOKASSA,
        ),
        "user": SimpleNamespace(id=7, is_email_verified=True),
        "subscription_dao": SimpleNamespace(get_current=AsyncMock(return_value=None)),
        "payment_gateway_dao": SimpleNamespace(get_by_type=AsyncMock(return_value=gateway)),
        "pricing_service": SimpleNamespace(
            calculate=lambda *args, **kwargs: PriceDetailsDto(
                original_amount=Decimal(0),
                discount_percent=0,
                final_amount=Decimal(0),
            )
        ),
        "get_available_plans": SimpleNamespace(system=AsyncMock(return_value=[plan])),
        "create_payment": create_payment,
        "process_payment": process_payment,
        "idempotency": idempotency,
        "idempotency_key": idempotency_key,
    }
    return handler, kwargs, process_payment, idempotency


@pytest.mark.asyncio
async def test_free_purchase_is_fulfilled_once_and_then_replayed(monkeypatch: Any) -> None:
    payment = PaymentResultDto(id=uuid4(), url=None)
    create_payment = AsyncMock(return_value=payment)
    handler, kwargs, process_payment, _ = build_purchase_call(
        monkeypatch,
        create_payment,
        "request-key-free-0001",
    )

    first = await handler(**kwargs)
    second = await handler(**kwargs)

    assert isinstance(first, PaymentInitResponse)
    assert second == first
    assert first.is_free is True
    assert first.status == "COMPLETED"
    assert create_payment.await_count == 1
    assert process_payment.system.await_count == 1


@pytest.mark.asyncio
async def test_error_after_side_effect_boundary_is_stable_unknown_409(monkeypatch: Any) -> None:
    create_payment = AsyncMock(
        side_effect=HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="provider rejected request",
        )
    )
    handler, kwargs, _, idempotency = build_purchase_call(
        monkeypatch,
        create_payment,
        "request-key-error-0001",
    )

    with pytest.raises(HTTPException) as raised:
        await handler(**kwargs)

    assert raised.value.status_code == status.HTTP_409_CONFLICT
    assert "payment outcome is unknown" in raised.value.detail.lower()
    assert idempotency.unknown_calls == 1


@pytest.mark.asyncio
async def test_legacy_request_preserves_original_error(monkeypatch: Any) -> None:
    create_payment = AsyncMock(
        side_effect=HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="provider rejected request",
        )
    )
    handler, kwargs, _, idempotency = build_purchase_call(monkeypatch, create_payment, None)

    with pytest.raises(HTTPException) as raised:
        await handler(**kwargs)

    assert raised.value.status_code == status.HTTP_400_BAD_REQUEST
    assert raised.value.detail == "provider rejected request"
    assert idempotency.unknown_calls == 0
