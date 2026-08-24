from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from src.core import logger as logger_module
from src.core.logger import (
    LOG_FILENAME,
    TASKIQ_SCHEDULER_LOG_FILENAME,
    TASKIQ_WORKER_LOG_FILENAME,
    sanitize_log_text,
    setup_logger,
)


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


def test_sanitize_log_text_keeps_human_readable_context() -> None:
    assert sanitize_log_text("Payment processing failed for gateway yookassa") == (
        "Payment processing failed for gateway yookassa"
    )


def test_runtime_roles_use_distinct_log_files() -> None:
    assert len(
        {
            LOG_FILENAME,
            TASKIQ_WORKER_LOG_FILENAME,
            TASKIQ_SCHEDULER_LOG_FILENAME,
        }
    ) == 3


def test_setup_logger_uses_requested_file_name(
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

    setup_logger(config, filename=TASKIQ_WORKER_LOG_FILENAME)

    assert tmp_path / TASKIQ_WORKER_LOG_FILENAME in added_sinks
    assert tmp_path / LOG_FILENAME not in added_sinks
