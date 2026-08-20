import importlib
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from src.application.dto import ReferralRewardDto
from src.core.enums import (
    ReferralAccrualStrategy,
    ReferralLevel,
    ReferralRewardState,
    ReferralRewardStrategy,
    ReferralRewardType,
    TransactionStatus,
)
from src.infrastructure.database.constraints import (
    REFERRAL_REWARDS_DURABLE_STATE_CONSTRAINT_NAME,
    REFERRAL_REWARDS_DURABLE_STATE_CONSTRAINT_SQL,
)
from src.infrastructure.database.dao.referral import ReferralDaoImpl
from src.infrastructure.database.models import (
    Referral,
    ReferralReward,
    ReferralRewardBackfillAudit,
    ReferralRewardResolution,
)


def _capture_migration(monkeypatch: pytest.MonkeyPatch, migration: Any) -> list[tuple[Any, ...]]:
    calls: list[tuple[Any, ...]] = []

    def recorder(name: str) -> Any:
        def record(*args: object, **kwargs: object) -> None:
            calls.append((name, args, kwargs))

        return record

    for name in (
        "add_column",
        "alter_column",
        "create_check_constraint",
        "create_foreign_key",
        "create_index",
        "create_table",
        "create_unique_constraint",
        "drop_column",
        "drop_constraint",
        "drop_index",
        "drop_table",
        "execute",
    ):
        monkeypatch.setattr(migration.op, name, recorder(name))
    monkeypatch.setattr(migration.op, "get_bind", lambda: object())
    monkeypatch.setattr(
        migration.postgresql.ENUM,
        "create",
        lambda self, *args, **kwargs: calls.append(("create_enum", (self.name, *args), kwargs)),
    )
    monkeypatch.setattr(
        migration.postgresql.ENUM,
        "drop",
        lambda self, *args, **kwargs: calls.append(("drop_enum", (self.name, *args), kwargs)),
    )
    return calls


def test_0052_backfills_legacy_ambiguity_and_installs_durable_fences(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module(
        "src.infrastructure.database.migrations.versions.0052_make_referral_rewards_durable"
    )
    calls = _capture_migration(monkeypatch, migration)

    migration.upgrade()

    assert migration.revision == "0052"
    assert migration.down_revision == "0051"
    executed = "\n".join(str(args[0]) for name, args, _ in calls if name == "execute")
    assert "LEGACY_AMBIGUOUS_ISSUANCE" in executed
    assert "manual_incident_version = CASE WHEN is_issued THEN 0 ELSE 1 END" in executed
    assert "manual_cause = CASE WHEN is_issued THEN NULL" in executed
    assert "MANUAL_REQUIRED" in executed
    assert "CASE WHEN is_issued THEN 'ISSUED'" in executed
    created_enums = [args[0] for name, args, _ in calls if name == "create_enum"]
    assert created_enums == [
        "referral_accrual_strategy",
        "referral_reward_strategy",
        "referral_reward_state",
    ]
    first_add_column = next(i for i, call in enumerate(calls) if call[0] == "add_column")
    assert all(calls.index(call) < first_add_column for call in calls if call[0] == "create_enum")

    state_column = next(
        args[1] for name, args, _ in calls if name == "add_column" and args[1].name == "state"
    )
    assert state_column.type.name == "referral_reward_state"
    assert state_column.type.create_type is False
    assert str(state_column.server_default.arg) == "'MANUAL_REQUIRED'::referral_reward_state"
    assert ReferralReward.__table__.c.state.server_default is None

    foreign_keys = {args[0]: kwargs for name, args, kwargs in calls if name == "create_foreign_key"}
    assert foreign_keys["referral_rewards_referral_id_fkey"]["ondelete"] == "RESTRICT"
    assert foreign_keys["referral_rewards_source_transaction_id_fkey"]["ondelete"] == "RESTRICT"
    assert foreign_keys["referral_rewards_origin_referral_id_fkey"]["ondelete"] == "RESTRICT"
    assert foreign_keys["referral_rewards_target_subscription_id_fkey"]["ondelete"] == "RESTRICT"

    unique = next(
        args
        for name, args, _ in calls
        if name == "create_unique_constraint"
        and args[0] == "uq_referral_rewards_source_transaction_origin_level"
    )
    assert unique[2] == ["source_transaction_id", "origin_referral_id", "level"]

    indexes = {args[0]: (args, kwargs) for name, args, kwargs in calls if name == "create_index"}
    first_where = str(
        indexes["uq_referral_rewards_first_payment_origin_level"][1]["postgresql_where"]
    )
    assert indexes["uq_referral_rewards_first_payment_origin_level"][0][2] == [
        "origin_referral_id",
        "level",
    ]
    assert first_where == "accrual_strategy = 'ON_FIRST_PAYMENT'"
    assert indexes["uq_referral_rewards_processing_recipient"][0][2] == ["user_id"]

    check = next(
        str(args[2])
        for name, args, _ in calls
        if name == "create_check_constraint"
        and args[0] == REFERRAL_REWARDS_DURABLE_STATE_CONSTRAINT_NAME
    )
    assert check == REFERRAL_REWARDS_DURABLE_STATE_CONSTRAINT_SQL

    issued_first_check = next(
        str(args[2])
        for name, args, _ in calls
        if name == "create_check_constraint"
        and args[0] == "ck_referral_rewards_issued_first_payment_claimed"
    )
    assert "state != 'ISSUED'" in issued_first_check
    assert "accrual_strategy_snapshot IS NULL" in issued_first_check
    assert "accrual_strategy = 'ON_FIRST_PAYMENT'" in issued_first_check

    model_checks = {
        constraint.name: str(constraint.sqltext)
        for constraint in ReferralReward.__table__.constraints
        if hasattr(constraint, "sqltext")
    }
    assert (
        model_checks[REFERRAL_REWARDS_DURABLE_STATE_CONSTRAINT_NAME]
        == REFERRAL_REWARDS_DURABLE_STATE_CONSTRAINT_SQL
        == check
    )
    assert model_checks["ck_referral_rewards_issued_first_payment_claimed"] == issued_first_check

    resolution_table = next(
        args
        for name, args, _ in calls
        if name == "create_table" and args[0] == "referral_reward_resolutions"
    )
    resolution_sql = " ".join(
        f"{value} {getattr(value, 'sqltext', '')}" for value in resolution_table[1:]
    )
    assert "operator_reference" in resolution_sql
    assert "resolved_by" in resolution_sql
    assert "resolved_at" in resolution_sql
    assert "reason" in resolution_sql
    assert "allow_drift" in resolution_sql
    assert "observed_subscription_id" in resolution_sql
    assert "observed_remote_uuid" in resolution_sql
    assert "observed_expire_at" in resolution_sql
    assert "incident_version" in resolution_sql
    assert "source_status" in resolution_sql
    assert "CONFIRM_ISSUED" in resolution_sql

    backfill_table = next(
        args
        for name, args, _ in calls
        if name == "create_table" and args[0] == "referral_reward_backfill_audits"
    )
    backfill_sql = " ".join(
        f"{value} {getattr(value, 'sqltext', '')}" for value in backfill_table[1:]
    )
    assert "request_hash" in backfill_sql
    assert "operator_identity" in backfill_sql
    assert "operator_reference" in backfill_sql
    assert "reason" in backfill_sql
    assert "source_transaction_ids" in backfill_sql
    assert "config_snapshot" in backfill_sql
    assert "preview_snapshot" in backfill_sql
    assert "PREVIEWED" in backfill_sql
    assert "APPLIED" in backfill_sql
    backfill_constraint_names = {getattr(value, "name", None) for value in backfill_table[1:]}
    assert "uq_referral_reward_backfill_audits_request_hash" in backfill_constraint_names

    backfill_indexes = [
        args[0]
        for name, args, _ in calls
        if name == "create_index" and "backfill_audits" in str(args[0])
    ]
    assert backfill_indexes == ["ix_referral_reward_backfill_audits_request_hash"]

    migration.downgrade()
    dropped_enums = [args[0] for name, args, _ in calls if name == "drop_enum"]
    assert dropped_enums == [
        "referral_reward_state",
        "referral_reward_strategy",
        "referral_accrual_strategy",
    ]


def test_0052_downgrade_first_guards_all_durable_evidence_but_allows_legacy_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    migration = importlib.import_module(
        "src.infrastructure.database.migrations.versions.0052_make_referral_rewards_durable"
    )
    calls = _capture_migration(monkeypatch, migration)

    migration.downgrade()

    assert calls[0][0] == "execute"
    guard_sql = str(calls[0][1][0]).upper()
    assert "LOCK TABLE" in guard_sql
    assert "IN SHARE MODE" in guard_sql
    assert "FROM REFERRAL_REWARD_RESOLUTIONS" in guard_sql
    assert "FROM REFERRAL_REWARD_BACKFILL_AUDITS" in guard_sql
    assert "FROM REFERRAL_REWARDS" in guard_sql
    assert "WHERE SOURCE_TRANSACTION_ID IS NOT NULL" in guard_sql
    assert guard_sql.count("RAISE EXCEPTION") == 3
    assert calls[1][0] == "drop_index"


def test_model_preserves_attribution_and_serializes_processing_per_recipient() -> None:
    referral_fk = next(
        fk
        for fk in ReferralReward.__table__.foreign_key_constraints
        if {column.name for column in fk.columns} == {"referral_id"}
    )
    origin_fk = next(
        fk
        for fk in ReferralReward.__table__.foreign_key_constraints
        if {column.name for column in fk.columns} == {"origin_referral_id"}
    )
    assert referral_fk.ondelete == "RESTRICT"
    assert origin_fk.ondelete == "RESTRICT"
    assert "delete" not in Referral.rewards.property.cascade
    assert "delete-orphan" not in Referral.rewards.property.cascade

    indexes = {index.name: index for index in ReferralReward.__table__.indexes}
    processing = indexes["uq_referral_rewards_processing_recipient"]
    assert processing.unique is True
    assert [column.name for column in processing.columns] == ["user_id"]
    assert str(processing.dialect_options["postgresql"]["where"]) == "state = 'PROCESSING'"


def test_backfill_audit_model_matches_migration_uniqueness_and_evidence_shape() -> None:
    table = ReferralRewardBackfillAudit.__table__
    constraint_names = {constraint.name for constraint in table.constraints}
    assert "ck_referral_reward_backfill_audits_status" in constraint_names
    assert "uq_referral_reward_backfill_audits_request_hash" in constraint_names
    assert {column.name for column in table.columns} >= {
        "request_hash",
        "status",
        "operator_identity",
        "operator_reference",
        "reason",
        "source_transaction_ids",
        "config_snapshot",
        "preview_snapshot",
        "applied_at",
    }
    request_hash_index = next(
        index
        for index in table.indexes
        if index.name == "ix_referral_reward_backfill_audits_request_hash"
    )
    assert [column.name for column in request_hash_index.columns] == ["request_hash"]


class _EmptyScalars:
    def all(self) -> list[object]:
        return []


class _ClaimSession:
    def __init__(self) -> None:
        self.executed: list[object] = []
        self.scalar_statements: list[object] = []

    async def execute(self, statement: object) -> SimpleNamespace:
        self.executed.append(statement)
        return SimpleNamespace(rowcount=0)

    async def scalars(self, statement: object) -> _EmptyScalars:
        self.scalar_statements.append(statement)
        return _EmptyScalars()


@pytest.mark.asyncio
async def test_worker_manualizes_ambiguous_extra_days_and_locks_recipient_rows() -> None:
    session = _ClaimSession()
    dao = ReferralDaoImpl.__new__(ReferralDaoImpl)
    dao.session = session  # type: ignore[assignment]

    rewards = await dao.claim_pending_rewards(
        token_hash="a" * 64,
        lease_for=timedelta(minutes=5),
        limit=100,
    )

    assert rewards == []

    def statement_with_value(value: str) -> object:
        return next(
            statement
            for statement in session.executed
            if value in statement.compile(dialect=postgresql.dialect()).params.values()  # type: ignore[attr-defined]
        )

    ambiguous = statement_with_value("EXTRA_DAYS_AMBIGUOUS_LEASE_EXPIRED")
    ambiguous_sql = str(ambiguous.compile(dialect=postgresql.dialect())).upper()  # type: ignore[attr-defined]
    ambiguous_params = ambiguous.compile(dialect=postgresql.dialect()).params  # type: ignore[attr-defined]
    assert "TARGET_EXPIRE_AT IS NOT NULL" in ambiguous_sql
    assert "PROCESSING_LEASE_EXPIRES_AT" in ambiguous_sql
    assert "EXTRA_DAYS_AMBIGUOUS_LEASE_EXPIRED" in ambiguous_params.values()

    pending_refund = statement_with_value("SOURCE_REFUNDED_BEFORE_REWARD_ISSUANCE")
    processing_refund = statement_with_value("SOURCE_REFUNDED_DURING_REWARD_ISSUANCE")
    issued_refund = statement_with_value("SOURCE_REFUNDED_AFTER_REWARD_ISSUANCE")
    assert (
        ReferralRewardState.SUPERSEDED
        in pending_refund.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect()
        ).params.values()
    )
    assert (
        ReferralRewardState.MANUAL_REQUIRED
        in processing_refund.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect()
        ).params.values()
    )
    assert (
        ReferralRewardState.MANUAL_REQUIRED
        in issued_refund.compile(  # type: ignore[attr-defined]
            dialect=postgresql.dialect()
        ).params.values()
    )
    processing_refund_sql = str(
        processing_refund.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    ).upper()
    issued_refund_sql = str(
        issued_refund.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    ).upper()
    assert "MANUAL_INCIDENT_VERSION +" in processing_refund_sql
    assert "MANUAL_INCIDENT_VERSION +" in issued_refund_sql
    assert "REFERRAL_REWARD_RESOLUTIONS" in issued_refund_sql
    assert "INCIDENT_VERSION" in issued_refund_sql
    assert "SOURCE_STATUS" in issued_refund_sql
    assert "NOT (EXISTS" in issued_refund_sql

    earlier_supersede = statement_with_value("FIRST_PAYMENT_EARLIER_SUCCESS")
    earlier_supersede_sql = str(
        earlier_supersede.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    ).upper()
    earlier_supersede_params = earlier_supersede.compile(  # type: ignore[attr-defined]
        dialect=postgresql.dialect()
    ).params
    assert "SUPERSEDING_EARLIER_TRANSACTION" in earlier_supersede_sql
    assert "SOURCE_TRANSACTION_ID" in earlier_supersede_sql
    assert "FIRST_PAYMENT_EARLIER_SUCCESS" in earlier_supersede_params.values()

    recipient_compiled = session.scalar_statements[0].compile(dialect=postgresql.dialect())
    recipient_lock_sql = str(recipient_compiled).upper()
    assert "FROM USERS" in recipient_lock_sql
    assert "FOR UPDATE SKIP LOCKED" in recipient_lock_sql
    assert "EARLIER_SUCCESSFUL_TRANSACTION" in recipient_lock_sql
    assert "FULFILLMENT_COMPLETED_AT" in recipient_lock_sql
    assert "REFUNDED" in str(recipient_compiled.params).upper()
    assert "NOT (EXISTS" in recipient_lock_sql


@pytest.mark.asyncio
async def test_refund_incident_versioning_reopens_only_after_nonrefund_resolution() -> None:
    session = _ClaimSession()
    dao = ReferralDaoImpl.__new__(ReferralDaoImpl)
    dao.session = session  # type: ignore[assignment]

    await dao.claim_pending_rewards(
        token_hash="a" * 64,
        lease_for=timedelta(minutes=5),
        limit=1,
    )

    issued_refund = next(
        statement
        for statement in session.executed
        if "SOURCE_REFUNDED_AFTER_REWARD_ISSUANCE"
        in statement.compile(dialect=postgresql.dialect()).params.values()  # type: ignore[attr-defined]
    )
    compiled = issued_refund.compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    sql = str(compiled).upper()
    # A prior ambiguity resolution records source_status=COMPLETED and therefore
    # does not satisfy this fence: the later refund increments a new incident.
    # Resolving that refund records REFUNDED for the current version, so all later
    # sweeps satisfy the correlated EXISTS and remain stable.
    assert "REFERRAL_REWARD_RESOLUTIONS.INCIDENT_VERSION = " in sql
    assert "REFERRAL_REWARDS.MANUAL_INCIDENT_VERSION" in sql
    assert "REFERRAL_REWARD_RESOLUTIONS.SOURCE_STATUS" in sql
    assert TransactionStatus.REFUNDED.value in compiled.params.values()
    assert "MANUAL_INCIDENT_VERSION +" in sql

    manual_rollover = next(
        statement
        for statement in session.executed
        if "MANUAL_CAUSE NOT IN" in str(statement.compile(dialect=postgresql.dialect())).upper()  # type: ignore[attr-defined]
    )
    rollover_sql = str(manual_rollover.compile(dialect=postgresql.dialect())).upper()  # type: ignore[attr-defined]
    assert "MANUAL_INCIDENT_VERSION +" in rollover_sql
    assert "LAST_ERROR=" not in rollover_sql


@pytest.mark.asyncio
async def test_attribution_fence_locks_payer_and_referrers_in_user_id_order() -> None:
    session = _ClaimSession()
    dao = ReferralDaoImpl.__new__(ReferralDaoImpl)
    dao.session = session  # type: ignore[assignment]

    await dao.lock_referral_attribution(9, (5, 2, 5))

    users_sql = str(
        session.executed[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    referrals_sql = str(
        session.executed[1].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert "USERS.ID IN (2, 5, 9)" in users_sql
    assert "ORDER BY USERS.ID" in users_sql
    assert "FOR UPDATE" in users_sql
    assert "REFERRALS.REFERRED_ID IN (2, 5, 9)" in referrals_sql
    assert "ORDER BY REFERRALS.ID" in referrals_sql
    assert "FOR UPDATE" in referrals_sql


class _DumpRetort:
    def dump(self, reward: ReferralRewardDto) -> dict[str, object]:
        return {
            "id": reward.id,
            "user_id": reward.user_id,
            "type": reward.type,
            "amount": reward.amount,
            "is_issued": reward.is_issued,
            "source_transaction_id": reward.source_transaction_id,
            "origin_referral_id": reward.origin_referral_id,
            "level": reward.level,
            "accrual_strategy_snapshot": reward.accrual_strategy_snapshot,
            "accrual_strategy": reward.accrual_strategy,
            "reward_strategy": reward.reward_strategy,
            "config_value": reward.config_value,
            "state": reward.state,
            "attempt_count": reward.attempt_count,
            "next_attempt_at": None,
            "processing_token_hash": None,
            "processing_lease_expires_at": None,
            "last_error": None,
            "manual_alerted_at": None,
            "refund_detected_at": None,
            "manual_incident_version": reward.manual_incident_version,
            "manual_cause": reward.manual_cause,
            "issued_at": None,
            "target_subscription_id": None,
            "baseline_expire_at": None,
            "target_expire_at": None,
            "created_at": None,
            "updated_at": None,
        }


class _ScalarSession:
    def __init__(self, values: list[object]) -> None:
        self.values = values
        self.statements: list[object] = []

    async def scalar(self, statement: object) -> object:
        self.statements.append(statement)
        return self.values.pop(0)


@pytest.mark.asyncio
async def test_idempotent_create_only_returns_the_exact_same_intent() -> None:
    exact = SimpleNamespace(id=91)
    session = _ScalarSession([None, exact])
    dao = ReferralDaoImpl.__new__(ReferralDaoImpl)
    dao.session = session  # type: ignore[assignment]
    dao.retort = _DumpRetort()  # type: ignore[assignment]
    dao._convert_to_reward_dto = lambda row: row  # type: ignore[method-assign]
    reward = ReferralRewardDto(
        user_id=2,
        type=ReferralRewardType.POINTS,
        amount=10,
        source_transaction_id=77,
        origin_referral_id=101,
        level=ReferralLevel.SECOND,
        accrual_strategy=ReferralAccrualStrategy.ON_FIRST_PAYMENT,
        reward_strategy=ReferralRewardStrategy.AMOUNT,
        config_value=10,
    )

    result = await dao.create_reward(reward, referral_id=202)

    assert result is exact
    exact_sql = str(session.statements[1].compile(dialect=postgresql.dialect())).upper()  # type: ignore[attr-defined]
    assert "SOURCE_TRANSACTION_ID" in exact_sql
    assert "ORIGIN_REFERRAL_ID" in exact_sql
    assert "LEVEL" in exact_sql


@pytest.mark.asyncio
async def test_manual_resolver_locks_source_transaction_before_decision() -> None:
    session = _ScalarSession([TransactionStatus.REFUNDED])
    dao = ReferralDaoImpl.__new__(ReferralDaoImpl)
    dao.session = session  # type: ignore[assignment]

    status = await dao.lock_manual_reward_source_status(8)

    assert status == TransactionStatus.REFUNDED
    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert "JOIN REFERRAL_REWARDS" in sql
    assert "REFERRAL_REWARDS.ID = 8" in sql
    assert "FOR UPDATE OF TRANSACTIONS" in sql


@pytest.mark.asyncio
async def test_side_effect_eligibility_locks_successful_source_transaction() -> None:
    session = _ScalarSession([77])
    dao = ReferralDaoImpl.__new__(ReferralDaoImpl)
    dao.session = session  # type: ignore[assignment]

    assert await dao.lock_reward_source_if_eligible(
        8,
        token_hash="t" * 64,
    )

    sql = str(
        session.statements[0].compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()
    assert "FOR UPDATE OF TRANSACTIONS" in sql
    assert "TRANSACTIONS.STATUS = 'COMPLETED'" in sql
    assert "TRANSACTIONS.FULFILLMENT_STATUS = 'SUCCEEDED'" in sql
    assert "PROCESSING_TOKEN_HASH" in sql


class _PointsSession:
    def __init__(self, reward_transition: int | None) -> None:
        self.reward_transition = reward_transition
        self.scalar_statement: object | None = None
        self.execute_statements: list[object] = []

    async def scalar(self, statement: object) -> int | None:
        self.scalar_statement = statement
        return self.reward_transition

    async def execute(self, statement: object) -> SimpleNamespace:
        self.execute_statements.append(statement)
        return SimpleNamespace(rowcount=1)


@pytest.mark.asyncio
async def test_points_increment_is_fenced_by_same_atomic_state_transition() -> None:
    session = _PointsSession(8)
    dao = ReferralDaoImpl.__new__(ReferralDaoImpl)
    dao.session = session  # type: ignore[assignment]

    issued = await dao.issue_points_reward(
        8,
        user_id=2,
        amount=10,
        token_hash="t" * 64,
    )

    assert issued is True
    reward_sql = str(
        session.scalar_statement.compile(dialect=postgresql.dialect())  # type: ignore[union-attr]
    ).upper()
    points_sql = str(
        session.execute_statements[0].compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    ).upper()
    assert "PROCESSING_TOKEN_HASH" in reward_sql
    assert "STATE" in reward_sql
    assert "USERS.POINTS +" in points_sql

    replay_session = _PointsSession(None)
    replay_dao = ReferralDaoImpl.__new__(ReferralDaoImpl)
    replay_dao.session = replay_session  # type: ignore[assignment]
    assert not await replay_dao.issue_points_reward(
        8,
        user_id=2,
        amount=10,
        token_hash="t" * 64,
    )
    assert replay_session.execute_statements == []


class _ResolutionSession:
    def __init__(self, scalar_values: list[object]) -> None:
        self.scalar_values = scalar_values
        self.added: list[object] = []
        self.executed: list[object] = []

    async def scalar(self, statement: object) -> object:
        return self.scalar_values.pop(0)

    def add(self, value: object) -> None:
        self.added.append(value)

    async def execute(self, statement: object) -> SimpleNamespace:
        self.executed.append(statement)
        return SimpleNamespace(rowcount=1)


@pytest.mark.asyncio
async def test_manual_resolution_is_audited_idempotently_and_preserves_cause() -> None:
    reward = SimpleNamespace(
        state=ReferralRewardState.MANUAL_REQUIRED,
        manual_incident_version=1,
        accrual_strategy_snapshot=None,
        accrual_strategy=None,
    )
    session = _ResolutionSession([reward, None])
    dao = ReferralDaoImpl.__new__(ReferralDaoImpl)
    dao.session = session  # type: ignore[assignment]

    assert await dao.resolve_manual_reward(
        8,
        expected_version=1,
        confirm_issued=True,
        operator_reference="alice/TICKET-123",
        resolved_by="ADMIN_API",
        reason="Verified external target",
        source_status=TransactionStatus.REFUNDED,
    )

    resolution = session.added[0]
    assert isinstance(resolution, ReferralRewardResolution)
    assert resolution.decision == "CONFIRM_ISSUED"
    assert resolution.operator_reference == "alice/TICKET-123"
    assert resolution.incident_version == 1
    assert resolution.source_status == TransactionStatus.REFUNDED.value
    assert resolution.allow_drift is False
    update_sql = str(
        session.executed[0].compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    ).upper()
    assert "LAST_ERROR" not in update_sql
    assert "COALESCE" in update_sql

    existing = SimpleNamespace(
        decision="CONFIRM_ISSUED",
        operator_reference="alice/TICKET-123",
        resolved_by="ADMIN_API",
        reason="Verified external target",
        allow_drift=False,
    )
    replay_session = _ResolutionSession(
        [SimpleNamespace(state=ReferralRewardState.ISSUED), existing]
    )
    replay_dao = ReferralDaoImpl.__new__(ReferralDaoImpl)
    replay_dao.session = replay_session  # type: ignore[assignment]
    assert await replay_dao.resolve_manual_reward(
        8,
        expected_version=1,
        confirm_issued=True,
        operator_reference="alice/TICKET-123",
        resolved_by="ADMIN_API",
        reason="Verified external target",
        allow_drift=False,
    )
    assert replay_session.added == []
    assert replay_session.executed == []

    conflict_session = _ResolutionSession(
        [SimpleNamespace(state=ReferralRewardState.ISSUED), existing]
    )
    conflict_dao = ReferralDaoImpl.__new__(ReferralDaoImpl)
    conflict_dao.session = conflict_session  # type: ignore[assignment]
    assert not await conflict_dao.resolve_manual_reward(
        8,
        expected_version=1,
        confirm_issued=False,
        operator_reference="bob/TICKET-999",
        resolved_by="ADMIN_API",
        reason="Different decision",
    )

    cancel_session = _ResolutionSession(
        [
            SimpleNamespace(
                state=ReferralRewardState.MANUAL_REQUIRED,
                manual_incident_version=1,
                accrual_strategy_snapshot=None,
                accrual_strategy=None,
            ),
            None,
        ]
    )
    cancel_dao = ReferralDaoImpl.__new__(ReferralDaoImpl)
    cancel_dao.session = cancel_session  # type: ignore[assignment]
    assert await cancel_dao.resolve_manual_reward(
        9,
        expected_version=1,
        confirm_issued=False,
        operator_reference="alice/TICKET-124",
        resolved_by="ADMIN_API",
        reason="Verified rollback",
    )
    cancel_sql = str(
        cancel_session.executed[0].compile(dialect=postgresql.dialect())  # type: ignore[attr-defined]
    ).upper()
    assert "ISSUED_AT" not in cancel_sql
