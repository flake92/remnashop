from typing import cast

from adaptix import Retort
from adaptix.conversion import ConversionRetort
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession

from src.infrastructure.database.dao.referral import ReferralDaoImpl


def test_referral_dao_resolves_reward_relationship_types_at_runtime() -> None:
    dao = ReferralDaoImpl(
        session=cast(AsyncSession, object()),
        retort=Retort(),
        conversion_retort=ConversionRetort(),
        redis=cast(Redis, object()),
    )

    assert callable(dao._convert_to_reward_dto)
    assert callable(dao._convert_to_reward_list)
