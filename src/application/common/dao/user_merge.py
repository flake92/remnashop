from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from src.application.dto import UserDto


class UserMergeNotFoundError(Exception): ...


@dataclass(frozen=True)
class UserMergeTargetSnapshot:
    id: int
    email: str | None
    telegram_id: int | None
    is_email_verified: bool
    current_subscription_id: int | None


@dataclass(frozen=True)
class UserMergePlan:
    source_user_id: int
    target_user_id: int
    target: UserMergeTargetSnapshot
    moved: dict[str, int]
    conflicts: list[str] = field(default_factory=list)


@runtime_checkable
class UserMergeDao(Protocol):
    async def plan(self, source_user_id: int, target_user_id: int) -> UserMergePlan: ...

    async def merge(
        self,
        *,
        actor: UserDto,
        source_user_id: int,
        target_user_id: int,
        reason: str,
    ) -> UserMergePlan: ...
