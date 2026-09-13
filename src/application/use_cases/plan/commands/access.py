from dataclasses import dataclass

from loguru import logger

from src.application.common import Interactor
from src.application.common.dao import PlanDao, UserDao
from src.application.common.policy import Permission
from src.application.common.uow import UnitOfWork
from src.application.dto import PlanDto, UserDto
from src.core.exceptions import UserAlreadyAllowedError
from src.core.utils.validators import is_valid_email


@dataclass(frozen=True)
class AddAllowedUserToPlanDto:
    plan: PlanDto
    input_value: str


class AddAllowedUserToPlan(Interactor[AddAllowedUserToPlanDto, PlanDto]):
    required_permission = Permission.REMNASHOP_PLAN_EDITOR

    async def _execute(self, actor: UserDto, data: AddAllowedUserToPlanDto) -> PlanDto:
        value = data.input_value.strip()

        if "@" in value:
            if not is_valid_email(value):
                logger.warning(f"{actor.log} Invalid email format supplied")
                raise ValueError(f"Invalid email format: '{value}'")
            if value in data.plan.allowed_emails:
                logger.warning(f"{actor.log} Email is already in allowed list")
                raise UserAlreadyAllowedError(f"Email '{value}' already allowed")
            data.plan.allowed_emails.append(value)
            logger.info(f"{actor.log} Added email identity to allowed list of plan in memory")
        elif value.isdigit():
            tg_id = int(value)
            if tg_id in data.plan.allowed_telegram_ids:
                logger.warning(f"{actor.log} Telegram ID '{tg_id}' is already in allowed list")
                raise UserAlreadyAllowedError(f"Telegram ID '{tg_id}' already allowed")
            data.plan.allowed_telegram_ids.append(tg_id)
            logger.info(
                f"{actor.log} Added telegram ID '{tg_id}' to allowed list of plan in memory"
            )
        else:
            logger.warning(f"{actor.log} Invalid input for allowed user: '{value}'")
            raise ValueError(f"Must be a Telegram ID (digits) or email (contains @), got '{value}'")

        return data.plan


@dataclass(frozen=True)
class ToggleUserPlanAccessDto:
    plan_id: int
    user_id: int


class ToggleUserPlanAccess(Interactor[ToggleUserPlanAccessDto, None]):
    required_permission = Permission.USER_EDITOR

    def __init__(self, uow: UnitOfWork, plan_dao: PlanDao, user_dao: UserDao) -> None:
        self.uow = uow
        self.plan_dao = plan_dao
        self.user_dao = user_dao

    async def _execute(self, actor: UserDto, data: ToggleUserPlanAccessDto) -> None:
        async with self.uow:
            plan = await self.plan_dao.get_by_id(data.plan_id)
            if not plan:
                raise ValueError(f"Plan '{data.plan_id}' not found")

            target_user = await self.user_dao.get_by_id(data.user_id)
            if not target_user:
                raise ValueError(f"User '{data.user_id}' not found")

            currently_allowed = (
                target_user.telegram_id is not None
                and target_user.telegram_id in plan.allowed_telegram_ids
            ) or (target_user.email is not None and target_user.email in plan.allowed_emails)

            if currently_allowed:
                if (
                    target_user.telegram_id is not None
                    and target_user.telegram_id in plan.allowed_telegram_ids
                ):
                    plan.allowed_telegram_ids.remove(target_user.telegram_id)
                if target_user.email is not None and target_user.email in plan.allowed_emails:
                    plan.allowed_emails.remove(target_user.email)
                action = "Revoked"
            else:
                if target_user.telegram_id is not None:
                    plan.allowed_telegram_ids.append(target_user.telegram_id)
                elif target_user.email is not None:
                    plan.allowed_emails.append(target_user.email)
                action = "Granted"

            await self.plan_dao.update(plan)
            await self.uow.commit()

        logger.info(
            f"{actor.log} {action} access to plan '{data.plan_id}' for user '{data.user_id}'"
        )
