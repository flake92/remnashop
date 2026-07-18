from datetime import timezone
from typing import Any

from sqlalchemy import ColumnElement, Text, case, cast, delete, func, or_, select, text, update
from sqlalchemy.dialects.postgresql import ARRAY, array
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.common.dao.payment_operation import PaymentOperationStatus
from src.application.common.dao.user_merge import (
    UserMergeDao,
    UserMergeNotFoundError,
    UserMergePaymentOperationConflictError,
    UserMergePlan,
    UserMergeTargetConflictError,
    UserMergeTargetSnapshot,
)
from src.application.dto import UserDto
from src.core.enums import TransactionFulfillmentStatus
from src.core.utils.time import datetime_now
from src.infrastructure.database.models import (
    PaymentOperation,
    Referral,
    ReferralReward,
    Subscription,
    Transaction,
    User,
    UserMergeAudit,
    UserOAuthProvider,
)


class UserMergeDaoImpl(UserMergeDao):
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def plan(self, source_user_id: int, target_user_id: int) -> UserMergePlan:
        source, target = await self._lock_users(source_user_id, target_user_id)
        existing_merge = await self._existing_merge_plan(source, target)
        if existing_merge is not None:
            return existing_merge
        await self._normalize_stale_payment_work(source.id, target.id)
        moved = await self._collect_moved_counts(source.id, target.id)
        return UserMergePlan(
            source_user_id=source_user_id,
            target_user_id=target_user_id,
            target=self._target_snapshot(target),
            moved=moved,
            conflicts=self._validate(
                source,
                target,
                payment_operation_duplicates=moved["payment_operation_duplicates"],
                active_payment_operations=moved.get("active_payment_operations", 0),
                fulfillment_processing=moved.get("fulfillment_processing", 0),
            ),
        )

    async def merge(
        self,
        *,
        actor: UserDto,
        source_user_id: int,
        target_user_id: int,
        reason: str,
    ) -> UserMergePlan:
        source, target = await self._lock_users(source_user_id, target_user_id)
        existing_merge = await self._existing_merge_plan(source, target)
        if existing_merge is not None:
            return existing_merge
        await self._normalize_stale_payment_work(source.id, target.id)
        await self._assert_no_active_payment_work(source.id, target.id)
        await self._assert_no_payment_operation_collisions(source.id, target.id)
        moved = await self._collect_moved_counts(source.id, target.id)
        await self._merge_records(source, target, moved)
        self.session.add(
            UserMergeAudit(
                actor_user_id=None if actor.id < 0 else actor.id,
                actor_role=actor.role.name,
                source_user_id=source.id,
                target_user_id=target.id,
                reason=reason,
                dry_run=False,
                moved=moved,
                conflicts=[],
            )
        )
        return UserMergePlan(
            source_user_id=source_user_id,
            target_user_id=target_user_id,
            target=self._target_snapshot(target),
            moved=moved,
            conflicts=[],
        )

    async def _existing_merge_plan(
        self,
        source: User,
        target: User,
    ) -> UserMergePlan | None:
        merged_target_id = source.merged_into_user_id
        if merged_target_id is None:
            return None
        if merged_target_id != target.id:
            raise UserMergeTargetConflictError(
                f"Source user '{source.id}' is already merged into user "
                f"'{merged_target_id}' and cannot be redirected to user '{target.id}'"
            )

        # A retry must describe the original operation, not the now-empty source.
        # Keep using the first successful audit entry so accidental historical
        # duplicate entries cannot make the response change between retries.
        stmt = (
            select(UserMergeAudit.moved)
            .where(
                UserMergeAudit.source_user_id == source.id,
                UserMergeAudit.target_user_id == target.id,
                UserMergeAudit.dry_run.is_(False),
            )
            .order_by(UserMergeAudit.id.asc())
            .limit(1)
        )
        persisted_moved = await self.session.scalar(stmt)
        moved = (
            {
                key: value
                for key, value in persisted_moved.items()
                if isinstance(key, str) and isinstance(value, int) and not isinstance(value, bool)
            }
            if isinstance(persisted_moved, dict)
            else {}
        )
        return UserMergePlan(
            source_user_id=source.id,
            target_user_id=target.id,
            target=self._target_snapshot(target),
            moved=moved,
            conflicts=[],
        )

    async def _lock_users(self, source_user_id: int, target_user_id: int) -> tuple[User, User]:
        ordered_ids = sorted([source_user_id, target_user_id])
        stmt = select(User).where(User.id.in_(ordered_ids)).order_by(User.id).with_for_update()
        users = list((await self.session.scalars(stmt)).all())
        by_id = {user.id: user for user in users}
        source = by_id.get(source_user_id)
        target = by_id.get(target_user_id)
        if source is None:
            raise UserMergeNotFoundError(f"Source user '{source_user_id}' not found")
        if target is None:
            raise UserMergeNotFoundError(f"Target user '{target_user_id}' not found")
        return source, target

    def _validate(
        self,
        source: User,
        target: User,
        *,
        payment_operation_duplicates: int = 0,
        active_payment_operations: int = 0,
        fulfillment_processing: int = 0,
    ) -> list[str]:
        conflicts: list[str] = []
        if target.email and source.email and target.email != source.email:
            conflicts.append("Both users have different emails")
        if (
            target.telegram_id is not None
            and source.telegram_id is not None
            and target.telegram_id != source.telegram_id
        ):
            conflicts.append("Both users have different Telegram accounts")
        if target.current_subscription_id and source.current_subscription_id:
            conflicts.append("Both users have current subscriptions")
        if payment_operation_duplicates:
            conflicts.append(
                "Payment idempotency key collision between source and target "
                f"({payment_operation_duplicates})"
            )
        if active_payment_operations:
            conflicts.append(
                f"Source user has active payment operations ({active_payment_operations})"
            )
        if fulfillment_processing:
            conflicts.append(
                f"Source user has payment fulfillment in progress ({fulfillment_processing})"
            )
        return conflicts

    async def _collect_moved_counts(
        self, source_user_id: int, target_user_id: int
    ) -> dict[str, int]:
        return {
            "subscriptions": await self._count(
                Subscription, Subscription.user_id == source_user_id
            ),
            "transactions": await self._count(Transaction, Transaction.user_id == source_user_id),
            "payment_operations": await self._count(
                PaymentOperation, PaymentOperation.user_id == source_user_id
            ),
            "payment_operation_duplicates": await self._count_payment_operation_duplicates(
                source_user_id, target_user_id
            ),
            "active_payment_operations": await self._count(
                PaymentOperation,
                PaymentOperation.user_id.in_((source_user_id, target_user_id)),
                or_(
                    PaymentOperation.status.in_(
                        (
                            PaymentOperationStatus.CLAIMED.value,
                            PaymentOperationStatus.PROCESSING.value,
                        )
                    ),
                    PaymentOperation.reconcile_token_hash.is_not(None),
                ),
            ),
            "fulfillment_processing": await self._count(
                Transaction,
                Transaction.user_id.in_((source_user_id, target_user_id)),
                or_(
                    Transaction.fulfillment_status == TransactionFulfillmentStatus.PROCESSING,
                    Transaction.fulfillment_token_hash.is_not(None),
                ),
            ),
            "referrals_as_referrer": await self._count(
                Referral, Referral.referrer_id == source_user_id
            ),
            "referrals_as_referred": await self._count(
                Referral, Referral.referred_id == source_user_id
            ),
            "referral_rewards": await self._count(
                ReferralReward, ReferralReward.user_id == source_user_id
            ),
            "promocode_activations": await self._count_promocode_activations(source_user_id),
            "promocode_activation_duplicates": await self._count_promocode_duplicates(
                source_user_id, target_user_id
            ),
            "oauth_providers": await self._count(
                UserOAuthProvider, UserOAuthProvider.user_id == source_user_id
            ),
            "oauth_provider_duplicates": await self._count_oauth_duplicates(
                source_user_id, target_user_id
            ),
        }

    async def _count(self, model: type[Any], *where: ColumnElement[bool]) -> int:
        stmt = select(func.count()).select_from(model).where(*where)
        return int(await self.session.scalar(stmt) or 0)

    async def _count_promocode_activations(self, source_user_id: int) -> int:
        stmt = text("select count(*) from promocode_activations where user_id = :source_user_id")
        return int(await self.session.scalar(stmt, {"source_user_id": source_user_id}) or 0)

    async def _count_promocode_duplicates(self, source_user_id: int, target_user_id: int) -> int:
        stmt = text(
            """
            select count(*)
            from promocode_activations source
            join promocode_activations target
              on target.promocode_id = source.promocode_id
             and target.user_id = :target_user_id
            where source.user_id = :source_user_id
            """
        )
        params = {"source_user_id": source_user_id, "target_user_id": target_user_id}
        return int(await self.session.scalar(stmt, params) or 0)

    async def _count_oauth_duplicates(self, source_user_id: int, target_user_id: int) -> int:
        stmt = text(
            """
            select count(*)
            from user_oauth_providers source
            join user_oauth_providers target
              on target.provider = source.provider
             and target.user_id = :target_user_id
            where source.user_id = :source_user_id
            """
        )
        params = {"source_user_id": source_user_id, "target_user_id": target_user_id}
        return int(await self.session.scalar(stmt, params) or 0)

    async def _count_payment_operation_duplicates(
        self,
        source_user_id: int,
        target_user_id: int,
    ) -> int:
        stmt = text(
            """
            select count(*)
            from payment_operations source
            join payment_operations target
              on target.operation = source.operation
             and target.idempotency_key = source.idempotency_key
             and target.user_id = :target_user_id
            where source.user_id = :source_user_id
            """
        )
        params = {"source_user_id": source_user_id, "target_user_id": target_user_id}
        return int(await self.session.scalar(stmt, params) or 0)

    async def _assert_no_payment_operation_collisions(
        self,
        source_user_id: int,
        target_user_id: int,
    ) -> None:
        duplicates = await self._count_payment_operation_duplicates(
            source_user_id,
            target_user_id,
        )
        if duplicates:
            raise UserMergePaymentOperationConflictError(
                f"Payment idempotency key collision between source and target ({duplicates})"
            )

    async def _normalize_stale_payment_work(self, *user_ids: int) -> None:
        now = func.clock_timestamp()
        await self.session.execute(
            delete(PaymentOperation).where(
                PaymentOperation.user_id.in_(user_ids),
                PaymentOperation.status == PaymentOperationStatus.CLAIMED.value,
                PaymentOperation.lease_expires_at <= now,
            )
        )
        await self.session.execute(
            update(PaymentOperation)
            .where(
                PaymentOperation.user_id.in_(user_ids),
                PaymentOperation.status == PaymentOperationStatus.PROCESSING.value,
                PaymentOperation.lease_expires_at <= now,
            )
            .values(
                status=PaymentOperationStatus.UNKNOWN.value,
                lease_expires_at=None,
            )
        )
        await self.session.execute(
            update(PaymentOperation)
            .where(
                PaymentOperation.user_id.in_(user_ids),
                PaymentOperation.reconcile_token_hash.is_not(None),
                PaymentOperation.reconcile_lease_expires_at <= now,
            )
            .values(
                status=PaymentOperationStatus.MANUAL_REQUIRED.value,
                reconcile_next_attempt_at=None,
                reconcile_last_error="RECONCILIATION_LEASE_EXPIRED_DURING_MERGE",
            )
        )
        await self.session.execute(
            update(Transaction)
            .where(
                Transaction.user_id.in_(user_ids),
                Transaction.fulfillment_status == TransactionFulfillmentStatus.PROCESSING,
                Transaction.fulfillment_lease_expires_at <= now,
            )
            .values(
                fulfillment_status=TransactionFulfillmentStatus.MANUAL_REQUIRED,
                fulfillment_lease_expires_at=None,
                fulfillment_last_error="FULFILLMENT_LEASE_EXPIRED_DURING_MERGE",
            )
        )

    async def _assert_no_active_payment_work(self, *user_ids: int) -> None:
        active_operations = await self._count(
            PaymentOperation,
            PaymentOperation.user_id.in_(user_ids),
            or_(
                PaymentOperation.status.in_(
                    (
                        PaymentOperationStatus.CLAIMED.value,
                        PaymentOperationStatus.PROCESSING.value,
                    )
                ),
                PaymentOperation.reconcile_token_hash.is_not(None),
            ),
        )
        processing_fulfillments = await self._count(
            Transaction,
            Transaction.user_id.in_(user_ids),
            or_(
                Transaction.fulfillment_status == TransactionFulfillmentStatus.PROCESSING,
                Transaction.fulfillment_token_hash.is_not(None),
            ),
        )
        if active_operations or processing_fulfillments:
            raise UserMergePaymentOperationConflictError(
                "Source user has active payment work "
                f"(operations={active_operations}, fulfillments={processing_fulfillments})"
            )

    async def _merge_records(self, source: User, target: User, moved: dict[str, int]) -> None:
        source_email = source.email
        source_email_verified = source.is_email_verified
        source_password_hash = source.password_hash
        source_telegram_id = source.telegram_id
        source_subscription_id = source.current_subscription_id
        target_subscription_id = target.current_subscription_id or source_subscription_id

        source.email = None
        source.pending_email = None
        source.email_verification_code_hash = None
        source.email_verification_expires_at = None
        source.password_reset_code_hash = None
        source.password_reset_expires_at = None
        source.password_hash = None
        source.is_email_verified = False
        source.telegram_id = None
        source.current_subscription_id = None
        source.token_version += 1
        source.is_blocked = True
        await self.session.flush()

        await self._move_simple_fk(Subscription, source.id, target.id)
        await self._move_simple_fk(Transaction, source.id, target.id)
        await self._move_payment_operations(source.id, target.id, moved)
        await self._move_referrals(source.id, target.id, moved)
        await self._move_promocode_activations(source.id, target.id)
        await self._move_oauth_providers(source.id, target.id)

        # The database rejects marking a source as merged while it still owns
        # payment operations. Keep this assignment after the atomic transfer so
        # both current and rolling-deploy application versions preserve them.
        source.merged_into_user_id = target.id
        source.merged_at = datetime_now().astimezone(timezone.utc)

        target.email = target.email or source_email
        target.password_hash = target.password_hash or source_password_hash
        target.is_email_verified = target.is_email_verified or source_email_verified
        target.telegram_id = target.telegram_id or source_telegram_id
        target.pending_email = None
        target.email_verification_code_hash = None
        target.email_verification_expires_at = None
        target.password_reset_code_hash = None
        target.password_reset_expires_at = None
        target.current_subscription_id = target_subscription_id
        target.token_version += 1
        await self.session.flush()

    async def _move_payment_operations(
        self,
        source_user_id: int,
        target_user_id: int,
        moved: dict[str, int],
    ) -> None:
        snapshot = PaymentOperation.resolved_payment_snapshot
        stmt = (
            update(PaymentOperation)
            .where(PaymentOperation.user_id == source_user_id)
            .values(
                user_id=target_user_id,
                resolved_payment_snapshot=case(
                    (
                        snapshot.is_not(None),
                        func.jsonb_set(
                            snapshot,
                            cast(array(["user_id"]), ARRAY(Text())),
                            func.to_jsonb(target_user_id),
                            True,
                        ),
                    ),
                    else_=snapshot,
                ),
            )
        )
        moved["payment_operations"] = int(
            getattr(await self.session.execute(stmt), "rowcount", 0) or 0
        )

    async def _move_simple_fk(
        self, model: type[object], source_user_id: int, target_user_id: int
    ) -> int:
        user_id = getattr(model, "user_id")
        stmt = update(model).where(user_id == source_user_id).values(user_id=target_user_id)
        return int(getattr(await self.session.execute(stmt), "rowcount", 0) or 0)

    async def _move_referrals(
        self, source_user_id: int, target_user_id: int, moved: dict[str, int]
    ) -> None:
        target_is_referred = bool(
            await self.session.scalar(
                select(Referral.id).where(Referral.referred_id == target_user_id).limit(1)
            )
        )
        if target_is_referred:
            deleted = await self.session.execute(
                delete(Referral).where(Referral.referred_id == source_user_id)
            )
            moved["referrals_as_referred_dropped"] = int(getattr(deleted, "rowcount", 0) or 0)
        else:
            await self.session.execute(
                update(Referral)
                .where(Referral.referred_id == source_user_id)
                .values(referred_id=target_user_id)
            )

        await self.session.execute(
            delete(Referral).where(
                Referral.referrer_id == source_user_id,
                Referral.referred_id == target_user_id,
            )
        )
        await self.session.execute(
            update(Referral)
            .where(Referral.referrer_id == source_user_id)
            .values(referrer_id=target_user_id)
        )
        await self.session.execute(
            delete(Referral).where(Referral.referrer_id == Referral.referred_id)
        )
        await self.session.execute(
            update(ReferralReward)
            .where(ReferralReward.user_id == source_user_id)
            .values(user_id=target_user_id)
        )

    async def _move_promocode_activations(self, source_user_id: int, target_user_id: int) -> None:
        await self.session.execute(
            text(
                """
                delete from promocode_activations source
                using promocode_activations target
                where source.user_id = :source_user_id
                  and target.user_id = :target_user_id
                  and source.promocode_id = target.promocode_id
                """
            ),
            {"source_user_id": source_user_id, "target_user_id": target_user_id},
        )
        await self.session.execute(
            text(
                """
                update promocode_activations
                   set user_id = :target_user_id
                 where user_id = :source_user_id
                """
            ),
            {"source_user_id": source_user_id, "target_user_id": target_user_id},
        )

    async def _move_oauth_providers(self, source_user_id: int, target_user_id: int) -> None:
        await self.session.execute(
            text(
                """
                delete from user_oauth_providers source
                using user_oauth_providers target
                where source.user_id = :source_user_id
                  and target.user_id = :target_user_id
                  and source.provider = target.provider
                """
            ),
            {"source_user_id": source_user_id, "target_user_id": target_user_id},
        )
        await self.session.execute(
            update(UserOAuthProvider)
            .where(UserOAuthProvider.user_id == source_user_id)
            .values(user_id=target_user_id)
        )

    def _target_snapshot(self, target: User) -> UserMergeTargetSnapshot:
        return UserMergeTargetSnapshot(
            id=target.id,
            email=target.email,
            telegram_id=target.telegram_id,
            is_email_verified=target.is_email_verified,
            current_subscription_id=target.current_subscription_id,
        )
