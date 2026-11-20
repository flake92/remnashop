import logging

import pytest

from src.core.logger import ReadinessProbeAccessFilter


def _access(method: str, path: str, status: int) -> logging.LogRecord:
    return logging.LogRecord(
        name="uvicorn.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg='%s - "%s %s HTTP/%s" %d',
        args=("172.18.0.11:51446", method, path, "1.1", status),
        exc_info=None,
    )


@pytest.mark.parametrize(
    ("path", "status"),
    [
        ("/api/v1/public/auth/email/start", 422),
        ("/api/v1/public/auth/identify", 422),
        ("/api/v1/public/auth/service-session", 422),
        ("/api/v1/public/auth/notification-preferences", 405),
    ],
)
def test_expected_readiness_refusals_are_dropped(path: str, status: int) -> None:
    assert ReadinessProbeAccessFilter().filter(_access("POST", path, status)) is False


@pytest.mark.parametrize(
    ("method", "path", "status"),
    [
        # A real sign-in on the same route.
        ("POST", "/api/v1/public/auth/email/start", 200),
        # A contract break must stay visible.
        ("POST", "/api/v1/public/auth/identify", 404),
        ("POST", "/api/v1/public/auth/notification-preferences", 422),
        ("GET", "/api/v1/public/auth/notification-preferences", 405),
        ("POST", "/api/v1/public/plans/public", 422),
    ],
)
def test_other_requests_are_still_logged(method: str, path: str, status: int) -> None:
    assert ReadinessProbeAccessFilter().filter(_access(method, path, status)) is True


def test_records_without_access_args_pass() -> None:
    record = logging.LogRecord("uvicorn.access", logging.INFO, __file__, 1, "plain", None, None)
    assert ReadinessProbeAccessFilter().filter(record) is True
