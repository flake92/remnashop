import mimetypes
from asyncio import TimeoutError as AsyncTimeoutError
from dataclasses import asdict
from io import BytesIO
from typing import Annotated, Optional

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from dishka import FromDishka
from dishka.integrations.fastapi import inject
from fastapi import APIRouter, HTTPException, Query, Response, Security, status

from src.application.common import NotificationChannels
from src.application.use_cases.notification import UnsubscribeEmail
from src.application.use_cases.notification.commands import UnsubscribeEmailDto
from src.core.config import AppConfig
from src.infrastructure.services.notification_channels import parse_unsubscribe_token
from src.web.dependencies import require_auth_service_key
from src.web.schemas.auth import (
    FeedPageResponse,
    FeedReadRequest,
    FeedUnreadResponse,
    PushKeyResponse,
    PushStatusResponse,
    PushSubscriptionDeleteRequest,
    PushSubscriptionRequest,
    UnsubscribeRequest,
    UnsubscribeResponse,
)

from ._common import CurrentUser

MEDIA_MAX_BYTES = 20 * 1024 * 1024
DEFAULT_MEDIA_TYPES = {"PHOTO": "image/jpeg", "VIDEO": "video/mp4", "GIF": "video/mp4"}

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


@router.get("/feed", response_model=FeedPageResponse)
@inject
async def get_feed(
    user: CurrentUser,
    channels: FromDishka[NotificationChannels],
    limit: Annotated[int, Query(ge=1, le=50)] = 30,
    before_id: Annotated[Optional[int], Query(ge=1)] = None,
) -> FeedPageResponse:
    page = await channels.feed_page(user.id, limit=limit, before_id=before_id)
    return FeedPageResponse.model_validate(asdict(page))


@router.get("/feed/unread", response_model=FeedUnreadResponse)
@inject
async def get_feed_unread(
    user: CurrentUser,
    channels: FromDishka[NotificationChannels],
) -> FeedUnreadResponse:
    return FeedUnreadResponse(unread_count=await channels.feed_unread_count(user.id))


@router.post("/feed/read", response_model=FeedUnreadResponse)
@inject
async def mark_feed_read(
    body: FeedReadRequest,
    user: CurrentUser,
    channels: FromDishka[NotificationChannels],
) -> FeedUnreadResponse:
    return FeedUnreadResponse(unread_count=await channels.feed_mark_read(user.id, body.up_to_id))


@router.get("/feed/{item_id}/media")
@inject
async def get_feed_media(
    item_id: int,
    user: CurrentUser,
    channels: FromDishka[NotificationChannels],
    bot: FromDishka[Bot],
) -> Response:
    media = await channels.feed_media(user.id, item_id)
    if media is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Media not found")
    try:
        # Bot API serves files up to 20 MB; a larger one stays Telegram-only.
        file = await bot.get_file(media.file_id)
        if not file.file_path or (file.file_size or 0) > MEDIA_MAX_BYTES:
            raise HTTPException(status.HTTP_404_NOT_FOUND, detail="Media is unavailable")
        target = BytesIO()
        await bot.download_file(file.file_path, destination=target, timeout=30)
    except (TelegramAPIError, AsyncTimeoutError, OSError) as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, detail="Media is unavailable") from exc
    content_type = mimetypes.guess_type(file.file_path)[0] or DEFAULT_MEDIA_TYPES.get(
        media.media_type, "application/octet-stream"
    )
    return Response(content=target.getvalue(), media_type=content_type)


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
