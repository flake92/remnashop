from typing import Optional

from pydantic import BaseModel


class PublicPlanDurationResponse(BaseModel):
    days: int
    price_rub: str


class PublicPlanLandingResponse(BaseModel):
    public_code: str
    name: str
    description: Optional[str] = None
    traffic_limit: int
    device_limit: int
    monthly_from_rub: str
    max_duration_days: int
    max_duration_price_rub: str
    # full RUB price list; the support bot uses it to quote plan changes
    durations: list[PublicPlanDurationResponse] = []


class PublicPlanLandingListResponse(BaseModel):
    plans: list[PublicPlanLandingResponse]
    # admin switch «Перерасчёт при смене тарифа»
    change_credit_enabled: bool = False
