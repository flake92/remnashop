from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.application.common.notification_channels import NotificationCategory
from src.application.dto import MessagePayloadDto
from src.application.use_cases.user.commands.messaging import (
    SendMessageToUser,
    SendMessageToUserDto,
)

ACTOR = SimpleNamespace(role=2, log="admin")
TARGET = SimpleNamespace(id=17, role=1)
PAYLOAD = MessagePayloadDto(i18n_key="raw-message")


def _use_case(*, queued: int, message: object) -> tuple[SendMessageToUser, AsyncMock]:
    notifier = SimpleNamespace(
        copy_to_channels=AsyncMock(return_value=queued),
        notify_user=AsyncMock(return_value=message),
    )
    user_dao = SimpleNamespace(get_by_id=AsyncMock(return_value=TARGET))
    use_case = SendMessageToUser(user_dao, notifier)  # type: ignore[arg-type]
    return use_case, notifier.copy_to_channels


async def test_direct_message_goes_to_every_channel() -> None:
    use_case, copy = _use_case(queued=1, message=object())

    sent = await use_case._execute(ACTOR, SendMessageToUserDto(17, PAYLOAD))  # type: ignore[arg-type]

    assert sent is True
    copy.assert_awaited_once()
    assert copy.await_args.args == ([TARGET], PAYLOAD)
    assert copy.await_args.kwargs["category"] is NotificationCategory.SERVICE


async def test_repeated_direct_messages_are_not_deduplicated() -> None:
    use_case, copy = _use_case(queued=1, message=object())

    await use_case._execute(ACTOR, SendMessageToUserDto(17, PAYLOAD))  # type: ignore[arg-type]
    await use_case._execute(ACTOR, SendMessageToUserDto(17, PAYLOAD))  # type: ignore[arg-type]

    prefixes = {call.kwargs["dedupe_prefix"] for call in copy.await_args_list}
    assert len(prefixes) == 2


async def test_web_only_user_counts_as_delivered_through_the_copies() -> None:
    use_case, _ = _use_case(queued=1, message=None)

    assert await use_case._execute(ACTOR, SendMessageToUserDto(17, PAYLOAD)) is True  # type: ignore[arg-type]


async def test_nothing_delivered_is_reported() -> None:
    use_case, _ = _use_case(queued=0, message=None)

    assert await use_case._execute(ACTOR, SendMessageToUserDto(17, PAYLOAD)) is False  # type: ignore[arg-type]
