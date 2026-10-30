from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from .email_delivery_lock import EmailDeliveryRunLock


@runtime_checkable
class EmailSender(Protocol):
    @property
    def is_enabled(self) -> bool: ...

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        message_id: str | None = None,
        rate_limit_per_minute: int | None = None,
        rate_limit_max_wait_seconds: float | None = None,
        html: str | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> None: ...

    async def probe(self) -> str | None:
        """Check that the mail server accepts a session without sending mail.
        Returns None when it does, otherwise the safe delivery error code."""
        ...


@runtime_checkable
class BulkEmailSender(EmailSender, Protocol):
    """Separate account for notification copies and news (BULK_EMAIL_*).

    A mailing service (HTTP API) can replace the SMTP implementation without
    touching the delivery logic.
    """


class BulkEmailRunLock(EmailDeliveryRunLock, Protocol):
    """Run lock and pacing timeline of the bulk account."""
