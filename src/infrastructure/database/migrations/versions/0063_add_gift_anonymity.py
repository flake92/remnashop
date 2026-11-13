from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0063"
down_revision: Union[str, None] = "0062"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Let a gift buyer stay anonymous.

    The buyer chooses before paying, so the choice is stored on the GIFT
    transaction and copied to the issued code. Existing rows default to "show the
    buyer", which is what 0062 displayed. Additive only: the previous runtime
    keeps working, so a deploy can retry.
    """
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '5min'"))

    op.add_column(
        "transactions",
        sa.Column("gift_anonymous", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "promocodes",
        sa.Column("gift_anonymous", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.drop_column("promocodes", "gift_anonymous")
    op.drop_column("transactions", "gift_anonymous")
