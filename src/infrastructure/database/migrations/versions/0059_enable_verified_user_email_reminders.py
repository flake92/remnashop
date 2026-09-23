from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0059"
down_revision: Union[str, None] = "0058"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Allow a newly verified identity to persist its default preference.

    Existing preferences are deliberately left untouched.  A false value may
    be an explicit opt-out, while every new verification is enabled by the
    application in the same update that records the verified identity.
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


def downgrade() -> None:
    """Restore the strict 0057 identity-change trigger."""

    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.execute(sa.text("SET LOCAL statement_timeout = '5min'"))
    # An earlier, unreleased draft of this revision created an audit table and
    # force-enabled existing rows.  If a disposable or pre-production database
    # used that draft, retain its exact timestamp fence during downgrade.  A
    # fresh trigger-only installation has no such table and this block is a
    # no-op.
    op.execute(
        sa.text(
            """
            DO $migration$
            BEGIN
                IF to_regclass(
                    'public.subscription_email_consent_backfill_0059'
                ) IS NOT NULL THEN
                    EXECUTE $restore$
                        UPDATE public.users AS users
                        SET subscription_expiration_email_enabled = false,
                            subscription_expiration_email_enabled_at = NULL
                        FROM public.subscription_email_consent_backfill_0059 AS backfill
                        WHERE users.id = backfill.user_id
                          AND users.subscription_expiration_email_enabled IS TRUE
                          AND users.subscription_expiration_email_enabled_at
                              = backfill.applied_enabled_at
                    $restore$;
                    EXECUTE
                        'DROP TABLE IF EXISTS public.subscription_email_consent_backfill_0059';
                END IF;
            END;
            $migration$;
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
