import smtplib
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from src.application.events import SmtpRestoredEvent, SmtpUnavailableEvent
from src.infrastructure.services.email_sender import SmtpEmailSender
from src.infrastructure.services.i18n import LayeredFileStorage
from src.infrastructure.services.smtp_health import (
    SMTP_HEALTH_REPEAT_SECONDS,
    SmtpHealthAction,
    SmtpHealthMonitor,
    SmtpHealthState,
    evaluate_smtp_probe,
    format_duration,
)

_NOW = 1_800_000_000.0
_TRANSLATIONS = Path(__file__).resolve().parents[4] / "assets" / "translations"


def _run(
    probes: list[str | None], start: float = _NOW, step: float = 300.0
) -> list[SmtpHealthAction]:
    state, actions = SmtpHealthState(), []
    for index, code in enumerate(probes):
        state, action = evaluate_smtp_probe(state, code, start + index * step)
        actions.append(action)
    return actions


def test_one_failed_probe_is_not_an_outage() -> None:
    assert _run(["SMTP_CONNECTION_ERROR", None]) == [SmtpHealthAction.NONE] * 2


def test_second_failure_in_a_row_alerts_once() -> None:
    actions = _run(["SMTP_CONNECTION_ERROR"] * 4)

    assert actions == [
        SmtpHealthAction.NONE,
        SmtpHealthAction.ALERT,
        SmtpHealthAction.NONE,
        SmtpHealthAction.NONE,
    ]


def test_long_outage_repeats_the_alert_after_twelve_hours() -> None:
    state, _ = evaluate_smtp_probe(SmtpHealthState(), "SMTP_TIMEOUT_OR_DISCONNECT", _NOW)
    state, alert = evaluate_smtp_probe(state, "SMTP_TIMEOUT_OR_DISCONNECT", _NOW + 300)
    state, quiet = evaluate_smtp_probe(
        state, "SMTP_TIMEOUT_OR_DISCONNECT", _NOW + SMTP_HEALTH_REPEAT_SECONDS
    )
    state, repeat = evaluate_smtp_probe(
        state, "SMTP_TIMEOUT_OR_DISCONNECT", _NOW + 300 + SMTP_HEALTH_REPEAT_SECONDS
    )

    assert (alert, quiet, repeat) == (
        SmtpHealthAction.ALERT,
        SmtpHealthAction.NONE,
        SmtpHealthAction.REPEAT,
    )
    assert state.failing_since == _NOW


def test_recovery_is_announced_only_after_an_alert() -> None:
    assert _run(["SMTP_AUTH_REJECTED", "SMTP_AUTH_REJECTED", None])[-1] is (
        SmtpHealthAction.RESTORED
    )
    assert _run(["SMTP_AUTH_REJECTED", None])[-1] is SmtpHealthAction.NONE


@pytest.mark.parametrize(
    ("seconds", "text"),
    [(20, "1 мин"), (600, "10 мин"), (3600, "1 ч"), (5400, "1 ч 30 мин")],
)
def test_duration_is_human_readable(seconds: float, text: str) -> None:
    assert format_duration(seconds) == text


class _FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}

    async def get(self, key: str) -> Any:
        return self.data.get(key)

    async def set(self, key: str, value: Any, ex: int | None = None) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


def _monitor(probe_results: list[str | None], *, enabled: bool = True) -> SmtpHealthMonitor:
    sender = SimpleNamespace(is_enabled=enabled, probe=AsyncMock(side_effect=probe_results))
    config = SimpleNamespace(email=SimpleNamespace(host="clear-vpn.org", port=587))
    return SmtpHealthMonitor(
        config,  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        _FakeRedis(),  # type: ignore[arg-type]
        SimpleNamespace(publish=AsyncMock()),  # type: ignore[arg-type]
    )


async def test_monitor_alerts_and_announces_recovery_through_redis_state() -> None:
    monitor = _monitor(["SMTP_CONNECTION_ERROR", "SMTP_CONNECTION_ERROR", None])
    publish = monitor.event_publisher.publish  # type: ignore[attr-defined]

    await monitor.check(_NOW)
    publish.assert_not_awaited()

    assert await monitor.check(_NOW + 300) is SmtpHealthAction.ALERT
    alert = publish.await_args.args[0]
    assert isinstance(alert, SmtpUnavailableEvent)
    assert alert.server == "clear-vpn.org:587"
    assert alert.error_code == "SMTP_CONNECTION_ERROR"
    assert alert.failed_checks == 2
    assert alert.failing_for == "5 мин"
    assert alert.failing_since.endswith("МСК")
    assert not alert.is_repeat

    assert await monitor.check(_NOW + 900) is SmtpHealthAction.RESTORED
    restored = publish.await_args.args[0]
    assert isinstance(restored, SmtpRestoredEvent)
    assert restored.failing_for == "15 мин"
    assert monitor.redis.data == {}  # type: ignore[attr-defined]


async def test_monitor_skips_probe_when_email_is_disabled() -> None:
    monitor = _monitor([], enabled=False)

    assert await monitor.check(_NOW) is SmtpHealthAction.NONE
    monitor.email_sender.probe.assert_not_awaited()  # type: ignore[attr-defined]


def _smtp_config() -> SimpleNamespace:
    return SimpleNamespace(
        email=SimpleNamespace(
            host="smtp.example.org",
            port=587,
            use_ssl=False,
            use_tls=True,
            username=SecretStr("smtp-user"),
            password=SecretStr("smtp-password"),
        )
    )


def _sender() -> SmtpEmailSender:
    return SmtpEmailSender(
        _smtp_config(),  # type: ignore[arg-type]
        SimpleNamespace(wait_for_send_slot=AsyncMock()),  # type: ignore[arg-type]
    )


async def test_probe_logs_in_without_sending_mail(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []

    class FakeSmtp:
        def __init__(self, *args: object, **kwargs: object) -> None:
            calls.append("connect")

        def ehlo(self) -> None:
            return None

        def starttls(self, **kwargs: object) -> None:
            calls.append("starttls")

        def login(self, username: str, password: str) -> None:
            calls.append("login")

        def noop(self) -> tuple[int, bytes]:
            return 250, b"2.0.0 Ok"

        def send_message(self, message: object) -> None:
            calls.append("send")

        def quit(self) -> None:
            calls.append("quit")

    monkeypatch.setattr("src.infrastructure.services.email_sender.smtplib.SMTP", FakeSmtp)
    sender = _sender()

    assert await sender.probe() is None
    assert calls == ["connect", "starttls", "login", "quit"]
    sender._delivery_rate_limiter.wait_for_send_slot.assert_not_awaited()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (ConnectionRefusedError(), "SMTP_CONNECTION_ERROR"),
        (TimeoutError(), "SMTP_TIMEOUT_OR_DISCONNECT"),
        (smtplib.SMTPAuthenticationError(535, b"bad credentials"), "SMTP_AUTH_REJECTED"),
    ],
)
async def test_probe_returns_the_safe_error_code(
    monkeypatch: pytest.MonkeyPatch, error: Exception, code: str
) -> None:
    sender = _sender()

    def fail() -> None:
        raise error

    monkeypatch.setattr(sender, "_open_client", fail)

    assert await sender.probe() == code


def _translate(tmp_path: Path, key: str, **kwargs: object) -> str:
    (tmp_path / "ru").mkdir(exist_ok=True)
    storage = LayeredFileStorage(
        user_translations_dir=tmp_path,
        default_translations_dir=_TRANSLATIONS,
    )
    (translator,) = storage.get_translators_for_language("ru")
    return str(translator.get(key, **kwargs))


def test_alert_and_recovery_messages_render(tmp_path: Path) -> None:
    alert = _translate(
        tmp_path,
        "event-smtp.unavailable",
        server="clear-vpn.org:587",
        role="MAIN",
        error_code="SMTP_CONNECTION_ERROR",
        failing_since="27.10 14:05 МСК",
        failing_for="10 мин",
        failed_checks=2,
        is_repeat=False,
    )
    repeat = _translate(
        tmp_path,
        "event-smtp.unavailable",
        server="clear-vpn.org:587",
        role="MAIN",
        error_code="SMTP_SOMETHING_NEW",
        failing_since="27.10 14:05 МСК",
        failing_for="12 ч 5 мин",
        failed_checks=146,
        is_repeat=True,
    )
    restored = _translate(
        tmp_path,
        "event-smtp.restored",
        server="clear-vpn.org:587",
        role="MAIN",
        failing_since="27.10 14:05 МСК",
        failing_for="25 мин",
    )

    assert "нет соединения с сервером" in alert
    assert "всё ещё" not in alert
    assert "всё ещё" in repeat
    assert "сервер не принимает сессию" in repeat
    assert "27.10 14:05 МСК" in restored
    assert "25 мин" in restored
    assert "Основной SMTP" in alert and "коды входа" in alert

    bulk = _translate(
        tmp_path,
        "event-smtp.unavailable",
        server="smtp.bulk.example:465",
        role="BULK",
        error_code="SMTP_AUTH_REJECTED",
        failing_since="27.10 14:05 МСК",
        failing_for="10 мин",
        failed_checks=2,
        is_repeat=False,
    )
    bulk_restored = _translate(
        tmp_path,
        "event-smtp.restored",
        server="smtp.bulk.example:465",
        role="BULK",
        failing_since="27.10 14:05 МСК",
        failing_for="25 мин",
    )
    assert "SMTP рассылок не отвечает" in bulk
    assert "BULK_EMAIL_" in bulk and "Коды входа идут через основной SMTP" in bulk
    assert "SMTP рассылок снова работает" in bulk_restored
