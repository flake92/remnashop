import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.application.dto import LegacyReferralRewardRecoveryDto
from src.application.legacy_referral_recovery import (
    LegacyReferralRecoveryAuthorizationError,
    LegacyReferralRecoveryAuthorizer,
    canonical_legacy_recovery_manifest_sha256,
)
from src.core.enums import (
    LegacyReferralRewardRecoveryAction,
    ReferralAccrualStrategy,
    ReferralLevel,
    ReferralRewardStrategy,
)


def _retry() -> LegacyReferralRewardRecoveryDto:
    return LegacyReferralRewardRecoveryDto(
        reward_id=1478,
        action=LegacyReferralRewardRecoveryAction.RETRY_PROVEN_MISSING,
        expected_version=1,
        source_transaction_id=8889,
        origin_referral_id=1678,
        level=ReferralLevel.FIRST,
        expected_reward_amount=14,
        accrual_strategy_snapshot=ReferralAccrualStrategy.ON_FIRST_PAYMENT,
        reward_strategy=ReferralRewardStrategy.AMOUNT,
        config_value=14,
        operator_reference="OWNER/INCIDENT-2026-08-22",
        reason="Canonical evidence",
        evidence_sha256="a" * 64,
    )


def _entry(recovery: LegacyReferralRewardRecoveryDto) -> dict[str, object]:
    return {
        "reward_id": recovery.reward_id,
        "action": recovery.action.value,
        "expected_version": recovery.expected_version,
        "source_transaction_id": recovery.source_transaction_id,
        "origin_referral_id": recovery.origin_referral_id,
        "level": recovery.level.value,
        "expected_reward_amount": recovery.expected_reward_amount,
        "accrual_strategy_snapshot": (
            recovery.accrual_strategy_snapshot.value
            if recovery.accrual_strategy_snapshot is not None
            else None
        ),
        "reward_strategy": (
            recovery.reward_strategy.value if recovery.reward_strategy is not None else None
        ),
        "config_value": recovery.config_value,
        "operator_reference": recovery.operator_reference,
        "reason": recovery.reason,
        "evidence_sha256": recovery.evidence_sha256,
    }


def _manifest(recovery: LegacyReferralRewardRecoveryDto) -> dict[str, object]:
    second_retry = replace(
        _retry(),
        reward_id=1486,
        source_transaction_id=8978,
        origin_referral_id=1618,
        level=ReferralLevel.SECOND,
        expected_reward_amount=7,
        config_value=7,
        evidence_sha256="b" * 64,
    )
    admin_entries = [
        replace(
            _retry(),
            reward_id=reward_id,
            action=LegacyReferralRewardRecoveryAction.CONFIRM_ADMIN_COMPENSATED,
            source_transaction_id=source_id,
            origin_referral_id=origin_id,
            expected_reward_amount=14,
            accrual_strategy_snapshot=None,
            reward_strategy=None,
            config_value=None,
            evidence_sha256="c" * 64,
        )
        for reward_id, source_id, origin_id in (
            (398, 3987, 788),
            (399, 3988, 956),
            (507, 4505, 1025),
            (637, 5224, 956),
            (638, 5225, 1025),
            (897, 6235, 1347),
            (932, 6365, 1366),
        )
    ]
    entries = [_retry(), second_retry, *admin_entries]
    replacement_index = (
        0 if recovery.action == LegacyReferralRewardRecoveryAction.RETRY_PROVEN_MISSING else 2
    )
    entries[replacement_index] = recovery
    return {
        "version": 1,
        "incident": "legacy-referral-rewards-2026-08-22",
        "entry_count": 9,
        "entries": [_entry(entry) for entry in entries],
        "admin_compensation": {
            "total_granted_days": 107,
            "allocated_days": 98,
            "unallocated_days": 9,
            "coverage_evidence_sha256": "c" * 64,
        },
    }


def _authorizer(path: Path, manifest: dict[str, object]) -> LegacyReferralRecoveryAuthorizer:
    path.write_text(json.dumps(manifest), encoding="utf-8")
    config = SimpleNamespace(
        referral_reward_legacy_recovery_enabled=True,
        referral_reward_legacy_recovery_manifest_path=path,
        referral_reward_legacy_recovery_manifest_sha256=(
            canonical_legacy_recovery_manifest_sha256(manifest)
        ),
    )
    return LegacyReferralRecoveryAuthorizer(config)  # type: ignore[arg-type]


def test_manifest_gate_authorizes_only_exact_finite_entry(tmp_path: Path) -> None:
    recovery = _retry()
    authorizer = _authorizer(tmp_path / "manifest.json", _manifest(recovery))

    assert authorizer.authorize(recovery) == canonical_legacy_recovery_manifest_sha256(
        _manifest(recovery)
    )
    for changed in (
        replace(recovery, reward_id=1479),
        replace(recovery, source_transaction_id=8890),
        replace(recovery, expected_reward_amount=15),
        replace(recovery, operator_reference="OWNER/DIFFERENT-INCIDENT"),
        replace(recovery, reason="Different decision"),
        replace(recovery, evidence_sha256="b" * 64),
    ):
        with pytest.raises(
            LegacyReferralRecoveryAuthorizationError,
            match="not an exact trusted manifest entry",
        ):
            authorizer.authorize(changed)

    with pytest.raises(
        LegacyReferralRecoveryAuthorizationError,
        match="fixed to ADMIN_API",
    ):
        authorizer.authorize(replace(recovery, resolved_by="SYSTEM"))


def test_manifest_gate_is_disabled_by_default() -> None:
    config = SimpleNamespace(
        referral_reward_legacy_recovery_enabled=False,
        referral_reward_legacy_recovery_manifest_path=None,
        referral_reward_legacy_recovery_manifest_sha256=None,
    )
    authorizer = LegacyReferralRecoveryAuthorizer(config)  # type: ignore[arg-type]

    with pytest.raises(LegacyReferralRecoveryAuthorizationError, match="gate is disabled"):
        authorizer.authorize(_retry())


def test_manifest_gate_enforces_admin_allocation_conservation(tmp_path: Path) -> None:
    recovery = replace(
        _retry(),
        reward_id=398,
        action=LegacyReferralRewardRecoveryAction.CONFIRM_ADMIN_COMPENSATED,
        source_transaction_id=3987,
        origin_referral_id=788,
        expected_reward_amount=14,
        accrual_strategy_snapshot=None,
        reward_strategy=None,
        config_value=None,
        evidence_sha256="c" * 64,
    )
    manifest = _manifest(recovery)
    manifest["admin_compensation"] = {
        "total_granted_days": 107,
        "allocated_days": 13,
        "unallocated_days": 94,
    }
    with pytest.raises(
        LegacyReferralRecoveryAuthorizationError,
        match="failed strict validation",
    ):
        _authorizer(tmp_path / "manifest.json", manifest)


def test_manifest_gate_rejects_digest_drift(tmp_path: Path) -> None:
    recovery = _retry()
    manifest = _manifest(recovery)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(manifest), encoding="utf-8")
    config = SimpleNamespace(
        referral_reward_legacy_recovery_enabled=True,
        referral_reward_legacy_recovery_manifest_path=path,
        referral_reward_legacy_recovery_manifest_sha256="f" * 64,
    )

    with pytest.raises(LegacyReferralRecoveryAuthorizationError, match="does not match"):
        LegacyReferralRecoveryAuthorizer(config)  # type: ignore[arg-type]


def test_tracked_incident_manifest_is_exact_and_reproducible() -> None:
    path = (
        Path(__file__).parents[4]
        / "src"
        / "infrastructure"
        / "recovery_manifests"
        / "legacy_referral_rewards_2026-08-22.v1.json"
    )
    manifest = json.loads(path.read_text(encoding="utf-8"))

    assert canonical_legacy_recovery_manifest_sha256(manifest) == (
        "51284b6c833968bf4e855de30fabe4616a616ab320d000fba399c6f280cf3506"
    )
    assert manifest["entry_count"] == len(manifest["entries"]) == 9
    assert {entry["reward_id"] for entry in manifest["entries"]} == {
        1478,
        1486,
        398,
        399,
        507,
        637,
        638,
        897,
        932,
    }
    admin_entries = [
        entry for entry in manifest["entries"] if entry["action"] == "CONFIRM_ADMIN_COMPENSATED"
    ]
    assert sum(entry["expected_reward_amount"] for entry in admin_entries) == 98
    assert manifest["admin_compensation"] == {
        "total_granted_days": 107,
        "allocated_days": 98,
        "unallocated_days": 9,
        "coverage_evidence_sha256": (
            "fb3dd384f6b42c2885056d9647f4842054052ea8fae349ae88810afd8e8995a1"
        ),
    }

    config = SimpleNamespace(
        referral_reward_legacy_recovery_enabled=True,
        referral_reward_legacy_recovery_manifest_path=path.resolve(),
        referral_reward_legacy_recovery_manifest_sha256=(
            "51284b6c833968bf4e855de30fabe4616a616ab320d000fba399c6f280cf3506"
        ),
    )
    authorizer = LegacyReferralRecoveryAuthorizer(config)  # type: ignore[arg-type]
    for entry in manifest["entries"]:
        recovery = LegacyReferralRewardRecoveryDto(
            reward_id=entry["reward_id"],
            action=LegacyReferralRewardRecoveryAction(entry["action"]),
            expected_version=entry["expected_version"],
            source_transaction_id=entry["source_transaction_id"],
            origin_referral_id=entry["origin_referral_id"],
            level=ReferralLevel(entry["level"]),
            expected_reward_amount=entry["expected_reward_amount"],
            accrual_strategy_snapshot=(
                ReferralAccrualStrategy(entry["accrual_strategy_snapshot"])
                if entry["accrual_strategy_snapshot"] is not None
                else None
            ),
            reward_strategy=(
                ReferralRewardStrategy(entry["reward_strategy"])
                if entry["reward_strategy"] is not None
                else None
            ),
            config_value=entry["config_value"],
            operator_reference=entry["operator_reference"],
            reason=entry["reason"],
            evidence_sha256=entry["evidence_sha256"],
        )
        assert authorizer.authorize(recovery) == (
            "51284b6c833968bf4e855de30fabe4616a616ab320d000fba399c6f280cf3506"
        )
