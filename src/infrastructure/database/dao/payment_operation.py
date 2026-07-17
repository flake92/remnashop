from datetime import timedelta
from typing import Any, Optional, cast

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.common.dao.payment_operation import (
    PaymentOperationDao,
    PaymentOperationRecord,
    PaymentOperationStatus,
)
from src.infrastructure.database.models import PaymentOperation


class PaymentOperationDaoImpl(PaymentOperationDao):
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    @staticmethod
    def _to_record(operation: PaymentOperation) -> PaymentOperationRecord:
        return PaymentOperationRecord(
            id=operation.id,
            user_id=operation.user_id,
            operation=operation.operation,
            idempotency_key=operation.idempotency_key,
            request_hash=operation.request_hash,
            status=PaymentOperationStatus(operation.status),
            provider_key=operation.provider_key,
            response=operation.response,
            lease_expires_at=operation.lease_expires_at,
            created_at=operation.created_at,
            updated_at=operation.updated_at,
        )

    async def claim(
        self,
        *,
        user_id: int,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        provider_key: str,
        lease_for: timedelta,
    ) -> tuple[PaymentOperationRecord, bool]:
        stmt = (
            insert(PaymentOperation)
            .values(
                user_id=user_id,
                operation=operation,
                idempotency_key=idempotency_key,
                request_hash=request_hash,
                status=PaymentOperationStatus.CLAIMED.value,
                provider_key=provider_key,
                lease_expires_at=func.clock_timestamp() + lease_for,
            )
            .on_conflict_do_nothing(constraint="uq_payment_operations_identity")
            .returning(PaymentOperation)
        )
        inserted = await self.session.scalar(stmt)
        if inserted is not None:
            return self._to_record(inserted), True

        existing = await self.session.scalar(
            select(PaymentOperation).where(
                PaymentOperation.user_id == user_id,
                PaymentOperation.operation == operation,
                PaymentOperation.idempotency_key == idempotency_key,
            )
        )
        if existing is None:
            raise RuntimeError("Payment operation conflict was not readable")
        return self._to_record(existing), False

    async def get_by_id(self, operation_id: int) -> Optional[PaymentOperationRecord]:
        operation = await self.session.scalar(
            select(PaymentOperation).where(PaymentOperation.id == operation_id)
        )
        return self._to_record(operation) if operation is not None else None

    async def reclaim_claimed(
        self,
        operation_id: int,
        *,
        lease_for: timedelta,
    ) -> bool:
        result = await self.session.execute(
            update(PaymentOperation)
            .where(
                PaymentOperation.id == operation_id,
                PaymentOperation.status == PaymentOperationStatus.CLAIMED.value,
                PaymentOperation.lease_expires_at <= func.clock_timestamp(),
            )
            .values(lease_expires_at=func.clock_timestamp() + lease_for)
        )
        return cast(int, result.rowcount) == 1  # type: ignore[attr-defined]

    async def mark_processing(
        self,
        operation_id: int,
        *,
        lease_for: timedelta,
    ) -> bool:
        result = await self.session.execute(
            update(PaymentOperation)
            .where(
                PaymentOperation.id == operation_id,
                PaymentOperation.status == PaymentOperationStatus.CLAIMED.value,
            )
            .values(
                status=PaymentOperationStatus.PROCESSING.value,
                lease_expires_at=func.clock_timestamp() + lease_for,
            )
        )
        return cast(int, result.rowcount) == 1  # type: ignore[attr-defined]

    async def complete(self, operation_id: int, response: dict[str, Any]) -> bool:
        result = await self.session.execute(
            update(PaymentOperation)
            .where(
                PaymentOperation.id == operation_id,
                PaymentOperation.status == PaymentOperationStatus.PROCESSING.value,
            )
            .values(
                status=PaymentOperationStatus.SUCCEEDED.value,
                response=response,
                lease_expires_at=None,
            )
        )
        return cast(int, result.rowcount) == 1  # type: ignore[attr-defined]

    async def mark_unknown(self, operation_id: int) -> bool:
        result = await self.session.execute(
            update(PaymentOperation)
            .where(
                PaymentOperation.id == operation_id,
                PaymentOperation.status == PaymentOperationStatus.PROCESSING.value,
            )
            .values(status=PaymentOperationStatus.UNKNOWN.value, lease_expires_at=None)
        )
        return cast(int, result.rowcount) == 1  # type: ignore[attr-defined]

    async def expire_processing(self, operation_id: int) -> bool:
        result = await self.session.execute(
            update(PaymentOperation)
            .where(
                PaymentOperation.id == operation_id,
                PaymentOperation.status == PaymentOperationStatus.PROCESSING.value,
                PaymentOperation.lease_expires_at <= func.clock_timestamp(),
            )
            .values(status=PaymentOperationStatus.UNKNOWN.value, lease_expires_at=None)
        )
        return cast(int, result.rowcount) == 1  # type: ignore[attr-defined]

    async def delete_claimed(self, operation_id: int) -> bool:
        result = await self.session.execute(
            delete(PaymentOperation).where(
                PaymentOperation.id == operation_id,
                PaymentOperation.status == PaymentOperationStatus.CLAIMED.value,
            )
        )
        return cast(int, result.rowcount) == 1  # type: ignore[attr-defined]
