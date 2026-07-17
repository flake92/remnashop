from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any, Optional, Protocol


class PaymentOperationStatus(StrEnum):
    CLAIMED = "CLAIMED"
    PROCESSING = "PROCESSING"
    SUCCEEDED = "SUCCEEDED"
    UNKNOWN = "UNKNOWN"


class PaymentOperationOwnerMergedError(Exception): ...


@dataclass(frozen=True)
class PaymentOperationRecord:
    id: int
    user_id: int
    operation: str
    idempotency_key: str
    request_hash: str
    status: PaymentOperationStatus
    provider_key: str
    response: Optional[dict[str, Any]]
    lease_expires_at: Optional[datetime]
    created_at: datetime
    updated_at: datetime


class PaymentOperationDao(Protocol):
    async def claim(
        self,
        *,
        user_id: int,
        operation: str,
        idempotency_key: str,
        request_hash: str,
        provider_key: str,
        lease_for: timedelta,
    ) -> tuple[PaymentOperationRecord, bool]: ...

    async def get_by_id(self, operation_id: int) -> Optional[PaymentOperationRecord]: ...

    async def reclaim_claimed(
        self,
        operation_id: int,
        *,
        lease_for: timedelta,
    ) -> bool: ...

    async def mark_processing(
        self,
        operation_id: int,
        *,
        lease_for: timedelta,
    ) -> bool: ...

    async def complete(self, operation_id: int, response: dict[str, Any]) -> bool: ...

    async def mark_unknown(self, operation_id: int) -> bool: ...

    async def expire_processing(self, operation_id: int) -> bool: ...

    async def delete_claimed(self, operation_id: int) -> bool: ...
