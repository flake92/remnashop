from datetime import timezone

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from src.application.common.dao.user_merge import (
    UserMergeDao,
    UserMergeNotFoundError,
    UserMergePlan,
    UserMergeTargetSnapshot,
)
from src.application.dto import UserDto
from src.core.utils.time import datetime_now
from src.infrastructure.database.models import (
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
        return UserMergePlan(
            source_user_id=source_user_id,
            target_user_id=target_user_id,
            target=self._target_snapshot(target),
            moved=await self._collect_moved_counts(source.id, target.id),
            conflicts=self._validate(source, target),
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

    def _validate(self, source: User, target: User) -> list[str]:
        conflicts: list[str] = []
        if not source.email:
            conflicts.append("Source user has no email")
        if not source.is_email_verified:
            conflicts.append("Source user email is not verified")
        if target.telegram_id is None:
            conflicts.append("Target user has no Telegram account")
        if source.telegram_id is not None and source.telegram_id != target.telegram_id:
            conflicts.append("Source user is linked to a different Telegram account")
        if target.email and source.email and target.email != source.email:
            conflicts.append("Target user already has a different email")
        return conflicts

    async def _collect_moved_counts(
        self, source_user_id: int, target_user_id: int
    ) -> dict[str, int]:
        return {
            "subscriptions": await self._count(
                Subscription, Subscription.user_id == source_user_id
            ),
            "transactions": await self._count(Transaction, Transaction.user_id == source_user_id),
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

    async def _count(self, model: type[object], *where: object) -> int:
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

    async def _merge_records(self, source: User, target: User, moved: dict[str, int]) -> None:
        source_email = source.email
        source_password_hash = source.password_hash
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
        source.merged_into_user_id = target.id
        source.merged_at = datetime_now().astimezone(timezone.utc)
        await self.session.flush()

        await self._move_simple_fk(Subscription, source.id, target.id)
        await self._move_simple_fk(Transaction, source.id, target.id)
        await self._move_referrals(source.id, target.id, moved)
        await self._move_promocode_activations(source.id, target.id)
        await self._move_oauth_providers(source.id, target.id)

        target.email = source_email
        target.password_hash = source_password_hash
        target.is_email_verified = True
        target.pending_email = None
        target.email_verification_code_hash = None
        target.email_verification_expires_at = None
        target.password_reset_code_hash = None
        target.password_reset_expires_at = None
        target.current_subscription_id = target_subscription_id
        target.token_version += 1
        await self.session.flush()

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
            moved["referrals_as_referred_dropped"] = int(
                getattr(deleted, "rowcount", 0) or 0
            )
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
