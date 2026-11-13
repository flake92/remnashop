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
    gift_anonymous: bool = False

    @property
    def is_gift(self) -> bool:
        return self.gift_payment_id is not None


@dataclass(kw_only=True)
class GiftDto:
    """A gift code as its buyer or its recipient sees it in the cabinet.

    The buyer sees only THAT the code was activated. ``gifted_by`` names the
    buyer for the recipient (a display name, never an id or an e-mail) and is
    None when the buyer chose to stay anonymous.
    """

    code: str
    payment_id: UUID
    plan_snapshot: dict[str, Any]
    created_at: Optional[datetime]
    expires_at: Optional[datetime]
    activated_at: Optional[datetime] = None
    gifted_by: Optional[str] = None
    gift_anonymous: bool = False


def gift_party_name(name: Optional[str], username: Optional[str]) -> Optional[str]:
    """How a gift's buyer or recipient is shown to the other side."""
    label = (name or "").strip()
    if username:
        return f"{label} (@{username})" if label else f"@{username}"
    return label or None


@dataclass(kw_only=True)
class PromocodeActivationDto(BaseDto):
    promocode_id: int
    user_id: int
    activated_at: datetime
