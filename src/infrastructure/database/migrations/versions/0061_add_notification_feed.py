from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0061"
down_revision: Union[str, None] = "0060"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Notification feed of the web cabinet and recall of delivered Web Push.

    The feed is the permanent copy of what the bot told a user. Push and e-mail
    links lead to it, and deleting a broadcast removes its items. Additive only:
    the previous runtime keeps working, so a deploy can retry.
    """

    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '5min'"))

    op.create_table(
        "notification_feed_items",
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column(
            "user_id",
            sa.Integer(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.String(8), nullable=False),
        sa.Column("source", sa.String(64), nullable=True),
        sa.Column("dedupe_key", sa.String(200), nullable=False),
        sa.Column("text_html", sa.Text(), nullable=False),
        sa.Column(
            "buttons",
            postgresql.JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.Column("media_type", sa.String(16), nullable=True),
        sa.Column("media_file_id", sa.String(512), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('UTC', now())"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("timezone('UTC', now())"),
            nullable=False,
        ),
        sa.UniqueConstraint("dedupe_key", name="uq_notification_feed_item_dedupe_key"),
        sa.CheckConstraint(
            "category IN ('SERVICE', 'NEWS')",
            name="ck_notification_feed_item_category",
        ),
    )
    op.create_index(
        "ix_notification_feed_items_user",
        "notification_feed_items",
        ["user_id", "id"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_notification_feed_items_source",
        "notification_feed_items",
        ["source"],
        postgresql_where=sa.text("source IS NOT NULL"),
    )
    op.add_column(
        "notification_deliveries",
        sa.Column("revoke_tag", sa.String(64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("notification_deliveries", "revoke_tag")
    op.drop_index("ix_notification_feed_items_source", table_name="notification_feed_items")
    op.drop_index("ix_notification_feed_items_user", table_name="notification_feed_items")
    op.drop_table("notification_feed_items")
