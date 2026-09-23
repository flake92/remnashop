import pytest
from pydantic import SecretStr, ValidationError

from src.core.config import AppConfig
from src.core.config.bot import BotConfig
from src.core.config.database import DatabaseConfig
from src.core.config.redis import RedisConfig
from src.core.config.remnawave import RemnawaveConfig


def test_rejects_short_jwt_secret_even_when_web_is_disabled() -> None:
    with pytest.raises(ValidationError, match="APP_JWT_SECRET must be at least 32 characters"):
        AppConfig(jwt_secret=SecretStr("too-short"))


def test_rejects_placeholder_service_secret() -> None:
    with pytest.raises(ValidationError, match="APP_API_KEY must not contain a placeholder value"):
        AppConfig(api_key=SecretStr("replace-me-with-real-api-secret"))


def test_rejects_reused_application_secrets() -> None:
    shared = SecretStr("correct-horse-battery-staple-123456")

    with pytest.raises(ValidationError, match="must use different secrets"):
        AppConfig(jwt_secret=shared, api_key=shared)


def test_accepts_independent_strong_application_secrets() -> None:
    config = AppConfig(
        jwt_secret=SecretStr("jwt-3uQ9pL2xV7cN4mK8sR1dF6hJ0wT5yBzA"),
        api_key=SecretStr("api-9Xr4Kp7Vm2Lc8Qs5Wd1H"),
        auth_service_key=SecretStr("auth-6Nt3Yj8Bk5Gv1Pc9Zq4S"),
    )

    assert config.jwt_secret is not None


def test_rejects_low_entropy_encryption_key() -> None:
    with pytest.raises(ValidationError, match="APP_CRYPT_KEY must contain at least"):
        AppConfig(crypt_key=SecretStr("A" * 43 + "="))


def test_rejects_weak_public_webhook_secrets() -> None:
    with pytest.raises(ValidationError, match="BOT_SECRET_TOKEN must be at least 32"):
        BotConfig(
            token=SecretStr("unit-test-token"),
            secret_token=SecretStr("weak"),
            owner_id=1,
            support_username=SecretStr("unit_test_support"),
        )
    with pytest.raises(ValidationError, match="REMNAWAVE_WEBHOOK_SECRET must be at least 32"):
        RemnawaveConfig(
            token=SecretStr("unit-test-remnawave-token"),
            webhook_secret=SecretStr("weak"),
        )


def test_rejects_weak_data_store_passwords_when_configured() -> None:
    with pytest.raises(ValidationError, match="DATABASE_PASSWORD must be at least 24"):
        DatabaseConfig(password=SecretStr("weak"))
    with pytest.raises(ValidationError, match="REDIS_PASSWORD must be at least 24"):
        RedisConfig(password=SecretStr("weak"))
