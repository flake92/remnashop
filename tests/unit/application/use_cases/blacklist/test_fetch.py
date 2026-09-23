from unittest.mock import Mock

import pytest

from src.application.use_cases.blacklist.queries.fetch import (
    MAX_BLACKLIST_IDS_PER_SOURCE,
    MAX_TELEGRAM_USER_ID,
    _parse_ids,
)
from src.application.use_cases.user.commands.blocking import BlockUsersByIds


def test_parse_ids_deduplicates_and_rejects_values_outside_database_range() -> None:
    text = (
        "42 first\n"
        "42 duplicate\n"
        "0\n"
        "-7\n"
        f"{MAX_TELEGRAM_USER_ID + 1}\n"
        f"{MAX_TELEGRAM_USER_ID}, trailing\n"
        "not-an-id\n"
    )

    assert _parse_ids(text) == [42, MAX_TELEGRAM_USER_ID]


def test_parse_ids_rejects_oversized_unique_source_instead_of_truncating() -> None:
    text = "\n".join(str(value) for value in range(1, MAX_BLACKLIST_IDS_PER_SOURCE + 2))

    with pytest.raises(ValueError, match="more than 20000 unique user IDs"):
        _parse_ids(text)


@pytest.mark.asyncio
async def test_manual_block_rejects_invalid_or_oversized_id_sets_before_database_work() -> None:
    interactor = BlockUsersByIds(Mock(), Mock(), Mock())
    actor = Mock()

    with pytest.raises(ValueError, match="positive signed 64-bit"):
        await interactor._execute(actor, [0])

    with pytest.raises(ValueError, match="more than 20000 IDs"):
        await interactor._execute(
            actor,
            list(range(1, MAX_BLACKLIST_IDS_PER_SOURCE + 2)),
        )
