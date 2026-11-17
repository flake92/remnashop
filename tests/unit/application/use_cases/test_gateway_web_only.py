from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest

from src.application.dto.payment_gateway import PaymentGatewayDto
from src.application.use_cases.gateways.commands.configuration import (
    TogglePaymentGatewayWebOnly,
)
from src.core.enums import Currency, PaymentGatewayType


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


def gateway(*, web_only: bool = False) -> PaymentGatewayDto:
    return PaymentGatewayDto(
        id=3,
        type=PaymentGatewayType.YOOKASSA,
        currency=Currency.RUB,
        is_active=True,
        web_only=web_only,
    )


def actor() -> Any:
    return SimpleNamespace(log="admin")


@pytest.mark.asyncio
async def test_toggle_marks_gateway_hidden_in_bot() -> None:
    stored = gateway(web_only=False)
    dao = SimpleNamespace(
        get_by_id=AsyncMock(return_value=stored),
        update=AsyncMock(return_value=stored),
    )
    uow = FakeUnitOfWork()
    toggle = TogglePaymentGatewayWebOnly(uow, dao)  # type: ignore[arg-type]

    await toggle._execute(actor(), 3)

    assert stored.web_only is True
    assert dao.update.await_count == 1
    assert uow.commits == 1


@pytest.mark.asyncio
async def test_toggle_returns_gateway_to_the_bot() -> None:
    stored = gateway(web_only=True)
    dao = SimpleNamespace(
        get_by_id=AsyncMock(return_value=stored),
        update=AsyncMock(return_value=stored),
    )
    toggle = TogglePaymentGatewayWebOnly(FakeUnitOfWork(), dao)  # type: ignore[arg-type]

    await toggle._execute(actor(), 3)

    assert stored.web_only is False


@pytest.mark.asyncio
async def test_toggle_rejects_unknown_gateway() -> None:
    dao = SimpleNamespace(
        get_by_id=AsyncMock(return_value=None),
        update=AsyncMock(),
    )
    toggle = TogglePaymentGatewayWebOnly(FakeUnitOfWork(), dao)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        await toggle._execute(actor(), 404)
