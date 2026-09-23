import gzip
import logging
import multiprocessing
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.core import logger as logger_module
from src.core.logger import (
    LOG_FILENAME,
    ConcurrentRetentionRotatingFileHandler,
    _parse_duration,
    _parse_size,
    _sanitize_record,
    sanitize_log_message,
    sanitize_log_text,
    setup_logger,
)


def _write_concurrent_log_records(path: str, worker_id: int) -> None:
    handler = ConcurrentRetentionRotatingFileHandler(
        Path(path),
        max_bytes=512,
        retention_seconds=60 * 60,
        use_gzip=True,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    for record_id in range(80):
        handler.emit(
            logging.LogRecord(
                name="concurrency-test",
                level=logging.INFO,
                pathname=__file__,
                lineno=1,
                msg=f"worker={worker_id} record={record_id}",
                args=(),
                exc_info=None,
            )
        )
    handler.close()


def test_sanitize_log_text_redacts_email_and_credentials() -> None:
    message = (
        "delivery to person@example.com failed; "
        "password=hunter2 token='abc.def' Authorization: Bearer bearer-secret "
        'signature="signed-value"'
    )

    sanitized = sanitize_log_text(message)

    assert "person@example.com" not in sanitized
    assert "hunter2" not in sanitized
    assert "abc.def" not in sanitized
    assert "bearer-secret" not in sanitized
    assert "signed-value" not in sanitized
    assert sanitized.count("[REDACTED]") >= 4


def test_sanitize_log_text_redacts_short_domain_email_and_transport_credentials() -> None:
    secrets = (
        "a@b.c",
        "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi",
        "dXNlcjpwYXNzd29yZA==",
        "redis-user:redis-password",
        "web-user:web-password",
    )
    message = (
        f"email={secrets[0]} "
        f"bot=https://api.telegram.org/bot{secrets[1]}/sendMessage "
        f"Authorization: Basic {secrets[2]} "
        f"redis=redis://{secrets[3]}@redis.internal:6379/0 "
        f"callback=https://{secrets[4]}@public.example/path"
    )

    sanitized = sanitize_log_text(message)

    for secret in secrets:
        assert secret not in sanitized
    assert "redis.internal:6379/0" in sanitized
    assert "public.example/path" in sanitized
    assert sanitized.count("[REDACTED]") >= 4


def test_sanitize_log_text_keeps_human_readable_context() -> None:
    assert sanitize_log_text("Payment processing failed for gateway yookassa") == (
        "Payment processing failed for gateway yookassa"
    )


def test_sanitize_log_text_redacts_external_identifiers() -> None:
    sanitized = sanitize_log_text(
        "User '7295815705' with telegram_id=7295815705, "
        "uuid=018f47a6-7b30-7112-8d2f-9a1b2c3d4e5f and @private_user"
    )

    assert "7295815705" not in sanitized
    assert "018f47a6-7b30-7112-8d2f-9a1b2c3d4e5f" not in sanitized
    assert "@private_user" not in sanitized


def test_sanitize_log_message_prevents_multiline_and_terminal_injection() -> None:
    sanitized = sanitize_log_message("customer\r\nforged\x1b[31m")

    assert sanitized == r"customer\r\nforged\x1b[31m"


def test_record_filter_sanitizes_exception_before_loguru_appends_it() -> None:
    try:
        raise ValueError(
            "delivery to private@example.com failed; password=hunter2"
            "\n2026-09-13 forged record\x1b[31m"
        )
    except ValueError:
        exception_type, exception_value, exception_traceback = sys.exc_info()

    record = {
        "message": "generic failure",
        "exception": SimpleNamespace(
            type=exception_type,
            value=exception_value,
            traceback=exception_traceback,
        ),
        "extra": {},
    }

    assert _sanitize_record(record) is True  # type: ignore[arg-type]
    assert record["exception"] is None
    rendered = record["extra"]["sanitized_exception"]
    assert "private@example.com" not in rendered
    assert "hunter2" not in rendered
    assert "[EMAIL]" in rendered
    assert "[REDACTED]" in rendered
    # The formatter owns exactly the first newline before the escaped
    # traceback payload; exception-controlled content cannot add another.
    payload = rendered.removeprefix("\n")
    assert "\n" not in payload
    assert "\x1b" not in payload
    assert r"\n2026-09-13 forged record\x1b[31m" in payload


def test_log_size_and_retention_defaults_are_parsed() -> None:
    assert _parse_size("100 MB") == 100_000_000
    assert _parse_duration("3 days") == 3 * 24 * 60 * 60


def test_setup_logger_keeps_single_multiprocess_safe_bot_log(
    monkeypatch,
    tmp_path: Path,
) -> None:
    added_sinks: list[object] = []
    monkeypatch.setattr(logger_module, "LOG_DIR", tmp_path)
    monkeypatch.setattr(logger_module.logger, "remove", Mock())
    monkeypatch.setattr(
        logger_module.logger,
        "add",
        lambda sink, **_: added_sinks.append(sink),
    )
    monkeypatch.setattr(logger_module.logging, "basicConfig", Mock())
    config = SimpleNamespace(
        log=SimpleNamespace(
            to_file=True,
            level="DEBUG",
            rotation="100 MB",
            retention="3 days",
            compression="zip",
        )
    )

    setup_logger(config)

    file_sinks = [
        sink for sink in added_sinks if isinstance(sink, ConcurrentRetentionRotatingFileHandler)
    ]
    assert len(file_sinks) == 1
    assert Path(file_sinks[0].baseFilename) == tmp_path / LOG_FILENAME
    file_sinks[0].close()


def test_concurrent_rotation_preserves_every_record_in_one_log(tmp_path: Path) -> None:
    log_path = tmp_path / LOG_FILENAME
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(target=_write_concurrent_log_records, args=(str(log_path), worker_id))
        for worker_id in range(4)
    ]

    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=30)
        assert process.exitcode == 0

    records: set[str] = set()
    for candidate in tmp_path.glob(f"{LOG_FILENAME}*"):
        if not candidate.is_file() or candidate.suffix == ".lock":
            continue
        if candidate.suffix == ".gz":
            with gzip.open(candidate, mode="rt", encoding="utf-8") as archive:
                records.update(archive.read().splitlines())
        else:
            records.update(candidate.read_text(encoding="utf-8").splitlines())
    expected = {
        f"worker={worker_id} record={record_id}"
        for worker_id in range(4)
        for record_id in range(80)
    }
    assert records == expected
