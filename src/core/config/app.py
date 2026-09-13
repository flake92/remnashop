import base64
import binascii
import re
import secrets
from pathlib import Path
from typing import Optional, Self

from pydantic import Field, SecretStr, ValidationInfo, field_validator, model_validator

from src.core.constants import API_V1, ASSETS_DEFAULT_DIR, ASSETS_DIR, PAYMENTS_WEBHOOK_PATH
from src.core.enums import Locale, PaymentGatewayType
from src.core.types import LocaleList, StringList
from src.core.utils.validators import is_valid_domain, is_valid_url

from .base import BaseConfig
from .bot import BotConfig
from .build import BuildConfig
from .database import DatabaseConfig
from .email import EmailConfig
from .log import LogConfig
from .redis import RedisConfig
from .remnawave import RemnawaveConfig
from .validators import validate_not_change_me, validate_strong_secret


def _validate_distinct_application_secrets(
    configured_secrets: dict[str, Optional[SecretStr]],
) -> None:
    present_secrets = [
        (name, value.get_secret_value())
        for name, value in configured_secrets.items()
        if value is not None
    ]
    for index, (left_name, left_value) in enumerate(present_secrets):
        for right_name, right_value in present_secrets[index + 1 :]:
            if secrets.compare_digest(left_value, right_value):
                raise ValueError(f"{left_name} and {right_name} must use different secrets")


class AppConfig(BaseConfig, env_prefix="APP_"):
    domain: SecretStr
    host: str = "0.0.0.0"
    port: int = 5000

    locales: LocaleList = LocaleList([Locale.RU])  # TODO: Change to EN
    default_locale: Locale = Locale.RU  # TODO: Change to EN

    crypt_key: SecretStr
    jwt_secret: Optional[SecretStr] = None
    api_key: Optional[SecretStr] = None
    auth_service_key: Optional[SecretStr] = None
    assets_dir: Path = ASSETS_DIR
    origins: StringList = StringList("")
    swagger_enabled: bool = False
    web_enabled: bool = Field(default=False, validation_alias="WEB_ENABLED")
    web_cabinet_url: str = Field(default="", validation_alias="WEB_CABINET_URL")
    referral_reward_backfill_enabled: bool = Field(
        default=False,
        validation_alias="REFERRAL_REWARD_BACKFILL_ENABLED",
    )
    referral_reward_legacy_recovery_enabled: bool = Field(
        default=False,
        validation_alias="REFERRAL_REWARD_LEGACY_RECOVERY_ENABLED",
    )
    referral_reward_legacy_recovery_manifest_path: Optional[Path] = Field(
        default=None,
        validation_alias="REFERRAL_REWARD_LEGACY_RECOVERY_MANIFEST_PATH",
    )
    referral_reward_legacy_recovery_manifest_sha256: Optional[str] = Field(
        default=None,
        validation_alias="REFERRAL_REWARD_LEGACY_RECOVERY_MANIFEST_SHA256",
    )

    bot: BotConfig = Field(default_factory=BotConfig)
    remnawave: RemnawaveConfig = Field(default_factory=RemnawaveConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    log: LogConfig = Field(default_factory=LogConfig)

    @property
    def default_assets_dir(self) -> Path:
        return ASSETS_DEFAULT_DIR

    @property
    def banners_dir(self) -> Path:
        return self.assets_dir / "banners"

    @property
    def translations_dir(self) -> Path:
        return self.assets_dir / "translations"

    @property
    def default_banners_dir(self) -> Path:
        return self.default_assets_dir / "banners"

    @property
    def default_translations_dir(self) -> Path:
        return self.default_assets_dir / "translations"

    def get_webhook(self, gateway_type: PaymentGatewayType) -> str:
        domain = f"https://{self.domain.get_secret_value()}"
        path = f"{API_V1 + PAYMENTS_WEBHOOK_PATH}/{gateway_type.lower()}"
        return domain + path

    @classmethod
    def get(cls) -> Self:
        return cls()

    @model_validator(mode="after")
    def validate_web_secrets(self) -> "AppConfig":
        if self.web_enabled:
            if not self.api_key:
                raise ValueError(
                    "APP_API_KEY must be set when WEB_ENABLED=true; "
                    "do not reuse APP_CRYPT_KEY for API authentication"
                )
            if not self.jwt_secret:
                raise ValueError(
                    "APP_JWT_SECRET must be set when WEB_ENABLED=true; "
                    "do not reuse APP_CRYPT_KEY for JWT signing"
                )
            if not self.auth_service_key:
                raise ValueError(
                    "APP_AUTH_SERVICE_KEY must be set when WEB_ENABLED=true; "
                    "use a dedicated least-privilege credential for web auth"
                )
            if self.api_key and secrets.compare_digest(
                self.api_key.get_secret_value(), self.auth_service_key.get_secret_value()
            ):
                raise ValueError("APP_AUTH_SERVICE_KEY must not reuse APP_API_KEY")
        _validate_distinct_application_secrets(
            {
                "APP_CRYPT_KEY": self.crypt_key,
                "APP_JWT_SECRET": self.jwt_secret,
                "APP_API_KEY": self.api_key,
                "APP_AUTH_SERVICE_KEY": self.auth_service_key,
                "BOT_SECRET_TOKEN": self.bot.secret_token,
                "REMNAWAVE_WEBHOOK_SECRET": self.remnawave.webhook_secret,
                "DATABASE_PASSWORD": self.database.password,
                "REDIS_PASSWORD": self.redis.password,
            }
        )
        if self.referral_reward_legacy_recovery_enabled:
            manifest_path = self.referral_reward_legacy_recovery_manifest_path
            manifest_sha256 = self.referral_reward_legacy_recovery_manifest_sha256
            if manifest_path is None or not manifest_path.is_absolute():
                raise ValueError(
                    "Legacy referral recovery requires an absolute trusted manifest path"
                )
            if (
                manifest_sha256 is None
                or re.fullmatch(
                    r"[0-9a-f]{64}",
                    manifest_sha256,
                )
                is None
            ):
                raise ValueError(
                    "Legacy referral recovery requires a lowercase trusted manifest SHA-256"
                )
        return self

    @field_validator("domain")
    @classmethod
    def validate_domain(cls, field: SecretStr, info: ValidationInfo) -> SecretStr:
        validate_not_change_me(field, info)

        if not is_valid_domain(field.get_secret_value()):
            raise ValueError("APP_DOMAIN has invalid format")

        return field

    @field_validator("web_cabinet_url")
    @classmethod
    def validate_web_cabinet_url(cls, value: str) -> str:
        url = value.strip()
        if url and not is_valid_url(url):
            raise ValueError("WEB_CABINET_URL must be an HTTPS URL")
        return url

    @field_validator("crypt_key")
    @classmethod
    def validate_crypt_key(cls, field: SecretStr, info: ValidationInfo) -> SecretStr:
        validate_not_change_me(field, info)

        value = field.get_secret_value()
        try:
            decoded = base64.b64decode(value, altchars=b"-_", validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("APP_CRYPT_KEY must be a valid Base64-encoded 32-byte key") from error
        if len(decoded) != 32:
            raise ValueError("APP_CRYPT_KEY must be a valid Base64-encoded 32-byte key")
        validate_strong_secret(field, info, minimum_length=44, env_prefix="APP_")

        return field

    @field_validator("jwt_secret")
    @classmethod
    def validate_jwt_secret(
        cls, field: Optional[SecretStr], info: ValidationInfo
    ) -> Optional[SecretStr]:
        if field is not None:
            validate_strong_secret(field, info, minimum_length=32, env_prefix="APP_")
        return field

    @field_validator("api_key", "auth_service_key")
    @classmethod
    def validate_service_secret(
        cls, field: Optional[SecretStr], info: ValidationInfo
    ) -> Optional[SecretStr]:
        if field is not None:
            validate_strong_secret(field, info, minimum_length=24, env_prefix="APP_")
        return field
