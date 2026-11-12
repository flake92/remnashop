from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0062"
down_revision: Union[str, None] = "0061"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Gift subscriptions bought in the web cabinet.

    A GIFT payment issues a single-use SUBSCRIPTION promocode instead of changing
    the buyer's subscription. The code row remembers the paying transaction (one
    code per payment, which makes fulfillment replays idempotent) and the buyer.
    Additive only: the previous runtime keeps working, so a deploy can retry.
    """

    # ALTER TYPE ... ADD VALUE cannot run inside a transaction block.
    with op.get_context().autocommit_block():
        op.execute(sa.text("ALTER TYPE purchase_type ADD VALUE IF NOT EXISTS 'GIFT'"))

    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '5min'"))

    op.add_column(
        "promocodes",
        sa.Column("gift_payment_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "promocodes",
        sa.Column("gift_buyer_id", sa.Integer(), nullable=True),
    )
    op.create_unique_constraint("uq_promocodes_gift_payment_id", "promocodes", ["gift_payment_id"])
    op.create_foreign_key(
        "fk_promocodes_gift_buyer_id_users",
        "promocodes",
        "users",
        ["gift_buyer_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_promocodes_gift_buyer_id", "promocodes", ["gift_buyer_id"])


def downgrade() -> None:
    # The 'GIFT' purchase_type label stays: PostgreSQL cannot drop an enum value.
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.drop_index("ix_promocodes_gift_buyer_id", table_name="promocodes")
    op.drop_constraint("fk_promocodes_gift_buyer_id_users", "promocodes", type_="foreignkey")
    op.drop_constraint("uq_promocodes_gift_payment_id", "promocodes", type_="unique")
    op.drop_column("promocodes", "gift_buyer_id")
    op.drop_column("promocodes", "gift_payment_id")
