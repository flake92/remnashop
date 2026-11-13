from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.application.dto import PlanSnapshotDto, PromocodeDto
from src.application.use_cases.promocode.queries.gift_outcome import (
    GiftOutcomeKind,
    PreviewGift,
    PreviewGiftDto,
    compute_gift_outcome,
)
from src.core.enums import Currency, PlanType, PromocodeRewardType
from src.core.utils.time import datetime_now
from src.infrastructure.services.i18n import LayeredFileStorage
from src.telegram.routers.subscription.promocode_handlers import gift_outcome_data

NOW = datetime(2026, 11, 13, 12, 0, tzinfo=timezone.utc)
TRANSLATIONS = Path(__file__).resolve().parents[4] / "assets" / "translations"


def snapshot(plan_id: int, duration: int = 30, *, is_trial: bool = False) -> PlanSnapshotDto:
    return PlanSnapshotDto(
        id=plan_id,
        name=f"Plan {plan_id}",
        type=PlanType.UNLIMITED,
        traffic_limit=0,
        device_limit=3,
        duration=duration,
        is_trial=is_trial,
    )


def durations(price_for_30_days: str) -> list[Any]:
    price = SimpleNamespace(currency=Currency.RUB, price=Decimal(price_for_30_days))
    return [SimpleNamespace(days=30, prices=[price])]


def subscription(plan: PlanSnapshotDto, *, days_left: float, **overrides: Any) -> Any:
    fields = dict(  # noqa: C408 - keyword overrides read best as a call
        is_trial=False,
        grace_until=None,
        is_active=True,
        expire_at=NOW + timedelta(days=days_left),
        plan_snapshot=plan,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def outcome(current: Any, gift: PlanSnapshotDto, old: str = "300", new: str = "300") -> Any:
    return compute_gift_outcome(
        subscription=current,
        gift_plan=gift,
        current_durations=durations(old),
        gift_durations=durations(new),
        now=NOW,
    )


def test_no_subscription_gets_a_new_one_for_the_gift_term() -> None:
    result = outcome(None, snapshot(2))

    assert result.kind == GiftOutcomeKind.NEW
    assert result.expire_at == NOW + timedelta(days=30)


def test_same_plan_adds_the_gift_term_to_the_remaining_time() -> None:
    result = outcome(subscription(snapshot(2), days_left=10.5), snapshot(2))

    assert result.kind == GiftOutcomeKind.EXTEND
    assert result.remaining_days == 10
    assert result.expire_at == NOW + timedelta(days=40.5)


def test_another_plan_converts_the_remaining_time_at_the_price_per_day() -> None:
    # 20 days of a 600 ₽ plan are worth 40 days of a 300 ₽ plan.
    result = outcome(subscription(snapshot(1), days_left=20), snapshot(2), old="600", new="300")

    assert result.kind == GiftOutcomeKind.CONVERT
    assert result.current_plan_name == "Plan 1"
    assert result.remaining_days == 20
    assert result.credit_days == 40
    assert result.expire_at == NOW + timedelta(days=40 + 30)


def test_a_cheaper_current_plan_converts_to_fewer_days() -> None:
    result = outcome(subscription(snapshot(1), days_left=20), snapshot(2), old="150", new="300")

    assert result.kind == GiftOutcomeKind.CONVERT
    assert result.credit_days == 10


def test_without_prices_the_remaining_days_are_kept() -> None:
    result = compute_gift_outcome(
        subscription=subscription(snapshot(1), days_left=20),
        gift_plan=snapshot(2),
        current_durations=None,
        gift_durations=durations("300"),
        now=NOW,
    )

    assert result.kind == GiftOutcomeKind.KEEP_DAYS
    assert result.expire_at == NOW + timedelta(days=50)


@pytest.mark.parametrize(
    "overrides",
    [
        {"is_trial": True},
        {"grace_until": NOW},
        {"is_active": False},
        {"plan_snapshot": snapshot(1, is_trial=True)},
    ],
)
def test_trial_lapsed_or_grace_restarts_from_now(overrides: dict[str, Any]) -> None:
    result = outcome(subscription(snapshot(1), days_left=5, **overrides), snapshot(2))

    assert result.kind == GiftOutcomeKind.RESTART
    assert result.expire_at == NOW + timedelta(days=30)


def test_unlimited_gift_makes_the_subscription_unlimited() -> None:
    result = outcome(subscription(snapshot(1), days_left=5), snapshot(2, duration=0))

    assert result.kind == GiftOutcomeKind.UNLIMITED
    assert result.is_unlimited


@pytest.mark.asyncio
async def test_preview_validates_without_redeeming_and_explains_a_gift() -> None:
    gift = PromocodeDto(
        id=5,
        code="GIFT-AAAA-BBBB-CCCC",
        is_active=True,
        reward_type=PromocodeRewardType.SUBSCRIPTION,
        plan_snapshot={"id": 2},
        gift_payment_id=uuid4(),
        gift_buyer_id=7,
    )
    validate = AsyncMock(return_value=gift)
    # The preview uses the real clock: ~20.54 days of 600 ₽ are 41 days of 300 ₽.
    current = subscription(
        snapshot(1), days_left=0, expire_at=datetime_now() + timedelta(days=20, hours=13)
    )
    plans = {
        1: SimpleNamespace(durations=durations("600")),
        2: SimpleNamespace(durations=durations("300")),
    }
    preview = PreviewGift(
        validate,  # type: ignore[arg-type]
        SimpleNamespace(get_current=AsyncMock(return_value=current)),  # type: ignore[arg-type]
        SimpleNamespace(get_by_id=AsyncMock(side_effect=lambda plan_id: plans[plan_id])),  # type: ignore[arg-type]
        SimpleNamespace(load=lambda data, _type: snapshot(2)),  # type: ignore[arg-type]
    )
    user = SimpleNamespace(id=11, log="u")

    result = await preview.system(PreviewGiftDto(code=gift.code, user=user))  # type: ignore[arg-type]

    assert result.outcome is not None
    assert result.outcome.kind == GiftOutcomeKind.CONVERT
    assert result.outcome.credit_days == 41
    validate.assert_awaited_once()


def test_gift_confirmation_renders_the_conversion() -> None:
    storage = LayeredFileStorage(
        user_translations_dir=TRANSLATIONS, default_translations_dir=TRANSLATIONS
    )
    (translator,) = storage.get_translators_for_language("ru")
    data = gift_outcome_data(
        outcome(subscription(snapshot(1), days_left=20), snapshot(2), old="600", new="300")
    )

    text = str(translator.get("msg-promocode-gift-confirm", promo_code="GIFT-X", **data))

    assert "с перерасчётом" in text
    assert "+40 дн." in text and "20 дн." in text and "+30 дн." in text
    assert (NOW + timedelta(days=70)).strftime("%d.%m.%Y") in text
