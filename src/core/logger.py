from __future__ import annotations

import inspect
import logging
import re
import sys
from collections import deque
from typing import TYPE_CHECKING, Final, Union

from loguru import logger

from src.core.config import AppConfig
from src.core.constants import LOG_DIR

if TYPE_CHECKING:
    from loguru import Record

LOG_BUFFER_CAPACITY: Final[int] = 200
LOG_FILENAME: Final[str] = "bot.log"
LOG_ENCODING: Final[str] = "utf-8"
LOG_FORMAT: Final[str] = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{line}</cyan> | <level>{message}</level>"
)
LOG_REDACTED: Final[str] = "[REDACTED]"
_EMAIL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}(?![\w.-])"
)
_BEARER_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"
)
_SECRET_VALUE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)([\"']?(?:password|passwd|secret|token|authorization|api[_-]?key|signature|sign)"
    r"[\"']?\s*[:=]\s*)([\"']?)[^\s,;&}\]]+"
)


def sanitize_log_text(value: str) -> str:
    """Remove common credentials and personal email addresses from log messages."""
    sanitized = _EMAIL_PATTERN.sub("[EMAIL]", value)
    sanitized = _BEARER_PATTERN.sub(rf"\1{LOG_REDACTED}", sanitized)
    return _SECRET_VALUE_PATTERN.sub(rf"\1{LOG_REDACTED}", sanitized)


def _sanitize_record(record: Record) -> bool:
    record["message"] = sanitize_log_text(str(record["message"]))
    return True


class LogBuffer:
    def __init__(self, capacity: int = LOG_BUFFER_CAPACITY) -> None:
        self._records: deque[str] = deque(maxlen=capacity)

    def write(self, message: str) -> None:
        self._records.append(message.rstrip())

    def get_context(self, lines: int = 100) -> str:
        records = list(self._records)
        return "\n".join(records[-lines:])


log_buffer = LogBuffer()


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: Union[str, int] = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame, depth = inspect.currentframe(), 0
        while frame:
            filename = frame.f_code.co_filename
            is_logging = filename == logging.__file__
            is_frozen = "importlib" in filename and "_bootstrap" in filename
            if depth > 0 and not (is_logging or is_frozen):
                break
            frame = frame.f_back
            depth += 1

        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def setup_logger(config: AppConfig) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    logger.remove()

    logger.add(
        sink=sys.stderr,
        level=config.log.level,
        format=LOG_FORMAT,
        colorize=True,
        diagnose=False,
        filter=_sanitize_record,
    )

    if config.log.to_file:
        logger.add(
            sink=LOG_DIR / LOG_FILENAME,
            level=config.log.level,
            format=LOG_FORMAT,
            rotation=config.log.rotation,
            retention=config.log.retention,
            compression=config.log.compression,
            encoding=LOG_ENCODING,
            diagnose=False,
            filter=_sanitize_record,
        )

    logger.add(
        sink=log_buffer.write,
        level=config.log.level,
        format=LOG_FORMAT,
        colorize=False,
        diagnose=False,
        filter=_sanitize_record,
    )

    intercept_handler = InterceptHandler()
    logging.basicConfig(handlers=[intercept_handler], level=logging.INFO, force=True)

    for logger_name in (
        "uvicorn",
        "uvicorn.error",
        "uvicorn.access",
        "fastapi",
    ):
        logging.getLogger(logger_name).handlers = [intercept_handler]

    # logging.getLogger("httpx").propagate = False
    # logging.getLogger("httpx").level = logging.WARNING
