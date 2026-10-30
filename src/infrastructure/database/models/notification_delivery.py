from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from .base import BaseSql
from .timestamp import TimestampMixin


class WebPushSubscription(BaseSql, TimestampMixin):
    __tablename__ = "web_push_subscriptions"
    __table_args__ = (
        UniqueConstraint("endpoint", name="uq_web_push_subscription_endpoint"),
        CheckConstraint("failure_count >= 0", name="ck_web_push_subscription_failure_count"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    endpoint: Mapped[str] = mapped_column(String(2048), nullable=False)
    p256dh: Mapped[str] = mapped_column(String(128), nullable=False)
    auth: Mapped[str] = mapped_column(String(64), nullable=False)
    user_agent: Mapped[Optional[str]] = mapped_column(String(256), nullable=True)
    failure_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    last_success_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class NotificationDelivery(BaseSql, TimestampMixin):
    """Outbox row: one rendered notification for one user on one channel."""

    __tablename__ = "notification_deliveries"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_notification_delivery_dedupe_key"),
        CheckConstraint("channel IN ('EMAIL', 'PUSH')", name="ck_notification_delivery_channel"),
        CheckConstraint(
            "category IN ('SERVICE', 'NEWS')",
            name="ck_notification_delivery_category",
        ),
        CheckConstraint(
            "(channel = 'PUSH') = (push_subscription_id IS NOT NULL)",
            name="ck_notification_delivery_push_target",
        ),
        CheckConstraint(
            "state IN ('PENDING', 'PROCESSING', 'RETRY_WAITING', 'SENT', 'CANCELED', 'FAILED')",
            name="ck_notification_delivery_state",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_notification_delivery_attempt_count"),
        CheckConstraint(
            "((state = 'PROCESSING' AND processing_token_hash IS NOT NULL "
            "AND processing_lease_expires_at IS NOT NULL) OR "
            "(state <> 'PROCESSING' AND processing_token_hash IS NULL "
            "AND processing_lease_expires_at IS NULL))",
            name="ck_notification_delivery_processing_fence",
        ),
        Index(
            "ix_notification_deliveries_due",
            "channel",
            "next_attempt_at",
            "id",
            postgresql_where=text("state IN ('PENDING', 'RETRY_WAITING', 'PROCESSING')"),
        ),
        Index(
            "ix_notification_deliveries_source",
            "source",
            "channel",
            "state",
            postgresql_where=text("source IS NOT NULL"),
        ),
        Index(
            "ix_notification_deliveries_terminal_retention",
            "updated_at",
            "id",
            postgresql_where=text("state IN ('SENT', 'CANCELED', 'FAILED')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(8), nullable=False)
    category: Mapped[str] = mapped_column(String(8), nullable=False)
    push_subscription_id: Mapped[Optional[int]] = mapped_column(
        Integer,
        ForeignKey("web_push_subscriptions.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    dedupe_key: Mapped[str] = mapped_column(String(200), nullable=False)
    source: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    text_body: Mapped[str] = mapped_column(Text, nullable=False)
    html_body: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    url: Mapped[Optional[str]] = mapped_column(String(1024), nullable=True)
    state: Mapped[str] = mapped_column(
        String(24), default="PENDING", server_default="PENDING", nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0", nullable=False
    )
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processing_token_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    processing_lease_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_error_code: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
