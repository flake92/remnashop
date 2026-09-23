from datetime import datetime
from typing import Union


class MenuRenderError(Exception): ...


class PermissionDeniedError(Exception): ...


class UserNotFoundError(Exception):
    def __init__(self, user_id: Union[int, str, None] = None) -> None:
        self.user_id = user_id
        super().__init__(f"User with id '{user_id}' not found" if user_id else "User not found")


class FileNotFoundError(Exception): ...


class LogsToFileDisabledError(Exception):
    def __init__(self) -> None:
        super().__init__("Logging to file is disabled in configuration")


class PlanError(Exception): ...


class SquadsEmptyError(PlanError): ...


class TrialDurationError(PlanError): ...


class PlanNameAlreadyExistsError(PlanError): ...


class UserAlreadyAllowedError(PlanError): ...


class DurationAlreadyExistsError(PlanError): ...


class PriceNotFoundError(PlanError): ...


class GatewayNotConfiguredError(Exception): ...


class PurchaseError(Exception): ...


class TransactionNotRetryableError(Exception): ...


class TrialNotAvailableError(Exception): ...


class MenuEditorInvalidPayloadError(Exception): ...


class BlacklistSourceAlreadyExistsError(Exception): ...


class CooldownError(Exception):
    def __init__(self, available_at: datetime) -> None:
        self.available_at = available_at
        super().__init__(f"Cooldown active until {available_at}")


class PromocodeError(Exception): ...


class PromocodeNotFoundError(PromocodeError): ...


class PromocodeNotAvailableError(PromocodeError): ...


class PromocodeExpiredError(PromocodeNotAvailableError): ...


class PromocodeAlreadyActivatedError(PromocodeError): ...


class EmailDeliveryError(Exception):
    """Safe, classified e-mail transport failure.

    ``code`` is suitable for durable outbox state and logs; it must never
    contain an address or the provider response body. ``retryable`` separates
    temporary transport/provider failures from permanent recipient or
    configuration rejection.
    """

    def __init__(
        self,
        message: str = "Failed to send email. Please try again later.",
        *,
        code: str = "SMTP_UNEXPECTED",
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class EmailDeliveryRateDeferredError(EmailDeliveryError):
    """No SMTP attempt started because the configured pacing window was full."""

    def __init__(self, message: str = "Email delivery is temporarily busy") -> None:
        super().__init__(message, code="SMTP_RATE_DEFERRED", retryable=True)


class EmailDeliveryDisabledError(Exception): ...
