from dishka.integrations.taskiq import FromDishka, inject

from src.application.common import NotificationChannels
from src.infrastructure.taskiq.broker import broker


@broker.task(schedule=[{"cron": "* * * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def deliver_notification_channels_task(
    channels: FromDishka[NotificationChannels],
) -> None:
    await channels.deliver_due()
