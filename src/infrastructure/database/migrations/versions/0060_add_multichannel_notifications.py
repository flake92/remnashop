from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0060"
down_revision: Union[str, None] = "0059"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """E-mail and Web Push copies of bot notifications.

    Service e-mail is on for every user, including existing ones: it carries
    account and subscription state, and every message has a one-click
    unsubscribe. News e-mail is advertising and stays off until the user gives
    explicit consent; the consent time is recorded with it.
    """

    # Bound DDL lock waits like 0057: a failed additive revision rolls back
    # cleanly and the previous runtime stays compatible, so a deploy can retry.
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '5min'"))

    # Constant server defaults: PostgreSQL adds these without a table rewrite.
    op.add_column(
        "users",
        sa.Column(
            "email_service_notifications_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "email_news_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "users",
        sa.Column("email_news_enabled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_check_constraint(
        "ck_users_email_news_consent_timestamp",
        "users",
        "(email_news_enabled AND email_news_enabled_at IS NOT NULL) OR "
        "(NOT email_news_enabled AND email_news_enabled_at IS NULL)",
    )

    op.create_table(
        "web_push_subscriptions",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("endpoint", sa.String(length=2048), nullable=False),
        sa.Column("p256dh", sa.String(length=128), nullable=False),
        sa.Column("auth", sa.String(length=64), nullable=False),
        sa.Column("user_agent", sa.String(length=256), nullable=True),
        sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("timezone('UTC', now())"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("timezone('UTC', now())"),
        ),
        sa.CheckConstraint("failure_count >= 0", name="ck_web_push_subscription_failure_count"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("endpoint", name="uq_web_push_subscription_endpoint"),
    )
    op.create_index(
        "ix_web_push_subscriptions_user_id",
        "web_push_subscriptions",
        ["user_id"],
    )

    op.create_table(
        "notification_deliveries",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("channel", sa.String(length=8), nullable=False),
        sa.Column("category", sa.String(length=8), nullable=False),
        sa.Column("push_subscription_id", sa.Integer(), nullable=True),
        sa.Column("dedupe_key", sa.String(length=200), nullable=False),
        # Groups rows of one broadcast for its report and for cancellation.
        sa.Column("source", sa.String(length=64), nullable=True),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("text_body", sa.Text(), nullable=False),
        sa.Column("html_body", sa.Text(), nullable=True),
        sa.Column("url", sa.String(length=1024), nullable=True),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="PENDING"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processing_token_hash", sa.String(length=64), nullable=True),
        sa.Column("processing_lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=64), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("timezone('UTC', now())"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("timezone('UTC', now())"),
        ),
        sa.CheckConstraint("channel IN ('EMAIL', 'PUSH')", name="ck_notification_delivery_channel"),
        sa.CheckConstraint(
            "category IN ('SERVICE', 'NEWS')",
            name="ck_notification_delivery_category",
        ),
        sa.CheckConstraint(
            "(channel = 'PUSH') = (push_subscription_id IS NOT NULL)",
            name="ck_notification_delivery_push_target",
        ),
        sa.CheckConstraint(
            "state IN ('PENDING', 'PROCESSING', 'RETRY_WAITING', 'SENT', 'CANCELED', 'FAILED')",
            name="ck_notification_delivery_state",
        ),
        sa.CheckConstraint("attempt_count >= 0", name="ck_notification_delivery_attempt_count"),
        sa.CheckConstraint(
            "((state = 'PROCESSING' AND processing_token_hash IS NOT NULL "
            "AND processing_lease_expires_at IS NOT NULL) OR "
            "(state <> 'PROCESSING' AND processing_token_hash IS NULL "
            "AND processing_lease_expires_at IS NULL))",
            name="ck_notification_delivery_processing_fence",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["push_subscription_id"],
            ["web_push_subscriptions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dedupe_key", name="uq_notification_delivery_dedupe_key"),
    )
    op.create_index(
        "ix_notification_deliveries_due",
        "notification_deliveries",
        ["channel", "next_attempt_at", "id"],
        postgresql_where=sa.text("state IN ('PENDING', 'RETRY_WAITING', 'PROCESSING')"),
    )
    op.create_index(
        "ix_notification_deliveries_terminal_retention",
        "notification_deliveries",
        ["updated_at", "id"],
        postgresql_where=sa.text("state IN ('SENT', 'CANCELED', 'FAILED')"),
    )
    op.create_index(
        "ix_notification_deliveries_source",
        "notification_deliveries",
        ["source", "channel", "state"],
        postgresql_where=sa.text("source IS NOT NULL"),
    )
    # The whole audience of a broadcast, web-only users included; total_count
    # stays the Telegram recipient count.
    op.add_column(
        "broadcasts",
        sa.Column("audience_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_notification_deliveries_user_id",
        "notification_deliveries",
        ["user_id"],
    )
    op.create_index(
        "ix_notification_deliveries_push_subscription_id",
        "notification_deliveries",
        ["push_subscription_id"],
    )


def downgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.drop_column("broadcasts", "audience_count")
    op.drop_table("notification_deliveries")
    op.drop_table("web_push_subscriptions")
    op.drop_constraint("ck_users_email_news_consent_timestamp", "users", type_="check")
    op.drop_column("users", "email_news_enabled_at")
    op.drop_column("users", "email_news_enabled")
    op.drop_column("users", "email_service_notifications_enabled")
