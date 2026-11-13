from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, Integer, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.core.enums import PromocodeAvailability, PromocodeRewardType

from .base import BaseSql
from .timestamp import TimestampMixin


class Promocode(BaseSql, TimestampMixin):
    __tablename__ = "promocodes"
    __table_args__ = (UniqueConstraint("gift_payment_id", name="uq_promocodes_gift_payment_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(unique=True, index=True, nullable=False)
    is_active: Mapped[bool] = mapped_column(nullable=False)

    reward_type: Mapped[PromocodeRewardType]
    reward: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    plan_snapshot: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    availability: Mapped[PromocodeAvailability]

    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    max_activations: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_reusable: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    # Set only on gift codes: the paid transaction that issued the code (one code
    # per payment) and the buyer who may list it in the cabinet.
    gift_payment_id: Mapped[Optional[UUID]] = mapped_column(nullable=True)
    gift_buyer_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
            name="fk_promocodes_gift_buyer_id_users",
        ),
        nullable=True,
        index=True,
    )
    # The buyer asked not to be named to the recipient.
    gift_anonymous: Mapped[bool] = mapped_column(nullable=False, server_default=text("false"))

    activations: Mapped[list["PromocodeActivation"]] = relationship(
        back_populates="promocode",
        cascade="all, delete-orphan",
        lazy="noload",
    )


class PromocodeActivation(BaseSql):
    __tablename__ = "promocode_activations"
    __table_args__ = (UniqueConstraint("promocode_id", "user_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    promocode_id: Mapped[int] = mapped_column(
        ForeignKey("promocodes.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    activated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("timezone('UTC', now())"),
    )

    promocode: Mapped["Promocode"] = relationship(back_populates="activations")
