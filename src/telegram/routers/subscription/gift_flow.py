"""Buying a subscription as a gift in the bot.

A gift reuses the ordinary purchase dialog with ``purchase_type=GIFT``; this
module holds the rules that differ: the list price is charged (no personal
discounts) and free or trial plans are not sold. The recipient is never told
who bought the gift.
"""

from typing import Any

from aiogram_dialog import DialogManager

from src.application.dto import PlanDto
from src.core.enums import PurchaseType


def is_gift(dialog_manager: DialogManager) -> bool:
    return dialog_manager.dialog_data.get("purchase_type") == PurchaseType.GIFT


def applies_discount(dialog_manager: DialogManager, plan: PlanDto) -> bool:
    """Personal discounts are the buyer's, so a gift is always sold at list price."""
    return not plan.is_trial and not is_gift(dialog_manager)


def giftable_plans(plans: list[PlanDto]) -> list[PlanDto]:
    return [plan for plan in plans if not plan.is_trial]


def gift_getter_data(dialog_manager: DialogManager) -> dict[str, Any]:
    return {
        "is_gift": 1 if is_gift(dialog_manager) else 0,
    }
