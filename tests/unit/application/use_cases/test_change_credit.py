from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src.application.use_cases.subscription.change_credit import change_credit, daily_rate
from src.application.use_cases.subscription.commands.purchase import (
    PurchaseSubscription,
    PurchaseSubscriptionDto,
)
from src.core.enums import Currency, PurchaseType, SubscriptionStatus


def tiers(**prices: int) -> list[SimpleNamespace]:
    return [
        SimpleNamespace(
            days=int(days[1:]),
            prices=[SimpleNamespace(currency=Currency.RUB, price=Decimal(price))],
        )
        for days, price in prices.items()
    ]


ONE = tiers(d0=6740, d7=54, d30=108, d60=179, d180=448, d365=895)
TWO = tiers(d0=8990, d30=199, d60=339, d180=899, d365=1690)
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def test_same_duration_is_price_ratio() -> None:
    credit = change_credit(
        old_expire_at=NOW + timedelta(days=56),
        now=NOW,
        old_durations=ONE,
        new_durations=TWO,
        new_days=365,
        old_days=365,
    )
    assert credit is not None
    assert credit.days == 29  # 56 * 895 / 1690 = 29.66


def test_old_rate_is_what_the_customer_paid_per_day() -> None:
    # bought 1 device for a month (108 / 30 = 3.6 per day), now 2 devices for a year (1690 / 365)
    credit = change_credit(
        old_expire_at=NOW + timedelta(days=55),
        now=NOW,
        old_durations=ONE,
        new_durations=TWO,
        new_days=365,
        old_days=30,
    )
    assert credit is not None
    assert credit.days == 42  # 55 * 3.6 / 4.630 = 42.76


def test_missing_tier_falls_back_to_closest_duration() -> None:
    # TWO has no 7-day tier: closest is 30 days (199 / 30)
    assert daily_rate(TWO, 7) == Decimal(199) / 30
    credit = change_credit(
        old_expire_at=NOW + timedelta(days=10),
        now=NOW,
        old_durations=ONE,
        new_durations=TWO,
        new_days=7,
        old_days=7,
    )
    assert credit is not None
    assert credit.days == 11  # 10 * (54/7) / (199/30) = 11.63


def test_downgrade_gives_more_days() -> None:
    credit = change_credit(
        old_expire_at=NOW + timedelta(days=30),
        now=NOW,
        old_durations=TWO,
        new_durations=ONE,
        new_days=30,
        old_days=30,
    )
    assert credit is not None
    assert credit.days == 55  # 30 * 199 / 108 = 55.27


@pytest.mark.parametrize(
    "expire, new_days",
    [(NOW - timedelta(days=1), 30), (NOW + timedelta(days=30), 0), (NOW + timedelta(hours=1), 30)],
)
def test_no_credit(expire: datetime, new_days: int) -> None:
    assert (
        change_credit(
            old_expire_at=expire,
            now=NOW,
            old_durations=ONE,
            new_durations=TWO,
            new_days=new_days,
            old_days=30,
        )
        is None
    )


def test_free_old_plan_gives_no_credit() -> None:
    free = tiers(d0=0)
    assert (
        change_credit(
            old_expire_at=NOW + timedelta(days=30),
            now=NOW,
            old_durations=free,
            new_durations=TWO,
            new_days=30,
            old_days=30,
        )
        is None
    )


class FakeUnitOfWork:
    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class FakeMutationLock:
    def hold(self, user_id: int) -> "FakeMutationLock":
        return self

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


def make_purchase(subscription, plans, enabled=True):  # type: ignore[no-untyped-def]
    updated_user = SimpleNamespace(
        uuid=subscription.user_remna_id,
        status=SubscriptionStatus.ACTIVE.value,
        expire_at=datetime(2027, 1, 1, tzinfo=timezone.utc),
        subscription_url="https://sub.example.com/x",
    )
    remnawave = SimpleNamespace(
        create_user=AsyncMock(), update_user=AsyncMock(return_value=updated_user)
    )
    purchase = PurchaseSubscription(
        uow=FakeUnitOfWork(),  # type: ignore[arg-type]
        user_dao=SimpleNamespace(update=AsyncMock()),  # type: ignore[arg-type]
        subscription_dao=SimpleNamespace(  # type: ignore[arg-type]
            get_current=AsyncMock(return_value=subscription), update=AsyncMock()
        ),
        remnawave=remnawave,  # type: ignore[arg-type]
        subscription_mutation_lock=FakeMutationLock(),  # type: ignore[arg-type]
        plan_dao=SimpleNamespace(get_by_id=AsyncMock(side_effect=lambda i: plans.get(i))),  # type: ignore[arg-type]
        settings_dao=SimpleNamespace(  # type: ignore[arg-type]
            get=AsyncMock(
                return_value=SimpleNamespace(extra=SimpleNamespace(change_credit=enabled))
            )
        ),
    )
    return purchase, remnawave


def snapshot(plan_id: int, duration: int, devices: int, trial: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=plan_id,
        is_trial=trial,
        duration=duration,
        device_limit=devices,
        traffic_limit=0,
        traffic_limit_strategy="NO_RESET",
        tag=None,
        internal_squads=[],
        external_squad=None,
    )


def subscription_with(old_plan: SimpleNamespace, expire_in_days: float, **kw):  # type: ignore[no-untyped-def]
    base = dict(  # noqa: C408 - keyword overrides read best as a call
        id=1,
        is_trial=old_plan.is_trial,
        user_remna_id=UUID("00000000-0000-0000-0000-000000000001"),
        status=SubscriptionStatus.ACTIVE,
        expire_at=datetime.now(timezone.utc) + timedelta(days=expire_in_days),
        url="https://sub.example.com/x",
        traffic_limit=0,
        device_limit=old_plan.device_limit,
        traffic_limit_strategy="NO_RESET",
        tag=None,
        internal_squads=[],
        external_squad=None,
        plan_snapshot=old_plan,
        grace_until=None,
    )
    base.update(kw)
    return SimpleNamespace(**base)


PLANS = {1: SimpleNamespace(durations=ONE), 10: SimpleNamespace(durations=TWO)}


async def run_change(subscription, enabled=True):  # type: ignore[no-untyped-def]
    new_plan = snapshot(10, 365, 2)
    purchase, remnawave = make_purchase(subscription, PLANS, enabled)
    transaction = SimpleNamespace(id=5, purchase_type=PurchaseType.CHANGE, plan_snapshot=new_plan)
    user = SimpleNamespace(id=42, remna_name="u", purchase_discount=0)
    await purchase._execute(
        SimpleNamespace(log="[SYSTEM]"),
        PurchaseSubscriptionDto(user, transaction, subscription),  # type: ignore[arg-type]
    )
    sent_plan = remnawave.update_user.await_args.kwargs["plan"]
    return sent_plan, new_plan


@pytest.mark.asyncio
async def test_change_adds_credit_days_to_panel_expiry() -> None:
    subscription = subscription_with(snapshot(1, 365, 1), 56.5)
    sent_plan, new_plan = await run_change(subscription)
    assert sent_plan.duration == 365 + 29
    assert subscription.plan_snapshot is new_plan  # stored snapshot keeps the bought duration
    assert new_plan.duration == 365


@pytest.mark.asyncio
async def test_trial_change_has_no_credit() -> None:
    subscription = subscription_with(snapshot(3, 7, 2, trial=True), 5)
    sent_plan, _ = await run_change(subscription)
    assert sent_plan.duration == 365


@pytest.mark.asyncio
async def test_grace_or_disabled_has_no_credit() -> None:
    for kw in (
        {"grace_until": datetime.now(timezone.utc)},
        {"status": SubscriptionStatus.DISABLED},
    ):
        subscription = subscription_with(snapshot(1, 365, 1), 56.5, **kw)
        sent_plan, _ = await run_change(subscription)
        assert sent_plan.duration == 365


@pytest.mark.asyncio
async def test_switch_off_keeps_old_behaviour() -> None:
    subscription = subscription_with(snapshot(1, 365, 1), 56.5)
    sent_plan, _ = await run_change(subscription, enabled=False)
    assert sent_plan.duration == 365
