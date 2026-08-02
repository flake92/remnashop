import asyncio
import smtplib
from email.message import EmailMessage

from loguru import logger

from src.application.common.email_sender import EmailSender
from src.core.config import AppConfig
from src.core.exceptions import EmailDeliveryError


class SmtpEmailSender(EmailSender):
    def __init__(self, config: AppConfig) -> None:
        self._config = config

    @property
    def is_enabled(self) -> bool:
        email = self._config.email
        return bool(
            email.enabled
            and email.host
            and email.from_email
            and email.username.get_secret_value()
            and email.password.get_secret_value()
        )

    async def send(self, *, to: str, subject: str, body: str) -> None:
        try:
            await asyncio.to_thread(self._send_sync, to=to, subject=subject, body=body)
        except Exception as e:
            logger.error(
                "Failed to send email (error_type={error_type})",
                error_type=type(e).__name__,
            )
            raise EmailDeliveryError(
                "Failed to send verification email. Please try again later."
            ) from e

    def _send_sync(self, *, to: str, subject: str, body: str) -> None:
        email = self._config.email
        message = EmailMessage()
        message["Subject"] = subject
        from_name = email.from_name.strip()
        from_email = email.from_email.strip()
        message["From"] = f"{from_name} <{from_email}>" if from_name else from_email
        message["To"] = to
        message.set_content(body)

        smtp_user = email.username.get_secret_value()
        smtp_password = email.password.get_secret_value()

        if email.use_ssl:
            with smtplib.SMTP_SSL(email.host, email.port, timeout=20) as client:
                client.login(smtp_user, smtp_password)
                client.send_message(message)
            return

        with smtplib.SMTP(email.host, email.port, timeout=20) as client:
            client.ehlo()
            if email.use_tls:
                client.starttls()
                client.ehlo()
            client.login(smtp_user, smtp_password)
            client.send_message(message)
