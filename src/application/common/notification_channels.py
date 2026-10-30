from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Optional, Protocol, runtime_checkable


class NotificationCategory(StrEnum):
    # Account and subscription state: on by default, one-click opt-out.
    SERVICE = "SERVICE"
    # Manual broadcasts (advertising): off until the user opts in.
    NEWS = "NEWS"


@dataclass(frozen=True, kw_only=True)
class ChannelCopy:
    """One user's copy of a bot message for e-mail and Web Push."""

    user_id: int
    # Telegram-flavoured HTML exactly as the bot would send it.
    text: str
    category: NotificationCategory
    # Unique per notification and user; channel suffixes are appended.
    dedupe_key: str
    link_url: Optional[str] = None
    # Groups the rows of one broadcast (report, cancellation).
    source: Optional[str] = None
    # The subscription-expiry e-mail has its own reminder pipeline.
    skip_email: bool = False


@runtime_checkable
class NotificationChannels(Protocol):
    @property
    def email_enabled(self) -> bool: ...

    @property
    def push_enabled(self) -> bool: ...

    async def enqueue(self, copies: Sequence[ChannelCopy]) -> int:
        """Queue e-mail and push deliveries; already queued keys are ignored."""
        ...

    async def cancel_pending(self, source: str) -> int:
        """Cancel the not yet sent rows of a source."""
        ...

    async def source_stats(self, source: str) -> "ChannelStats": ...

    async def deliver_due(self) -> int:
        """Deliver due e-mail and push rows; returns the number sent."""
        ...

    async def save_push_subscription(
        self,
        *,
        user_id: int,
        endpoint: str,
        p256dh: str,
        auth: str,
        user_agent: Optional[str],
        max_per_user: int = 10,
    ) -> None:
        """Raises ValueError for an endpoint outside the known push services."""
        ...

    async def delete_push_subscription(self, *, user_id: int, endpoint: str) -> bool: ...

    async def push_device_count(self, user_id: int) -> int: ...


@dataclass(frozen=True, kw_only=True)
class ChannelCounts:
    # Distinct users with a row on this channel (push: users, not devices).
    recipients: int = 0
    rows: int = 0
    sent: int = 0
    queued: int = 0
    failed: int = 0
    canceled: int = 0


@dataclass(frozen=True, kw_only=True)
class ChannelStats:
    email: ChannelCounts
    push: ChannelCounts
