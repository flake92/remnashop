from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from src.core.enums import PaymentGatewayType


class SubscriptionInfoResponse(BaseModel):
    user_remna_id: str
    status: str
    is_trial: bool
    traffic_limit: int
    device_limit: int
    traffic_limit_strategy: str
    expire_at: datetime
    url: str
    plan_name: str
    plan_duration_days: int
    used_traffic_bytes: Optional[int] = None
    lifetime_used_traffic_bytes: Optional[int] = None
    online_at: Optional[datetime] = None


class DeviceResponse(BaseModel):
    hwid: str
    platform: Optional[str] = None
    device_model: Optional[str] = None
    os_version: Optional[str] = None
    user_agent: Optional[str] = None


class DevicesResponse(BaseModel):
    devices: list[DeviceResponse]
    current_count: int
    max_count: int


class DeviceDeleteResponse(BaseModel):
    deleted: bool


class DevicesDeleteAllResponse(BaseModel):
    success: bool


class PromocodeActivateRequest(BaseModel):
    code: str


class PromocodeActivateResponse(BaseModel):
    success: bool
    reward_type: str


class TrialPurchaseRequest(BaseModel):
    gateway_type: PaymentGatewayType


class ReissueResponse(BaseModel):
    success: bool


class PurchaseRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    plan_code: str = Field(min_length=3, max_length=64)
    duration_days: int = Field(ge=0)
    gateway_type: PaymentGatewayType


class ExtendRequest(BaseModel):
    duration_days: int = Field(ge=0)
    gateway_type: PaymentGatewayType


class PaymentInitResponse(BaseModel):
    payment_id: str
    payment_url: Optional[str] = None
    purchase_type: str
    status: str
    is_free: bool
    final_amount: str
    currency: str


class PaymentTransactionResponse(BaseModel):
    payment_id: str
    purchase_type: str
    status: str
    gateway_type: PaymentGatewayType
    final_amount: str
    currency: str
    plan_name: Optional[str] = None
    duration_days: Optional[int] = None
    device_limit: Optional[int] = None
    traffic_limit: Optional[int] = None
    created_at: datetime
    updated_at: datetime


class GatewayOfferResponse(BaseModel):
    gateway_type: PaymentGatewayType
    currency: str
    currency_symbol: str


class DurationGatewayPriceResponse(BaseModel):
    gateway_type: PaymentGatewayType
    currency: str
    currency_symbol: str
    original_amount: str
    discount_percent: int
    final_amount: str
    is_free: bool


class DurationOfferResponse(BaseModel):
    days: int
    prices: list[DurationGatewayPriceResponse]


class TrialActivateResponse(BaseModel):
    is_free: bool
    activated: bool
    duration_days: int
    gateways: list[DurationGatewayPriceResponse] = []


class PlanOfferResponse(BaseModel):
    id: int
    public_code: str
    name: str
    description: Optional[str] = None
    traffic_limit: int
    device_limit: int
    type: str
    recommended_purchase_type: str
    durations: list[DurationOfferResponse]


class SubscriptionOffersResponse(BaseModel):
    gateways: list[GatewayOfferResponse]
    plans: list[PlanOfferResponse]
    has_current_subscription: bool
    current_subscription_status: Optional[str] = None
