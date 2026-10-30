from email import message_from_bytes
from email.policy import default
from types import SimpleNamespace
from unittest.mock import AsyncMock

from pydantic import SecretStr

from src.application.common import NotificationCategory
from src.infrastructure.services.channel_render import (
    push_text,
    render_telegram_html,
    safe_link,
    subject_for,
)
from src.infrastructure.services.email_sender import SmtpEmailSender
from src.infrastructure.services.notification_channels import (
    NotificationChannelsImpl,
    make_unsubscribe_token,
    parse_unsubscribe_token,
)
from src.infrastructure.services.smtp_health import (
    SMTP_HEALTH_STATE_KEY,
    SMTP_ROLE_BULK,
    SmtpHealthMonitor,
)

SECRET = "unit-test-secret"


def test_telegram_html_keeps_only_safe_markup() -> None:
    rendered = render_telegram_html(
        '<b>Подписка</b> истекает <tg-emoji emoji-id="1">⏳</tg-emoji>\n'
        '<a href="https://example.com/renew">Продлить</a> '
        '<a href="javascript:alert(1)">x</a><script>alert(1)</script>'
        "<tg-spoiler>секрет</tg-spoiler> &lt;tag&gt;"
    )

    assert "<strong>Подписка</strong>" in rendered.html
    assert "⏳" in rendered.html
    assert '<a href="https://example.com/renew"' in rendered.html
    assert "javascript" not in rendered.html
    assert "<script>" not in rendered.html
    assert "&lt;tag&gt;" in rendered.html
    assert "<br>" in rendered.html
    assert rendered.text.startswith("Подписка истекает ⏳\nПродлить (https://example.com/renew)")
    assert "секрет" in rendered.text


def test_subject_and_push_text_skip_emoji_only_first_line() -> None:
    assert subject_for("🎉\nПодписка продлена", "Кабинет") == "Кабинет"
    assert subject_for("Подписка продлена\nДетали", "Кабинет") == "Подписка продлена"
    title, body = push_text("Трафик исчерпан\n" + "длинный текст " * 40, "Кабинет")
    assert title == "Трафик исчерпан"
    assert len(body) <= 180 and body.endswith("…")


def test_safe_link_accepts_only_web_and_mail_urls() -> None:
    assert safe_link("https://t.me/support") == "https://t.me/support"
    assert safe_link("tg://resolve?domain=x") is None
    assert safe_link("javascript:alert(1)") is None
    assert safe_link("https://") is None


def test_unsubscribe_token_round_trip_and_tamper_resistance() -> None:
    token = make_unsubscribe_token(SECRET, 42, NotificationCategory.NEWS)

    assert parse_unsubscribe_token(SECRET, token) == (42, NotificationCategory.NEWS)
    assert parse_unsubscribe_token("other-secret", token) is None
    forged = token.replace("42.N", "43.N")
    assert parse_unsubscribe_token(SECRET, forged) is None
    service = make_unsubscribe_token(SECRET, 42, NotificationCategory.SERVICE)
    assert service != token
    assert parse_unsubscribe_token(SECRET, service) == (42, NotificationCategory.SERVICE)
    assert parse_unsubscribe_token(SECRET, "42.N.zz") is None


def _channels() -> NotificationChannelsImpl:
    config = SimpleNamespace(
        web_cabinet_url="https://cabinet.example.com/auth/telegram/webapp",
        crypt_key=SecretStr(SECRET),
        bulk_email=SimpleNamespace(
            from_email="news@mail.example.com",
            from_name="Clean Pay",
            reply_to="support@example.com",
        ),
    )
    return NotificationChannelsImpl(
        config,  # type: ignore[arg-type]
        session_maker=None,  # type: ignore[arg-type]
        bulk_sender=SimpleNamespace(is_enabled=True),  # type: ignore[arg-type]
        bulk_run_lock=None,  # type: ignore[arg-type]
    )


def test_news_email_has_one_click_unsubscribe_and_bulk_headers() -> None:
    channels = _channels()
    message = channels.build_email(
        user_id=7,
        category=NotificationCategory.NEWS,
        text="Новый тариф",
        html_fragment="<strong>Новый тариф</strong>",
        url=None,
    )
    token = make_unsubscribe_token(SECRET, 7, NotificationCategory.NEWS)

    headers = message["headers"]
    assert headers["List-Unsubscribe"] == (
        f"<https://cabinet.example.com/api/notifications/unsubscribe?token={token}>"
    )
    assert headers["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    assert headers["List-Id"] == "news <news.mail.example.com>"
    assert headers["Precedence"] == "bulk"
    assert headers["Reply-To"] == "support@example.com"
    assert "Auto-Submitted" not in headers
    assert f"https://cabinet.example.com/unsubscribe?token={token}" in message["html"]
    assert f"https://cabinet.example.com/unsubscribe?token={token}" in message["text"]
    assert "Отписаться от новостей" in message["text"]
    assert "https://cabinet.example.com/profile" in message["text"]


def test_service_email_is_marked_auto_generated() -> None:
    message = _channels().build_email(
        user_id=7,
        category=NotificationCategory.SERVICE,
        text="Подписка истекла",
        html_fragment="Подписка истекла",
        url="https://t.me/support",
    )

    assert message["headers"]["Auto-Submitted"] == "auto-generated"
    assert "Precedence" not in message["headers"]
    assert 'href="https://t.me/support"' in message["html"]


def test_smtp_message_is_multipart_with_custom_headers() -> None:
    sender = SmtpEmailSender(
        SimpleNamespace(  # type: ignore[arg-type]
            email=SimpleNamespace(from_email="news@mail.example.com", from_name="Clean Pay")
        ),
        SimpleNamespace(),  # type: ignore[arg-type]
    )
    unsubscribe = (
        "<https://cabinet.example.com/api/notifications/unsubscribe"
        "?token=35.N.e259c604454ada96b92b6b80e84d7d9e>"
    )
    message = sender._build_message(
        to="user@example.org",
        subject="Тема",
        body="Текст",
        message_id="<1@mail.example.com>",
        html="<p>Текст</p>",
        headers={
            "List-Unsubscribe": unsubscribe,
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        },
    )

    raw = message.as_bytes()
    # Providers read the URL only from a plain header line, never from an encoded-word.
    assert f"List-Unsubscribe: {unsubscribe}\n".encode() in raw
    parsed = message_from_bytes(raw, policy=default)
    assert parsed.get_content_type() == "multipart/alternative"
    assert parsed["List-Unsubscribe"] == unsubscribe
    assert parsed["List-Unsubscribe-Post"] == "List-Unsubscribe=One-Click"
    parts = [part.get_content_type() for part in parsed.iter_parts()]  # type: ignore[attr-defined]
    assert parts == ["text/plain", "text/html"]


class _FakeRedis:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    async def get(self, key: str) -> object:
        return self.data.get(key)

    async def set(self, key: str, value: object, ex: int | None = None) -> None:
        self.data[key] = value

    async def delete(self, key: str) -> None:
        self.data.pop(key, None)


async def test_bulk_smtp_outage_is_reported_separately_and_labelled() -> None:
    redis = _FakeRedis()
    publish = AsyncMock()
    config = SimpleNamespace(
        email=SimpleNamespace(host="smtp.main.example", port=587),
        bulk_email=SimpleNamespace(host="smtp.bulk.example", port=465),
    )
    sender = SimpleNamespace(is_enabled=True, probe=AsyncMock(return_value="SMTP_AUTH_REJECTED"))
    monitor = SmtpHealthMonitor(
        config,  # type: ignore[arg-type]
        sender,  # type: ignore[arg-type]
        redis,  # type: ignore[arg-type]
        SimpleNamespace(publish=publish),  # type: ignore[arg-type]
        role=SMTP_ROLE_BULK,
    )

    await monitor.check(1_000.0)
    await monitor.check(1_300.0)

    event = publish.await_args.args[0]
    assert event.role == "BULK"
    assert event.server == "smtp.bulk.example:465"
    assert SMTP_HEALTH_STATE_KEY not in redis.data
    assert f"{SMTP_HEALTH_STATE_KEY}:bulk" in redis.data
