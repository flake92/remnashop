from contextlib import AbstractAsyncContextManager
from typing import Protocol
from uuid import UUID


class BroadcastExecutionLock(Protocol):
    def hold(self, task_id: UUID) -> AbstractAsyncContextManager[None]: ...
