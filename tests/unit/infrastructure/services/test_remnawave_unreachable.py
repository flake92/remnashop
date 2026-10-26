from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from uuid import UUID

import httpx
import pytest

from src.core.exceptions import RemnawaveUnreachableError
from src.infrastructure.services.remnawave import RemnawaveImpl, never_reached_panel


def chained(outer: Exception, cause: BaseException) -> Exception:
    try:
        raise outer from cause
    except Exception as exc:  # noqa: BLE001
        return exc


@pytest.mark.parametrize(
    "error",
    [
        httpx.ConnectTimeout("tls handshake"),
        httpx.ConnectError("connection refused"),
        httpx.PoolTimeout("no free connection"),
        chained(RuntimeError("sdk wrapper"), httpx.ConnectTimeout("tls handshake")),
    ],
)
def test_connection_phase_failures_never_reached_panel(error: Exception) -> None:
    assert never_reached_panel(error)


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("response lost"),
        httpx.WriteTimeout("body partially sent"),
        httpx.RemoteProtocolError("server disconnected"),
        httpx.ReadError("reset"),
        RuntimeError("panel returned 500"),
    ],
)
def test_ambiguous_failures_are_not_classified_as_unsent(error: Exception) -> None:
    assert not never_reached_panel(error)


class _Lock:
    def hold(self, user_id: int) -> Any:
        lock = self

        class _Ctx:
            async def __aenter__(self) -> Any:
                return lock

            async def __aexit__(self, *args: object) -> None:
                return None

        return _Ctx()


def remnawave(users: Any) -> RemnawaveImpl:
    impl = RemnawaveImpl(SimpleNamespace(users=users), _Lock())  # type: ignore[arg-type]
    request = SimpleNamespace(username="u", uuid=UUID(int=1))
    impl._build_create_request = lambda *args, **kwargs: request  # type: ignore[method-assign]
    impl._build_update_request = lambda *args, **kwargs: request  # type: ignore[method-assign]
    return impl


@pytest.mark.asyncio
async def test_create_user_connect_timeout_is_unreachable() -> None:
    impl = remnawave(
        SimpleNamespace(create_user=AsyncMock(side_effect=httpx.ConnectTimeout("tls")))
    )

    with pytest.raises(RemnawaveUnreachableError):
        await impl.create_user(SimpleNamespace(id=1))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_create_user_read_timeout_stays_ambiguous() -> None:
    impl = remnawave(SimpleNamespace(create_user=AsyncMock(side_effect=httpx.ReadTimeout("lost"))))

    with pytest.raises(httpx.ReadTimeout):
        await impl.create_user(SimpleNamespace(id=1))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_traffic_reset_after_applied_update_is_not_unreachable() -> None:
    users = SimpleNamespace(
        update_user=AsyncMock(
            return_value=SimpleNamespace(username="u", uuid=UUID(int=1), telegram_id=1)
        ),
        reset_user_traffic=AsyncMock(side_effect=httpx.ConnectTimeout("tls")),
    )
    impl = remnawave(users)

    with pytest.raises(Exception) as caught:
        await impl.update_user(
            SimpleNamespace(id=1),  # type: ignore[arg-type]
            uuid=UUID(int=1),
            reset_traffic=True,
        )

    assert not isinstance(caught.value, RemnawaveUnreachableError)
