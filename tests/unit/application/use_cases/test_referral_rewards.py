import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from src.application.dto import ReferralRewardDto
from src.application.use_cases.referral.commands.attachment import (
    AttachReferral,
    AttachReferralDto,
)
from src.application.use_cases.referral.commands.rewards import (
    AssignReferralRewards,
    AssignReferralRewardsDto,
    GiveReferrerReward,
    GiveReferrerRewardDto,
    ResolveManualReferralReward,
    ResolveManualReferralRewardDto,
    RetryPendingReferralRewards,
)
from src.core.enums import (
    PurchaseType,
    ReferralAccrualStrategy,
    ReferralLevel,
    ReferralRewardState,
    ReferralRewardStrategy,
    ReferralRewardType,
    SubscriptionStatus,
    TransactionFulfillmentStatus,
    TransactionStatus,
)
from src.core.utils.time import datetime_now


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.commit = AsyncMock()
        self.rollback = AsyncMock()

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is not None:
            await self.rollback()


class FakeMutationLock:
    def __init__(self) -> None:
        self.user_ids: list[int] = []

    def hold(self, user_id: int) -> "FakeMutationLock":
        self.user_ids.append(user_id)
        return self

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


class SerialMutationLock:
    def __init__(self) -> None:
        self.locks: dict[int, asyncio.Lock] = {}

    def hold(self, user_id: int) -> asyncio.Lock:
        return self.locks.setdefault(user_id, asyncio.Lock())


def _settings(*, level: ReferralLevel = ReferralLevel.SECOND) -> SimpleNamespace:
    reward = SimpleNamespace(
        type=ReferralRewardType.POINTS,
        strategy=ReferralRewardStrategy.AMOUNT,
        config={ReferralLevel.FIRST: 10, ReferralLevel.SECOND: 5},
    )
    return SimpleNamespace(
        referral=SimpleNamespace(
            enable=True,
            level=level,
            accrual_strategy=ReferralAccrualStrategy.ON_FIRST_PAYMENT,
            reward=reward,
        )
    )


def _transaction(purchase_type: PurchaseType) -> SimpleNamespace:
    return SimpleNamespace(
        id=77,
        purchase_type=purchase_type,
        is_test=False,
        status=TransactionStatus.COMPLETED,
        fulfillment_status=TransactionFulfillmentStatus.PROCESSING,
        pricing=SimpleNamespace(is_free=False),
        plan_snapshot=SimpleNamespace(is_trial=False),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("purchase_type", [PurchaseType.CHANGE, PurchaseType.RENEW])
async def test_first_payment_intents_ignore_purchase_type_and_keep_l1_l2_origin(
    purchase_type: PurchaseType,
) -> None:
    uow = FakeUnitOfWork()
    direct_referrer = SimpleNamespace(id=2, remna_name="direct")
    grandparent = SimpleNamespace(id=3, remna_name="grandparent")
    direct_attribution = SimpleNamespace(id=101, referrer=direct_referrer)
    parent_attribution = SimpleNamespace(id=202, referrer=grandparent)
    referral_dao = SimpleNamespace(
        lock_referral_attribution=AsyncMock(),
        get_referral_chain=AsyncMock(
            side_effect=[
                (direct_attribution, parent_attribution),
                (direct_attribution, parent_attribution),
            ]
        ),
        create_reward=AsyncMock(),
    )
    calculate = SimpleNamespace(system=AsyncMock(side_effect=[10, 5]))
    use_case = AssignReferralRewards(
        uow,
        SimpleNamespace(get=AsyncMock(return_value=_settings())),
        referral_dao,
        calculate,
    )
    user = SimpleNamespace(id=9, name="payer", log="payer")

    await use_case._execute(  # type: ignore[arg-type]
        SimpleNamespace(),
        AssignReferralRewardsDto(user=user, transaction=_transaction(purchase_type)),
    )

    assert referral_dao.create_reward.await_count == 2
    referral_dao.lock_referral_attribution.assert_awaited_once_with(9, (2, 3))
    assert referral_dao.get_referral_chain.await_count == 2
    first_call, second_call = referral_dao.create_reward.await_args_list
    first_reward = first_call.kwargs["reward"]
    second_reward = second_call.kwargs["reward"]
    assert first_call.kwargs["referral_id"] == 101
    assert second_call.kwargs["referral_id"] == 202
    assert first_reward.origin_referral_id == 101
    assert second_reward.origin_referral_id == 101
    assert first_reward.level == ReferralLevel.FIRST
    assert second_reward.level == ReferralLevel.SECOND
    assert first_reward.source_transaction_id == 77
    assert second_reward.source_transaction_id == 77
    assert first_reward.state == ReferralRewardState.PENDING
    assert first_reward.accrual_strategy is None
    assert first_reward.accrual_strategy_snapshot == ReferralAccrualStrategy.ON_FIRST_PAYMENT
    assert second_reward.accrual_strategy_snapshot == ReferralAccrualStrategy.ON_FIRST_PAYMENT
    uow.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_reward_assignment_aborts_if_chain_changes_while_waiting_for_fence() -> None:
    direct_referrer = SimpleNamespace(id=2, remna_name="direct")
    moved_referrer = SimpleNamespace(id=3, remna_name="moved")
    before = SimpleNamespace(id=101, referrer=direct_referrer)
    after = SimpleNamespace(id=101, referrer=moved_referrer)
    referral_dao = SimpleNamespace(
        lock_referral_attribution=AsyncMock(),
        get_referral_chain=AsyncMock(side_effect=[(before, None), (after, None)]),
        create_reward=AsyncMock(),
    )
    use_case = AssignReferralRewards(
        FakeUnitOfWork(),  # type: ignore[arg-type]
        SimpleNamespace(get=AsyncMock(return_value=_settings(level=ReferralLevel.FIRST))),
        referral_dao,
        SimpleNamespace(system=AsyncMock(return_value=10)),
    )

    with pytest.raises(RuntimeError, match="attribution changed"):
        await use_case._execute(  # type: ignore[arg-type]
            SimpleNamespace(),
            AssignReferralRewardsDto(
                user=SimpleNamespace(id=9, name="payer", log="payer"),
                transaction=_transaction(PurchaseType.NEW),
            ),
        )

    referral_dao.lock_referral_attribution.assert_awaited_once_with(9, (2,))
    referral_dao.create_reward.assert_not_awaited()


@pytest.mark.asyncio
async def test_attach_referral_locks_users_before_existing_check_and_insert() -> None:
    events: list[str] = []
    referrer = SimpleNamespace(id=2, remna_name="referrer")
    referred = SimpleNamespace(id=9, name="payer")

    async def lock(*args: object) -> None:
        events.append("lock")

    async def chain(*args: object) -> tuple[None, None]:
        events.append("chain")
        return None, None

    async def create(*args: object) -> None:
        events.append("create")

    referral_dao = SimpleNamespace(
        lock_referral_attribution=AsyncMock(side_effect=lock),
        get_referral_chain=AsyncMock(side_effect=chain),
        create_referral=AsyncMock(side_effect=create),
    )
    publisher = SimpleNamespace(publish=AsyncMock())
    uow = FakeUnitOfWork()
    use_case = AttachReferral(
        uow,  # type: ignore[arg-type]
        SimpleNamespace(
            get_by_referral_code=AsyncMock(return_value=referrer),
            get_by_id=AsyncMock(return_value=referred),
        ),
        referral_dao,
        publisher,
    )

    result = await use_case._execute(  # type: ignore[arg-type]
        SimpleNamespace(),
        AttachReferralDto(user_id=9, referral_code="CODE"),
    )

    assert result is referrer
    assert events == ["lock", "chain", "create"]
    referral_dao.lock_referral_attribution.assert_awaited_once_with(9, (2,))
    publisher.publish.assert_awaited_once()
    uow.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_missing_subscription_defers_without_repeated_failure_event() -> None:
    uow = FakeUnitOfWork()
    referral_dao = SimpleNamespace(
        defer_reward=AsyncMock(return_value=True),
    )
    publisher = SimpleNamespace(publish=AsyncMock())
    remnawave = SimpleNamespace(update_user=AsyncMock())
    use_case = GiveReferrerReward(
        uow,
        SimpleNamespace(get_by_id=AsyncMock(return_value=SimpleNamespace(id=2, remna_name="r"))),
        SimpleNamespace(get_current=AsyncMock(return_value=None)),
        referral_dao,
        publisher,
        remnawave,
        FakeMutationLock(),  # type: ignore[arg-type]
    )
    reward = ReferralRewardDto(
        id=8,
        user_id=2,
        type=ReferralRewardType.EXTRA_DAYS,
        amount=3,
        attempt_count=1,
        state=ReferralRewardState.PROCESSING,
    )

    await use_case._execute(  # type: ignore[arg-type]
        SimpleNamespace(log="system"),
        GiveReferrerRewardDto(2, reward, "payer", "t" * 64),
    )

    referral_dao.defer_reward.assert_awaited_once()
    publisher.publish.assert_not_awaited()
    remnawave.update_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_ambiguous_extra_days_is_not_retried_automatically() -> None:
    uow = FakeUnitOfWork()
    baseline = datetime_now() + timedelta(days=10)
    subscription = SimpleNamespace(
        id=41,
        user_remna_id="remna-id",
        expire_at=baseline,
        is_trial=False,
        current_status=SubscriptionStatus.ACTIVE,
    )
    fresh_subscription = SimpleNamespace(**subscription.__dict__)
    referral_dao = SimpleNamespace(
        set_extra_days_target=AsyncMock(return_value=True),
        mark_reward_manual_required=AsyncMock(return_value=True),
        lock_reward_source_if_eligible=AsyncMock(return_value=True),
        cancel_claimed_reward=AsyncMock(),
    )
    publisher = SimpleNamespace(publish=AsyncMock())
    remnawave = SimpleNamespace(update_user=AsyncMock(side_effect=TimeoutError("unknown")))
    use_case = GiveReferrerReward(
        uow,
        SimpleNamespace(get_by_id=AsyncMock(return_value=SimpleNamespace(id=2, remna_name="r"))),
        SimpleNamespace(get_current=AsyncMock(side_effect=[subscription, fresh_subscription])),
        referral_dao,
        publisher,
        remnawave,
        FakeMutationLock(),  # type: ignore[arg-type]
    )
    reward = ReferralRewardDto(
        id=8,
        user_id=2,
        type=ReferralRewardType.EXTRA_DAYS,
        amount=3,
        state=ReferralRewardState.PROCESSING,
    )

    await use_case._execute(  # type: ignore[arg-type]
        SimpleNamespace(log="system"),
        GiveReferrerRewardDto(2, reward, "payer", "t" * 64),
    )

    referral_dao.set_extra_days_target.assert_awaited_once_with(
        8,
        token_hash="t" * 64,
        subscription_id=41,
        baseline_expire_at=baseline,
        target_expire_at=baseline + timedelta(days=3),
    )
    remnawave.update_user.assert_awaited_once()
    referral_dao.mark_reward_manual_required.assert_awaited_once()
    assert (
        "EXTRA_DAYS_AMBIGUOUS"
        in (referral_dao.mark_reward_manual_required.await_args.kwargs["error_code"])
    )
    publisher.publish.assert_awaited_once()


@pytest.mark.asyncio
async def test_extra_days_2xx_without_read_after_write_target_requires_manual_review() -> None:
    uow = FakeUnitOfWork()
    baseline = datetime_now() + timedelta(days=10)
    subscription = SimpleNamespace(
        id=41,
        user_remna_id="remna-id",
        expire_at=baseline,
        is_trial=False,
        current_status=SubscriptionStatus.ACTIVE,
    )
    fresh_subscription = SimpleNamespace(**subscription.__dict__)
    referral_dao = SimpleNamespace(
        set_extra_days_target=AsyncMock(return_value=True),
        lock_reward_source_if_eligible=AsyncMock(return_value=True),
        cancel_claimed_reward=AsyncMock(),
        mark_reward_manual_required=AsyncMock(return_value=True),
        finish_extra_days_reward=AsyncMock(),
    )

    async def update_user(**kwargs: object) -> SimpleNamespace:
        value = kwargs["subscription"]
        return SimpleNamespace(
            uuid=value.user_remna_id,  # type: ignore[union-attr]
            expire_at=value.expire_at,  # type: ignore[union-attr]
        )

    remnawave = SimpleNamespace(
        update_user=AsyncMock(side_effect=update_user),
        get_user_by_uuid=AsyncMock(
            return_value=SimpleNamespace(uuid="remna-id", expire_at=baseline)
        ),
    )
    use_case = GiveReferrerReward(
        uow,
        SimpleNamespace(get_by_id=AsyncMock(return_value=SimpleNamespace(id=2, remna_name="r"))),
        SimpleNamespace(
            get_current=AsyncMock(side_effect=[subscription, fresh_subscription]),
            update=AsyncMock(),
        ),
        referral_dao,
        SimpleNamespace(publish=AsyncMock()),
        remnawave,
        FakeMutationLock(),  # type: ignore[arg-type]
    )
    reward = ReferralRewardDto(
        id=8,
        user_id=2,
        type=ReferralRewardType.EXTRA_DAYS,
        amount=3,
        state=ReferralRewardState.PROCESSING,
    )

    await use_case._execute(  # type: ignore[arg-type]
        SimpleNamespace(log="system"),
        GiveReferrerRewardDto(2, reward, "payer", "t" * 64),
    )

    referral_dao.finish_extra_days_reward.assert_not_awaited()
    referral_dao.mark_reward_manual_required.assert_awaited_once()


def test_extra_days_expiry_verification_allows_panel_subsecond_normalization() -> None:
    expected = datetime_now() + timedelta(days=3)
    assert GiveReferrerReward._same_expiry(
        expected.replace(microsecond=0),
        expected,
    )
    assert not GiveReferrerReward._same_expiry(expected + timedelta(seconds=2), expected)


@pytest.mark.asyncio
async def test_inactive_nontrial_subscription_waits_safely() -> None:
    uow = FakeUnitOfWork()
    subscription = SimpleNamespace(
        is_trial=False,
        current_status=SubscriptionStatus.EXPIRED,
    )
    referral_dao = SimpleNamespace(defer_reward=AsyncMock(return_value=True))
    use_case = GiveReferrerReward(
        uow,
        SimpleNamespace(get_by_id=AsyncMock(return_value=SimpleNamespace(id=2, remna_name="r"))),
        SimpleNamespace(get_current=AsyncMock(return_value=subscription)),
        referral_dao,
        SimpleNamespace(publish=AsyncMock()),
        SimpleNamespace(update_user=AsyncMock()),
        FakeMutationLock(),  # type: ignore[arg-type]
    )
    reward = ReferralRewardDto(
        id=8,
        user_id=2,
        type=ReferralRewardType.EXTRA_DAYS,
        amount=3,
        state=ReferralRewardState.PROCESSING,
    )

    await use_case._execute(  # type: ignore[arg-type]
        SimpleNamespace(log="system"),
        GiveReferrerRewardDto(2, reward, "payer", "t" * 64),
    )

    assert (
        referral_dao.defer_reward.await_args.kwargs["error_code"]
        == "RECIPIENT_SUBSCRIPTION_INACTIVE"
    )


@pytest.mark.asyncio
async def test_parallel_extra_days_grants_are_serialized_and_additive() -> None:
    baseline = datetime_now() + timedelta(days=10)
    subscription = SimpleNamespace(
        id=41,
        user_remna_id="remna-id",
        expire_at=baseline,
        is_trial=False,
        current_status=SubscriptionStatus.ACTIVE,
    )
    subscription_dao = SimpleNamespace(
        get_current=AsyncMock(side_effect=lambda user_id: subscription),
        update=AsyncMock(side_effect=lambda value: value),
    )
    referral_dao = SimpleNamespace(
        set_extra_days_target=AsyncMock(return_value=True),
        finish_extra_days_reward=AsyncMock(return_value=True),
        lock_reward_source_if_eligible=AsyncMock(return_value=True),
        cancel_claimed_reward=AsyncMock(),
    )

    async def delayed_update(**kwargs: object) -> SimpleNamespace:
        await asyncio.sleep(0.01)
        value = kwargs["subscription"]
        return SimpleNamespace(
            uuid=value.user_remna_id,  # type: ignore[union-attr]
            expire_at=value.expire_at,  # type: ignore[union-attr]
        )

    async def get_remote(uuid: object) -> SimpleNamespace:
        return SimpleNamespace(uuid=uuid, expire_at=subscription.expire_at)

    remnawave = SimpleNamespace(
        update_user=AsyncMock(side_effect=delayed_update),
        get_user_by_uuid=AsyncMock(side_effect=get_remote),
    )
    mutation_lock = SerialMutationLock()

    def use_case() -> GiveReferrerReward:
        return GiveReferrerReward(
            FakeUnitOfWork(),  # type: ignore[arg-type]
            SimpleNamespace(
                get_by_id=AsyncMock(return_value=SimpleNamespace(id=2, remna_name="recipient"))
            ),
            subscription_dao,
            referral_dao,
            SimpleNamespace(publish=AsyncMock()),
            remnawave,
            mutation_lock,  # type: ignore[arg-type]
        )

    rewards = [
        ReferralRewardDto(
            id=8,
            user_id=2,
            type=ReferralRewardType.EXTRA_DAYS,
            amount=2,
            state=ReferralRewardState.PROCESSING,
        ),
        ReferralRewardDto(
            id=9,
            user_id=2,
            type=ReferralRewardType.EXTRA_DAYS,
            amount=3,
            state=ReferralRewardState.PROCESSING,
        ),
    ]
    await asyncio.gather(
        *(
            use_case()._execute(  # type: ignore[arg-type]
                SimpleNamespace(log="system"),
                GiveReferrerRewardDto(2, reward, "payer", str(reward.id) * 64),
            )
            for reward in rewards
        )
    )

    assert subscription.expire_at == baseline + timedelta(days=5)
    targets = [
        call.kwargs["target_expire_at"]
        for call in referral_dao.set_extra_days_target.await_args_list
    ]
    assert targets == [baseline + timedelta(days=2), baseline + timedelta(days=5)]


@pytest.mark.asyncio
async def test_refund_after_claim_cancels_before_points_side_effect() -> None:
    referral_dao = SimpleNamespace(
        lock_reward_source_if_eligible=AsyncMock(return_value=False),
        cancel_claimed_reward=AsyncMock(return_value=True),
        issue_points_reward=AsyncMock(),
    )
    use_case = GiveReferrerReward(
        FakeUnitOfWork(),  # type: ignore[arg-type]
        SimpleNamespace(get_by_id=AsyncMock(return_value=SimpleNamespace(id=2, remna_name="r"))),
        SimpleNamespace(),
        referral_dao,
        SimpleNamespace(publish=AsyncMock()),
        SimpleNamespace(),
        FakeMutationLock(),  # type: ignore[arg-type]
    )
    reward = ReferralRewardDto(
        id=8,
        user_id=2,
        type=ReferralRewardType.POINTS,
        amount=10,
        state=ReferralRewardState.PROCESSING,
    )

    await use_case._execute(  # type: ignore[arg-type]
        SimpleNamespace(log="system"),
        GiveReferrerRewardDto(2, reward, "payer", "t" * 64),
    )

    referral_dao.cancel_claimed_reward.assert_awaited_once_with(
        8,
        token_hash="t" * 64,
        error_code="SOURCE_NOT_ELIGIBLE_BEFORE_GRANT",
    )
    referral_dao.issue_points_reward.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_alerts_each_manual_reward_only_once() -> None:
    reward = ReferralRewardDto(
        id=8,
        user_id=2,
        type=ReferralRewardType.EXTRA_DAYS,
        amount=3,
        state=ReferralRewardState.MANUAL_REQUIRED,
    )
    referral_dao = SimpleNamespace(
        claim_pending_rewards=AsyncMock(return_value=[]),
        claim_manual_required_rewards_for_alert=AsyncMock(side_effect=[[reward], []]),
        mark_manual_rewards_alerted=AsyncMock(),
    )
    worker = RetryPendingReferralRewards(
        FakeUnitOfWork(),  # type: ignore[arg-type]
        referral_dao,
        SimpleNamespace(system=AsyncMock()),
    )

    assert await worker._execute(SimpleNamespace(log="system")) == 0  # type: ignore[arg-type]
    assert await worker._execute(SimpleNamespace(log="system")) == 0  # type: ignore[arg-type]
    assert referral_dao.claim_manual_required_rewards_for_alert.await_count == 2
    referral_dao.mark_manual_rewards_alerted.assert_awaited_once_with([8])


@pytest.mark.asyncio
async def test_worker_claims_each_reward_only_when_it_is_ready_to_start() -> None:
    events: list[str] = []
    rewards = [
        ReferralRewardDto(
            id=8,
            user_id=2,
            type=ReferralRewardType.POINTS,
            amount=1,
            state=ReferralRewardState.PROCESSING,
        ),
        ReferralRewardDto(
            id=9,
            user_id=3,
            type=ReferralRewardType.POINTS,
            amount=1,
            state=ReferralRewardState.PROCESSING,
        ),
    ]
    claims = iter([[rewards[0]], [rewards[1]], []])

    async def claim(**kwargs: object) -> list[ReferralRewardDto]:
        events.append("claim")
        assert kwargs["limit"] == 1
        return next(claims)

    async def give(data: GiveReferrerRewardDto) -> None:
        events.append(f"give:{data.reward.id}")
        await asyncio.sleep(0.001)

    referral_dao = SimpleNamespace(
        claim_pending_rewards=AsyncMock(side_effect=claim),
        get_reward_referred_name=AsyncMock(return_value="payer"),
        claim_manual_required_rewards_for_alert=AsyncMock(return_value=[]),
        mark_manual_rewards_alerted=AsyncMock(),
    )
    worker = RetryPendingReferralRewards(
        FakeUnitOfWork(),  # type: ignore[arg-type]
        referral_dao,
        SimpleNamespace(system=AsyncMock(side_effect=give)),
    )

    assert await worker._execute(SimpleNamespace(log="system")) == 2  # type: ignore[arg-type]
    assert events == ["claim", "give:8", "claim", "give:9", "claim"]
    token_hashes = [
        call.kwargs["token_hash"] for call in referral_dao.claim_pending_rewards.await_args_list[:2]
    ]
    assert token_hashes[0] != token_hashes[1]


@pytest.mark.asyncio
async def test_operator_resolution_records_decision_without_replaying_reward() -> None:
    uow = FakeUnitOfWork()
    reward = ReferralRewardDto(
        id=8,
        user_id=2,
        type=ReferralRewardType.POINTS,
        amount=10,
        state=ReferralRewardState.MANUAL_REQUIRED,
    )
    referral_dao = SimpleNamespace(
        get_reward_by_id=AsyncMock(side_effect=[reward, reward]),
        manual_resolution_match=AsyncMock(return_value=None),
        resolve_manual_reward=AsyncMock(return_value=True),
    )
    use_case = ResolveManualReferralReward(
        uow,
        referral_dao,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        FakeMutationLock(),  # type: ignore[arg-type]
    )

    await use_case._execute(  # type: ignore[arg-type]
        SimpleNamespace(log="operator"),
        ResolveManualReferralRewardDto(
            reward_id=8,
            expected_version=0,
            confirm_issued=True,
            operator_reference="alice/TICKET-123",
            reason="Verified ledger balance",
        ),
    )

    referral_dao.resolve_manual_reward.assert_awaited_once_with(
        8,
        expected_version=0,
        confirm_issued=True,
        operator_reference="alice/TICKET-123",
        resolved_by="ADMIN_API",
        reason="Verified ledger balance",
        allow_drift=False,
        observed_subscription_id=None,
        observed_remote_uuid=None,
        observed_expire_at=None,
        source_status=None,
    )
    uow.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_operator_cannot_confirm_markerless_on_first_manual_intent() -> None:
    # A later transaction may already own the unique ON_FIRST marker. The
    # marker-less manual T1 must not be confirmed into a second grant.
    reward = ReferralRewardDto(
        id=8,
        user_id=2,
        type=ReferralRewardType.POINTS,
        amount=10,
        state=ReferralRewardState.MANUAL_REQUIRED,
        accrual_strategy_snapshot=ReferralAccrualStrategy.ON_FIRST_PAYMENT,
        accrual_strategy=None,
    )
    referral_dao = SimpleNamespace(
        get_reward_by_id=AsyncMock(return_value=reward),
        manual_resolution_match=AsyncMock(return_value=None),
        resolve_manual_reward=AsyncMock(),
    )
    use_case = ResolveManualReferralReward(
        FakeUnitOfWork(),  # type: ignore[arg-type]
        referral_dao,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        FakeMutationLock(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="did not claim first-payment eligibility"):
        await use_case._execute(  # type: ignore[arg-type]
            SimpleNamespace(log="operator"),
            ResolveManualReferralRewardDto(
                reward_id=8,
                expected_version=0,
                confirm_issued=True,
                operator_reference="alice/TICKET-123",
                reason="T2 is already the selected winner",
            ),
        )

    referral_dao.resolve_manual_reward.assert_not_awaited()


@pytest.mark.asyncio
async def test_operator_can_confirm_manual_on_first_intent_that_holds_marker() -> None:
    reward = ReferralRewardDto(
        id=8,
        user_id=2,
        type=ReferralRewardType.POINTS,
        amount=10,
        state=ReferralRewardState.MANUAL_REQUIRED,
        accrual_strategy_snapshot=ReferralAccrualStrategy.ON_FIRST_PAYMENT,
        accrual_strategy=ReferralAccrualStrategy.ON_FIRST_PAYMENT,
    )
    referral_dao = SimpleNamespace(
        get_reward_by_id=AsyncMock(side_effect=[reward, reward]),
        manual_resolution_match=AsyncMock(return_value=None),
        resolve_manual_reward=AsyncMock(return_value=True),
    )
    use_case = ResolveManualReferralReward(
        FakeUnitOfWork(),  # type: ignore[arg-type]
        referral_dao,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        FakeMutationLock(),  # type: ignore[arg-type]
    )

    await use_case._execute(  # type: ignore[arg-type]
        SimpleNamespace(log="operator"),
        ResolveManualReferralRewardDto(
            reward_id=8,
            expected_version=0,
            confirm_issued=True,
            operator_reference="alice/TICKET-124",
            reason="Verified selected winner grant",
        ),
    )

    referral_dao.resolve_manual_reward.assert_awaited_once()


@pytest.mark.asyncio
async def test_operator_confirm_refunded_source_requires_override_and_audits_status() -> None:
    reward = ReferralRewardDto(
        id=8,
        user_id=2,
        type=ReferralRewardType.POINTS,
        amount=10,
        source_transaction_id=77,
        state=ReferralRewardState.MANUAL_REQUIRED,
        manual_incident_version=1,
    )
    strict_dao = SimpleNamespace(
        get_reward_by_id=AsyncMock(side_effect=[reward, reward]),
        manual_resolution_match=AsyncMock(return_value=None),
        lock_manual_reward_source_status=AsyncMock(return_value=TransactionStatus.REFUNDED),
        resolve_manual_reward=AsyncMock(),
    )
    strict = ResolveManualReferralReward(
        FakeUnitOfWork(),  # type: ignore[arg-type]
        strict_dao,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        FakeMutationLock(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="refunded reward"):
        await strict._execute(  # type: ignore[arg-type]
            SimpleNamespace(log="operator"),
            ResolveManualReferralRewardDto(
                reward_id=8,
                expected_version=1,
                confirm_issued=True,
                operator_reference="alice/TICKET-126",
                reason="Source refund observed",
            ),
        )
    strict_dao.resolve_manual_reward.assert_not_awaited()

    override_dao = SimpleNamespace(
        get_reward_by_id=AsyncMock(side_effect=[reward, reward]),
        manual_resolution_match=AsyncMock(return_value=None),
        lock_manual_reward_source_status=AsyncMock(return_value=TransactionStatus.REFUNDED),
        resolve_manual_reward=AsyncMock(return_value=True),
    )
    override = ResolveManualReferralReward(
        FakeUnitOfWork(),  # type: ignore[arg-type]
        override_dao,
        SimpleNamespace(),
        SimpleNamespace(),
        SimpleNamespace(),
        FakeMutationLock(),  # type: ignore[arg-type]
    )
    await override._execute(  # type: ignore[arg-type]
        SimpleNamespace(log="operator"),
        ResolveManualReferralRewardDto(
            reward_id=8,
            expected_version=1,
            confirm_issued=True,
            operator_reference="alice/TICKET-126",
            reason="Audited refund and explicit clawback policy",
            allow_drift=True,
        ),
    )

    assert override_dao.resolve_manual_reward.await_args.kwargs["source_status"] == (
        TransactionStatus.REFUNDED
    )
    assert override_dao.resolve_manual_reward.await_args.kwargs["expected_version"] == 1


@pytest.mark.asyncio
async def test_operator_confirm_extra_days_verifies_remote_and_repairs_local_expiry() -> None:
    baseline = datetime_now() + timedelta(days=10)
    target = baseline + timedelta(days=3)
    reward = ReferralRewardDto(
        id=8,
        user_id=2,
        type=ReferralRewardType.EXTRA_DAYS,
        amount=3,
        state=ReferralRewardState.MANUAL_REQUIRED,
        target_subscription_id=41,
        baseline_expire_at=baseline,
        target_expire_at=target,
    )
    subscription = SimpleNamespace(
        id=41,
        user_remna_id="remna-id",
        expire_at=baseline,
    )
    referral_dao = SimpleNamespace(
        get_reward_by_id=AsyncMock(side_effect=[reward, reward]),
        manual_resolution_match=AsyncMock(return_value=None),
        resolve_manual_reward=AsyncMock(return_value=True),
    )
    subscription_dao = SimpleNamespace(
        get_current=AsyncMock(return_value=subscription),
        update=AsyncMock(side_effect=lambda value: value),
    )
    renewed_target = target + timedelta(days=5)
    use_case = ResolveManualReferralReward(
        FakeUnitOfWork(),  # type: ignore[arg-type]
        referral_dao,
        SimpleNamespace(get_by_id=AsyncMock(return_value=SimpleNamespace(id=2))),
        subscription_dao,
        SimpleNamespace(
            get_user_by_uuid=AsyncMock(
                return_value=SimpleNamespace(
                    uuid="remna-id",
                    expire_at=renewed_target,
                )
            )
        ),
        FakeMutationLock(),  # type: ignore[arg-type]
    )

    await use_case._execute(  # type: ignore[arg-type]
        SimpleNamespace(log="operator"),
        ResolveManualReferralRewardDto(
            reward_id=8,
            expected_version=0,
            confirm_issued=True,
            operator_reference="alice/TICKET-123",
            reason="Verified remote target",
        ),
    )

    assert subscription.expire_at == renewed_target
    subscription_dao.update.assert_awaited_once_with(subscription)


@pytest.mark.asyncio
async def test_operator_cancel_extra_days_rejects_remote_target_still_applied() -> None:
    baseline = datetime_now() + timedelta(days=10)
    target = baseline + timedelta(days=3)
    reward = ReferralRewardDto(
        id=8,
        user_id=2,
        type=ReferralRewardType.EXTRA_DAYS,
        amount=3,
        state=ReferralRewardState.MANUAL_REQUIRED,
        target_subscription_id=41,
        baseline_expire_at=baseline,
        target_expire_at=target,
    )
    referral_dao = SimpleNamespace(
        get_reward_by_id=AsyncMock(side_effect=[reward, reward]),
        manual_resolution_match=AsyncMock(return_value=None),
        resolve_manual_reward=AsyncMock(),
    )
    use_case = ResolveManualReferralReward(
        FakeUnitOfWork(),  # type: ignore[arg-type]
        referral_dao,
        SimpleNamespace(get_by_id=AsyncMock(return_value=SimpleNamespace(id=2))),
        SimpleNamespace(
            get_current=AsyncMock(
                return_value=SimpleNamespace(
                    id=41,
                    user_remna_id="remna-id",
                    expire_at=baseline,
                )
            ),
            update=AsyncMock(),
        ),
        SimpleNamespace(
            get_user_by_uuid=AsyncMock(
                return_value=SimpleNamespace(uuid="remna-id", expire_at=target)
            )
        ),
        FakeMutationLock(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="does not match"):
        await use_case._execute(  # type: ignore[arg-type]
            SimpleNamespace(log="operator"),
            ResolveManualReferralRewardDto(
                reward_id=8,
                expected_version=0,
                confirm_issued=False,
                operator_reference="alice/TICKET-123",
                reason="Attempt cancel",
            ),
        )

    referral_dao.resolve_manual_reward.assert_not_awaited()


@pytest.mark.asyncio
async def test_operator_target_replacement_requires_explicit_drift_override() -> None:
    baseline = datetime_now() + timedelta(days=10)
    reward = ReferralRewardDto(
        id=8,
        user_id=2,
        type=ReferralRewardType.EXTRA_DAYS,
        amount=3,
        state=ReferralRewardState.MANUAL_REQUIRED,
        target_subscription_id=41,
        baseline_expire_at=baseline,
        target_expire_at=baseline + timedelta(days=3),
    )
    referral_dao = SimpleNamespace(
        get_reward_by_id=AsyncMock(side_effect=[reward, reward]),
        manual_resolution_match=AsyncMock(return_value=None),
        resolve_manual_reward=AsyncMock(),
    )
    use_case = ResolveManualReferralReward(
        FakeUnitOfWork(),  # type: ignore[arg-type]
        referral_dao,
        SimpleNamespace(),
        SimpleNamespace(
            get_current=AsyncMock(
                return_value=SimpleNamespace(
                    id=99,
                    user_remna_id="replacement-remna-id",
                    expire_at=baseline,
                )
            ),
            update=AsyncMock(),
        ),
        SimpleNamespace(
            get_user_by_uuid=AsyncMock(
                return_value=SimpleNamespace(
                    uuid="replacement-remna-id",
                    expire_at=baseline + timedelta(days=30),
                )
            )
        ),
        FakeMutationLock(),  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="drift override is required"):
        await use_case._execute(  # type: ignore[arg-type]
            SimpleNamespace(log="operator"),
            ResolveManualReferralRewardDto(
                reward_id=8,
                expected_version=0,
                confirm_issued=True,
                operator_reference="alice/TICKET-125",
                reason="Replacement subscription requires audit",
            ),
        )

    referral_dao.resolve_manual_reward.assert_not_awaited()


@pytest.mark.asyncio
async def test_operator_drift_override_snapshots_remote_evidence_and_syncs_current() -> None:
    baseline = datetime_now() + timedelta(days=10)
    observed_expiry = baseline + timedelta(days=30)
    reward = ReferralRewardDto(
        id=8,
        user_id=2,
        type=ReferralRewardType.EXTRA_DAYS,
        amount=3,
        state=ReferralRewardState.MANUAL_REQUIRED,
        target_subscription_id=41,
        baseline_expire_at=baseline,
        target_expire_at=baseline + timedelta(days=3),
    )
    current = SimpleNamespace(
        id=99,
        user_remna_id="replacement-remna-id",
        expire_at=baseline,
    )
    referral_dao = SimpleNamespace(
        get_reward_by_id=AsyncMock(side_effect=[reward, reward]),
        manual_resolution_match=AsyncMock(return_value=None),
        resolve_manual_reward=AsyncMock(return_value=True),
    )
    subscription_dao = SimpleNamespace(
        get_current=AsyncMock(return_value=current),
        update=AsyncMock(side_effect=lambda value: value),
    )
    use_case = ResolveManualReferralReward(
        FakeUnitOfWork(),  # type: ignore[arg-type]
        referral_dao,
        SimpleNamespace(),
        subscription_dao,
        SimpleNamespace(
            get_user_by_uuid=AsyncMock(
                return_value=SimpleNamespace(
                    uuid="replacement-remna-id",
                    expire_at=observed_expiry,
                )
            )
        ),
        FakeMutationLock(),  # type: ignore[arg-type]
    )

    await use_case._execute(  # type: ignore[arg-type]
        SimpleNamespace(log="operator"),
        ResolveManualReferralRewardDto(
            reward_id=8,
            expected_version=0,
            confirm_issued=True,
            operator_reference="alice/TICKET-125",
            reason="Audited replacement subscription and external ledger",
            allow_drift=True,
        ),
    )

    assert current.expire_at == observed_expiry
    subscription_dao.update.assert_awaited_once_with(current)
    referral_dao.resolve_manual_reward.assert_awaited_once_with(
        8,
        expected_version=0,
        confirm_issued=True,
        operator_reference="alice/TICKET-125",
        resolved_by="ADMIN_API",
        reason="Audited replacement subscription and external ledger",
        allow_drift=True,
        observed_subscription_id=99,
        observed_remote_uuid="replacement-remna-id",
        observed_expire_at=observed_expiry,
        source_status=None,
    )


@pytest.mark.asyncio
async def test_operator_resolution_identical_retry_skips_remote_drift_check() -> None:
    reward = ReferralRewardDto(
        id=8,
        user_id=2,
        type=ReferralRewardType.EXTRA_DAYS,
        amount=3,
        state=ReferralRewardState.ISSUED,
    )
    referral_dao = SimpleNamespace(
        get_reward_by_id=AsyncMock(return_value=reward),
        manual_resolution_match=AsyncMock(return_value=True),
        resolve_manual_reward=AsyncMock(),
    )
    remnawave = SimpleNamespace(get_user_by_uuid=AsyncMock())
    use_case = ResolveManualReferralReward(
        FakeUnitOfWork(),  # type: ignore[arg-type]
        referral_dao,
        SimpleNamespace(),
        SimpleNamespace(),
        remnawave,
        FakeMutationLock(),  # type: ignore[arg-type]
    )

    await use_case._execute(  # type: ignore[arg-type]
        SimpleNamespace(log="operator"),
        ResolveManualReferralRewardDto(
            reward_id=8,
            expected_version=0,
            confirm_issued=True,
            operator_reference="alice/TICKET-123",
            reason="Verified remote target",
            allow_drift=True,
        ),
    )

    referral_dao.manual_resolution_match.assert_awaited_once_with(
        8,
        expected_version=0,
        confirm_issued=True,
        operator_reference="alice/TICKET-123",
        resolved_by="ADMIN_API",
        reason="Verified remote target",
        allow_drift=True,
    )
    remnawave.get_user_by_uuid.assert_not_awaited()
    referral_dao.resolve_manual_reward.assert_not_awaited()
