import time

from dishka.integrations.taskiq import FromDishka, inject
from redis.asyncio import Redis

from src.application.common import EmailSender, EventPublisher
from src.core.config import AppConfig
from src.infrastructure.services.smtp_health import SmtpHealthMonitor
from src.infrastructure.taskiq.broker import broker


@broker.task(schedule=[{"cron": "*/5 * * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def check_smtp_health_task(
    config: FromDishka[AppConfig],
    email_sender: FromDishka[EmailSender],
    redis: FromDishka[Redis],
    event_publisher: FromDishka[EventPublisher],
) -> None:
    monitor = SmtpHealthMonitor(config, email_sender, redis, event_publisher)
    await monitor.check(time.time())
