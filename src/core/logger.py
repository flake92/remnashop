from __future__ import annotations

import inspect
import logging
import re
import sys
import time
import traceback
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Final, Union

from concurrent_log_handler import ConcurrentRotatingFileHandler
from loguru import logger

from src.core.config import AppConfig
from src.core.constants import LOG_DIR

if TYPE_CHECKING:
    from loguru import Record

LOG_BUFFER_CAPACITY: Final[int] = 200
LOG_FILENAME: Final[str] = "bot.log"
LOG_ENCODING: Final[str] = "utf-8"
LOG_BACKUP_LIMIT: Final[int] = 100
LOG_FORMAT: Final[str] = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{line}</cyan> | "
    "<level>{message}</level>{extra[sanitized_exception]}"
)
LOG_REDACTED: Final[str] = "[REDACTED]"
_URL_USERINFO_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s?#@]+@"
)
_TELEGRAM_BOT_TOKEN_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<!\d)\d{5,}:[A-Za-z0-9_-]{20,}(?![\w-])"
)
_EMAIL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<![\w.+-])[\w.+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+(?![\w.-])"
)
_BEARER_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")
_BASIC_AUTH_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b(basic\s+)[A-Za-z0-9+/]+={0,2}(?![A-Za-z0-9+/=])"
)
_SECRET_VALUE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)([\"']?(?:password|passwd|secret|token|authorization|api[_-]?key|signature|sign)"
    r"[\"']?\s*[:=]\s*)([\"']?)[^\s,;&}\]]+"
)
_UUID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_REMNA_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?i)\brs_(?:web_)?\d+\b")
_TELEGRAM_HANDLE_PATTERN: Final[re.Pattern[str]] = re.compile(r"(?<![\w@])@[A-Za-z0-9_]{5,32}\b")
_LABELED_EXTERNAL_ID_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?i)(\b(?:telegram(?:[_ ]?id)?|chat(?:[_ ]?id)?|remna(?:user|_uuid)?|"
    r"hwid(?:_uuid)?|user(?:_id)?)"
    r"\b\s*(?:[:=]|\bis\b)?\s*['\"]?)(?:rs_(?:web_)?\d+|\d{5,})(['\"]?)"
)
_LOG_CONTROL_PATTERN: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SIZE_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>[KMGT]?B)?\s*$",
    re.IGNORECASE,
)
_DURATION_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"^\s*(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>second|minute|hour|day|week)s?\s*$",
    re.IGNORECASE,
)
_SIZE_MULTIPLIERS: Final[dict[str, int]] = {
    "B": 1,
    "KB": 1_000,
    "MB": 1_000_000,
    "GB": 1_000_000_000,
    "TB": 1_000_000_000_000,
}
_DURATION_MULTIPLIERS: Final[dict[str, int]] = {
    "second": 1,
    "minute": 60,
    "hour": 60 * 60,
    "day": 24 * 60 * 60,
    "week": 7 * 24 * 60 * 60,
}


def sanitize_log_text(value: str) -> str:
    """Remove common credentials and personal email addresses from log messages."""
    # Redact URL userinfo before the e-mail pattern can consume the
    # ``password@host`` portion and leave the username behind.
    # Match handles before inserting ``[REDACTED]@host`` for URL userinfo;
    # otherwise the generated marker could make a host look like a handle.
    sanitized = _TELEGRAM_HANDLE_PATTERN.sub("[USERNAME]", value)
    sanitized = _URL_USERINFO_PATTERN.sub(rf"\1{LOG_REDACTED}@", sanitized)
    sanitized = _TELEGRAM_BOT_TOKEN_PATTERN.sub(LOG_REDACTED, sanitized)
    sanitized = _EMAIL_PATTERN.sub("[EMAIL]", sanitized)
    sanitized = _BEARER_PATTERN.sub(rf"\1{LOG_REDACTED}", sanitized)
    sanitized = _BASIC_AUTH_PATTERN.sub(rf"\1{LOG_REDACTED}", sanitized)
    sanitized = _SECRET_VALUE_PATTERN.sub(rf"\1{LOG_REDACTED}", sanitized)
    sanitized = _UUID_PATTERN.sub("[UUID]", sanitized)
    sanitized = _REMNA_NAME_PATTERN.sub("[USER]", sanitized)
    return _LABELED_EXTERNAL_ID_PATTERN.sub(r"\1[ID]\2", sanitized)


def sanitize_log_message(value: str) -> str:
    """Keep a record on one terminal-safe line after redacting its contents."""
    sanitized = sanitize_log_text(value).replace("\r", r"\r").replace("\n", r"\n")
    return _LOG_CONTROL_PATTERN.sub(
        lambda match: rf"\x{ord(match.group(0)):02x}",
        sanitized,
    )


def sanitize_log_exception(exception: object) -> str:
    """Render an exception without bypassing the normal log redaction rules."""
    exception_type = getattr(exception, "type", None)
    exception_value = getattr(exception, "value", None)
    exception_traceback = getattr(exception, "traceback", None)
    if isinstance(exception_type, type) and isinstance(exception_value, BaseException):
        rendered = "".join(
            traceback.format_exception(
                exception_type,
                exception_value,
                exception_traceback,
            )
        )
    else:
        rendered = str(exception)
    # A traceback is attached after one formatter-controlled newline. Escape
    # every newline and terminal control byte originating from the exception
    # itself so user/provider text cannot forge a second log record.
    return sanitize_log_message(rendered).rstrip()


def _sanitize_record(record: Record) -> bool:
    record["message"] = sanitize_log_message(str(record["message"]))
    exception = record["exception"]
    if exception is not None:
        record["extra"]["sanitized_exception"] = f"\n{sanitize_log_exception(exception)}"
    else:
        # Multiple sinks may apply this filter to the same record. Preserve a
        # traceback already sanitized by an earlier sink.
        record["extra"].setdefault("sanitized_exception", "")
    # Loguru appends record["exception"] after the configured format. Clear the
    # raw object so its message cannot bypass the sanitizer, and render the
    # sanitized traceback through the explicit format field above instead.
    record["exception"] = None
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


def _parse_size(value: str) -> int:
    match = _SIZE_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"LOG_ROTATION must be a size such as '100 MB', got '{value}'")
    unit = (match.group("unit") or "B").upper()
    return int(float(match.group("value")) * _SIZE_MULTIPLIERS[unit])


def _parse_duration(value: str) -> float:
    match = _DURATION_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError(f"LOG_RETENTION must be a duration such as '3 days', got '{value}'")
    unit = match.group("unit").lower()
    return float(match.group("value")) * _DURATION_MULTIPLIERS[unit]


class ConcurrentRetentionRotatingFileHandler(ConcurrentRotatingFileHandler):
    """Cross-process-safe size rotation with time-based archive retention."""

    def __init__(
        self,
        filename: Path,
        *,
        max_bytes: int,
        retention_seconds: float,
        use_gzip: bool,
    ) -> None:
        self._retention_seconds = retention_seconds
        super().__init__(
            filename=str(filename),
            maxBytes=max_bytes,
            backupCount=LOG_BACKUP_LIMIT,
            encoding=LOG_ENCODING,
            use_gzip=use_gzip,
        )

    def doRollover(self) -> None:  # noqa: N802 - inherited logging API
        super().doRollover()
        self._remove_expired_archives()

    def _remove_expired_archives(self) -> None:
        log_path = Path(self.baseFilename)
        cutoff = time.time() - self._retention_seconds
        candidates = {
            *log_path.parent.glob(f"{log_path.name}.*"),
            *log_path.parent.glob(f"{log_path.stem}.*{log_path.suffix}.*"),
        }
        for candidate in candidates:
            if candidate.suffix == ".lock":
                continue
            try:
                if candidate.stat().st_mtime < cutoff:
                    candidate.unlink()
            except FileNotFoundError:
                # Another process may have completed the same retention pass.
                continue


def _create_file_handler(config: AppConfig) -> ConcurrentRetentionRotatingFileHandler:
    compression = config.log.compression.strip().lower()
    handler = ConcurrentRetentionRotatingFileHandler(
        LOG_DIR / LOG_FILENAME,
        max_bytes=_parse_size(config.log.rotation),
        retention_seconds=_parse_duration(config.log.retention),
        use_gzip=compression not in {"", "none", "false"},
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    return handler


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
            sink=_create_file_handler(config),
            level=config.log.level,
            format=LOG_FORMAT,
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
