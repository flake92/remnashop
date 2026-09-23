from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.infrastructure.database.dao.broadcast import BroadcastDaoImpl


@pytest.mark.asyncio
async def test_get_by_task_id_refreshes_request_session_identity_map() -> None:
    session = MagicMock()
    session.scalar = AsyncMock(return_value=None)
    conversion_retort = MagicMock()
    conversion_retort.get_converter.return_value = MagicMock()
    dao = BroadcastDaoImpl(
        session=session,
        retort=MagicMock(),
        conversion_retort=conversion_retort,
        redis=MagicMock(),
    )

    assert await dao.get_by_task_id(uuid4()) is None

    statement = session.scalar.await_args.args[0]
    assert statement.get_execution_options()["populate_existing"] is True
