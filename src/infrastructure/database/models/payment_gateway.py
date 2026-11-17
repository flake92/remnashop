from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Mapped, mapped_column

from src.core.enums import Currency, PaymentGatewayType

from .base import BaseSql


class PaymentGateway(BaseSql):
    __tablename__ = "payment_gateways"

    id: Mapped[int] = mapped_column(primary_key=True)

    order_index: Mapped[int] = mapped_column(index=True)
    type: Mapped[PaymentGatewayType] = mapped_column(unique=True)
    currency: Mapped[Currency]

    is_active: Mapped[bool]
    web_only: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default=text("false")
    )
    settings: Mapped[Optional[dict[str, Any]]]
