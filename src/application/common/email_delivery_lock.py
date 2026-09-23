from contextlib import AbstractAsyncContextManager
from typing import Protocol


class EmailDeliveryRunBusyError(RuntimeError):
    """Another scheduler invocation currently owns reminder delivery."""


class EmailDeliveryRunLockLostError(RuntimeError):
    """The reminder delivery lease expired before clean release."""


class EmailDeliveryRunLock(Protocol):
    def hold(self) -> AbstractAsyncContextManager[None]: ...

    async def wait_for_send_slot(
        self,
        *,
        rate_per_minute: int,
        max_wait_seconds: float,
    ) -> bool: ...
