from taskiq import TaskiqScheduler
from taskiq.schedule_sources import LabelScheduleSource

from src.core.config import AppConfig
from src.core.logger import TASKIQ_SCHEDULER_LOG_FILENAME, setup_logger

from .broker import broker


def scheduler() -> TaskiqScheduler:
    setup_logger(AppConfig.get(), filename=TASKIQ_SCHEDULER_LOG_FILENAME)

    scheduler = TaskiqScheduler(
        broker=broker,
        sources=[LabelScheduleSource(broker)],
    )

    return scheduler
