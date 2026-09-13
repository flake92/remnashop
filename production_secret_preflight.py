#!/usr/bin/env python3
"""Read-only production secret policy preflight.

The command reports environment variable names and policy reasons only. It
never prints secret values and never modifies the environment file.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import re
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

MAX_ENV_FILE_BYTES = 1_048_576
PLACEHOLDER_PARTS = ("changeme", "replaceme", "example", "placeholder")
TRUE_VALUES = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class SecretRule:
    minimum_length: int
    required: bool = True
    base64_bytes: int | None = None


SECRET_RULES = {
    "APP_CRYPT_KEY": SecretRule(44, base64_bytes=32),
    "APP_JWT_SECRET": SecretRule(32, required=False),
    "APP_API_KEY": SecretRule(24, required=False),
    "APP_AUTH_SERVICE_KEY": SecretRule(24, required=False),
    "BOT_SECRET_TOKEN": SecretRule(32),
    "REMNAWAVE_WEBHOOK_SECRET": SecretRule(32),
    "DATABASE_PASSWORD": SecretRule(24),
    "REDIS_PASSWORD": SecretRule(24),
}
WEB_SECRETS = ("APP_JWT_SECRET", "APP_API_KEY", "APP_AUTH_SERVICE_KEY")


def _parse_value(raw_value: str) -> str:
    value = raw_value.strip()
    if not value:
        return ""

    if value[0] not in {"'", '"'}:
        comment = re.search(r"\s+#", value)
        return value[: comment.start()].rstrip() if comment else value

    quote = value[0]
    if "\\" in value:
        raise ValueError("quoted escape sequences are not supported")
    closing_quote = value.find(quote, 1)
    if closing_quote < 0:
        raise ValueError("quoted value is not terminated")
    trailing = value[closing_quote + 1 :].strip()
    if trailing and not trailing.startswith("#"):
        raise ValueError("unexpected characters after quoted value")
    return value[1:closing_quote]


def read_environment(path: Path) -> tuple[dict[str, str], set[str], set[str]]:
    if not path.is_file():
        raise ValueError("environment file is missing or is not a regular file")
    if path.stat().st_size > MAX_ENV_FILE_BYTES:
        raise ValueError("environment file exceeds the 1 MiB safety limit")

    values: dict[str, str] = {}
    duplicates: set[str] = set()
    syntax_errors: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", name):
            continue
        if name in values:
            duplicates.add(name)
        try:
            values[name] = _parse_value(raw_value)
        except ValueError:
            values[name] = ""
            if name in SECRET_RULES or name == "WEB_ENABLED":
                syntax_errors.add(name)
    return values, duplicates, syntax_errors


def _secret_reasons(value: str, rule: SecretRule) -> set[str]:
    reasons: set[str] = set()
    normalized = re.sub(r"[^a-z0-9]", "", value.lower())

    if not value:
        return {"missing"}
    if any(placeholder in normalized for placeholder in PLACEHOLDER_PARTS):
        reasons.add("placeholder")
    if len(value) < rule.minimum_length:
        reasons.add(f"shorter than {rule.minimum_length} characters")
    if len(set(value)) < 8:
        reasons.add("fewer than 8 distinct characters")
    if re.fullmatch(r"(.{1,8})\1+", value):
        reasons.add("repeated pattern")

    if rule.base64_bytes is not None:
        try:
            decoded = base64.b64decode(value, altchars=b"-_", validate=True)
        except (binascii.Error, ValueError):
            reasons.add(f"not a valid Base64 {rule.base64_bytes}-byte key")
        else:
            if len(decoded) != rule.base64_bytes:
                reasons.add(f"not a valid Base64 {rule.base64_bytes}-byte key")
    return reasons


def validate_environment(
    values: dict[str, str],
    duplicates: set[str],
    syntax_errors: set[str] | None = None,
) -> dict[str, set[str]]:
    issues: dict[str, set[str]] = defaultdict(set)
    for name in syntax_errors or set():
        issues[name].add("ambiguous .env value syntax")
    web_enabled = values.get("WEB_ENABLED", "").strip().lower() in TRUE_VALUES

    for name, rule in SECRET_RULES.items():
        required = rule.required or (web_enabled and name in WEB_SECRETS)
        value = values.get(name)
        if value is None:
            if required:
                issues[name].add("missing")
            continue
        issues[name].update(_secret_reasons(value, rule))

    configured = [name for name in SECRET_RULES if values.get(name)]
    for index, left_name in enumerate(configured):
        for right_name in configured[index + 1 :]:
            if values[left_name] == values[right_name]:
                issues[left_name].add(f"reused by {right_name}")
                issues[right_name].add(f"reuses {left_name}")

    managed_names = SECRET_RULES.keys() | {"WEB_ENABLED"}
    for name in duplicates & managed_names:
        issues[name].add("defined more than once")

    return {name: reasons for name, reasons in issues.items() if reasons}


def run(env_file: Path) -> int:
    try:
        values, duplicates, syntax_errors = read_environment(env_file)
    except (OSError, UnicodeError, ValueError) as error:
        sys.stderr.write(f"Production secret preflight failed: {error}.\n")
        sys.stderr.write("No environment values were printed or changed.\n")
        return 2

    issues = validate_environment(values, duplicates, syntax_errors)
    if issues:
        sys.stderr.write("Production secret preflight failed; no files or services were changed.\n")
        for name in sorted(issues):
            sys.stderr.write(f"- {name}: {', '.join(sorted(issues[name]))}\n")
        sys.stderr.write(
            "Do not replace DATABASE_PASSWORD or REDIS_PASSWORD directly when persistent "
            "volumes exist; follow PRODUCTION_SECRET_ROTATION.md.\n"
        )
        return 2

    sys.stdout.write(
        "Production secret preflight passed; secret values were not printed and nothing "
        "was changed.\n"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    arguments = parser.parse_args(argv)
    return run(arguments.env_file)


if __name__ == "__main__":
    raise SystemExit(main())
