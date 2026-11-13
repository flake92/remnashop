from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import pytest

from src.application.dto import (
    PlanSnapshotDto,
    PriceDetailsDto,
    PromocodeActivationDto,
    PromocodeDto,
    TransactionDto,
)
from src.application.events.system import PromocodeActivatedEvent, UserPurchaseEvent
from src.application.use_cases.gateways.commands.payment import ProcessPayment
from src.application.use_cases.promocode.commands.activate import ActivatePromocode
from src.application.use_cases.promocode.commands.gift import (
    IssueGift,
    IssueGiftDto,
)
from src.application.use_cases.promocode.queries.gifts import (
    GetGiftDetails,
    GetGiftList,
    GetGiftListDto,
)
from src.core.enums import (
    Currency,
    PaymentGatewayType,
    PlanType,
    PromocodeAvailability,
    PromocodeRewardType,
    PurchaseType,
    TransactionStatus,
)
from src.infrastructure.services.i18n import LayeredFileStorage
from src.telegram.routers.dashboard.gifts.getters import gift_status
from src.telegram.routers.subscription.gift_flow import (
    applies_discount,
    giftable_plans,
    is_gift,
)
from src.web.endpoints.public.subscription import _payment_request_hash
from src.web.schemas import GiftPurchaseRequest

PAYMENT_ID = UUID("00000000-0000-0000-0000-000000000a01")
TRANSLATIONS = Path(__file__).resolve().parents[4] / "assets" / "translations"


def plan(*, is_trial: bool = False) -> PlanSnapshotDto:
    return PlanSnapshotDto(
        id=1,
        name="Basic",
        tag=None,
        type=PlanType.UNLIMITED,
        traffic_limit=0,
        device_limit=3,
        duration=30,
        traffic_limit_strategy=None,  # type: ignore[arg-type]
        internal_squads=[],
        external_squad=None,
        is_trial=is_trial,
    )


def gift_transaction() -> TransactionDto:
    return TransactionDto(
        payment_id=PAYMENT_ID,
        user_id=7,
        status=TransactionStatus.COMPLETED,
        purchase_type=PurchaseType.GIFT,
        gateway_type=PaymentGatewayType.YOOKASSA,
        pricing=PriceDetailsDto(
            original_amount=Decimal("300"),
            discount_percent=0,
            final_amount=Decimal("300"),
        ),
        currency=Currency.RUB,
        plan_snapshot=plan(),
    )


def gift_promo() -> PromocodeDto:
    return PromocodeDto(
        id=5,
        code="GIFT-AAAA-BBBB-CCCC",
        is_active=True,
        reward_type=PromocodeRewardType.SUBSCRIPTION,
        plan_snapshot={"name": "Basic", "duration": 30},
        availability=PromocodeAvailability.ALL,
        max_activations=1,
        gift_payment_id=PAYMENT_ID,
        gift_buyer_id=7,
    )


def buyer() -> Any:
    return SimpleNamespace(
        id=7,
        remna_name="buyer",
        log="buyer",
        telegram_id=42,
        username="alice",
        name="Alice",
        email="alice@example.com",
    )


def recipient() -> Any:
    return SimpleNamespace(
        id=11, telegram_id=None, username=None, name="Friend", email="f@example.com", log="f"
    )


@pytest.mark.asyncio
async def test_issued_code_never_carries_a_privacy_choice() -> None:
    created: list[PromocodeDto] = []

    async def create(promo: PromocodeDto) -> PromocodeDto:
        promo.id = 1
        created.append(promo)
        return promo

    class Uow:
        async def __aenter__(self) -> "Uow":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def commit(self) -> None:
            return None

    dao = SimpleNamespace(get_by_gift_payment_id=AsyncMock(return_value=None), create=create)
    issue = IssueGift(Uow(), dao, SimpleNamespace(dump=lambda snapshot: {"name": "Basic"}))  # type: ignore[arg-type]

    await issue.system(IssueGiftDto(buyer=buyer(), transaction=gift_transaction()))

    assert created[0].gift_anonymous is False
    assert created[0].gift_buyer_id == 7


@pytest.mark.asyncio
async def test_operator_purchase_notice_carries_the_code() -> None:
    events = SimpleNamespace(publish=AsyncMock())
    notifier = SimpleNamespace(notify_user=AsyncMock(), notify_system=AsyncMock())
    process = ProcessPayment(
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        SimpleNamespace(get_current=AsyncMock()),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
        events,  # type: ignore[arg-type]
        notifier,  # type: ignore[arg-type]
        SimpleNamespace(to_success_payment=AsyncMock()),  # type: ignore[arg-type]
        SimpleNamespace(system=AsyncMock()),  # type: ignore[arg-type]
        SimpleNamespace(system=AsyncMock()),  # type: ignore[arg-type]
        SimpleNamespace(system=AsyncMock(return_value=gift_promo())),  # type: ignore[arg-type]
    )

    await process._handle_gift_success(buyer(), gift_transaction())

    event = events.publish.await_args.args[0]
    assert isinstance(event, UserPurchaseEvent)
    assert event.as_payload().i18n_kwargs["gift_code"] == "GIFT-AAAA-BBBB-CCCC"
    assert "gift_anonymous" not in event.as_payload().i18n_kwargs
    assert "anonymous" not in notifier.notify_user.await_args.kwargs["payload"].i18n_kwargs


@pytest.mark.asyncio
async def test_gift_activation_notice_names_the_buyer_and_the_payment() -> None:
    events = SimpleNamespace(publish=AsyncMock())
    use_case = object.__new__(ActivatePromocode)
    use_case.user_dao = SimpleNamespace(get_by_id=AsyncMock(return_value=buyer()))  # type: ignore[assignment]
    use_case.event_publisher = events  # type: ignore[assignment]

    # The recipient never learns the buyer, but the operator sees everything.
    await use_case._publish_activated(recipient(), gift_promo())

    event = events.publish.await_args.args[0]
    assert isinstance(event, PromocodeActivatedEvent)
    assert event.event_key == "event-promocode.gift-activated"
    payload = event.as_payload().i18n_kwargs
    assert payload["gift_buyer_name"] == "Alice"
    assert payload["gift_buyer_telegram_id"] == 42
    assert payload["gift_payment_id"] == str(PAYMENT_ID)
    assert payload["email"] == "f@example.com"


def test_ordinary_promocode_keeps_the_old_activation_notice() -> None:
    event = PromocodeActivatedEvent(
        user_id=1,
        telegram_id=1,
        username=None,
        name="U",
        promocode_code="SPRING",
        reward_type="DURATION",
        reward=7,
        plan_name="",
    )

    assert event.event_key == "event-promocode.activated"


def test_old_clean_pay_anonymous_flag_is_accepted_and_ignored() -> None:
    fields = {"plan_code": "basic", "duration_days": 30, "gateway_type": "YOOKASSA"}

    # Clean Pay 0.6.1 may still send it; the request must keep validating.
    request = GiftPurchaseRequest(**fields, anonymous=True)

    assert request.anonymous is True
    assert GiftPurchaseRequest(**fields).anonymous is False
    assert _payment_request_hash("PURCHASE", request)


def _manager(**data: Any) -> Any:
    return SimpleNamespace(dialog_data=data)


def test_bot_gift_flow_charges_list_price_and_skips_trials() -> None:
    gift = _manager(purchase_type=PurchaseType.GIFT)
    purchase = _manager(purchase_type=PurchaseType.NEW)
    paid = SimpleNamespace(is_trial=False)
    trial = SimpleNamespace(is_trial=True)

    assert is_gift(gift) and not is_gift(purchase)
    assert not applies_discount(gift, paid)  # type: ignore[arg-type]
    assert applies_discount(purchase, paid)  # type: ignore[arg-type]
    assert not applies_discount(purchase, trial)  # type: ignore[arg-type]
    assert giftable_plans([paid, trial]) == [paid]  # type: ignore[list-item]


def test_admin_status_prefers_activation_over_expiry() -> None:
    past = datetime(2020, 1, 1, tzinfo=timezone.utc)
    promo = gift_promo()
    assert gift_status(promo, redeemed=False) == "AVAILABLE"
    assert gift_status(promo, redeemed=True) == "ACTIVATED"
    promo.expires_at = past
    assert gift_status(promo, redeemed=False) == "EXPIRED"
    assert gift_status(promo, redeemed=True) == "ACTIVATED"
    promo.is_active = False
    assert gift_status(promo, redeemed=False) == "INACTIVE"


@pytest.mark.asyncio
async def test_admin_gift_card_joins_buyer_payment_and_recipient() -> None:
    activation = PromocodeActivationDto(
        promocode_id=5, user_id=11, activated_at=datetime(2026, 11, 1, tzinfo=timezone.utc)
    )
    users = {7: buyer(), 11: recipient()}
    dao = SimpleNamespace(
        get_by_id=AsyncMock(return_value=gift_promo()),
        get_activations=AsyncMock(return_value=[activation]),
    )
    transaction = gift_transaction()
    details = await GetGiftDetails(
        dao,  # type: ignore[arg-type]
        SimpleNamespace(get_by_id=AsyncMock(side_effect=lambda user_id: users[user_id])),  # type: ignore[arg-type]
        SimpleNamespace(get_by_payment_id=AsyncMock(return_value=transaction)),  # type: ignore[arg-type]
    ).system(5)

    assert details is not None
    assert details.buyer is users[7]
    assert details.recipient is users[11]
    assert details.transaction is transaction
    assert details.activated_at == activation.activated_at


@pytest.mark.asyncio
async def test_admin_gift_card_ignores_ordinary_promocodes() -> None:
    ordinary = gift_promo()
    ordinary.gift_payment_id = None
    dao = SimpleNamespace(get_by_id=AsyncMock(return_value=ordinary))

    details = await GetGiftDetails(dao, SimpleNamespace(), SimpleNamespace()).system(5)  # type: ignore[arg-type]

    assert details is None


@pytest.mark.asyncio
async def test_admin_gift_list_marks_redeemed_codes() -> None:
    first, second = gift_promo(), gift_promo()
    second.id = 6
    dao = SimpleNamespace(
        get_gift_list=AsyncMock(return_value=[first, second]),
        get_gift_count=AsyncMock(return_value=2),
        get_activations_count=AsyncMock(side_effect=lambda promo_id: 1 if promo_id == 6 else 0),
    )

    result = await GetGiftList(dao).system(GetGiftListDto(limit=10))  # type: ignore[arg-type]

    assert result.total == 2
    assert result.redeemed_ids == frozenset({6})
    dao.get_gift_list.assert_awaited_once_with(limit=10, offset=0)


def _translator() -> Any:
    storage = LayeredFileStorage(
        user_translations_dir=TRANSLATIONS, default_translations_dir=TRANSLATIONS
    )
    (translator,) = storage.get_translators_for_language("ru")
    return translator


def test_new_gift_messages_render_in_russian() -> None:
    translator = _translator()

    activated = str(
        translator.get(
            "event-promocode.gift-activated",
            telegram_id=0,
            username=0,
            name="Friend",
            email="f@example.com",
            promocode_code="GIFT-AAAA-BBBB-CCCC",
            plan_name="Basic",
            gift_payment_id=str(PAYMENT_ID),
            gift_buyer_telegram_id=42,
            gift_buyer_username="alice",
            gift_buyer_name="Alice",
            gift_buyer_email="—",
        )
    )
    assert "Активация подарка" in activated
    assert "Alice" in activated and str(PAYMENT_ID) in activated

    activated_message = str(translator.get("ntf-promocode.gift-activated", expire="01.12.2026"))
    assert "Подарок активирован" in activated_message and "Alice" not in activated_message
    assert "01.12.2026" in activated_message
    purchased = str(
        translator.get("ntf-gift.purchased", code="GIFT-X", plan_name="Basic", duration="30 дней")
    )
    assert "не увидит, от кого подарок" in purchased

    details = str(
        translator.get(
            "msg-gift-details",
            code="GIFT-AAAA-BBBB-CCCC",
            status="ACTIVATED",
            activated_at="01.11.2026 10:00",
            created_at="31.10.2026 09:00",
            expires_at="31.10.2027 09:00",
            plan_name="Basic",
            plan_duration="30 дней",
            plan_traffic="∞",
            plan_devices="3",
            payment_id=str(PAYMENT_ID),
            payment_amount="300",
            payment_currency="₽",
            payment_gateway="YOOKASSA",
            payment_status="COMPLETED",
            payment_created_at="31.10.2026 08:59",
            buyer_exists=1,
            buyer_name="Alice",
            buyer_telegram_id=42,
            buyer_username="alice",
            buyer_email="alice@example.com",
            recipient_exists=1,
            recipient_name="Friend",
            recipient_telegram_id=0,
            recipient_username=0,
            recipient_email="f@example.com",
        )
    )
    assert "Alice" in details and "Friend" in details

    item = str(
        translator.get("btn-gifts.item", status="AVAILABLE", code="GIFT-X", created_at="01.11.2026")
    )
    assert item.startswith("🟢 GIFT-X")
    assert "Подарить подписку" in str(translator.get("btn-subscription.gift"))
