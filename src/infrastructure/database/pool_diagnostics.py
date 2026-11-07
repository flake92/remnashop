"""Name the code that leaked a pooled connection.

SQLAlchemy only says "The garbage collector is trying to clean up non-checked-in
connection …" and gives no hint about who took that connection. Each checkout here
remembers the application frames that asked for it, and when the garbage collector
detaches an abandoned connection the warning names that place.
"""

import sys
from types import FrameType
from typing import Any, Final, Optional

from greenlet import getcurrent  # type: ignore[import-untyped]
from loguru import logger
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine

_INFO_KEY: Final[str] = "remnashop_checkout_site"
_MAX_FRAMES: Final[int] = 8
_APP_MARKER: Final[str] = "/src/"
_SELF: Final[str] = __file__.replace("\\", "/")


def _parent_greenlet_frame() -> Optional[FrameType]:
    # AsyncSession runs its sync core inside a greenlet: the coroutines that awaited
    # the query live on the parent greenlet's stack, not on the current one.
    parent = getcurrent().parent
    return getattr(parent, "gr_frame", None) if parent is not None else None


def _app_frames(*starts: Optional[FrameType]) -> list[str]:
    sites: list[str] = []
    for start in starts:
        frame = start
        while frame is not None and len(sites) < _MAX_FRAMES:
            filename = frame.f_code.co_filename.replace("\\", "/")
            if _APP_MARKER in filename and filename != _SELF:
                path = filename[filename.rindex(_APP_MARKER) + 1 :]
                sites.append(f"{path}:{frame.f_lineno} {frame.f_code.co_name}")
            frame = frame.f_back
    return sites


def checkout_site() -> str:
    sites = _app_frames(sys._getframe(1), _parent_greenlet_frame())
    return " <- ".join(sites) if sites else "unknown (no application frame)"


def _on_checkout(dbapi_connection: Any, connection_record: Any, connection_proxy: Any) -> None:
    connection_record.info[_INFO_KEY] = checkout_site()


def _on_checkin(dbapi_connection: Any, connection_record: Any) -> None:
    if connection_record is not None:
        connection_record.info.pop(_INFO_KEY, None)


def _on_detach(dbapi_connection: Any, connection_record: Any) -> None:
    # The application never detaches pooled connections itself, so a detach is the
    # garbage-collector path that precedes SQLAlchemy's "non-checked-in" error.
    site = connection_record.info.pop(_INFO_KEY, None) if connection_record is not None else None
    if site:
        logger.warning(f"Pooled DB connection was abandoned; it was checked out at: {site}")


def install_pool_diagnostics(engine: AsyncEngine) -> None:
    pool = engine.sync_engine.pool
    event.listen(pool, "checkout", _on_checkout)
    event.listen(pool, "checkin", _on_checkin)
    event.listen(pool, "detach", _on_detach)
