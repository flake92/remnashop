import base64
import binascii
from typing import Self

from pydantic import Field, SecretStr, field_validator, model_validator

from .base import BaseConfig


class BulkEmailConfig(BaseConfig, env_prefix="BULK_EMAIL_"):
    """Separate SMTP account for e-mail copies of bot notifications.

    Kept apart from the transactional account (EMAIL_*) so a bulk send can
    neither delay login codes nor spend their sender reputation. Without it
    the e-mail channel stays off; Telegram and push are unaffected.
    """

    enabled: bool = False
    host: str = ""
    port: int = 587
    timeout_seconds: int = Field(default=20, ge=1, le=30)
    use_tls: bool = True
    use_ssl: bool = False
    allow_insecure_smtp: bool = False
    username: SecretStr = SecretStr("")
    password: SecretStr = SecretStr("")
    from_email: str = ""
    from_name: str = ""
    reply_to: str = ""
    # Conservative default: new sending domains warm up slowly, and most
    # mailbox providers throttle or junk sudden volume.
    rate_per_minute: int = Field(default=30, ge=1, le=600)
    max_per_run: int = Field(default=30, ge=1, le=1000)
    max_runtime_seconds: int = Field(default=45, ge=5, le=50)

    @model_validator(mode="after")
    def validate_smtp_transport_security(self) -> Self:
        if self.use_tls and self.use_ssl:
            raise ValueError("BULK_EMAIL_USE_TLS and BULK_EMAIL_USE_SSL cannot both be true")
        if self.enabled and not self.use_tls and not self.use_ssl and not self.allow_insecure_smtp:
            raise ValueError(
                "Enabled bulk SMTP requires BULK_EMAIL_USE_TLS=true or BULK_EMAIL_USE_SSL=true; "
                "plaintext is development-only and requires BULK_EMAIL_ALLOW_INSECURE_SMTP=true"
            )
        return self


def _b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class WebPushConfig(BaseConfig, env_prefix="WEB_PUSH_"):
    """VAPID identity (RFC 8292) for Web Push to the installed cabinet."""

    vapid_public_key: str = ""
    vapid_private_key: SecretStr = SecretStr("")
    # mailto: or https: contact that push services may use about abuse.
    vapid_subject: str = ""
    max_per_run: int = Field(default=200, ge=1, le=5000)

    @property
    def is_configured(self) -> bool:
        return bool(
            self.vapid_public_key
            and self.vapid_private_key.get_secret_value()
            and self.vapid_subject
        )

    @field_validator("vapid_public_key")
    @classmethod
    def validate_public_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return value
        try:
            raw = _b64url_decode(value)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("WEB_PUSH_VAPID_PUBLIC_KEY must be base64url") from exc
        if len(raw) != 65 or raw[0] != 0x04:
            raise ValueError(
                "WEB_PUSH_VAPID_PUBLIC_KEY must be an uncompressed P-256 point (65 bytes)"
            )
        return value

    @field_validator("vapid_subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        value = value.strip()
        if value and not (value.startswith("mailto:") or value.startswith("https://")):
            raise ValueError("WEB_PUSH_VAPID_SUBJECT must start with mailto: or https://")
        return value
