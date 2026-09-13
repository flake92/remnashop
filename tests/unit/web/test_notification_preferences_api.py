from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.application.dto import UserDto
from src.core.exceptions import EmailDeliveryRateDeferredError
from src.web.endpoints.public.auth import request_email_verification_code, router
from src.web.schemas import (
    NotificationPreferencesResponse,
    RequestEmailVerificationCodeRequest,
    UpdateNotificationPreferencesRequest,
)


def test_notification_preferences_api_is_authenticated_and_not_a_send_endpoint() -> None:
    routes = [route for route in router.routes if isinstance(route, APIRoute)]
    get_route = next(
        route
        for route in routes
        if route.path == "/auth/notification-preferences" and route.methods == {"GET"}
    )
    assert get_route.methods == {"GET"}
    assert get_route.dependencies

    patch_route = next(
        route
        for route in routes
        if route.path == "/auth/notification-preferences" and route.methods == {"PATCH"}
    )
    assert patch_route.dependencies
    assert all("send-email" not in route.path for route in routes)


def test_notification_preferences_response_contract() -> None:
    response = NotificationPreferencesResponse(
        subscription_expiration_email_enabled=False,
        email_eligible=True,
        sender_email="notice@example.org",
        days_before=[7, 3, 1],
    )

    assert response.model_dump() == {
        "subscription_expiration_email_enabled": False,
        "email_eligible": True,
        "sender_email": "notice@example.org",
        "days_before": [7, 3, 1],
    }


def test_notification_opt_in_requires_an_explicit_json_boolean() -> None:
    with pytest.raises(ValidationError):
        UpdateNotificationPreferencesRequest(
            subscription_expiration_email_enabled="true",  # type: ignore[arg-type]
        )
    with pytest.raises(ValidationError):
        UpdateNotificationPreferencesRequest(
            subscription_expiration_email_enabled=1,  # type: ignore[arg-type]
        )


def test_notification_preferences_path_has_a_side_effect_free_rollout_probe() -> None:
    app = FastAPI()
    app.include_router(router, prefix="/api/v1/public")

    # Clean Pay probes the exact path with an unsupported method before it has
    # a user session. FastAPI must distinguish the present route (405) from an
    # older image where the path is absent (404), without invoking a use case.
    response = TestClient(app).post(
        "/api/v1/public/auth/notification-preferences",
        json={},
    )

    assert response.status_code == 405


@pytest.mark.asyncio
async def test_transactional_rate_defer_is_a_clear_retryable_503() -> None:
    request_verification = AsyncMock(
        side_effect=EmailDeliveryRateDeferredError("internal pacing detail")
    )

    with pytest.raises(HTTPException) as raised:
        await request_email_verification_code.__dishka_orig_func__(
            body=RequestEmailVerificationCodeRequest(email="user@example.org"),
            user=UserDto(id=17, name="User"),
            request_verification=request_verification,
        )

    assert raised.value.status_code == 503
    assert raised.value.detail == ("Email delivery is temporarily busy. Please try again shortly.")
    assert "remna" not in str(raised.value.detail).lower()
