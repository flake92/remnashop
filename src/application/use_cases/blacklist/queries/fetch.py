from src.application.common import HttpClient, Interactor
from src.application.dto import UserDto
from src.application.use_cases.blacklist.limits import (
    MAX_BLACKLIST_IDS_PER_SOURCE,
    MAX_TELEGRAM_USER_ID,
)
from src.core.utils.validators import parse_int


class FetchBlacklistIds(Interactor[str, list[int]]):
    required_permission = None

    def __init__(self, http_client: HttpClient) -> None:
        self.http_client = http_client

    async def _execute(self, actor: UserDto, url: str) -> list[int]:
        text = await self.http_client.get_text(url)
        if text is None:
            return []
        return _parse_ids(text)


class ParseBlacklistIds(Interactor[str, list[int]]):
    required_permission = None

    async def _execute(self, actor: UserDto, text: str) -> list[int]:
        return _parse_ids(text)


def _parse_ids(text: str) -> list[int]:
    ids: list[int] = []
    seen: set[int] = set()
    for line in text.splitlines():
        token = line.strip().split()[0].rstrip(",;") if line.strip() else ""
        parsed = parse_int(token)
        if parsed is None or parsed < 1 or parsed > MAX_TELEGRAM_USER_ID or parsed in seen:
            continue
        if len(ids) >= MAX_BLACKLIST_IDS_PER_SOURCE:
            raise ValueError(
                "Blacklist source contains more than "
                f"{MAX_BLACKLIST_IDS_PER_SOURCE} unique user IDs"
            )
        seen.add(parsed)
        ids.append(parsed)
    return ids
