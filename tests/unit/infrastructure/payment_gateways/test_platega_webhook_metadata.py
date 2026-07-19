from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import ANY, AsyncMock
from uuid import UUID

import orjson
import pytest
from starlette.requests import Request

import src.web.endpoints.payments as payments_endpoint
from src.core.enums import PaymentGatewayType, TransactionStatus
from src.infrastructure.payment_gateways.platega import PlategaGateway
from src.web.endpoints.payments import _process_payment_webhook


class FakeUnitOfWork:
    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class FakeTransactionDao:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.events: list[dict[str, Any]] = []

    async def store_webhook_event(self, **kwargs: Any) -> None:
        if self.fail:
            raise RuntimeError("database unavailable")
        self.events.append(kwargs)


def signed_request(payment_method: Any) -> Request:
    body = orjson.dumps(
        {
            "id": "00000000-0000-0000-0000-000000000888",
            "status": "CONFIRMED",
            "paymentMethod": payment_method,
        }
    )
    sent = False

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/payments/platega",
            "headers": [
                (b"x-merchantid", b"merchant"),
                (b"x-secret", b"secret"),
            ],
        },
        receive,
    )


def gateway() -> PlategaGateway:
    result = object.__new__(PlategaGateway)
    result.merchant_id = "merchant"
    result.api_key = "secret"
    result.selected_payment_method = None
    return result


@pytest.mark.parametrize(
    "value",
    ["X" * 65, "CARD\nINJECT", 123, ["CARD"], {"method": "CARD"}],
)
def test_platega_rejects_noncanonical_payment_method(value: Any) -> None:
    with pytest.raises(ValueError):
        PlategaGateway._normalize_payment_method(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, None), ("  ", None), (" CARD-SBP_2 ", "CARD-SBP_2")],
)
def test_platega_normalizes_safe_payment_method(value: Any, expected: Optional[str]) -> None:
    assert PlategaGateway._normalize_payment_method(value) == expected


@pytest.mark.asyncio
async def test_signed_terminal_webhook_with_invalid_method_is_processed_without_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dao = FakeTransactionDao()
    platega = gateway()
    enqueue = AsyncMock(return_value=None)
    monkeypatch.setattr(payments_endpoint, "_enqueue_payment_task", enqueue)

    response = await _process_payment_webhook(
        gateway_type="platega",
        request=signed_request("CARD\nINJECT"),
        config=SimpleNamespace(build=SimpleNamespace(data={})),  # type: ignore[arg-type]
        event_publisher=SimpleNamespace(publish=AsyncMock()),  # type: ignore[arg-type]
        get_payment_gateway_instance=SimpleNamespace(  # type: ignore[arg-type]
            system=AsyncMock(return_value=platega)
        ),
        transaction_dao=dao,  # type: ignore[arg-type]
        uow=FakeUnitOfWork(),  # type: ignore[arg-type]
    )

    assert response.status_code == 200
    assert dao.events == [
        {
            "payment_id": UUID("00000000-0000-0000-0000-000000000888"),
            "gateway_type": PaymentGatewayType.PLATEGA,
            "status": TransactionStatus.COMPLETED,
            "selected_payment_method": None,
            "error_code": None,
        }
    ]
    enqueue.assert_awaited_once_with(
        UUID("00000000-0000-0000-0000-000000000888"),
        TransactionStatus.COMPLETED,
        PaymentGatewayType.PLATEGA,
        "platega",
        ANY,
        ANY,
    )


@pytest.mark.asyncio
async def test_invalid_method_is_not_acknowledged_when_durable_store_fails() -> None:
    platega = gateway()

    response = await _process_payment_webhook(
        gateway_type="platega",
        request=signed_request("X" * 65),
        config=SimpleNamespace(build=SimpleNamespace(data={})),  # type: ignore[arg-type]
        event_publisher=SimpleNamespace(publish=AsyncMock()),  # type: ignore[arg-type]
        get_payment_gateway_instance=SimpleNamespace(  # type: ignore[arg-type]
            system=AsyncMock(return_value=platega)
        ),
        transaction_dao=FakeTransactionDao(fail=True),  # type: ignore[arg-type]
        uow=FakeUnitOfWork(),  # type: ignore[arg-type]
    )

    assert response.status_code == 503
