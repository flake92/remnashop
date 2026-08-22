import asyncio
import os
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.application.dto import ReferralRewardDto
from src.core.enums import (
    ReferralAccrualStrategy,
    ReferralLevel,
    ReferralRewardState,
    ReferralRewardStrategy,
    ReferralRewardType,
)
from src.infrastructure.database.dao.referral import ReferralDaoImpl

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")

pytestmark = pytest.mark.skipif(
    TEST_DATABASE_URL is None,
    reason="TEST_DATABASE_URL is required for PostgreSQL concurrency tests",
)

RECIPIENT_ID = 91_001
PAYER_IDS = (91_002, 91_003)
REFERRAL_IDS = (92_001, 92_002)
TRANSACTION_IDS = (93_001, 93_002)
REWARD_IDS = (94_001, 94_002)
CLAIM_B_ROLE = "referral_concurrency_claimant"
ADVISORY_LOCK_KEY = 91_001_94_001


def _dao(session: AsyncSession) -> ReferralDaoImpl:
    dao = ReferralDaoImpl.__new__(ReferralDaoImpl)
    dao.session = session
    dao._convert_to_reward_list = lambda rows: rows  # type: ignore[method-assign]
    return dao


class _DtoDumpRetort:
    def dump(self, reward: ReferralRewardDto) -> dict[str, object]:
        return {
            key: value
            for key, value in vars(reward).items()
            if not key.startswith("_")
        }


def _creation_dao(session: AsyncSession) -> ReferralDaoImpl:
    dao = _dao(session)
    dao.retort = _DtoDumpRetort()  # type: ignore[assignment]
    dao._convert_to_reward_dto = lambda reward: reward  # type: ignore[method-assign]
    return dao


async def _delete_fixture_rows(session: AsyncSession) -> None:
    await session.execute(
        text(
            "DELETE FROM referral_rewards "
            "WHERE id = ANY(:ids) OR source_transaction_id = ANY(:transaction_ids)"
        ),
        {"ids": list(REWARD_IDS), "transaction_ids": list(TRANSACTION_IDS)},
    )
    await session.execute(
        text("DELETE FROM transactions WHERE id = ANY(:ids)"),
        {"ids": list(TRANSACTION_IDS)},
    )
    await session.execute(
        text("DELETE FROM referrals WHERE id = ANY(:ids)"),
        {"ids": list(REFERRAL_IDS)},
    )
    await session.execute(
        text("DELETE FROM users WHERE id = ANY(:ids)"),
        {"ids": [RECIPIENT_ID, *PAYER_IDS]},
    )


async def _seed_two_rewards_for_one_recipient(session: AsyncSession) -> None:
    await _delete_fixture_rows(session)
    for user_id in (RECIPIENT_ID, *PAYER_IDS):
        await session.execute(
            text(
                """
                INSERT INTO users (
                    id, telegram_id, name, role, language, referral_code,
                    personal_discount, purchase_discount, points, is_blocked,
                    is_bot_blocked, is_rules_accepted, is_trial_available,
                    is_email_verified, auth_type, token_version
                ) VALUES (
                    :id, :telegram_id, :name, 'USER', 'EN', :referral_code,
                    0, 0, 0, false, false, true, false, false, 'telegram', 0
                )
                """
            ),
            {
                "id": user_id,
                "telegram_id": user_id,
                "name": f"concurrency-user-{user_id}",
                "referral_code": f"concurrency-{user_id}",
            },
        )

    for referral_id, payer_id in zip(REFERRAL_IDS, PAYER_IDS, strict=True):
        await session.execute(
            text(
                """
                INSERT INTO referrals (id, referrer_id, referred_id, level)
                VALUES (:id, :recipient_id, :payer_id, 'FIRST')
                """
            ),
            {
                "id": referral_id,
                "recipient_id": RECIPIENT_ID,
                "payer_id": payer_id,
            },
        )

    for offset, (transaction_id, payer_id) in enumerate(
        zip(TRANSACTION_IDS, PAYER_IDS, strict=True),
        start=1,
    ):
        await session.execute(
            text(
                """
                INSERT INTO transactions (
                    id, payment_id, user_id, status, is_test, purchase_type,
                    gateway_type, pricing, currency, plan_snapshot,
                    fulfillment_status, fulfillment_started_at,
                    fulfillment_completed_at
                ) VALUES (
                    :id, CAST(:payment_id AS uuid), :payer_id, 'COMPLETED', false,
                    'NEW', 'YOOKASSA', CAST(:pricing AS jsonb), 'RUB',
                    CAST(:plan_snapshot AS jsonb), 'SUCCEEDED',
                    clock_timestamp() - (:offset * interval '2 minutes'),
                    clock_timestamp() - (:offset * interval '1 minute')
                )
                """
            ),
            {
                "id": transaction_id,
                "payment_id": f"00000000-0000-0000-0000-{transaction_id:012d}",
                "payer_id": payer_id,
                "pricing": '{"final_amount": "100"}',
                "plan_snapshot": '{"is_trial": false}',
                "offset": offset,
            },
        )

    for reward_id, referral_id, transaction_id in zip(
        REWARD_IDS,
        REFERRAL_IDS,
        TRANSACTION_IDS,
        strict=True,
    ):
        await session.execute(
            text(
                """
                INSERT INTO referral_rewards (
                    id, referral_id, user_id, source_transaction_id,
                    origin_referral_id, type, amount, is_issued, state, level,
                    accrual_strategy_snapshot, accrual_strategy,
                    reward_strategy, config_value
                ) VALUES (
                    :id, :referral_id, :recipient_id, :transaction_id,
                    :referral_id, 'POINTS', 10, false, 'PENDING', 'FIRST',
                    'ON_EACH_PAYMENT', 'ON_EACH_PAYMENT', 'AMOUNT', 10
                )
                """
            ),
            {
                "id": reward_id,
                "referral_id": referral_id,
                "recipient_id": RECIPIENT_ID,
                "transaction_id": transaction_id,
            },
        )
    await session.commit()


async def _install_recipient_scan_barrier(session: AsyncSession) -> None:
    # The policy is test-only instrumentation. It pauses claimant B inside the
    # candidate SELECT after PostgreSQL has fixed that statement's snapshot but
    # before the recipient row is locked. This makes the historical stale-snapshot
    # interleaving deterministic instead of relying on scheduler timing.
    await session.execute(text(f"DROP ROLE IF EXISTS {CLAIM_B_ROLE}"))
    await session.execute(text(f"CREATE ROLE {CLAIM_B_ROLE} NOLOGIN"))
    await session.execute(text(f"GRANT USAGE ON SCHEMA public TO {CLAIM_B_ROLE}"))
    await session.execute(
        text(
            f"GRANT SELECT, UPDATE ON users, referral_rewards TO {CLAIM_B_ROLE}"
        )
    )
    await session.execute(
        text(
            f"GRANT SELECT ON transactions, referral_reward_resolutions TO {CLAIM_B_ROLE}"
        )
    )
    await session.execute(
        text(
            f"""
            CREATE OR REPLACE FUNCTION referral_concurrency_candidate_barrier(user_id integer)
            RETURNS boolean
            LANGUAGE plpgsql
            VOLATILE
            AS $$
            BEGIN
                IF current_user = '{CLAIM_B_ROLE}' AND user_id = {RECIPIENT_ID} THEN
                    PERFORM pg_advisory_xact_lock({ADVISORY_LOCK_KEY});
                END IF;
                RETURN true;
            END;
            $$
            """
        )
    )
    await session.execute(text("ALTER TABLE users ENABLE ROW LEVEL SECURITY"))
    await session.execute(
        text("DROP POLICY IF EXISTS referral_concurrency_barrier ON users")
    )
    await session.execute(
        text(
            f"""
            CREATE POLICY referral_concurrency_barrier ON users
            FOR ALL TO {CLAIM_B_ROLE}
            USING (referral_concurrency_candidate_barrier(id))
            WITH CHECK (true)
            """
        )
    )
    await session.commit()


async def _remove_recipient_scan_barrier(session: AsyncSession) -> None:
    await session.execute(
        text("DROP POLICY IF EXISTS referral_concurrency_barrier ON users")
    )
    await session.execute(text("ALTER TABLE users DISABLE ROW LEVEL SECURITY"))
    await session.execute(
        text("DROP FUNCTION IF EXISTS referral_concurrency_candidate_barrier(integer)")
    )
    await session.execute(text(f"DROP OWNED BY {CLAIM_B_ROLE}"))
    await session.execute(text(f"DROP ROLE IF EXISTS {CLAIM_B_ROLE}"))


async def _wait_until_backend_blocks(
    monitor: AsyncSession,
    backend_pid: int,
    claimant: asyncio.Task[list[Any]],
) -> None:
    for _ in range(100):
        if claimant.done():
            await claimant
            raise AssertionError("second referral claimant finished before the barrier")
        wait_event_type = await monitor.scalar(
            text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"),
            {"pid": backend_pid},
        )
        if wait_event_type == "Lock":
            return
        await asyncio.sleep(0.05)
    raise AssertionError("second referral claimant did not wait on the recipient lock")


@pytest.mark.asyncio
async def test_two_concurrent_claimants_skip_stale_recipient_candidate() -> None:
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL, pool_size=4, max_overflow=0)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    claimant_b_task: asyncio.Task[list[Any]] | None = None

    try:
        async with sessions() as setup:
            await _seed_two_rewards_for_one_recipient(setup)
            await _install_recipient_scan_barrier(setup)

        async with sessions() as claimant_a, sessions() as claimant_b, sessions() as monitor:
            assert await claimant_a.scalar(text("SHOW transaction_isolation")) == "read committed"
            backend_b = await claimant_b.scalar(text("SELECT pg_backend_pid()"))
            assert isinstance(backend_b, int)

            await claimant_a.scalar(
                text("SELECT pg_advisory_lock(:key)"),
                {"key": ADVISORY_LOCK_KEY},
            )

            claimant_b_task = asyncio.create_task(
                _claim_as_barrier_role(claimant_b)
            )
            await _wait_until_backend_blocks(monitor, backend_b, claimant_b_task)

            rewards_a = await _dao(claimant_a).claim_pending_rewards(
                token_hash="a" * 64,
                lease_for=timedelta(minutes=5),
                limit=1,
            )
            assert [reward.id for reward in rewards_a] == [REWARD_IDS[0]]
            await claimant_a.commit()
            assert await claimant_a.scalar(
                text("SELECT pg_advisory_unlock(:key)"),
                {"key": ADVISORY_LOCK_KEY},
            )
            rewards_b = await asyncio.wait_for(claimant_b_task, timeout=5)
            await claimant_b.commit()

            # Claimant B evaluated the recipient while A's PROCESSING row was
            # uncommitted. After acquiring the User lock it must use a new
            # READ COMMITTED statement snapshot and leave the second reward pending.
            assert rewards_b == []

        async with sessions() as verify:
            rows = (
                await verify.execute(
                    text(
                        """
                        SELECT id, state::text, processing_token_hash
                        FROM referral_rewards
                        WHERE id = ANY(:ids)
                        ORDER BY id
                        """
                    ),
                    {"ids": list(REWARD_IDS)},
                )
            ).all()
            assert rows == [
                (REWARD_IDS[0], "PROCESSING", "a" * 64),
                (REWARD_IDS[1], "PENDING", None),
            ]
    finally:
        if claimant_b_task is not None and not claimant_b_task.done():
            claimant_b_task.cancel()
        async with sessions() as cleanup:
            await _delete_fixture_rows(cleanup)
            await _remove_recipient_scan_barrier(cleanup)
            await cleanup.commit()
        await engine.dispose()


@pytest.mark.asyncio
async def test_create_reward_uses_database_owned_timestamps() -> None:
    assert TEST_DATABASE_URL is not None
    engine = create_async_engine(TEST_DATABASE_URL)
    sessions = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    try:
        async with sessions() as setup:
            await _seed_two_rewards_for_one_recipient(setup)
            await setup.execute(
                text("DELETE FROM referral_rewards WHERE source_transaction_id = ANY(:ids)"),
                {"ids": list(TRANSACTION_IDS)},
            )
            await setup.commit()

        async with sessions() as session:
            created = await _creation_dao(session).create_reward(
                ReferralRewardDto(
                    user_id=RECIPIENT_ID,
                    type=ReferralRewardType.EXTRA_DAYS,
                    amount=14,
                    source_transaction_id=TRANSACTION_IDS[0],
                    origin_referral_id=REFERRAL_IDS[0],
                    level=ReferralLevel.FIRST,
                    accrual_strategy_snapshot=ReferralAccrualStrategy.ON_FIRST_PAYMENT,
                    accrual_strategy=None,
                    reward_strategy=ReferralRewardStrategy.AMOUNT,
                    config_value=14,
                    state=ReferralRewardState.PENDING,
                ),
                referral_id=REFERRAL_IDS[0],
            )
            await session.commit()

            assert created is not None
            assert created.created_at is not None
            assert created.updated_at is not None
            assert created.updated_at >= created.created_at
    finally:
        async with sessions() as cleanup:
            await _delete_fixture_rows(cleanup)
            await cleanup.commit()
        await engine.dispose()


async def _claim_as_barrier_role(session: AsyncSession) -> list[Any]:
    await session.execute(text(f"SET LOCAL ROLE {CLAIM_B_ROLE}"))
    return await _dao(session).claim_pending_rewards(
        token_hash="b" * 64,
        lease_for=timedelta(minutes=5),
        limit=1,
    )
