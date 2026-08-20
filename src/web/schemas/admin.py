from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator


class MergeUsersRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source_user_id: int = Field(gt=0)
    target_user_id: int = Field(gt=0)
    reason: str = Field(min_length=1, max_length=1024)
    email_resolution: Literal["REJECT", "KEEP_TARGET"] = "REJECT"
    telegram_resolution: Literal["REJECT", "KEEP_SOURCE"] = "REJECT"
    payment_resolution: Literal["REJECT", "REKEY_SOURCE"] = "REJECT"


class MergeUsersTargetResponse(BaseModel):
    id: int
    email: str | None
    telegram_id: int | None
    is_email_verified: bool
    current_subscription_id: int | None


class MergeUsersResponse(BaseModel):
    dry_run: bool
    source_user_id: int
    target_user_id: int
    target: MergeUsersTargetResponse
    moved: dict[str, int]
    conflicts: list[str]
    requires_relogin: bool


class ManualReferralRewardResponse(BaseModel):
    id: int
    user_id: int
    source_transaction_id: int | None
    origin_referral_id: int | None
    level: int | None
    type: str
    amount: int
    state: str
    is_issued: bool
    last_error: str | None
    attempt_count: int
    target_subscription_id: int | None
    baseline_expire_at: datetime | None
    target_expire_at: datetime | None
    created_at: datetime | None
    updated_at: datetime | None
    manual_alerted_at: datetime | None
    refund_detected_at: datetime | None
    manual_incident_version: int
    manual_cause: str | None


class ResolveManualReferralRewardRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    resolution: Literal["CONFIRM_ISSUED", "CANCEL"]
    expected_version: int = Field(ge=0)
    operator_reference: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=1024)
    allow_drift: bool = False


class HistoricalReferralBackfillIntentResponse(BaseModel):
    source_transaction_id: int
    payer_user_id: int
    recipient_user_id: int
    origin_referral_id: int
    reward_referral_id: int
    level: int
    amount: int
    config_value: int
    reward_type: str
    reward_strategy: str
    accrual_strategy: str


class HistoricalReferralBackfillTransactionResponse(BaseModel):
    source_transaction_id: int
    payer_user_id: int | None
    fulfillment_completed_at: str | None
    intents: list[HistoricalReferralBackfillIntentResponse]
    errors: list[str]


class HistoricalReferralBackfillConfigSnapshot(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    enabled: StrictBool
    max_level: StrictInt = Field(ge=1, le=2)
    accrual_strategy: Literal["ON_FIRST_PAYMENT", "ON_EACH_PAYMENT"]
    reward_type: Literal["POINTS", "EXTRA_DAYS"]
    reward_strategy: Literal["AMOUNT", "PERCENT"]
    reward_config: dict[Literal["1", "2"], StrictInt]

    @model_validator(mode="after")
    def validate_positive_enabled_level_config(
        self,
    ) -> "HistoricalReferralBackfillConfigSnapshot":
        allowed_keys = {str(level) for level in range(1, self.max_level + 1)}
        if not set(self.reward_config) <= allowed_keys:
            raise ValueError("reward_config contains a disabled referral level")
        if any(value <= 0 for value in self.reward_config.values()):
            raise ValueError("reward_config values must be positive strict integers")
        return self


class HistoricalReferralBackfillInventoryResponse(BaseModel):
    read_only: bool
    limit: int
    offset: int
    config_snapshot: HistoricalReferralBackfillConfigSnapshot
    candidates: list[HistoricalReferralBackfillTransactionResponse]


class HistoricalReferralBackfillPreviewRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    source_transaction_ids: list[int] = Field(min_length=1, max_length=100)
    operator_identity: str = Field(min_length=1, max_length=128)
    operator_reference: str = Field(min_length=1, max_length=256)
    reason: str = Field(min_length=1, max_length=1024)


class HistoricalReferralBackfillPreviewResponse(BaseModel):
    preview_id: int
    status: Literal["PREVIEWED", "APPLIED"]
    source_transaction_ids: list[int]
    config_snapshot: HistoricalReferralBackfillConfigSnapshot
    can_apply: bool
    transactions: list[HistoricalReferralBackfillTransactionResponse]
    intents: list[HistoricalReferralBackfillIntentResponse]


class HistoricalReferralBackfillApplyRequest(HistoricalReferralBackfillPreviewRequest):
    expected_config_snapshot: HistoricalReferralBackfillConfigSnapshot


class HistoricalReferralBackfillApplyResponse(BaseModel):
    preview_id: int
    status: Literal["APPLIED"]
    created_intents: int
    idempotent_replay: bool
