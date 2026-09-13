from typing import Optional

from pydantic import RedisDsn, SecretStr, ValidationInfo, field_validator

from .base import BaseConfig
from .validators import validate_strong_secret


class RedisConfig(BaseConfig, env_prefix="REDIS_"):
    host: str = "remnashop-redis"
    port: int = 6379
    name: str = "0"
    password: Optional[SecretStr] = None

    @field_validator("password")
    @classmethod
    def validate_redis_password(
        cls,
        field: Optional[SecretStr],
        info: ValidationInfo,
    ) -> Optional[SecretStr]:
        if field is not None:
            validate_strong_secret(field, info, minimum_length=24, env_prefix="REDIS_")
        return field

    @property
    def dsn(self) -> str:
        return RedisDsn.build(
            scheme="redis",
            password=self.password.get_secret_value() if self.password else None,
            host=self.host,
            port=self.port,
            path=self.name,
        ).unicode_string()
