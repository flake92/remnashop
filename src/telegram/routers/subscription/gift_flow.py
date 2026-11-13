"""Buying a subscription as a gift in the bot.

A gift reuses the ordinary purchase dialog with ``purchase_type=GIFT``; this
module holds the few rules that differ: the buyer first chooses whether the
recipient sees their name, the list price is charged (no personal discounts),
and free or trial plans are not sold.
"""

from typing import Any, Final

from aiogram_dialog import DialogManager

from src.application.dto import PlanDto
from src.core.enums import PurchaseType

GIFT_NAMED: Final[str] = "gift_named"
GIFT_ANONYMOUS: Final[str] = "gift_anonymous"
GIFT_ANONYMOUS_KEY: Final[str] = "gift_anonymous"


def is_gift(dialog_manager: DialogManager) -> bool:
    return dialog_manager.dialog_data.get("purchase_type") == PurchaseType.GIFT


def gift_anonymous(dialog_manager: DialogManager) -> bool:
    return is_gift(dialog_manager) and bool(dialog_manager.dialog_data.get(GIFT_ANONYMOUS_KEY))


def applies_discount(dialog_manager: DialogManager, plan: PlanDto) -> bool:
    """Personal discounts are the buyer's, so a gift is always sold at list price."""
    return not plan.is_trial and not is_gift(dialog_manager)


def giftable_plans(plans: list[PlanDto]) -> list[PlanDto]:
    return [plan for plan in plans if not plan.is_trial]


def gift_getter_data(dialog_manager: DialogManager) -> dict[str, Any]:
    return {
        "is_gift": 1 if is_gift(dialog_manager) else 0,
        "gift_anonymous": 1 if gift_anonymous(dialog_manager) else 0,
    }
