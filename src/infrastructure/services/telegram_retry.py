import asyncio
from typing import Awaitable, Callable, TypeVar

from aiohttp import (
    ClientConnectorCertificateError,
    ClientConnectorError,
    ServerDisconnectedError,
)
from loguru import logger

T = TypeVar("T")

# Retry a call only while Telegram could not be reached. The home router restarts its
# VPN tunnel when it switches nodes, which leaves api.telegram.org unreachable for
# 15-25 s, so the pauses cover about a minute. Each pause is awaited with asyncio.sleep
# inside the calling task, so the bot keeps serving other updates; the budget caps how
# long a single call can hold its task, counting the time the failed attempts took.
TELEGRAM_CONNECT_RETRY_DELAYS: tuple[float, ...] = (1.0, 3.0, 5.0, 10.0, 15.0, 20.0)
TELEGRAM_CONNECT_RETRY_BUDGET = 90.0

try:
    from python_socks import ProxyConnectionError, ProxyError, ProxyTimeoutError

    _PROXY_CONNECT_ERRORS: tuple[type[BaseException], ...] = (
        ProxyConnectionError,
        ProxyError,
        ProxyTimeoutError,
    )
except ImportError:  # pragma: no cover - python_socks ships with aiohttp-socks
    _PROXY_CONNECT_ERRORS = ()


def is_telegram_unreachable_error(error: BaseException) -> bool:
    """True when the request failed because the connection to Telegram broke, not
    because Telegram answered: DNS failures, refused connections, resets during the TLS
    handshake, a SOCKS proxy that could not open the tunnel, or a pooled keep-alive
    connection the tunnel restart dropped before any response (ServerDisconnectedError).
    Timeouts are not retried, because Telegram may have delivered the message and is
    only slow to answer. Certificate errors and API errors (4xx, 429) are not transient
    network failures either."""
    current: BaseException | None = error
    while current is not None:
        if isinstance(current, ClientConnectorCertificateError):
            return False
        if isinstance(current, (ClientConnectorError, ServerDisconnectedError)):
            return True
        if _PROXY_CONNECT_ERRORS and isinstance(current, _PROXY_CONNECT_ERRORS):
            return True
        current = current.__cause__
    return False


async def call_with_connect_retry(
    call: Callable[[], Awaitable[T]],
    log_context: str,
) -> T:
    loop = asyncio.get_running_loop()
    started = loop.time()
    delays = (*TELEGRAM_CONNECT_RETRY_DELAYS, None)
    for attempt, delay in enumerate(delays, start=1):
        try:
            return await call()
        except Exception as error:
            if (
                delay is None
                or not is_telegram_unreachable_error(error)
                or loop.time() - started + delay > TELEGRAM_CONNECT_RETRY_BUDGET
            ):
                raise
            logger.warning(
                f"Telegram is unreachable ({type(error).__name__}) during {log_context}; "
                f"attempt {attempt} of {len(delays)}, retrying in {delay:g}s"
            )
            await asyncio.sleep(delay)
    raise AssertionError("unreachable")  # pragma: no cover
