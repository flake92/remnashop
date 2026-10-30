import time

from dishka.integrations.taskiq import FromDishka, inject
from loguru import logger
from redis.asyncio import Redis

from src.application.common import BulkEmailSender, EmailSender, EventPublisher
from src.core.config import AppConfig
from src.infrastructure.services.smtp_health import (
    SMTP_ROLE_BULK,
    SMTP_ROLE_MAIN,
    SmtpHealthMonitor,
)
from src.infrastructure.taskiq.broker import broker


@broker.task(schedule=[{"cron": "*/5 * * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def check_smtp_health_task(
    config: FromDishka[AppConfig],
    email_sender: FromDishka[EmailSender],
    bulk_email_sender: FromDishka[BulkEmailSender],
    redis: FromDishka[Redis],
    event_publisher: FromDishka[EventPublisher],
) -> None:
    # Two independent accounts: each alert names the server it is about.
    now = time.time()
    for role, sender in ((SMTP_ROLE_MAIN, email_sender), (SMTP_ROLE_BULK, bulk_email_sender)):
        monitor = SmtpHealthMonitor(config, sender, redis, event_publisher, role=role)
        try:
            await monitor.check(now)
        except Exception:
            # A Redis or publish failure for one server must not hide the other.
            logger.exception(f"{role} SMTP health check failed")
