from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0059"
down_revision: Union[str, None] = "0058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Enable reminders for every existing verified e-mail account.

    The companion audit table is intentionally retained until this revision is
    downgraded.  It lets rollback restore only rows that the migration changed
    and that the user has not changed again after rollout.
    """

    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '5min'"))
    # 0057 cleared consent on every e-mail change. That also erased a fresh
    # opt-in written in the same confirmed-identity UPDATE. Permit that one
    # atomic transition only with a newly recorded consent timestamp;
    # rolling/legacy writers that merely carry old consent still fail closed.
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION
                public.clear_subscription_email_consent_on_identity_change()
            RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog
            AS $function$
            BEGIN
                IF NEW.email IS NULL
                   OR NEW.is_email_verified IS NOT TRUE
                   OR (
                       NEW.email IS DISTINCT FROM OLD.email
                       AND NOT (
                           NEW.subscription_expiration_email_enabled IS TRUE
                           AND NEW.subscription_expiration_email_enabled_at IS NOT NULL
                           AND NEW.subscription_expiration_email_enabled_at
                               IS DISTINCT FROM
                               OLD.subscription_expiration_email_enabled_at
                       )
                   ) THEN
                    NEW.subscription_expiration_email_enabled := false;
                    NEW.subscription_expiration_email_enabled_at := NULL;
                END IF;
                RETURN NEW;
            END;
            $function$
            """
        )
    )
    op.create_table(
        "subscription_email_consent_backfill_0059",
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column(
            "applied_enabled_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("user_id"),
        schema="public",
    )
    op.execute(
        sa.text(
            """
            WITH migration_clock AS (
                SELECT clock_timestamp() AS applied_enabled_at
            ), changed AS (
                INSERT INTO public.subscription_email_consent_backfill_0059 (
                    user_id,
                    applied_enabled_at
                )
                SELECT users.id, migration_clock.applied_enabled_at
                FROM public.users
                CROSS JOIN migration_clock
                WHERE users.email IS NOT NULL
                  AND users.is_email_verified IS TRUE
                  AND users.subscription_expiration_email_enabled IS NOT TRUE
                RETURNING user_id, applied_enabled_at
            )
            UPDATE public.users AS users
            SET subscription_expiration_email_enabled = true,
                subscription_expiration_email_enabled_at = changed.applied_enabled_at
            FROM changed
            WHERE users.id = changed.user_id
            """
        )
    )


def downgrade() -> None:
    """Restore only untouched preferences changed by ``upgrade``.

    A user who disabled or re-enabled reminders after deployment must keep that
    newer choice; matching both the enabled flag and exact migration timestamp
    distinguishes untouched backfilled rows from later writes.
    """

    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '5min'"))
    op.execute(
        sa.text(
            """
            UPDATE public.users AS users
            SET subscription_expiration_email_enabled = false,
                subscription_expiration_email_enabled_at = NULL
            FROM public.subscription_email_consent_backfill_0059 AS backfill
            WHERE users.id = backfill.user_id
              AND users.subscription_expiration_email_enabled IS TRUE
              AND users.subscription_expiration_email_enabled_at
                  = backfill.applied_enabled_at
            """
        )
    )
    # Restore the strict 0057 trigger before this revision is considered
    # downgraded, so an older runtime cannot carry consent to another address.
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION
                public.clear_subscription_email_consent_on_identity_change()
            RETURNS trigger
            LANGUAGE plpgsql
            SET search_path = pg_catalog
            AS $function$
            BEGIN
                IF NEW.email IS DISTINCT FROM OLD.email
                   OR NEW.is_email_verified IS NOT TRUE THEN
                    NEW.subscription_expiration_email_enabled := false;
                    NEW.subscription_expiration_email_enabled_at := NULL;
                END IF;
                RETURN NEW;
            END;
            $function$
            """
        )
    )
    op.drop_table(
        "subscription_email_consent_backfill_0059",
        schema="public",
    )
