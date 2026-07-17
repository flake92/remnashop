from datetime import datetime
from typing import Any, Optional

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseSql
from .timestamp import TimestampMixin


class PaymentOperation(BaseSql, TimestampMixin):
    __tablename__ = "payment_operations"
    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "operation",
            "idempotency_key",
            name="uq_payment_operations_identity",
        ),
        CheckConstraint(
            "operation IN ('PURCHASE', 'EXTEND')",
            name="ck_payment_operations_operation",
        ),
        CheckConstraint(
            "status IN ('CLAIMED', 'PROCESSING', 'SUCCEEDED', 'UNKNOWN')",
            name="ck_payment_operations_status",
        ),
        CheckConstraint(
            "((status IN ('CLAIMED', 'PROCESSING') AND lease_expires_at IS NOT NULL) "
            "OR (status IN ('SUCCEEDED', 'UNKNOWN') AND lease_expires_at IS NULL))",
            name="ck_payment_operations_lease",
        ),
        Index("ix_payment_operations_status_updated_at", "status", "updated_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        index=True,
    )
    operation: Mapped[str] = mapped_column(String(16))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    provider_key: Mapped[str] = mapped_column(String(64), unique=True)
    response: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    lease_expires_at: Mapped[Optional[datetime]] = mapped_column(nullable=True)
