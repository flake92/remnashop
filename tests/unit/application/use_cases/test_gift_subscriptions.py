import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import Any, Optional
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from remnapy.enums.users import TrafficLimitStrategy

from src.application.dto import PlanSnapshotDto, PriceDetailsDto, PromocodeDto, TransactionDto
from src.application.events.system import UserPurchaseEvent
from src.application.use_cases.gateways.commands.payment import ProcessPayment
from src.application.use_cases.promocode.commands.activate import (
    ActivatePromocode,
    ActivatePromocodeDto,
)
from src.application.use_cases.promocode.commands.gift import (
    GIFT_CODE_TTL,
    IssueGift,
    IssueGiftDto,
    generate_gift_code,
)
from src.core.enums import (
    Currency,
    PaymentGatewayType,
    PlanType,
    PromocodeAvailability,
    PromocodeRewardType,
    PurchaseType,
    SubscriptionStatus,
    TransactionStatus,
)
from src.core.exceptions import PromocodeNotAvailableError
from src.web.endpoints.public.subscription import _payment_request_hash
from src.web.schemas import GiftPurchaseRequest, PurchaseRequest

PAYMENT_ID = UUID("00000000-0000-0000-0000-000000000901")
REMNA_ID = UUID("00000000-0000-0000-0000-000000000902")


class FakeUnitOfWork:
    def __init__(self) -> None:
        self.commits = 0

    async def __aenter__(self) -> "FakeUnitOfWork":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class FakeMutationLock:
    def hold(self, user_id: int) -> "FakeMutationLock":
        return self

    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: object) -> None:
        return None


def plan(duration: int = 30) -> PlanSnapshotDto:
    return PlanSnapshotDto(
        id=1,
        name="Basic",
        type=PlanType.UNLIMITED,
        traffic_limit_strategy=TrafficLimitStrategy.NO_RESET,
        traffic_limit=0,
        device_limit=3,
        duration=duration,
    )


def gift_transaction(purchase_type: PurchaseType = PurchaseType.GIFT) -> TransactionDto:
    return TransactionDto(
        id=1,
        payment_id=PAYMENT_ID,
        user_id=7,
        status=TransactionStatus.COMPLETED,
        purchase_type=purchase_type,
        gateway_type=PaymentGatewayType.YOOKASSA,
        pricing=PriceDetailsDto(
            original_amount=Decimal("300"),
            discount_percent=0,
            final_amount=Decimal("300"),
        ),
        currency=Currency.RUB,
        plan_snapshot=plan(),
    )


def buyer() -> Any:
    return SimpleNamespace(
        id=7,
        remna_name="buyer",
        log="buyer",
        telegram_id=42,
        username=None,
        name="Buyer",
        email=None,
    )


class FakePromocodeDao:
    def __init__(self) -> None:
        self.by_payment: dict[UUID, PromocodeDto] = {}
        self.created: list[PromocodeDto] = []
        self.activations: set[tuple[int, int]] = set()
        self.deleted: list[tuple[int, int]] = []

    async def get_by_gift_payment_id(self, payment_id: UUID) -> Optional[PromocodeDto]:
        return self.by_payment.get(payment_id)

    async def create(self, promocode: PromocodeDto) -> PromocodeDto:
        promocode.id = len(self.created) + 1
        self.created.append(promocode)
        if promocode.gift_payment_id is not None:
            self.by_payment[promocode.gift_payment_id] = promocode
        return promocode

    async def create_activation(self, activation: Any, **kwargs: Any) -> Any:
        key = (activation.promocode_id, activation.user_id)
        if self.activations:
            raise PromocodeNotAvailableError("Promocode activation limit reached")
        self.activations.add(key)
        return activation

    async def delete_activation(self, promocode_id: int, user_id: int) -> bool:
        self.deleted.append((promocode_id, user_id))
        self.activations.discard((promocode_id, user_id))
        return True


def test_gift_codes_are_unambiguous_and_grouped() -> None:
    codes = {generate_gift_code() for _ in range(200)}
    assert len(codes) == 200
    for code in codes:
        assert re.fullmatch(r"GIFT-[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}", code)


@pytest.mark.asyncio
async def test_issue_gift_creates_one_single_use_subscription_code_per_payment() -> None:
    dao = FakePromocodeDao()
    retort = SimpleNamespace(dump=lambda snapshot: {"name": snapshot.name, "duration": 30})
    issue = IssueGift(FakeUnitOfWork(), dao, retort)  # type: ignore[arg-type]

    first = await issue._execute(
        buyer(), IssueGiftDto(buyer=buyer(), transaction=gift_transaction())
    )
    replay = await issue._execute(
        buyer(), IssueGiftDto(buyer=buyer(), transaction=gift_transaction())
    )

    assert replay is first
    assert len(dao.created) == 1
    assert first.reward_type == PromocodeRewardType.SUBSCRIPTION
    assert first.availability == PromocodeAvailability.ALL
    assert first.max_activations == 1
    assert first.is_reusable is False
    assert first.gift_payment_id == PAYMENT_ID
    assert first.gift_buyer_id == 7
    assert first.plan_snapshot == {"name": "Basic", "duration": 30}
    assert first.expires_at is not None
    remaining = first.expires_at - datetime.now(timezone.utc)
    assert GIFT_CODE_TTL - timedelta(minutes=1) < remaining <= GIFT_CODE_TTL


@pytest.mark.asyncio
async def test_issue_gift_rejects_a_purchase_for_the_buyer_themselves() -> None:
    issue = IssueGift(FakeUnitOfWork(), FakePromocodeDao(), SimpleNamespace())  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        await issue._execute(
            buyer(),
            IssueGiftDto(buyer=buyer(), transaction=gift_transaction(PurchaseType.NEW)),
        )


def gift_promo() -> PromocodeDto:
    return PromocodeDto(
        id=5,
        code="GIFT-AAAA-BBBB-CCCC",
        is_active=True,
        reward_type=PromocodeRewardType.SUBSCRIPTION,
        plan_snapshot={"duration": 30},
        max_activations=1,
        gift_payment_id=PAYMENT_ID,
        gift_buyer_id=7,
    )


def subscription(*, expire_at: datetime, is_trial: bool = False) -> Any:
    return SimpleNamespace(
        user_remna_id=REMNA_ID,
        status=SubscriptionStatus.ACTIVE,
        is_trial=is_trial,
        is_active=True,
        grace_until=None,
        expire_at=expire_at,
        traffic_limit=10,
        device_limit=1,
        traffic_limit_strategy=TrafficLimitStrategy.NO_RESET,
        tag=None,
        internal_squads=[],
        external_squad=None,
        url="https://sub.example.com/old",
        plan_snapshot=plan(),
    )


def activate_use_case(
    dao: FakePromocodeDao,
    current: Any,
    remnawave: Any,
) -> tuple[ActivatePromocode, Any]:
    subscription_dao = SimpleNamespace(
        get_current=AsyncMock(return_value=current),
        update=AsyncMock(),
        create=AsyncMock(),
    )
    use_case = ActivatePromocode(
        FakeUnitOfWork(),  # type: ignore[arg-type]
        dao,  # type: ignore[arg-type]
        SimpleNamespace(update=AsyncMock(), get_by_id=AsyncMock(return_value=buyer())),  # type: ignore[arg-type]
        subscription_dao,  # type: ignore[arg-type]
        remnawave,
        AsyncMock(return_value=gift_promo()),  # type: ignore[arg-type]
        SimpleNamespace(publish=AsyncMock()),  # type: ignore[arg-type]
        SimpleNamespace(load=lambda data, _type: plan(data["duration"])),  # type: ignore[arg-type]
        FakeMutationLock(),  # type: ignore[arg-type]
        SimpleNamespace(get_by_id=AsyncMock(return_value=None)),  # type: ignore[arg-type]
    )
    return use_case, subscription_dao


def recipient() -> Any:
    return SimpleNamespace(
        id=11, telegram_id=None, username=None, name="Friend", email=None, log="friend"
    )


@pytest.mark.asyncio
async def test_gift_extends_an_active_subscription_instead_of_replacing_it() -> None:
    expire_at = datetime.now(timezone.utc) + timedelta(days=10)
    current = subscription(expire_at=expire_at)
    remnawave = SimpleNamespace(
        update_user=AsyncMock(
            return_value=SimpleNamespace(subscription_url="https://sub.example.com/old")
        )
    )
    dao = FakePromocodeDao()
    use_case, subscription_dao = activate_use_case(dao, current, remnawave)

    await use_case._execute_locked(
        recipient(), ActivatePromocodeDto(code="gift-aaaa-bbbb-cccc", user=recipient())
    )

    assert current.expire_at == expire_at + timedelta(days=30)
    assert current.device_limit == 3
    assert current.is_trial is False
    assert remnawave.update_user.await_args.kwargs["reset_traffic"] is True
    subscription_dao.update.assert_awaited_once_with(current)
    assert dao.activations == {(5, 11)}


@pytest.mark.asyncio
async def test_gift_on_a_trial_starts_the_paid_term_from_now() -> None:
    current = subscription(expire_at=datetime.now(timezone.utc) + timedelta(days=2), is_trial=True)
    remnawave = SimpleNamespace(
        update_user=AsyncMock(return_value=SimpleNamespace(subscription_url="u"))
    )
    use_case, _ = activate_use_case(FakePromocodeDao(), current, remnawave)
    before = datetime.now(timezone.utc)

    await use_case._execute_locked(
        recipient(), ActivatePromocodeDto(code="GIFT-AAAA-BBBB-CCCC", user=recipient())
    )

    assert before + timedelta(days=30) <= current.expire_at
    assert current.expire_at < before + timedelta(days=30, minutes=1)
    assert current.is_trial is False


@pytest.mark.asyncio
async def test_failed_panel_call_releases_the_gift_claim() -> None:
    current = subscription(expire_at=datetime.now(timezone.utc) + timedelta(days=5))
    remnawave = SimpleNamespace(update_user=AsyncMock(side_effect=RuntimeError("panel down")))
    dao = FakePromocodeDao()
    use_case, subscription_dao = activate_use_case(dao, current, remnawave)

    with pytest.raises(RuntimeError):
        await use_case._execute_locked(
            recipient(), ActivatePromocodeDto(code="GIFT-AAAA-BBBB-CCCC", user=recipient())
        )

    assert dao.deleted == [(5, 11)]
    assert dao.activations == set()
    subscription_dao.update.assert_not_awaited()


@pytest.mark.asyncio
async def test_already_claimed_gift_never_reaches_the_panel() -> None:
    dao = FakePromocodeDao()
    dao.activations.add((5, 99))
    remnawave = SimpleNamespace(update_user=AsyncMock(), create_user=AsyncMock())
    use_case, _ = activate_use_case(dao, None, remnawave)

    with pytest.raises(PromocodeNotAvailableError):
        await use_case._execute_locked(
            recipient(), ActivatePromocodeDto(code="GIFT-AAAA-BBBB-CCCC", user=recipient())
        )

    remnawave.update_user.assert_not_awaited()
    remnawave.create_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_paid_gift_issues_a_code_and_leaves_the_buyer_subscription_alone() -> None:
    issued = gift_promo()
    notifier = SimpleNamespace(notify_user=AsyncMock(), notify_system=AsyncMock())
    events = SimpleNamespace(publish=AsyncMock())
    purchase = SimpleNamespace(system=AsyncMock())
    rewards = SimpleNamespace(system=AsyncMock())
    redirect = SimpleNamespace(to_success_payment=AsyncMock())
    process = ProcessPayment(
        FakeUnitOfWork(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(get_current=AsyncMock()),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        events,  # type: ignore[arg-type]
        notifier,  # type: ignore[arg-type]
        redirect,  # type: ignore[arg-type]
        rewards,  # type: ignore[arg-type]
        purchase,  # type: ignore[arg-type]
        SimpleNamespace(system=AsyncMock(return_value=issued)),  # type: ignore[arg-type]
    )

    await process._handle_success(buyer(), gift_transaction())

    purchase.system.assert_not_awaited()
    rewards.system.assert_not_awaited()
    redirect.to_success_payment.assert_not_awaited()
    event = events.publish.await_args.args[0]
    assert isinstance(event, UserPurchaseEvent)
    assert event.event_key == "event-subscription.gift"
    payload = notifier.notify_user.await_args.kwargs["payload"]
    assert payload.i18n_key == "ntf-gift.purchased"
    assert payload.i18n_kwargs["code"] == issued.code
    assert payload.delete_after is None


def test_gift_and_purchase_of_one_offer_never_share_an_idempotency_hash() -> None:
    fields = {"plan_code": "basic", "duration_days": 30, "gateway_type": "YOOKASSA"}
    gift = _payment_request_hash("PURCHASE", GiftPurchaseRequest(**fields))
    purchase = _payment_request_hash("PURCHASE", PurchaseRequest(**fields))

    assert gift != purchase
    # Clean Pay sends no `gift` field: the default must still mark the request.
    assert GiftPurchaseRequest(**fields).gift is True
