from typing import Any

from redis.asyncio import Redis

from src.application.common import BulkEmailRunLock, BulkEmailSender
from src.core.config import AppConfig

from .email_delivery_lock import RedisEmailDeliveryRunLock
from .email_sender import SmtpEmailSender


class RedisBulkEmailRunLock(RedisEmailDeliveryRunLock, BulkEmailRunLock):
    # Own run lock and pacing timeline: the bulk account has its own provider
    # limits and must not slow down reminders or login codes.
    KEY = "email:bulk-notification-delivery-run:v1"
    RATE_KEY = "email:bulk-notification-delivery-rate:v1"

    def __init__(self, redis: Redis) -> None:
        super().__init__(redis)


class BulkSmtpEmailSender(SmtpEmailSender, BulkEmailSender):
    """SMTP sender for the BULK_EMAIL_* account."""

    def __init__(self, config: AppConfig, delivery_rate_limiter: BulkEmailRunLock) -> None:
        super().__init__(config, delivery_rate_limiter)

    @property
    def _settings(self) -> Any:
        return self._config.bulk_email


__all__ = ["BulkSmtpEmailSender", "RedisBulkEmailRunLock"]
