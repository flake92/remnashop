from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from src.core.constants import EMAIL_CODE_LENGTH
from src.core.enums import AuthType


class RegisterRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=8, max_length=256)
    name: Optional[str] = Field(default=None, min_length=1, max_length=128)
    referral_code: Optional[str] = Field(default=None, min_length=3, max_length=64)
    # News e-mail is advertising: only an explicit tick opts in.
    email_news_consent: bool = Field(default=False, strict=True)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()

    @field_validator("referral_code")
    @classmethod
    def normalize_referral_code(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None


class LoginRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    password: str = Field(min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()


class StartGenericEmailAuthRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()


class CompleteGenericEmailAuthRequest(StartGenericEmailAuthRequest):
    code: str = Field(
        min_length=EMAIL_CODE_LENGTH,
        max_length=EMAIL_CODE_LENGTH,
        pattern=r"^\d{6}$",
    )
    password: str = Field(min_length=8, max_length=256)


class GenericEmailAuthStartResponse(BaseModel):
    success: bool


class IdentifyEmailResponse(BaseModel):
    exists: bool


class ServiceSessionRequest(StartGenericEmailAuthRequest):
    user_id: str = Field(min_length=1, max_length=32, pattern=r"^[1-9]\d*$")


class ChangePasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=256)
    new_password: str = Field(min_length=8, max_length=256)


class RequestPasswordResetRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()


class ConfirmPasswordResetRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    code: str = Field(
        min_length=EMAIL_CODE_LENGTH,
        max_length=EMAIL_CODE_LENGTH,
        pattern=r"^\d{6}$",
    )
    new_password: str = Field(min_length=8, max_length=256)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()


class MigrateTelegramRequest(RegisterRequest):
    pass


class AuthResponse(BaseModel):
    expires_at: datetime
    refresh_expires_at: datetime


class MeResponse(BaseModel):
    telegram_id: Optional[int]
    auth_type: AuthType
    email: Optional[str]
    is_email_verified: bool
    has_password: bool
    pending_email: Optional[str]
    name: str
    username: Optional[str]
    language: str


class NotificationPreferencesResponse(BaseModel):
    subscription_expiration_email_enabled: bool
    email_eligible: bool
    sender_email: Optional[str]
    days_before: list[int]
    service_email_enabled: bool = True
    news_email_enabled: bool = False
    bulk_sender_email: Optional[str] = None
    push_available: bool = False


class UpdateNotificationPreferencesRequest(BaseModel):
    # Every field is optional; at least one must be present.
    subscription_expiration_email_enabled: Optional[bool] = Field(default=None, strict=True)
    service_email_enabled: Optional[bool] = Field(default=None, strict=True)
    news_email_enabled: Optional[bool] = Field(default=None, strict=True)

    @model_validator(mode="after")
    def require_one_field(self) -> "UpdateNotificationPreferencesRequest":
        if (
            self.subscription_expiration_email_enabled is None
            and self.service_email_enabled is None
            and self.news_email_enabled is None
        ):
            raise ValueError("At least one preference is required")
        return self


class PushSubscriptionKeys(BaseModel):
    p256dh: str = Field(min_length=80, max_length=128, pattern=r"^[A-Za-z0-9_-]+=*$")
    auth: str = Field(min_length=16, max_length=64, pattern=r"^[A-Za-z0-9_-]+=*$")


class PushSubscriptionRequest(BaseModel):
    endpoint: str = Field(min_length=12, max_length=2048)
    keys: PushSubscriptionKeys
    user_agent: Optional[str] = Field(default=None, max_length=512)


class PushSubscriptionDeleteRequest(BaseModel):
    endpoint: str = Field(min_length=12, max_length=2048)


class PushKeyResponse(BaseModel):
    enabled: bool
    public_key: Optional[str]


class PushStatusResponse(BaseModel):
    devices: int


class UnsubscribeRequest(BaseModel):
    token: str = Field(min_length=5, max_length=128)


class UnsubscribeResponse(BaseModel):
    category: str


class ChangePasswordResponse(BaseModel):
    success: bool


class PasswordResetResponse(BaseModel):
    success: bool


class PasswordResetConfirmResponse(PasswordResetResponse):
    expires_at: datetime
    refresh_expires_at: datetime


class ChangeEmailRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: str = Field(max_length=255, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: str) -> str:
        return value.lower()


class ChangeEmailResponse(BaseModel):
    success: bool
    pending_email: str


class RequestEmailVerificationCodeRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    email: Optional[str] = Field(
        default=None,
        max_length=255,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return value.lower()


class RequestEmailVerificationCodeResponse(BaseModel):
    success: bool
    target_email: str
    expires_at: datetime


class ConfirmEmailVerificationRequest(BaseModel):
    code: str = Field(
        min_length=EMAIL_CODE_LENGTH,
        max_length=EMAIL_CODE_LENGTH,
        pattern=r"^\d{6}$",
    )


class ConfirmEmailVerificationResponse(BaseModel):
    success: bool
    email: str


class TelegramAuthRequest(BaseModel):
    id: int
    first_name: str
    last_name: Optional[str] = None
    username: Optional[str] = None
    photo_url: Optional[str] = None
    auth_date: int
    hash: str


class TelegramWebAppAuthRequest(BaseModel):
    init_data: str


class LogoutResponse(BaseModel):
    success: bool
