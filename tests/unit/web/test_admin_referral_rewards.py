from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import BaseModel, ValidationError

from src.application.use_cases.referral.commands.backfill import (
    HistoricalReferralBackfillUnavailableError,
)
from src.web.endpoints.admin.referral_rewards import (
    apply_historical_referral_rewards,
    inventory_historical_referral_rewards,
    preview_historical_referral_rewards,
    resolve_manual_referral_reward,
)
from src.web.schemas import (
    HistoricalReferralBackfillApplyRequest,
    HistoricalReferralBackfillPreviewRequest,
    ResolveManualReferralRewardRequest,
)

resolve_manual_referral_reward_impl = (  # type: ignore[attr-defined]
    resolve_manual_referral_reward.__dishka_orig_func__
)
inventory_historical_referral_rewards_impl = (  # type: ignore[attr-defined]
    inventory_historical_referral_rewards.__dishka_orig_func__
)
preview_historical_referral_rewards_impl = (  # type: ignore[attr-defined]
    preview_historical_referral_rewards.__dishka_orig_func__
)
apply_historical_referral_rewards_impl = (  # type: ignore[attr-defined]
    apply_historical_referral_rewards.__dishka_orig_func__
)


def _backfill_request_payload() -> dict[str, object]:
    return {
        "source_transaction_ids": [77, 91],
        "operator_identity": "alice",
        "operator_reference": "TICKET-135",
        "reason": "Verified missing durable intents against payment records",
    }


def _backfill_config_snapshot() -> dict[str, object]:
    return {
        "enabled": True,
        "max_level": 2,
        "accrual_strategy": "ON_FIRST_PAYMENT",
        "reward_type": "POINTS",
        "reward_strategy": "AMOUNT",
        "reward_config": {"1": 10, "2": 5},
    }


def test_manual_resolution_requires_operator_reference_and_reason() -> None:
    with pytest.raises(ValidationError):
        ResolveManualReferralRewardRequest(
            resolution="CONFIRM_ISSUED",
            operator_reference=" ",
            reason=" ",
        )


@pytest.mark.parametrize(
    "schema",
    [HistoricalReferralBackfillPreviewRequest, HistoricalReferralBackfillApplyRequest],
)
@pytest.mark.parametrize(
    "field",
    ["operator_identity", "operator_reference", "reason"],
)
def test_historical_backfill_schema_requires_operator_evidence(
    schema: type[BaseModel],
    field: str,
) -> None:
    payload = _backfill_request_payload()
    if schema is HistoricalReferralBackfillApplyRequest:
        payload["expected_config_snapshot"] = _backfill_config_snapshot()

    missing = dict(payload)
    missing.pop(field)
    with pytest.raises(ValidationError):
        schema.model_validate(missing)

    blank = dict(payload)
    blank[field] = " "
    with pytest.raises(ValidationError):
        schema.model_validate(blank)


@pytest.mark.parametrize(
    "schema",
    [HistoricalReferralBackfillPreviewRequest, HistoricalReferralBackfillApplyRequest],
)
def test_historical_backfill_schema_requires_explicit_source_ids(
    schema: type[BaseModel],
) -> None:
    payload = _backfill_request_payload()
    if schema is HistoricalReferralBackfillApplyRequest:
        payload["expected_config_snapshot"] = _backfill_config_snapshot()

    payload.pop("source_transaction_ids")
    with pytest.raises(ValidationError):
        schema.model_validate(payload)

    payload["source_transaction_ids"] = []
    with pytest.raises(ValidationError):
        schema.model_validate(payload)


def test_historical_backfill_apply_schema_requires_explicit_config_snapshot() -> None:
    with pytest.raises(ValidationError):
        HistoricalReferralBackfillApplyRequest.model_validate(_backfill_request_payload())


@pytest.mark.parametrize(
    "config_snapshot",
    [
        _backfill_config_snapshot() | {"enabled": 1},
        _backfill_config_snapshot() | {"max_level": True},
        _backfill_config_snapshot() | {"max_level": "2"},
        _backfill_config_snapshot() | {"reward_config": {"1": 10, "2": True}},
        _backfill_config_snapshot() | {"unexpected": "value"},
    ],
)
def test_historical_backfill_config_snapshot_rejects_type_spoofing(
    config_snapshot: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        HistoricalReferralBackfillApplyRequest.model_validate(
            _backfill_request_payload() | {"expected_config_snapshot": config_snapshot}
        )


@pytest.mark.asyncio
async def test_historical_backfill_inventory_dispatches_pagination() -> None:
    backfill = SimpleNamespace(system=AsyncMock(return_value={"read_only": True}))

    result = await inventory_historical_referral_rewards_impl(
        backfill,
        limit=25,
        offset=50,
        _=None,
    )

    assert result == {"read_only": True}
    request = backfill.system.await_args.args[0]
    assert request.action == "INVENTORY"
    assert request.limit == 25
    assert request.offset == 50
    assert request.source_transaction_ids == ()


@pytest.mark.asyncio
async def test_historical_backfill_preview_dispatches_explicit_evidence() -> None:
    backfill = SimpleNamespace(system=AsyncMock(return_value={"preview_id": 31}))
    body = HistoricalReferralBackfillPreviewRequest.model_validate(_backfill_request_payload())

    result = await preview_historical_referral_rewards_impl(body, backfill, None)

    assert result == {"preview_id": 31}
    request = backfill.system.await_args.args[0]
    assert request.action == "PREVIEW"
    assert request.source_transaction_ids == (77, 91)
    assert request.operator_identity == "alice"
    assert request.operator_reference == "TICKET-135"
    assert request.reason == "Verified missing durable intents against payment records"


@pytest.mark.asyncio
async def test_historical_backfill_disabled_is_explicitly_unavailable() -> None:
    backfill = SimpleNamespace(
        system=AsyncMock(
            side_effect=HistoricalReferralBackfillUnavailableError("backfill disabled")
        )
    )
    body = HistoricalReferralBackfillPreviewRequest.model_validate(_backfill_request_payload())

    with pytest.raises(HTTPException) as exc_info:
        await preview_historical_referral_rewards_impl(body, backfill, None)

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "backfill disabled"


@pytest.mark.asyncio
async def test_historical_backfill_apply_dispatches_preview_id_and_exact_config() -> None:
    backfill = SimpleNamespace(system=AsyncMock(return_value={"status": "APPLIED"}))
    config_snapshot = _backfill_config_snapshot()
    body = HistoricalReferralBackfillApplyRequest.model_validate(
        _backfill_request_payload() | {"expected_config_snapshot": config_snapshot}
    )

    result = await apply_historical_referral_rewards_impl(31, body, backfill, None)

    assert result == {"status": "APPLIED"}
    request = backfill.system.await_args.args[0]
    assert request.action == "APPLY"
    assert request.preview_id == 31
    assert request.source_transaction_ids == (77, 91)
    assert request.operator_identity == "alice"
    assert request.operator_reference == "TICKET-135"
    assert request.reason == "Verified missing durable intents against payment records"
    assert request.expected_config_snapshot == config_snapshot


@pytest.mark.asyncio
async def test_manual_resolution_endpoint_replays_identical_evidence_idempotently() -> None:
    resolver = SimpleNamespace(system=AsyncMock())
    body = ResolveManualReferralRewardRequest(
        resolution="CONFIRM_ISSUED",
        expected_version=3,
        operator_reference="alice/TICKET-123",
        reason="Verified Remnawave target and local balance",
        allow_drift=True,
    )

    await resolve_manual_referral_reward_impl(8, body, resolver, None)  # type: ignore[arg-type]
    await resolve_manual_referral_reward_impl(8, body, resolver, None)  # type: ignore[arg-type]

    assert resolver.system.await_count == 2
    first, second = resolver.system.await_args_list
    assert first.args[0] == second.args[0]
    assert first.args[0].operator_reference == "alice/TICKET-123"
    assert first.args[0].expected_version == 3
    assert first.args[0].allow_drift is True
