from src.application.dto import UserDto
from src.core.utils.time import datetime_now


def enable_expiration_reminders_after_email_verification(user: UserDto) -> None:
    """Enable the default reminder preference at the verified-email boundary."""
    if (
        user.subscription_expiration_email_enabled
        and user.subscription_expiration_email_enabled_at is not None
    ):
        return

    user.subscription_expiration_email_enabled = True
    user.subscription_expiration_email_enabled_at = datetime_now()
