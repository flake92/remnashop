from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum, auto
from typing import Final, Optional

import orjson
from loguru import logger
from redis.asyncio import Redis

from src.application.common import EmailSender, EventPublisher
from src.application.events import SmtpRestoredEvent, SmtpUnavailableEvent
from src.core.config import AppConfig

SMTP_HEALTH_STATE_KEY: Final[str] = "smtp_health:state"
# One failed probe is often a Postfix reload or a short network blip; alert once
# the server has failed two probes in a row (5-10 minutes with the 5-minute cron).
SMTP_HEALTH_ALERT_AFTER_FAILURES: Final[int] = 2
SMTP_HEALTH_REPEAT_SECONDS: Final[float] = 12 * 3600
_STATE_TTL_SECONDS: Final[int] = 7 * 24 * 3600
_MSK: Final[timezone] = timezone(timedelta(hours=3))


@dataclass(frozen=True)
class SmtpHealthState:
    failures: int = 0
    failing_since: Optional[float] = None
    last_code: Optional[str] = None
    alerted_at: Optional[float] = None


class SmtpHealthAction(Enum):
    NONE = auto()
    ALERT = auto()
    REPEAT = auto()
    RESTORED = auto()


def evaluate_smtp_probe(
    state: SmtpHealthState,
    error_code: Optional[str],
    now: float,
) -> tuple[SmtpHealthState, SmtpHealthAction]:
    if error_code is None:
        # announce recovery only if the outage was announced
        if state.alerted_at is not None:
            return SmtpHealthState(), SmtpHealthAction.RESTORED
        return SmtpHealthState(), SmtpHealthAction.NONE

    state = replace(
        state,
        failures=state.failures + 1,
        failing_since=state.failing_since if state.failing_since is not None else now,
        last_code=error_code,
    )
    if state.failures < SMTP_HEALTH_ALERT_AFTER_FAILURES:
        return state, SmtpHealthAction.NONE
    if state.alerted_at is None:
        return replace(state, alerted_at=now), SmtpHealthAction.ALERT
    if now - state.alerted_at >= SMTP_HEALTH_REPEAT_SECONDS:
        return replace(state, alerted_at=now), SmtpHealthAction.REPEAT
    return state, SmtpHealthAction.NONE


def format_msk(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=_MSK).strftime("%d.%m %H:%M МСК")


def format_duration(seconds: float) -> str:
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    if hours == 0:
        return f"{minutes} мин"
    if minutes == 0:
        return f"{hours} ч"
    return f"{hours} ч {minutes} мин"


class SmtpHealthMonitor:
    def __init__(
        self,
        config: AppConfig,
        email_sender: EmailSender,
        redis: Redis,
        event_publisher: EventPublisher,
    ) -> None:
        self.config = config
        self.email_sender = email_sender
        self.redis = redis
        self.event_publisher = event_publisher

    @property
    def _server(self) -> str:
        return f"{self.config.email.host}:{self.config.email.port}"

    async def check(self, now: float) -> SmtpHealthAction:
        if not self.email_sender.is_enabled:
            await self.redis.delete(SMTP_HEALTH_STATE_KEY)
            return SmtpHealthAction.NONE

        error_code = await self.email_sender.probe()
        previous = await self._load_state()
        state, action = evaluate_smtp_probe(previous, error_code, now)

        if error_code is None:
            await self.redis.delete(SMTP_HEALTH_STATE_KEY)
        else:
            logger.warning(
                f"SMTP probe failed (code={error_code}, failures in a row={state.failures})"
            )
            await self.redis.set(
                SMTP_HEALTH_STATE_KEY,
                orjson.dumps(asdict(state)),
                ex=_STATE_TTL_SECONDS,
            )

        if action in (SmtpHealthAction.ALERT, SmtpHealthAction.REPEAT):
            assert state.failing_since is not None and state.last_code is not None
            await self.event_publisher.publish(
                SmtpUnavailableEvent(
                    server=self._server,
                    error_code=state.last_code,
                    failing_since=format_msk(state.failing_since),
                    failing_for=format_duration(now - state.failing_since),
                    failed_checks=state.failures,
                    is_repeat=action is SmtpHealthAction.REPEAT,
                )
            )
        elif action is SmtpHealthAction.RESTORED:
            assert previous.failing_since is not None
            logger.info("SMTP server is reachable again")
            await self.event_publisher.publish(
                SmtpRestoredEvent(
                    server=self._server,
                    failing_since=format_msk(previous.failing_since),
                    failing_for=format_duration(now - previous.failing_since),
                )
            )
        return action

    async def _load_state(self) -> SmtpHealthState:
        raw = await self.redis.get(SMTP_HEALTH_STATE_KEY)
        if not raw:
            return SmtpHealthState()
        try:
            return SmtpHealthState(**orjson.loads(raw))
        except (orjson.JSONDecodeError, TypeError):
            logger.warning("Discarding unreadable SMTP health state")
            return SmtpHealthState()
