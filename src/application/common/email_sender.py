from typing import Protocol, runtime_checkable


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
    ) -> None: ...

    async def probe(self) -> str | None:
        """Check that the mail server accepts a session without sending mail.
        Returns None when it does, otherwise the safe delivery error code."""
        ...
