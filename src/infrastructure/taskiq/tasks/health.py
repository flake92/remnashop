import time

from dishka.integrations.taskiq import FromDishka, inject
from redis.asyncio import Redis

from src.infrastructure.taskiq.broker import broker
from src.infrastructure.taskiq.health_state import (
    TASKIQ_PIPELINE_HEARTBEAT_TTL_SECONDS,
    taskiq_heartbeat_key,
)


@broker.task(schedule=[{"cron": "* * * * *"}], retry_on_error=False)
@inject(patch_module=True)
async def taskiq_pipeline_heartbeat_task(redis: FromDishka[Redis]) -> None:
    await redis.set(
        taskiq_heartbeat_key(),
        str(int(time.time())),
        ex=TASKIQ_PIPELINE_HEARTBEAT_TTL_SECONDS,
    )
