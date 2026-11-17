from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0064"
down_revision: Union[str, None] = "0063"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Hide non-Stars payment gateways from the Telegram bot.

    Telegram requires digital goods inside bots to be sold for Telegram
    Stars only. A web_only gateway stays available to the web cabinet
    (Clean Pay) but the bot no longer lists it as a payment method.
    Additive only: every existing gateway keeps appearing in the bot.
    """
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '5min'"))

    op.add_column(
        "payment_gateways",
        sa.Column("web_only", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.drop_column("payment_gateways", "web_only")
