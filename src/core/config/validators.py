import re
from typing import Any

from pydantic import SecretStr, ValidationInfo

from src.core.utils.validators import is_valid_username


def validate_not_change_me(value: Any, info: ValidationInfo) -> Any:
    current_value = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
    env_prefix = info.config.get("env_prefix", "") if info.config else ""
    field_name = info.field_name.upper() if info.field_name else "UNKNOWN_FIELD"
    full_env_var_name = f"{env_prefix}{field_name}"

    if not current_value or current_value.strip().lower() == "change_me":
        raise ValueError(f"{full_env_var_name} must be set and not equal to 'change_me'")

    return value


def validate_strong_secret(
    value: Any,
    info: ValidationInfo,
    *,
    minimum_length: int,
    env_prefix: str = "",
) -> Any:
    current_value = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
    configured_prefix = info.config.get("env_prefix", "") if info.config else ""
    field_name = info.field_name.upper() if info.field_name else "UNKNOWN_FIELD"
    full_env_var_name = f"{env_prefix or configured_prefix}{field_name}"
    normalized = re.sub(r"[^a-z0-9]", "", current_value.lower())

    if len(current_value) < minimum_length:
        raise ValueError(f"{full_env_var_name} must be at least {minimum_length} characters")
    if any(
        placeholder in normalized
        for placeholder in ("changeme", "replaceme", "example", "placeholder")
    ):
        raise ValueError(f"{full_env_var_name} must not contain a placeholder value")
    if len(set(current_value)) < 8:
        raise ValueError(f"{full_env_var_name} must contain at least 8 distinct characters")
    if re.fullmatch(r"(.{1,8})\1+", current_value):
        raise ValueError(f"{full_env_var_name} must not be a repeated pattern")

    return value


def validate_username(value: Any, info: ValidationInfo) -> Any:
    current_value = value.get_secret_value() if isinstance(value, SecretStr) else str(value)
    env_prefix = info.config.get("env_prefix", "") if info.config else ""
    field_name = info.field_name.upper() if info.field_name else "UNKNOWN_FIELD"
    full_env_var_name = f"{env_prefix}{field_name}"

    if not is_valid_username(f"@{current_value}"):
        raise ValueError(f"{full_env_var_name} contains invalid Telegram username")

    return value
