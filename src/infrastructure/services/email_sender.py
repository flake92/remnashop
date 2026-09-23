import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from loguru import logger

from src.application.common.email_delivery_lock import EmailDeliveryRunLock
from src.application.common.email_sender import EmailSender
from src.core.config import AppConfig
from src.core.exceptions import EmailDeliveryError, EmailDeliveryRateDeferredError

_SMTP_AUTH_MAX_ATTEMPTS = 2
_SMTP_AUTH_RETRY_DELAY_SECONDS = 0.5
_TRANSACTIONAL_RATE_MAX_WAIT_SECONDS = 10.0


def _response_is_retryable(code: int) -> bool:
    return 400 <= code < 500


def _response_error(code: int, *, auth: bool = False) -> EmailDeliveryError:
    retryable = _response_is_retryable(code)
    if auth:
        safe_code = "SMTP_AUTH_TEMPORARY" if retryable else "SMTP_AUTH_REJECTED"
    elif retryable and code in (450, 451, 452):
        safe_code = "SMTP_RATE_LIMITED"
    else:
        safe_code = "SMTP_RESPONSE_TEMPORARY" if retryable else "SMTP_RESPONSE_REJECTED"
    return EmailDeliveryError(code=safe_code, retryable=retryable)


def _classify_tls_error(exc: ssl.SSLError) -> EmailDeliveryError:
    if isinstance(exc, ssl.SSLCertVerificationError):
        return EmailDeliveryError(code="SMTP_TLS_CERTIFICATE_ERROR", retryable=False)
    if isinstance(exc, ssl.SSLEOFError):
        return EmailDeliveryError(code="SMTP_TLS_DISCONNECTED", retryable=True)
    permanent_reasons = {
        "CERTIFICATE_VERIFY_FAILED",
        "HOSTNAME_MISMATCH",
        "NO_CIPHERS_AVAILABLE",
        "NO_SHARED_CIPHER",
        "UNKNOWN_PROTOCOL",
        "UNSUPPORTED_PROTOCOL",
        "WRONG_VERSION_NUMBER",
    }
    reason = str(getattr(exc, "reason", "")).upper()
    permanent = reason in permanent_reasons
    return EmailDeliveryError(
        code="SMTP_TLS_CONFIGURATION" if permanent else "SMTP_TLS_TRANSIENT",
        retryable=not permanent,
    )


def _classify_smtp_error(exc: Exception) -> EmailDeliveryError:
    if isinstance(exc, EmailDeliveryError):
        return exc
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return _response_error(exc.smtp_code, auth=True)
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        response_codes = [
            details[0]
            for details in exc.recipients.values()
            if isinstance(details, tuple) and details and isinstance(details[0], int)
        ]
        retryable = bool(response_codes) and any(
            _response_is_retryable(code) for code in response_codes
        )
        return EmailDeliveryError(
            code=("SMTP_RECIPIENT_TEMPORARY" if retryable else "SMTP_RECIPIENT_REJECTED"),
            retryable=retryable,
        )
    if isinstance(exc, smtplib.SMTPResponseException):
        return _response_error(exc.smtp_code)
    if isinstance(exc, (TimeoutError, smtplib.SMTPServerDisconnected)):
        return EmailDeliveryError(code="SMTP_TIMEOUT_OR_DISCONNECT", retryable=True)
    if isinstance(exc, ssl.SSLError):
        return _classify_tls_error(exc)
    if isinstance(exc, OSError):
        return EmailDeliveryError(code="SMTP_CONNECTION_ERROR", retryable=True)
    if isinstance(exc, smtplib.SMTPException):
        return EmailDeliveryError(code="SMTP_PROTOCOL_ERROR", retryable=False)
    return EmailDeliveryError(code="SMTP_UNEXPECTED", retryable=False)


class SmtpEmailSender(EmailSender):
    """Serialized SMTP sender with one reusable authenticated connection."""

    def __init__(
        self,
        config: AppConfig,
        delivery_rate_limiter: EmailDeliveryRunLock,
    ) -> None:
        self._config = config
        self._delivery_rate_limiter = delivery_rate_limiter
        self._send_lock = asyncio.Lock()
        self._client: smtplib.SMTP | smtplib.SMTP_SSL | None = None

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

    async def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        message_id: str | None = None,
        rate_limit_per_minute: int | None = None,
        rate_limit_max_wait_seconds: float | None = None,
    ) -> None:
        # SmtpEmailSender is application-scoped. Serialize access so the shared
        # smtplib connection is never used concurrently by web and worker jobs.
        async with self._send_lock:
            try:
                effective_rate = (
                    rate_limit_per_minute
                    if rate_limit_per_minute is not None
                    else getattr(
                        self._config.email,
                        "subscription_expiration_delivery_rate_per_minute",
                        10,
                    )
                )
                effective_max_wait = (
                    rate_limit_max_wait_seconds
                    if rate_limit_max_wait_seconds is not None
                    else min(
                        float(getattr(self._config.email, "timeout_seconds", 20)),
                        _TRANSACTIONAL_RATE_MAX_WAIT_SECONDS,
                    )
                )
                await self._send_with_auth_retry(
                    to=to,
                    subject=subject,
                    body=body,
                    message_id=message_id,
                    rate_limit_per_minute=effective_rate,
                    rate_limit_max_wait_seconds=effective_max_wait,
                )
            except EmailDeliveryRateDeferredError:
                raise
            except Exception as exc:
                classified = _classify_smtp_error(exc)
                logger.error(
                    "Failed to send email (code={code}, retryable={retryable})",
                    code=classified.code,
                    retryable=classified.retryable,
                )
                raise classified from exc

    async def _wait_for_rate_permit(
        self,
        *,
        rate_limit_per_minute: int,
        max_wait_seconds: float,
    ) -> None:
        try:
            slot_available = await self._delivery_rate_limiter.wait_for_send_slot(
                rate_per_minute=rate_limit_per_minute,
                max_wait_seconds=max_wait_seconds,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise EmailDeliveryRateDeferredError(
                "Email delivery pacing is temporarily unavailable"
            ) from exc
        if not slot_available:
            raise EmailDeliveryRateDeferredError("Email delivery pacing window is full")

    async def _send_with_auth_retry(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        message_id: str | None,
        rate_limit_per_minute: int,
        rate_limit_max_wait_seconds: float,
    ) -> None:
        loop = asyncio.get_running_loop()
        rate_wait_deadline = loop.time() + rate_limit_max_wait_seconds
        for attempt in range(1, _SMTP_AUTH_MAX_ATTEMPTS + 1):
            remaining_rate_wait = (
                rate_limit_max_wait_seconds
                if attempt == 1
                else max(0.0, rate_wait_deadline - loop.time())
            )
            if attempt > 1 and remaining_rate_wait == 0:
                raise EmailDeliveryRateDeferredError("Email delivery pacing window is full")
            # Each network attempt consumes provider capacity. In particular,
            # a retry after a temporary SMTP authentication response must not
            # bypass the global account rate.
            await self._wait_for_rate_permit(
                rate_limit_per_minute=rate_limit_per_minute,
                max_wait_seconds=remaining_rate_wait,
            )
            try:
                send_task = asyncio.create_task(
                    asyncio.to_thread(
                        self._send_sync,
                        to=to,
                        subject=subject,
                        body=body,
                        message_id=message_id,
                    ),
                    name="smtp-send-thread",
                )
                try:
                    await asyncio.shield(send_task)
                except asyncio.CancelledError:
                    # Cancelling asyncio.to_thread does not stop smtplib. Keep
                    # the sender lock and await the real thread before exposing
                    # cancellation; otherwise a later call could reuse/close
                    # the same socket concurrently with the orphaned thread.
                    try:
                        await send_task
                    except BaseException:
                        pass
                    self._discard_client()
                    raise
                return
            except smtplib.SMTPAuthenticationError as exc:
                if not _response_is_retryable(exc.smtp_code) or attempt == _SMTP_AUTH_MAX_ATTEMPTS:
                    raise
                logger.warning(
                    "Temporary SMTP authentication failure on attempt {attempt}; retrying once",
                    attempt=attempt,
                )
                await asyncio.sleep(_SMTP_AUTH_RETRY_DELAY_SECONDS)

    def _build_message(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        message_id: str | None,
    ) -> EmailMessage:
        email = self._config.email
        message = EmailMessage()
        message["Subject"] = subject
        from_name = email.from_name.strip()
        from_email = email.from_email.strip()
        message["From"] = f"{from_name} <{from_email}>" if from_name else from_email
        message["To"] = to
        if message_id:
            message["Message-ID"] = message_id
        message.set_content(body)
        return message

    def _open_client(self) -> smtplib.SMTP | smtplib.SMTP_SSL:
        email = self._config.email
        smtp_user = email.username.get_secret_value()
        smtp_password = email.password.get_secret_value()
        tls_context = ssl.create_default_context()
        timeout = getattr(email, "timeout_seconds", 20)

        client: smtplib.SMTP | smtplib.SMTP_SSL
        if email.use_ssl:
            client = smtplib.SMTP_SSL(
                email.host,
                email.port,
                timeout=timeout,
                context=tls_context,
            )
        else:
            client = smtplib.SMTP(email.host, email.port, timeout=timeout)
        try:
            if not email.use_ssl:
                client.ehlo()
                if email.use_tls:
                    client.starttls(context=tls_context)
                    client.ehlo()
            client.login(smtp_user, smtp_password)
        except Exception:
            try:
                client.close()
            except Exception:
                # A failed cleanup must not replace the SMTP failure that
                # determines retryability and the persisted delivery code.
                pass
            raise
        return client

    def _discard_client(self) -> None:
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                # Preserve the original SMTP failure and its safe code.
                pass

    def _send_sync(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        message_id: str | None,
    ) -> None:
        message = self._build_message(
            to=to,
            subject=subject,
            body=body,
            message_id=message_id,
        )
        try:
            if self._client is None:
                self._client = self._open_client()
            self._client.send_message(message)
        except Exception:
            self._discard_client()
            raise


__all__ = ["SmtpEmailSender"]
