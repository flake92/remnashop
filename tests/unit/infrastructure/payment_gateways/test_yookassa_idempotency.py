from decimal import Decimal
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import orjson
import pytest

from src.infrastructure.payment_gateways.yookassa import YookassaGateway


class FakeResponse:
    def __init__(self, payment_id: str) -> None:
        self.content = orjson.dumps(
            {
                "id": payment_id,
                "status": "pending",
                "confirmation": {"confirmation_url": "https://payment.example/confirm"},
            }
        )

    def raise_for_status(self) -> None:
        return None


class FakeClient:
    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.headers: list[dict[str, str]] = []

    async def post(self, *args: Any, **kwargs: Any) -> FakeResponse:
        self.headers.append(kwargs["headers"])
        return self.response


@pytest.mark.asyncio
async def test_yookassa_uses_durable_provider_key() -> None:
    gateway = object.__new__(YookassaGateway)
    client = FakeClient(FakeResponse(str(uuid4())))
    gateway._client = client
    gateway._create_payment_payload = AsyncMock(return_value={})  # type: ignore[method-assign]

    await gateway.create_payment(
        Decimal("100"),
        "Subscription",
        idempotency_key="durable-provider-key",
    )
    await gateway.create_payment(
        Decimal("100"),
        "Subscription",
        idempotency_key="durable-provider-key",
    )

    assert client.headers == [
        {"Idempotence-Key": "durable-provider-key"},
        {"Idempotence-Key": "durable-provider-key"},
    ]
