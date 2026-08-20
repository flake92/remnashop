from datetime import datetime, timedelta
from typing import Any, Optional, cast

from adaptix import Retort
from adaptix.conversion import ConversionRetort
from loguru import logger
from redis.asyncio import Redis
from sqlalchemy import Numeric, and_, case, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from src.application.common.dao import ReferralDao
from src.application.dto import (
    ReferralDto,
    ReferralRewardBackfillAuditDto,
    ReferralRewardDto,
    ReferralStatisticsDto,
    UserReferralStatsDto,
)
from src.core.enums import (
    ReferralAccrualStrategy,
    ReferralLevel,
    ReferralRewardState,
    ReferralRewardType,
    TransactionFulfillmentStatus,
    TransactionStatus,
)
from src.core.utils.time import datetime_now
from src.infrastructure.database.models import (
    Referral,
    ReferralReward,
    ReferralRewardBackfillAudit,
    ReferralRewardResolution,
    Transaction,
)
from src.infrastructure.database.models.user import User


class ReferralDaoImpl(ReferralDao):
    def __init__(
        self,
        session: AsyncSession,
        retort: Retort,
        conversion_retort: ConversionRetort,
        redis: Redis,
    ) -> None:
        self.session = session
        self.retort = retort
        self.conversion_retort = conversion_retort
        self.redis = redis

        self._convert_to_referral_dto = self.conversion_retort.get_converter(Referral, ReferralDto)
        self._convert_to_referral_list = self.conversion_retort.get_converter(
            list[Referral],
            list[ReferralDto],
        )
        self._convert_to_reward_dto = self.conversion_retort.get_converter(
            ReferralReward,
            ReferralRewardDto,
        )
        self._convert_to_reward_list = self.conversion_retort.get_converter(
            list[ReferralReward],
            list[ReferralRewardDto],
        )

    async def create_referral(self, referral: ReferralDto) -> ReferralDto:
        db_referral = Referral(
            referrer_id=referral.referrer.id,
            referred_id=referral.referred.id,
            level=referral.level,
        )

        self.session.add(db_referral)
        await self.session.flush()
        await self.session.refresh(db_referral, attribute_names=["referrer", "referred"])

        logger.debug(
            f"Created referral: referrer id='{referral.referrer.id}' "
            f"invited referred id='{referral.referred.id}'"
        )
        return self._convert_to_referral_dto(db_referral)

    @staticmethod
    def _backfill_audit_to_dto(
        audit: ReferralRewardBackfillAudit,
    ) -> ReferralRewardBackfillAuditDto:
        return ReferralRewardBackfillAuditDto(
            id=audit.id,
            request_hash=audit.request_hash,
            status=audit.status,
            operator_identity=audit.operator_identity,
            operator_reference=audit.operator_reference,
            reason=audit.reason,
            source_transaction_ids=list(audit.source_transaction_ids),
            config_snapshot=dict(audit.config_snapshot),
            preview_snapshot=dict(audit.preview_snapshot),
            applied_at=audit.applied_at,
            created_at=audit.created_at,
            updated_at=audit.updated_at,
        )

    async def get_rewards_by_source_transaction(
        self,
        source_transaction_id: int,
    ) -> list[ReferralRewardDto]:
        rewards = cast(
            list,
            (
                await self.session.scalars(
                    select(ReferralReward)
                    .where(
                        ReferralReward.source_transaction_id == source_transaction_id,
                    )
                    .order_by(ReferralReward.level, ReferralReward.id)
                )
            ).all(),
        )
        return self._convert_to_reward_list(rewards)

    async def has_legacy_ambiguous_reward(
        self,
        *,
        referral_ids: list[int],
        recipient_user_ids: list[int],
    ) -> bool:
        if not referral_ids and not recipient_user_ids:
            return False
        return bool(
            await self.session.scalar(
                select(ReferralReward.id)
                .where(
                    ReferralReward.source_transaction_id.is_(None),
                    or_(
                        ReferralReward.referral_id.in_(referral_ids),
                        ReferralReward.user_id.in_(recipient_user_ids),
                    ),
                )
                .limit(1)
            )
        )

    async def acquire_historical_backfill_lock(self) -> None:
        # Serialize operator backfills across distinct previews. These are rare,
        # high-impact operations and deterministic global ordering is preferable
        # to deadlocks across overlapping referral chains.
        await self.session.scalar(select(func.pg_advisory_xact_lock(738_341_552)))

    async def create_or_get_backfill_preview(
        self,
        *,
        request_hash: str,
        operator_identity: str,
        operator_reference: str,
        reason: str,
        source_transaction_ids: list[int],
        config_snapshot: dict[str, Any],
        preview_snapshot: dict[str, Any],
    ) -> ReferralRewardBackfillAuditDto:
        preview_id = await self.session.scalar(
            insert(ReferralRewardBackfillAudit)
            .values(
                request_hash=request_hash,
                status="PREVIEWED",
                operator_identity=operator_identity,
                operator_reference=operator_reference,
                reason=reason,
                source_transaction_ids=source_transaction_ids,
                config_snapshot=config_snapshot,
                preview_snapshot=preview_snapshot,
            )
            .on_conflict_do_nothing(index_elements=["request_hash"])
            .returning(ReferralRewardBackfillAudit.id)
        )
        audit = (
            await self.session.get(ReferralRewardBackfillAudit, preview_id)
            if preview_id is not None
            else await self.session.scalar(
                select(ReferralRewardBackfillAudit).where(
                    ReferralRewardBackfillAudit.request_hash == request_hash
                )
            )
        )
        if audit is None:
            raise RuntimeError("Historical referral backfill preview disappeared")
        return self._backfill_audit_to_dto(audit)

    async def get_backfill_preview_for_update(
        self,
        preview_id: int,
    ) -> Optional[ReferralRewardBackfillAuditDto]:
        audit = await self.session.scalar(
            select(ReferralRewardBackfillAudit)
            .where(ReferralRewardBackfillAudit.id == preview_id)
            .with_for_update()
        )
        return self._backfill_audit_to_dto(audit) if audit is not None else None

    async def mark_backfill_preview_applied(self, preview_id: int) -> bool:
        result = await self.session.execute(
            update(ReferralRewardBackfillAudit)
            .where(
                ReferralRewardBackfillAudit.id == preview_id,
                ReferralRewardBackfillAudit.status == "PREVIEWED",
            )
            .values(status="APPLIED", applied_at=datetime_now())
        )
        return bool(getattr(result, "rowcount", 0))

    async def get_by_referred_id(self, referred_id: int) -> Optional[ReferralDto]:
        stmt = (
            select(Referral)
            .where(Referral.referred_id == referred_id)
            .options(selectinload(Referral.referrer), selectinload(Referral.referred))
        )
        db_referral = await self.session.scalar(stmt)

        if db_referral:
            logger.debug(f"Referrer for user_id '{referred_id}' found")
            return self._convert_to_referral_dto(db_referral)

        logger.debug(f"Referrer for user_id '{referred_id}' not found")
        return None

    async def get_referrals_count(self, referrer_id: int) -> int:
        stmt = select(func.count()).select_from(Referral).where(Referral.referrer_id == referrer_id)
        count = await self.session.scalar(stmt) or 0

        logger.debug(f"User_id '{referrer_id}' has '{count}' referrals")
        return count

    async def get_referrals_list(
        self,
        referrer_id: int,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ReferralDto]:
        stmt = (
            select(Referral)
            .where(Referral.referrer_id == referrer_id)
            .options(selectinload(Referral.referred))
            .limit(limit)
            .offset(offset)
            .order_by(Referral.created_at.desc())
        )
        result = await self.session.scalars(stmt)
        db_referrals = cast(list, result.all())

        logger.debug(
            f"Retrieved '{len(db_referrals)}' referrals for user_id '{referrer_id}' "
            f"with limit '{limit}' and offset '{offset}'"
        )
        return self._convert_to_referral_list(db_referrals)

    async def create_reward(
        self,
        reward: ReferralRewardDto,
        referral_id: int,
    ) -> Optional[ReferralRewardDto]:
        reward_data = self.retort.dump(reward)
        reward_data.pop("id", None)
        stmt = (
            insert(ReferralReward)
            .values(**reward_data, referral_id=referral_id)
            .on_conflict_do_nothing()
            .returning(ReferralReward.id)
        )
        reward_id = await self.session.scalar(stmt)

        if reward_id is None:
            db_reward = await self.session.scalar(
                select(ReferralReward).where(
                    ReferralReward.source_transaction_id == reward.source_transaction_id,
                    ReferralReward.origin_referral_id == reward.origin_referral_id,
                    ReferralReward.level == reward.level,
                )
            )
            if db_reward is None:
                # Never return an unrelated historical reward. A conflict with the
                # ON_FIRST claim marker means another successful source won. The
                # marker intentionally survives retry/manual/terminal transitions.
                if reward.accrual_strategy_snapshot == ReferralAccrualStrategy.ON_FIRST_PAYMENT:
                    winner = await self.session.scalar(
                        select(ReferralReward.id).where(
                            ReferralReward.origin_referral_id == reward.origin_referral_id,
                            ReferralReward.level == reward.level,
                            ReferralReward.accrual_strategy
                            == ReferralAccrualStrategy.ON_FIRST_PAYMENT,
                        )
                    )
                    if winner is not None:
                        return None
                raise RuntimeError("Referral reward conflict could not be resolved exactly")
            return self._convert_to_reward_dto(db_reward)

        db_reward = await self.session.get(ReferralReward, reward_id)
        if db_reward is None:
            raise RuntimeError("Created referral reward disappeared")

        logger.debug(f"Created reward amount '{reward.amount}' for referral ID '{referral_id}'")
        return self._convert_to_reward_dto(db_reward)

    async def get_reward_by_id(self, reward_id: int) -> Optional[ReferralRewardDto]:
        reward = await self.session.get(ReferralReward, reward_id)
        return self._convert_to_reward_dto(reward) if reward is not None else None

    async def lock_manual_reward_source_status(
        self,
        reward_id: int,
    ) -> Optional[TransactionStatus]:
        return cast(
            Optional[TransactionStatus],
            await self.session.scalar(
                select(Transaction.status)
                .join(
                    ReferralReward,
                    ReferralReward.source_transaction_id == Transaction.id,
                )
                .where(ReferralReward.id == reward_id)
                .with_for_update(of=Transaction)
            ),
        )

    async def claim_pending_rewards(
        self,
        *,
        token_hash: str,
        lease_for: timedelta,
        limit: int,
    ) -> list[ReferralRewardDto]:
        now = datetime_now()

        non_issuable_sources = select(Transaction.id).where(
            Transaction.fulfillment_status == TransactionFulfillmentStatus.MANUAL_REQUIRED,
        )
        await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.source_transaction_id.in_(non_issuable_sources),
                ReferralReward.state.in_(
                    (
                        ReferralRewardState.PENDING,
                        ReferralRewardState.RETRY_WAITING,
                    )
                ),
            )
            .values(
                state=ReferralRewardState.MANUAL_REQUIRED,
                manual_incident_version=ReferralReward.manual_incident_version + 1,
                manual_cause="SOURCE_FULFILLMENT_NOT_PROVEN",
                processing_token_hash=None,
                processing_lease_expires_at=None,
                next_attempt_at=None,
                last_error="SOURCE_FULFILLMENT_NOT_PROVEN",
                manual_alerted_at=None,
            )
        )

        # Once an absolute EXTRA_DAYS target was persisted, a dead worker may have
        # reached Remnawave. Replaying the additive operation is ambiguous, so fence it
        # for an operator instead of reclaiming it.
        await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.state == ReferralRewardState.PROCESSING,
                ReferralReward.type == ReferralRewardType.EXTRA_DAYS,
                ReferralReward.target_expire_at.is_not(None),
                ReferralReward.processing_lease_expires_at <= now,
            )
            .values(
                state=ReferralRewardState.MANUAL_REQUIRED,
                manual_incident_version=ReferralReward.manual_incident_version + 1,
                manual_cause="EXTRA_DAYS_AMBIGUOUS_LEASE_EXPIRED",
                processing_token_hash=None,
                processing_lease_expires_at=None,
                next_attempt_at=None,
                last_error="EXTRA_DAYS_AMBIGUOUS_LEASE_EXPIRED",
                manual_alerted_at=None,
            )
        )

        # A crashed POINTS grant, or EXTRA_DAYS grant that had not persisted an
        # external target yet, is safe to retry. Normalizing first also releases the
        # per-recipient PROCESSING fence.
        await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.state == ReferralRewardState.PROCESSING,
                ReferralReward.processing_lease_expires_at <= now,
            )
            .values(
                state=ReferralRewardState.RETRY_WAITING,
                processing_token_hash=None,
                processing_lease_expires_at=None,
                next_attempt_at=now,
                last_error="REWARD_PROCESSING_LEASE_EXPIRED",
            )
        )

        refunded_sources = select(Transaction.id).where(
            Transaction.status == TransactionStatus.REFUNDED
        )
        resolved_current_refund_incident = select(ReferralRewardResolution.id).where(
            ReferralRewardResolution.reward_id == ReferralReward.id,
            ReferralRewardResolution.incident_version == ReferralReward.manual_incident_version,
            ReferralRewardResolution.source_status == TransactionStatus.REFUNDED.value,
        )
        # A reward that never left the durable pending states has no side effect
        # to claw back. Refund terminalizes it without reopening ON_FIRST.
        await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.source_transaction_id.in_(refunded_sources),
                ReferralReward.state.in_(
                    (
                        ReferralRewardState.PENDING,
                        ReferralRewardState.RETRY_WAITING,
                    )
                ),
            )
            .values(
                state=ReferralRewardState.SUPERSEDED,
                next_attempt_at=None,
                last_error="SOURCE_REFUNDED_BEFORE_REWARD_ISSUANCE",
            )
        )
        # PROCESSING is ambiguous and ISSUED needs an explicit operator clawback
        # decision. Preserve is_issued/issued_at on issued history while surfacing it.
        await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.source_transaction_id.in_(refunded_sources),
                ReferralReward.state == ReferralRewardState.PROCESSING,
            )
            .values(
                state=ReferralRewardState.MANUAL_REQUIRED,
                manual_incident_version=ReferralReward.manual_incident_version + 1,
                manual_cause="SOURCE_REFUNDED_DURING_REWARD_ISSUANCE",
                processing_token_hash=None,
                processing_lease_expires_at=None,
                next_attempt_at=None,
                last_error="SOURCE_REFUNDED_DURING_REWARD_ISSUANCE",
                manual_alerted_at=None,
                refund_detected_at=now,
            )
        )
        await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.source_transaction_id.in_(refunded_sources),
                ReferralReward.state == ReferralRewardState.ISSUED,
                ~resolved_current_refund_incident.exists(),
            )
            .values(
                state=ReferralRewardState.MANUAL_REQUIRED,
                manual_incident_version=ReferralReward.manual_incident_version + 1,
                manual_cause="SOURCE_REFUNDED_AFTER_REWARD_ISSUANCE",
                next_attempt_at=None,
                manual_alerted_at=None,
                refund_detected_at=now,
            )
        )
        # A reward may already be MANUAL_REQUIRED for another ambiguous cause.
        # Surface the later refund without destroying that original diagnosis.
        await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.source_transaction_id.in_(refunded_sources),
                ReferralReward.state == ReferralRewardState.MANUAL_REQUIRED,
                or_(
                    ReferralReward.manual_cause.is_(None),
                    ReferralReward.manual_cause.notin_(
                        (
                            "SOURCE_REFUNDED_DURING_REWARD_ISSUANCE",
                            "SOURCE_REFUNDED_AFTER_REWARD_ISSUANCE",
                        )
                    ),
                ),
            )
            .values(
                manual_incident_version=ReferralReward.manual_incident_version + 1,
                manual_cause="SOURCE_REFUNDED_DURING_REWARD_ISSUANCE",
                refund_detected_at=now,
                manual_alerted_at=None,
            )
        )

        source_transaction = aliased(Transaction, name="reward_source_transaction")
        earlier_transaction = aliased(Transaction, name="earlier_successful_transaction")
        supersede_source = aliased(Transaction, name="superseded_reward_source")
        supersede_earlier = aliased(Transaction, name="superseding_earlier_transaction")
        active_winner = aliased(ReferralReward, name="active_first_payment_winner")

        earlier_success_exists = select(earlier_transaction.id).where(
            earlier_transaction.user_id == source_transaction.user_id,
            earlier_transaction.status.in_(
                (TransactionStatus.COMPLETED, TransactionStatus.REFUNDED)
            ),
            earlier_transaction.fulfillment_status == TransactionFulfillmentStatus.SUCCEEDED,
            earlier_transaction.is_test.is_(False),
            earlier_transaction.pricing["final_amount"].astext.cast(Numeric) > 0,
            earlier_transaction.plan_snapshot["is_trial"].astext == "false",
            or_(
                earlier_transaction.fulfillment_completed_at
                < source_transaction.fulfillment_completed_at,
                and_(
                    earlier_transaction.fulfillment_completed_at
                    == source_transaction.fulfillment_completed_at,
                    earlier_transaction.id < source_transaction.id,
                ),
            ),
        )
        active_winner_exists = select(active_winner.id).where(
            active_winner.origin_referral_id == ReferralReward.origin_referral_id,
            active_winner.level == ReferralReward.level,
            active_winner.accrual_strategy == ReferralAccrualStrategy.ON_FIRST_PAYMENT,
            active_winner.id != ReferralReward.id,
        )

        earlier_success_for_reward = (
            select(supersede_earlier.id)
            .select_from(supersede_source)
            .join(
                supersede_earlier,
                supersede_earlier.user_id == supersede_source.user_id,
            )
            .where(
                supersede_source.id == ReferralReward.source_transaction_id,
                supersede_earlier.status.in_(
                    (TransactionStatus.COMPLETED, TransactionStatus.REFUNDED)
                ),
                supersede_earlier.fulfillment_status == TransactionFulfillmentStatus.SUCCEEDED,
                supersede_earlier.is_test.is_(False),
                supersede_earlier.pricing["final_amount"].astext.cast(Numeric) > 0,
                supersede_earlier.plan_snapshot["is_trial"].astext == "false",
                or_(
                    supersede_earlier.fulfillment_completed_at
                    < supersede_source.fulfillment_completed_at,
                    and_(
                        supersede_earlier.fulfillment_completed_at
                        == supersede_source.fulfillment_completed_at,
                        supersede_earlier.id < supersede_source.id,
                    ),
                ),
            )
        )

        # A source cannot remain pending forever merely because its first eligible
        # paid transaction predates referral attribution or enabled settings.
        await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.accrual_strategy_snapshot
                == ReferralAccrualStrategy.ON_FIRST_PAYMENT,
                ReferralReward.accrual_strategy.is_(None),
                ReferralReward.state.in_(
                    (
                        ReferralRewardState.PENDING,
                        ReferralRewardState.RETRY_WAITING,
                    )
                ),
                earlier_success_for_reward.exists(),
            )
            .values(
                state=ReferralRewardState.SUPERSEDED,
                next_attempt_at=None,
                last_error="FIRST_PAYMENT_EARLIER_SUCCESS",
            )
        )

        # Once a winner has claimed the ON_FIRST marker, every other durable
        # candidate for the same direct referral and level is terminal history.
        await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.accrual_strategy_snapshot
                == ReferralAccrualStrategy.ON_FIRST_PAYMENT,
                ReferralReward.accrual_strategy.is_(None),
                ReferralReward.state.in_(
                    (
                        ReferralRewardState.PENDING,
                        ReferralRewardState.RETRY_WAITING,
                    )
                ),
                active_winner_exists.exists(),
            )
            .values(
                state=ReferralRewardState.SUPERSEDED,
                next_attempt_at=None,
                last_error="FIRST_PAYMENT_SUPERSEDED",
            )
        )
        due_condition = or_(
            ReferralReward.state == ReferralRewardState.PENDING,
            and_(
                ReferralReward.state == ReferralRewardState.RETRY_WAITING,
                or_(
                    ReferralReward.next_attempt_at.is_(None),
                    ReferralReward.next_attempt_at <= now,
                ),
            ),
        )
        source_condition = and_(
            source_transaction.status == TransactionStatus.COMPLETED,
            source_transaction.fulfillment_status == TransactionFulfillmentStatus.SUCCEEDED,
            source_transaction.is_test.is_(False),
            source_transaction.pricing["final_amount"].astext.cast(Numeric) > 0,
            source_transaction.plan_snapshot["is_trial"].astext == "false",
        )
        strategy_condition = or_(
            ReferralReward.accrual_strategy_snapshot != ReferralAccrualStrategy.ON_FIRST_PAYMENT,
            and_(
                ReferralReward.accrual_strategy_snapshot
                == ReferralAccrualStrategy.ON_FIRST_PAYMENT,
                ~earlier_success_exists.exists(),
                ~active_winner_exists.exists(),
            ),
        )

        due_for_user = (
            select(ReferralReward.id)
            .join(
                source_transaction,
                source_transaction.id == ReferralReward.source_transaction_id,
            )
            .where(
                ReferralReward.user_id == User.id,
                due_condition,
                source_condition,
                strategy_condition,
            )
        )
        active_for_user = select(ReferralReward.id).where(
            ReferralReward.user_id == User.id,
            ReferralReward.state == ReferralRewardState.PROCESSING,
        )
        # User rows are the common lock shared by all rewards for a recipient. After
        # commit, the durable PROCESSING row (plus the partial unique index) keeps the
        # recipient serialized across the external EXTRA_DAYS call.
        candidate_user_ids = (
            select(User.id)
            .where(due_for_user.exists(), ~active_for_user.exists())
            .order_by(User.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        user_ids = list((await self.session.scalars(candidate_user_ids)).all())
        ids: list[int] = []
        for user_id in user_ids:
            reward_id = await self.session.scalar(
                select(ReferralReward.id)
                .join(
                    source_transaction,
                    source_transaction.id == ReferralReward.source_transaction_id,
                )
                .where(
                    ReferralReward.user_id == user_id,
                    due_condition,
                    source_condition,
                    strategy_condition,
                )
                .order_by(
                    ReferralReward.next_attempt_at.asc().nullsfirst(),
                    ReferralReward.id,
                )
                .limit(1)
            )
            if reward_id is not None:
                ids.append(reward_id)

        if not ids:
            return []

        stmt = (
            update(ReferralReward)
            .where(ReferralReward.id.in_(ids))
            .values(
                state=ReferralRewardState.PROCESSING,
                accrual_strategy=case(
                    (
                        ReferralReward.accrual_strategy_snapshot
                        == ReferralAccrualStrategy.ON_FIRST_PAYMENT,
                        ReferralAccrualStrategy.ON_FIRST_PAYMENT,
                    ),
                    else_=ReferralReward.accrual_strategy_snapshot,
                ),
                processing_token_hash=token_hash,
                processing_lease_expires_at=now + lease_for,
                attempt_count=ReferralReward.attempt_count + 1,
                next_attempt_at=None,
                last_error=None,
            )
            .returning(ReferralReward)
        )
        rewards = cast(list, (await self.session.scalars(stmt)).all())
        for reward in rewards:
            if reward.accrual_strategy_snapshot == ReferralAccrualStrategy.ON_FIRST_PAYMENT:
                await self.session.execute(
                    update(ReferralReward)
                    .where(
                        ReferralReward.id != reward.id,
                        ReferralReward.origin_referral_id == reward.origin_referral_id,
                        ReferralReward.level == reward.level,
                        ReferralReward.accrual_strategy_snapshot
                        == ReferralAccrualStrategy.ON_FIRST_PAYMENT,
                        ReferralReward.accrual_strategy.is_(None),
                        ReferralReward.state.in_(
                            (
                                ReferralRewardState.PENDING,
                                ReferralRewardState.RETRY_WAITING,
                            )
                        ),
                    )
                    .values(
                        state=ReferralRewardState.SUPERSEDED,
                        next_attempt_at=None,
                        last_error="FIRST_PAYMENT_SUPERSEDED",
                    )
                )
        return self._convert_to_reward_list(rewards)

    async def defer_reward(
        self,
        reward_id: int,
        *,
        token_hash: str,
        retry_after: timedelta,
        error_code: str,
    ) -> bool:
        result = await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.id == reward_id,
                ReferralReward.state == ReferralRewardState.PROCESSING,
                ReferralReward.processing_token_hash == token_hash,
                ReferralReward.target_expire_at.is_(None),
            )
            .values(
                state=ReferralRewardState.RETRY_WAITING,
                processing_token_hash=None,
                processing_lease_expires_at=None,
                next_attempt_at=datetime_now() + retry_after,
                last_error=error_code[:64],
            )
        )
        return bool(getattr(result, "rowcount", 0))

    async def mark_reward_manual_required(
        self,
        reward_id: int,
        *,
        token_hash: str,
        error_code: str,
    ) -> bool:
        result = await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.id == reward_id,
                ReferralReward.state == ReferralRewardState.PROCESSING,
                ReferralReward.processing_token_hash == token_hash,
            )
            .values(
                state=ReferralRewardState.MANUAL_REQUIRED,
                manual_incident_version=ReferralReward.manual_incident_version + 1,
                manual_cause=error_code[:64],
                processing_token_hash=None,
                processing_lease_expires_at=None,
                next_attempt_at=None,
                last_error=error_code[:64],
                manual_alerted_at=None,
            )
        )
        return bool(getattr(result, "rowcount", 0))

    async def lock_reward_source_if_eligible(
        self,
        reward_id: int,
        *,
        token_hash: str,
    ) -> bool:
        source_id = await self.session.scalar(
            select(Transaction.id)
            .join(
                ReferralReward,
                ReferralReward.source_transaction_id == Transaction.id,
            )
            .where(
                ReferralReward.id == reward_id,
                ReferralReward.state == ReferralRewardState.PROCESSING,
                ReferralReward.processing_token_hash == token_hash,
                Transaction.status == TransactionStatus.COMPLETED,
                Transaction.fulfillment_status == TransactionFulfillmentStatus.SUCCEEDED,
            )
            .with_for_update(of=Transaction)
        )
        return source_id is not None

    async def cancel_claimed_reward(
        self,
        reward_id: int,
        *,
        token_hash: str,
        error_code: str,
    ) -> bool:
        result = await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.id == reward_id,
                ReferralReward.state == ReferralRewardState.PROCESSING,
                ReferralReward.processing_token_hash == token_hash,
            )
            .values(
                state=ReferralRewardState.SUPERSEDED,
                processing_token_hash=None,
                processing_lease_expires_at=None,
                next_attempt_at=None,
                last_error=error_code[:64],
            )
        )
        return bool(getattr(result, "rowcount", 0))

    async def set_extra_days_target(
        self,
        reward_id: int,
        *,
        token_hash: str,
        subscription_id: int,
        baseline_expire_at: datetime,
        target_expire_at: datetime,
    ) -> bool:
        result = await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.id == reward_id,
                ReferralReward.state == ReferralRewardState.PROCESSING,
                ReferralReward.processing_token_hash == token_hash,
                ReferralReward.target_expire_at.is_(None),
            )
            .values(
                target_subscription_id=subscription_id,
                baseline_expire_at=baseline_expire_at,
                target_expire_at=target_expire_at,
            )
        )
        return bool(getattr(result, "rowcount", 0))

    async def issue_points_reward(
        self,
        reward_id: int,
        *,
        user_id: int,
        amount: int,
        token_hash: str,
    ) -> bool:
        now = datetime_now()
        reward_id_result = await self.session.scalar(
            update(ReferralReward)
            .where(
                ReferralReward.id == reward_id,
                ReferralReward.user_id == user_id,
                ReferralReward.state == ReferralRewardState.PROCESSING,
                ReferralReward.processing_token_hash == token_hash,
            )
            .values(
                is_issued=True,
                state=ReferralRewardState.ISSUED,
                issued_at=now,
                processing_token_hash=None,
                processing_lease_expires_at=None,
                next_attempt_at=None,
                last_error=None,
            )
            .returning(ReferralReward.id)
        )
        if reward_id_result is None:
            return False
        user_result = await self.session.execute(
            update(User).where(User.id == user_id).values(points=User.points + amount)
        )
        if not getattr(user_result, "rowcount", 0):
            raise RuntimeError(f"Referral reward recipient '{user_id}' disappeared")
        return True

    async def finish_extra_days_reward(
        self,
        reward_id: int,
        *,
        token_hash: str,
    ) -> bool:
        result = await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.id == reward_id,
                ReferralReward.state == ReferralRewardState.PROCESSING,
                ReferralReward.processing_token_hash == token_hash,
                ReferralReward.target_expire_at.is_not(None),
            )
            .values(
                is_issued=True,
                state=ReferralRewardState.ISSUED,
                issued_at=datetime_now(),
                processing_token_hash=None,
                processing_lease_expires_at=None,
                next_attempt_at=None,
                last_error=None,
            )
        )
        return bool(getattr(result, "rowcount", 0))

    async def get_reward_referred_name(self, reward_id: int) -> str:
        stmt = (
            select(User.name)
            .join(Transaction, Transaction.user_id == User.id)
            .join(
                ReferralReward,
                ReferralReward.source_transaction_id == Transaction.id,
            )
            .where(ReferralReward.id == reward_id)
        )
        return await self.session.scalar(stmt) or "Referral"

    async def get_manual_required_rewards(
        self,
        *,
        limit: int = 100,
    ) -> list[ReferralRewardDto]:
        rows = cast(
            list,
            (
                await self.session.scalars(
                    select(ReferralReward)
                    .where(ReferralReward.state == ReferralRewardState.MANUAL_REQUIRED)
                    .order_by(ReferralReward.updated_at.asc(), ReferralReward.id)
                    .limit(limit)
                )
            ).all(),
        )
        return self._convert_to_reward_list(rows)

    async def claim_manual_required_rewards_for_alert(
        self,
        *,
        limit: int = 100,
    ) -> list[ReferralRewardDto]:
        candidate_ids = (
            select(ReferralReward.id)
            .where(
                ReferralReward.state == ReferralRewardState.MANUAL_REQUIRED,
                ReferralReward.manual_alerted_at.is_(None),
            )
            .order_by(ReferralReward.updated_at, ReferralReward.id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        ids = list((await self.session.scalars(candidate_ids)).all())
        if not ids:
            return []
        rows = cast(
            list,
            (
                await self.session.scalars(
                    select(ReferralReward)
                    .where(ReferralReward.id.in_(ids))
                    .order_by(ReferralReward.updated_at, ReferralReward.id)
                )
            ).all(),
        )
        return self._convert_to_reward_list(rows)

    async def mark_manual_rewards_alerted(self, reward_ids: list[int]) -> None:
        if not reward_ids:
            return
        await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.id.in_(reward_ids),
                ReferralReward.state == ReferralRewardState.MANUAL_REQUIRED,
                ReferralReward.manual_alerted_at.is_(None),
            )
            .values(manual_alerted_at=datetime_now())
        )

    async def resolve_manual_reward(
        self,
        reward_id: int,
        *,
        expected_version: int,
        confirm_issued: bool,
        operator_reference: str,
        resolved_by: str,
        reason: str,
        allow_drift: bool = False,
        observed_subscription_id: Optional[int] = None,
        observed_remote_uuid: Optional[str] = None,
        observed_expire_at: Optional[datetime] = None,
        source_status: Optional[TransactionStatus] = None,
    ) -> bool:
        reward = await self.session.scalar(
            select(ReferralReward).where(ReferralReward.id == reward_id).with_for_update()
        )
        if reward is None:
            return False

        decision = "CONFIRM_ISSUED" if confirm_issued else "CANCEL"
        existing = await self.session.scalar(
            select(ReferralRewardResolution).where(
                ReferralRewardResolution.reward_id == reward_id,
                ReferralRewardResolution.incident_version == expected_version,
            )
        )
        if existing is not None:
            return bool(
                existing.decision == decision
                and existing.operator_reference == operator_reference
                and existing.resolved_by == resolved_by
                and existing.reason == reason
                and existing.allow_drift == allow_drift
            )
        if reward.state != ReferralRewardState.MANUAL_REQUIRED:
            return False
        if reward.manual_incident_version != expected_version:
            return False
        if (
            confirm_issued
            and reward.accrual_strategy_snapshot == ReferralAccrualStrategy.ON_FIRST_PAYMENT
            and reward.accrual_strategy != ReferralAccrualStrategy.ON_FIRST_PAYMENT
        ):
            return False

        self.session.add(
            ReferralRewardResolution(
                reward_id=reward_id,
                incident_version=expected_version,
                decision=decision,
                operator_reference=operator_reference,
                resolved_by=resolved_by,
                reason=reason,
                allow_drift=allow_drift,
                observed_subscription_id=observed_subscription_id,
                observed_remote_uuid=observed_remote_uuid,
                observed_expire_at=observed_expire_at,
                source_status=(source_status.value if source_status is not None else None),
            )
        )

        values: dict[str, object]
        if confirm_issued:
            values = {
                "state": ReferralRewardState.ISSUED,
                "is_issued": True,
                "issued_at": func.coalesce(
                    ReferralReward.issued_at,
                    datetime_now(),
                ),
            }
        else:
            values = {
                "state": ReferralRewardState.SUPERSEDED,
                "is_issued": False,
            }
        result = await self.session.execute(
            update(ReferralReward)
            .where(
                ReferralReward.id == reward_id,
                ReferralReward.state == ReferralRewardState.MANUAL_REQUIRED,
                ReferralReward.manual_incident_version == expected_version,
            )
            .values(
                **values,
                processing_token_hash=None,
                processing_lease_expires_at=None,
                next_attempt_at=None,
            )
        )
        return bool(getattr(result, "rowcount", 0))

    async def manual_resolution_match(
        self,
        reward_id: int,
        *,
        expected_version: int,
        confirm_issued: bool,
        operator_reference: str,
        resolved_by: str,
        reason: str,
        allow_drift: bool = False,
    ) -> Optional[bool]:
        existing = await self.session.scalar(
            select(ReferralRewardResolution).where(
                ReferralRewardResolution.reward_id == reward_id,
                ReferralRewardResolution.incident_version == expected_version,
            )
        )
        if existing is None:
            return None
        decision = "CONFIRM_ISSUED" if confirm_issued else "CANCEL"
        return bool(
            existing.decision == decision
            and existing.operator_reference == operator_reference
            and existing.resolved_by == resolved_by
            and existing.reason == reason
            and existing.allow_drift == allow_drift
        )

    async def get_referral_chain(
        self,
        referred_id: int,
    ) -> tuple[Optional[ReferralDto], Optional[ReferralDto]]:
        first_level = await self.get_by_referred_id(referred_id)
        if not first_level:
            return None, None

        second_level = await self.get_by_referred_id(first_level.referrer.id)

        logger.debug(
            f"Referral chain for user_id '{referred_id}': "
            f"level 1 referrer id='{first_level.referrer.id}', "
            f"level 2 referrer id='{second_level.referrer.id if second_level else 'none'}'"
        )

        return first_level, second_level

    async def lock_referral_attribution(
        self,
        referred_id: int,
        referrer_ids: tuple[int, ...] = (),
    ) -> None:
        # User merge takes the same rows in ascending order before rewriting
        # attribution. Lock the payer and every L1/L2 recipient deterministically,
        # then lock their attribution rows. The caller re-reads and validates the
        # chain after this fence before creating any durable intent.
        participant_ids = tuple(sorted({referred_id, *referrer_ids}))
        await self.session.execute(
            select(User.id).where(User.id.in_(participant_ids)).order_by(User.id).with_for_update()
        )
        await self.session.execute(
            select(Referral.id)
            .where(Referral.referred_id.in_(participant_ids))
            .order_by(Referral.id)
            .with_for_update()
        )

    async def get_stats(self) -> ReferralStatisticsDto:
        stmt = select(
            func.count().label("total_referrals"),
            func.sum(case((Referral.level == ReferralLevel.FIRST, 1), else_=0)).label(
                "level_1_count"
            ),
            func.sum(case((Referral.level == ReferralLevel.SECOND, 1), else_=0)).label(
                "level_2_count"
            ),
            func.count(func.distinct(Referral.referrer_id)).label("unique_referrers"),
        )

        rewards_stmt = select(
            func.sum(case((ReferralReward.is_issued.is_(True), 1), else_=0)).label(
                "total_rewards_issued"
            ),
            func.sum(
                case(
                    (
                        and_(
                            ReferralReward.is_issued.is_(True),
                            ReferralReward.type == ReferralRewardType.POINTS,
                        ),
                        ReferralReward.amount,
                    ),
                    else_=0,
                )
            ).label("total_points_issued"),
            func.sum(
                case(
                    (
                        and_(
                            ReferralReward.is_issued.is_(True),
                            ReferralReward.type == ReferralRewardType.EXTRA_DAYS,
                        ),
                        ReferralReward.amount,
                    ),
                    else_=0,
                )
            ).label("total_days_issued"),
        )

        top_referrer_stmt = (
            select(
                Referral.referrer_id,
                func.count().label("referrals_count"),
            )
            .group_by(Referral.referrer_id)
            .order_by(func.count().desc())
            .limit(1)
        )

        referral_row = (await self.session.execute(stmt)).mappings().one()
        reward_row = (await self.session.execute(rewards_stmt)).mappings().one()
        top_referrer_row = (await self.session.execute(top_referrer_stmt)).mappings().first()

        logger.debug("Referral stats fetched")
        return ReferralStatisticsDto(
            total_referrals=int(referral_row["total_referrals"] or 0),
            level_1_count=int(referral_row["level_1_count"] or 0),
            level_2_count=int(referral_row["level_2_count"] or 0),
            unique_referrers=int(referral_row["unique_referrers"] or 0),
            total_rewards_issued=int(reward_row["total_rewards_issued"] or 0),
            total_points_issued=int(reward_row["total_points_issued"] or 0),
            total_days_issued=int(reward_row["total_days_issued"] or 0),
            top_referrer_referrals_count=int(top_referrer_row["referrals_count"])
            if top_referrer_row
            else 0,
            top_referrer_id=top_referrer_row["referrer_id"] if top_referrer_row else None,
        )

    async def get_user_referral_stats(self, user_id: int) -> UserReferralStatsDto:
        # Referrer info: find the User who referred this user (referred_id = user_id)
        referrer_stmt = (
            select(User.telegram_id, User.email, User.username)
            .join(Referral, Referral.referrer_id == User.id)
            .where(Referral.referred_id == user_id)
        )

        invited_stmt = select(
            func.sum(case((Referral.level == ReferralLevel.FIRST, 1), else_=0)).label("level_1"),
            func.sum(case((Referral.level == ReferralLevel.SECOND, 1), else_=0)).label("level_2"),
        ).where(Referral.referrer_id == user_id)

        rewards_stmt = select(
            func.sum(
                case(
                    (
                        and_(
                            ReferralReward.is_issued.is_(True),
                            ReferralReward.type == ReferralRewardType.POINTS,
                        ),
                        ReferralReward.amount,
                    ),
                    else_=0,
                )
            ).label("reward_points"),
            func.sum(
                case(
                    (
                        and_(
                            ReferralReward.is_issued.is_(True),
                            ReferralReward.type == ReferralRewardType.EXTRA_DAYS,
                        ),
                        ReferralReward.amount,
                    ),
                    else_=0,
                )
            ).label("reward_days"),
        ).where(ReferralReward.user_id == user_id)

        referrer_row = (await self.session.execute(referrer_stmt)).mappings().first()
        invited_row = (await self.session.execute(invited_stmt)).mappings().one()
        rewards_row = (await self.session.execute(rewards_stmt)).mappings().one()

        return UserReferralStatsDto(
            referrer_telegram_id=referrer_row["telegram_id"] if referrer_row else None,
            referrer_email=referrer_row["email"] if referrer_row else None,
            referrer_username=referrer_row["username"] if referrer_row else None,
            referrals_level_1=int(invited_row["level_1"] or 0),
            referrals_level_2=int(invited_row["level_2"] or 0),
            reward_points=int(rewards_row["reward_points"] or 0),
            reward_days=int(rewards_row["reward_days"] or 0),
        )

    async def get_referrals_with_payment_count(self, user_id: int) -> int:
        stmt = (
            select(func.count(func.distinct(Referral.referred_id)))
            .join(Transaction, Transaction.user_id == Referral.referred_id)
            .where(
                Referral.referrer_id == user_id,
                Transaction.status == TransactionStatus.COMPLETED,
                Transaction.fulfillment_status == TransactionFulfillmentStatus.SUCCEEDED,
                Transaction.is_test.is_(False),
                Transaction.pricing["final_amount"].astext.cast(Numeric) > 0,
                Transaction.plan_snapshot["is_trial"].astext == "false",
            )
        )
        count = await self.session.scalar(stmt) or 0

        logger.debug(f"User_id '{user_id}' has '{count}' referrals with payments")
        return int(count)
