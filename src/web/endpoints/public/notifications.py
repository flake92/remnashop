from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, HTTPException, Security, status

from src.application.common import NotificationChannels
from src.application.use_cases.notification import UnsubscribeEmail
from src.application.use_cases.notification.commands import UnsubscribeEmailDto
from src.core.config import AppConfig
from src.infrastructure.services.notification_channels import parse_unsubscribe_token
from src.web.dependencies import require_auth_service_key
from src.web.schemas.auth import (
    PushKeyResponse,
    PushStatusResponse,
    PushSubscriptionDeleteRequest,
    PushSubscriptionRequest,
    UnsubscribeRequest,
    UnsubscribeResponse,
)

from ._common import CurrentUser

router = APIRouter(
    prefix="/auth/notifications",
    tags=["Public - Notifications"],
    dependencies=[Security(require_auth_service_key)],
)


@router.get("/push/key", response_model=PushKeyResponse)
@inject
async def get_push_key(config: FromDishka[AppConfig]) -> PushKeyResponse:
    push = config.web_push
    return PushKeyResponse(
        enabled=push.is_configured,
        public_key=push.vapid_public_key if push.is_configured else None,
    )


@router.get("/push/subscription", response_model=PushStatusResponse)
@inject
async def get_push_status(
    user: CurrentUser,
    channels: FromDishka[NotificationChannels],
) -> PushStatusResponse:
    return PushStatusResponse(devices=await channels.push_device_count(user.id))


@router.post("/push/subscription", response_model=PushStatusResponse)
@inject
async def save_push_subscription(
    body: PushSubscriptionRequest,
    user: CurrentUser,
    channels: FromDishka[NotificationChannels],
) -> PushStatusResponse:
    if not channels.push_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, detail="Push notifications are disabled")
    try:
        await channels.save_push_subscription(
            user_id=user.id,
            endpoint=body.endpoint,
            p256dh=body.keys.p256dh.rstrip("="),
            auth=body.keys.auth.rstrip("="),
            user_agent=body.user_agent,
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)) from exc
    return PushStatusResponse(devices=await channels.push_device_count(user.id))


@router.post("/push/subscription/delete", response_model=PushStatusResponse)
@inject
async def delete_push_subscription(
    body: PushSubscriptionDeleteRequest,
    user: CurrentUser,
    channels: FromDishka[NotificationChannels],
) -> PushStatusResponse:
    await channels.delete_push_subscription(user_id=user.id, endpoint=body.endpoint)
    return PushStatusResponse(devices=await channels.push_device_count(user.id))


@router.post("/unsubscribe", response_model=UnsubscribeResponse)
@inject
async def unsubscribe_email(
    body: UnsubscribeRequest,
    config: FromDishka[AppConfig],
    unsubscribe: FromDishka[UnsubscribeEmail],
) -> UnsubscribeResponse:
    # No login: the signed token from the e-mail is the authorization, as
    # one-click unsubscribe (RFC 8058) requires.
    parsed = parse_unsubscribe_token(config.crypt_key.get_secret_value(), body.token)
    if parsed is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, detail="Invalid unsubscribe token")
    user_id, category = parsed
    await unsubscribe.system(UnsubscribeEmailDto(user_id=user_id, category=category))
    return UnsubscribeResponse(category=category.value)
