from __future__ import annotations

import base64
import subprocess
import sys
from pathlib import Path

from production_secret_preflight import read_environment, validate_environment


def _valid_values() -> dict[str, str]:
    return {
        "APP_CRYPT_KEY": base64.b64encode(bytes(range(32))).decode(),
        "APP_JWT_SECRET": "jwt-7Gk2Pq9Vm4Xs8Hr3Ty6Wb1Nc5Df0Za4L",
        "APP_API_KEY": "api-8Hm3Qr6Zn5Yt9Js4Ux7Wc2Pd",
        "APP_AUTH_SERVICE_KEY": "auth-9Jn4Rs7Ao6Zu0Kt5Vy8Xd3Qe",
        "BOT_SECRET_TOKEN": "bot-1Kp5St8Bp7Av2Lu6Wz9Ye4Rf3CxQ",
        "REMNAWAVE_WEBHOOK_SECRET": "webhook-2Lq6Tu9Cq8Bw3Mv7Xa0Zf5Sg",
        "DATABASE_PASSWORD": "database-3Mr7Uv0Dr9Cx4Nw8Yb1Ag6Th",
        "REDIS_PASSWORD": "valkey-4Ns8Vw1Es0Dy5Px9Zc2Bh7Ui",
        "WEB_ENABLED": "true",
    }


def _write_env(path: Path, values: dict[str, str]) -> bytes:
    content = "".join(f"{name}={value}\n" for name, value in values.items()).encode()
    path.write_bytes(content)
    return content


def test_preflight_accepts_independent_strong_secrets() -> None:
    assert validate_environment(_valid_values(), set()) == {}


def test_preflight_lists_names_only_and_never_mutates_the_env(tmp_path: Path) -> None:
    values = _valid_values()
    reused = "shared-5Pt9Wx2Ft1Ez6Qy0Ad3Ci8Vj"
    values["DATABASE_PASSWORD"] = "oldshort"
    values["REDIS_PASSWORD"] = "change_me"
    values["APP_API_KEY"] = reused
    values["APP_AUTH_SERVICE_KEY"] = reused
    env_file = tmp_path / ".env"
    before = _write_env(env_file, values)

    result = subprocess.run(
        [sys.executable, "production_secret_preflight.py", "--env-file", str(env_file)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 2
    assert env_file.read_bytes() == before
    assert {
        "APP_API_KEY",
        "APP_AUTH_SERVICE_KEY",
        "DATABASE_PASSWORD",
        "REDIS_PASSWORD",
    } <= {line.split(":", 1)[0].removeprefix("- ") for line in result.stderr.splitlines()}
    for secret in ("oldshort", "change_me", reused):
        assert secret not in result.stdout
        assert secret not in result.stderr
    assert "no files or services were changed" in result.stderr
    assert "PRODUCTION_SECRET_ROTATION.md" in result.stderr


def test_preflight_requires_web_secrets_only_when_web_is_enabled() -> None:
    values = _valid_values()
    for name in ("APP_JWT_SECRET", "APP_API_KEY", "APP_AUTH_SERVICE_KEY"):
        values.pop(name)
    values["WEB_ENABLED"] = "false"
    assert validate_environment(values, set()) == {}

    values["WEB_ENABLED"] = "true"
    issues = validate_environment(values, set())
    assert set(issues) == {"APP_JWT_SECRET", "APP_API_KEY", "APP_AUTH_SERVICE_KEY"}
    assert all(reasons == {"missing"} for reasons in issues.values())


def test_preflight_rejects_invalid_crypt_key_and_duplicate_secret_definition() -> None:
    values = _valid_values()
    values["APP_CRYPT_KEY"] = base64.b64encode(b"not-32-bytes").decode()
    issues = validate_environment(values, {"BOT_SECRET_TOKEN"})

    assert "not a valid Base64 32-byte key" in issues["APP_CRYPT_KEY"]
    assert "defined more than once" in issues["BOT_SECRET_TOKEN"]


def test_preflight_parses_comments_without_counting_them_as_secret_strength(
    tmp_path: Path,
) -> None:
    env_file = tmp_path / ".env"
    values = _valid_values()
    values["DATABASE_PASSWORD"] = '"short-value" # a long comment is not password entropy'
    _write_env(env_file, values)

    parsed, duplicates, syntax_errors = read_environment(env_file)
    issues = validate_environment(parsed, duplicates, syntax_errors)

    assert "shorter than 24 characters" in issues["DATABASE_PASSWORD"]


def test_preflight_fails_closed_on_ambiguous_quoted_secret(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"
    values = _valid_values()
    values["REDIS_PASSWORD"] = '"strong-looking-value-with-an-unterminated-quote'
    _write_env(env_file, values)

    parsed, duplicates, syntax_errors = read_environment(env_file)
    issues = validate_environment(parsed, duplicates, syntax_errors)

    assert "ambiguous .env value syntax" in issues["REDIS_PASSWORD"]


def test_makefile_and_rotation_runbook_keep_existing_production_fail_closed() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    runbook = Path("PRODUCTION_SECRET_ROTATION.md").read_text(encoding="utf-8")

    assert "_run_prod: production-preflight" in makefile
    assert "production_secret_preflight.py --env-file .env" in makefile
    assert "not validated or rotated" in makefile
    assert "does not make an existing production deployment ready" in makefile
    assert "Secrets updated" not in makefile

    assert "do not replace its password only in `.env`" in runbook
    assert "ALTER ROLE" in runbook
    assert "CONFIG SET requirepass" in runbook
    assert "os.replace" in runbook
    assert "PostgreSQL rollback" in runbook
    assert "Valkey rollback" in runbook
    assert 'printf %s "$next_password"' in runbook
    assert 'printf %s "$old_password"' in runbook
    assert "printf '%s\\n' \"$next_password\" |" not in runbook
    assert "printf '%s\\n' \"$old_password\" |" not in runbook
    assert 'REDISCLI_AUTH="$old_password" valkey-cli' in runbook
    assert 'REDISCLI_AUTH="$next_password" valkey-cli' in runbook
    assert "IFS= read -r old_password" in runbook
    assert "IFS= read -r next_password" in runbook
    assert 'replace_env_from_file DATABASE_PASSWORD "$ROTATION_DIR/database.old"' in runbook
    assert 'replace_env_from_file REDIS_PASSWORD "$ROTATION_DIR/valkey.old"' in runbook
    assert "down -v" not in runbook
