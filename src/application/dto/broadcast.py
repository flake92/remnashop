from dataclasses import dataclass, field
from typing import Optional
from uuid import UUID

from src.core.enums import BroadcastAudience, BroadcastMessageStatus, BroadcastStatus

from .base import BaseDto, TimestampMixin, TrackableMixin
from .message_payload import MessagePayloadDto


@dataclass(kw_only=True)
class BroadcastDto(BaseDto, TrackableMixin, TimestampMixin):
    task_id: UUID

    status: BroadcastStatus
    audience: BroadcastAudience

    total_count: int = 0
    success_count: int = 0
    failed_count: int = 0
    # Whole audience (web-only users included); total_count is Telegram only.
    audience_count: int = 0

    payload: MessagePayloadDto

    messages: list["BroadcastMessageDto"] = field(default_factory=list)


@dataclass(kw_only=True)
class BroadcastMessageDto(BaseDto, TrackableMixin):
    user_id: int
    user_telegram_id: Optional[int] = None  # Telegram chat ID; None for web-only users
    message_id: Optional[int] = None

    status: BroadcastMessageStatus
