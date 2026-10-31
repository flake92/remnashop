from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.application.common import NotificationCategory
from src.application.dto import UserDto
from src.application.use_cases.notification.commands import (
    UnsubscribeEmail,
    UnsubscribeEmailDto,
    UpdateNotificationPreferences,
    UpdateNotificationPreferencesDto,
)
from src.web.endpoints.public.notifications import router
from src.web.schemas.auth import RegisterRequest, UpdateNotificationPreferencesRequest


def test_channel_routes_live_under_the_service_key_protected_auth_prefix() -> None:
    routes = {
        (route.path, method)
        for route in router.routes
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert routes == {
        ("/auth/notifications/push/key", "GET"),
        ("/auth/notifications/push/subscription", "GET"),
        ("/auth/notifications/push/subscription", "POST"),
        ("/auth/notifications/push/subscription/delete", "POST"),
        ("/auth/notifications/feed", "GET"),
        ("/auth/notifications/feed/read", "POST"),
        ("/auth/notifications/feed/unread", "GET"),
        ("/auth/notifications/feed/{item_id}/media", "GET"),
        ("/auth/notifications/unsubscribe", "POST"),
    }
    assert router.dependencies


def test_push_key_probe_distinguishes_a_present_route() -> None:
    # Clean Pay's rollout preflight posts to this GET-only path: 405 proves the
    # 0060 API is deployed, 404 means an older Remnashop image.
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/public")
    response = TestClient(app).post("/api/v1/public/auth/notifications/push/key", json={})
    assert response.status_code == 405


def test_preference_update_needs_at_least_one_strict_boolean() -> None:
    with pytest.raises(ValidationError):
        UpdateNotificationPreferencesRequest()
    with pytest.raises(ValidationError):
        UpdateNotificationPreferencesRequest(news_email_enabled="true")  # type: ignore[arg-type]
    assert UpdateNotificationPreferencesRequest(news_email_enabled=True).news_email_enabled


def test_registration_news_consent_is_explicit_and_off_by_default() -> None:
    body = {"email": "user@example.org", "password": "long-password"}
    assert RegisterRequest(**body).email_news_consent is False
    assert RegisterRequest(**body, email_news_consent=True).email_news_consent is True
    with pytest.raises(ValidationError):
        RegisterRequest(**body, email_news_consent="yes")  # type: ignore[arg-type]


def _user(**overrides: object) -> UserDto:
    values: dict[str, object] = {
        "id": 17,
        "name": "user",
        "email": "user@example.org",
        "is_email_verified": True,
    }
    values.update(overrides)
    return UserDto(**values)  # type: ignore[arg-type]


class _Uow:
    def __init__(self) -> None:
        self.commit = AsyncMock()

    async def __aenter__(self) -> "_Uow":
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


def _config(*, bulk_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        email=SimpleNamespace(from_email="notice@example.org"),
        bulk_email=SimpleNamespace(enabled=bulk_enabled, from_email="news@example.org"),
        web_push=SimpleNamespace(is_configured=True),
    )


async def test_disabled_bulk_account_address_is_not_advertised() -> None:
    updated = _user()
    user_dao = SimpleNamespace(
        set_email_channel_preferences=AsyncMock(return_value=updated),
        set_subscription_expiration_email_preference=AsyncMock(return_value=updated),
    )
    use_case = UpdateNotificationPreferences(
        _config(bulk_enabled=False),  # type: ignore[arg-type]
        user_dao,  # type: ignore[arg-type]
        _Uow(),  # type: ignore[arg-type]
    )

    result = await use_case._execute(
        _user(), UpdateNotificationPreferencesDto(service_email_enabled=True)
    )

    assert result.sender_email == "notice@example.org"
    assert result.bulk_sender_email is None


async def test_service_switch_also_switches_expiry_reminders() -> None:
    updated = _user(email_service_notifications_enabled=False)
    user_dao = SimpleNamespace(
        set_email_channel_preferences=AsyncMock(return_value=updated),
        set_subscription_expiration_email_preference=AsyncMock(return_value=updated),
    )
    use_case = UpdateNotificationPreferences(_config(), user_dao, _Uow())  # type: ignore[arg-type]

    result = await use_case._execute(
        _user(), UpdateNotificationPreferencesDto(service_email_enabled=False)
    )

    user_dao.set_email_channel_preferences.assert_awaited_once_with(17, service=False, news=None)
    user_dao.set_subscription_expiration_email_preference.assert_awaited_once_with(
        17, enabled=False
    )
    assert result.service_email_enabled is False
    assert result.bulk_sender_email == "news@example.org"
    assert result.push_available is True


async def test_news_consent_does_not_touch_reminders() -> None:
    updated = _user(email_news_enabled=True)
    user_dao = SimpleNamespace(
        set_email_channel_preferences=AsyncMock(return_value=updated),
        set_subscription_expiration_email_preference=AsyncMock(),
    )
    use_case = UpdateNotificationPreferences(_config(), user_dao, _Uow())  # type: ignore[arg-type]

    result = await use_case._execute(
        _user(), UpdateNotificationPreferencesDto(news_email_enabled=True)
    )

    user_dao.set_email_channel_preferences.assert_awaited_once_with(17, service=None, news=True)
    user_dao.set_subscription_expiration_email_preference.assert_not_awaited()
    assert result.news_email_enabled is True


async def test_unsubscribe_turns_off_only_the_category_of_the_link() -> None:
    user = _user()
    user_dao = SimpleNamespace(
        set_email_channel_preferences=AsyncMock(return_value=user),
        set_subscription_expiration_email_preference=AsyncMock(return_value=user),
    )
    use_case = UnsubscribeEmail(user_dao, _Uow())  # type: ignore[arg-type]

    assert await use_case._execute(
        user, UnsubscribeEmailDto(user_id=17, category=NotificationCategory.NEWS)
    )
    user_dao.set_email_channel_preferences.assert_awaited_once_with(17, news=False)
    user_dao.set_subscription_expiration_email_preference.assert_not_awaited()

    user_dao.set_email_channel_preferences.reset_mock()
    assert await use_case._execute(
        user, UnsubscribeEmailDto(user_id=17, category=NotificationCategory.SERVICE)
    )
    user_dao.set_email_channel_preferences.assert_awaited_once_with(17, service=False)
    user_dao.set_subscription_expiration_email_preference.assert_awaited_once_with(
        17, enabled=False
    )
