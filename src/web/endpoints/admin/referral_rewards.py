from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, HTTPException, Security, status

from src.application.common.dao import ReferralDao
from src.application.dto import ReferralRewardDto
from src.application.use_cases.referral.commands.backfill import (
    HistoricalReferralBackfillUnavailableError,
    HistoricalReferralRewardBackfillDto,
    ManageHistoricalReferralRewards,
)
from src.application.use_cases.referral.commands.rewards import (
    ResolveManualReferralReward,
    ResolveManualReferralRewardDto,
)
from src.web.dependencies import require_api_key
from src.web.schemas import (
    HistoricalReferralBackfillApplyRequest,
    HistoricalReferralBackfillApplyResponse,
    HistoricalReferralBackfillInventoryResponse,
    HistoricalReferralBackfillPreviewRequest,
    HistoricalReferralBackfillPreviewResponse,
    ManualReferralRewardResponse,
    ResolveManualReferralRewardRequest,
)

router = APIRouter(
    prefix="/referral-rewards",
    tags=["Admin - Referral Rewards"],
)


def _manual_response(reward: ReferralRewardDto) -> ManualReferralRewardResponse:
    return ManualReferralRewardResponse(
        id=reward.id,
        user_id=reward.user_id,
        source_transaction_id=reward.source_transaction_id,
        origin_referral_id=reward.origin_referral_id,
        level=reward.level.value if reward.level else None,
        type=reward.type.value,
        amount=reward.amount,
        state=reward.state.value,
        is_issued=reward.is_issued,
        last_error=reward.last_error,
        attempt_count=reward.attempt_count,
        target_subscription_id=reward.target_subscription_id,
        baseline_expire_at=reward.baseline_expire_at,
        target_expire_at=reward.target_expire_at,
        created_at=reward.created_at,
        updated_at=reward.updated_at,
        manual_alerted_at=reward.manual_alerted_at,
        refund_detected_at=reward.refund_detected_at,
        manual_incident_version=reward.manual_incident_version,
        manual_cause=reward.manual_cause,
    )


@router.get("/manual", response_model=list[ManualReferralRewardResponse])
@inject
async def list_manual_referral_rewards(
    referral_dao: FromDishka[ReferralDao],
    _: None = Security(require_api_key),
) -> list[ManualReferralRewardResponse]:
    rewards = await referral_dao.get_manual_required_rewards(limit=100)
    return [_manual_response(reward) for reward in rewards]


@router.get(
    "/backfill/inventory",
    response_model=HistoricalReferralBackfillInventoryResponse,
)
@inject
async def inventory_historical_referral_rewards(
    backfill: FromDishka[ManageHistoricalReferralRewards],
    limit: int = 100,
    offset: int = 0,
    _: None = Security(require_api_key),
) -> dict[str, object]:
    if not 1 <= limit <= 500 or offset < 0:
        raise HTTPException(status_code=422, detail="Invalid inventory pagination")
    return await backfill.system(
        HistoricalReferralRewardBackfillDto(
            action="INVENTORY",
            limit=limit,
            offset=offset,
        )
    )


@router.post(
    "/backfill/preview",
    response_model=HistoricalReferralBackfillPreviewResponse,
)
@inject
async def preview_historical_referral_rewards(
    body: HistoricalReferralBackfillPreviewRequest,
    backfill: FromDishka[ManageHistoricalReferralRewards],
    _: None = Security(require_api_key),
) -> dict[str, object]:
    try:
        return await backfill.system(
            HistoricalReferralRewardBackfillDto(
                action="PREVIEW",
                source_transaction_ids=tuple(body.source_transaction_ids),
                operator_identity=body.operator_identity,
                operator_reference=body.operator_reference,
                reason=body.reason,
            )
        )
    except HistoricalReferralBackfillUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post(
    "/backfill/{preview_id}/apply",
    response_model=HistoricalReferralBackfillApplyResponse,
)
@inject
async def apply_historical_referral_rewards(
    preview_id: int,
    body: HistoricalReferralBackfillApplyRequest,
    backfill: FromDishka[ManageHistoricalReferralRewards],
    _: None = Security(require_api_key),
) -> dict[str, object]:
    try:
        return await backfill.system(
            HistoricalReferralRewardBackfillDto(
                action="APPLY",
                preview_id=preview_id,
                source_transaction_ids=tuple(body.source_transaction_ids),
                operator_identity=body.operator_identity,
                operator_reference=body.operator_reference,
                reason=body.reason,
                expected_config_snapshot=body.expected_config_snapshot.model_dump(),
            )
        )
    except HistoricalReferralBackfillUnavailableError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


@router.post("/{reward_id}/resolve", status_code=status.HTTP_204_NO_CONTENT)
@inject
async def resolve_manual_referral_reward(
    reward_id: int,
    body: ResolveManualReferralRewardRequest,
    resolve_reward: FromDishka[ResolveManualReferralReward],
    _: None = Security(require_api_key),
) -> None:
    try:
        await resolve_reward.system(
            ResolveManualReferralRewardDto(
                reward_id=reward_id,
                expected_version=body.expected_version,
                confirm_issued=body.resolution == "CONFIRM_ISSUED",
                operator_reference=body.operator_reference,
                reason=body.reason,
                allow_drift=body.allow_drift,
            )
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc
