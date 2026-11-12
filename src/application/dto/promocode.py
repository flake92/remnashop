from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from src.core.enums import PromocodeAvailability, PromocodeRewardType

from .base import BaseDto, TimestampMixin, TrackableMixin


@dataclass(kw_only=True)
class PromocodeDto(BaseDto, TrackableMixin, TimestampMixin):
    code: str
    is_active: bool
    reward_type: PromocodeRewardType
    reward: Optional[int] = None
    plan_snapshot: Optional[dict[str, Any]] = None
    availability: PromocodeAvailability = PromocodeAvailability.ALL
    expires_at: Optional[datetime] = None
    max_activations: Optional[int] = None
    is_reusable: bool = False
    gift_payment_id: Optional[UUID] = None
    gift_buyer_id: Optional[int] = None

    @property
    def is_gift(self) -> bool:
        return self.gift_payment_id is not None


@dataclass(kw_only=True)
class GiftDto:
    """A gift code as its buyer sees it in the cabinet."""

    code: str
    payment_id: UUID
    plan_snapshot: dict[str, Any]
    created_at: Optional[datetime]
    expires_at: Optional[datetime]
    activated_at: Optional[datetime] = None


@dataclass(kw_only=True)
class PromocodeActivationDto(BaseDto):
    promocode_id: int
    user_id: int
    activated_at: datetime
