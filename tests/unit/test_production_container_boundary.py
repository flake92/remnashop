from pathlib import Path

import pytest

COMPOSE_FILES = (
    "docker-compose.prod.internal.yml",
    "docker-compose.prod.external.yml",
    "docker-compose.local.yml",
)


def _service_lines(name: str, service_name: str) -> set[str]:
    lines = Path(name).read_text(encoding="utf-8").splitlines()
    marker = f"  {service_name}:"
    start = lines.index(marker) + 1
    service_lines: set[str] = set()
    for line in lines[start:]:
        if line.strip() and not line.startswith("    "):
            break
        service_lines.add(line.strip())
    return service_lines


def test_application_entrypoint_does_not_run_database_migrations() -> None:
    entrypoint = Path("docker-entrypoint.sh").read_text(encoding="utf-8")
    migration = Path("docker-migrate.sh").read_text(encoding="utf-8")

    assert "alembic" not in entrypoint
    assert "alembic -c src/infrastructure/database/alembic.ini upgrade head" in migration


def test_production_services_wait_for_one_shot_migration() -> None:
    for name in ("docker-compose.prod.internal.yml", "docker-compose.prod.external.yml"):
        compose = Path(name).read_text(encoding="utf-8")
        assert "remnashop-migration:" in compose
        assert 'restart: "no"' in compose
        assert 'command: ["./docker-migrate.sh"]' in compose
        assert compose.count("condition: service_completed_successfully") == 4
        assert "remnashop-storage-init:" in compose


def test_task_queue_does_not_trim_or_ack_unfinished_work() -> None:
    broker = Path("src/infrastructure/taskiq/broker.py").read_text(encoding="utf-8")
    assert "maxlen=" not in broker
    assert "REFRESH_PENDING_LEASE_SCRIPT" in broker
    assert "ACK_AND_DELETE_OWNED_ENTRY_SCRIPT" in broker
    assert "pending[1][2] ~= ARGV[2]" in broker
    assert "'XCLAIM'" in broker
    assert "'JUSTID'" in broker
    assert "redis.call('XACK'" in broker
    assert "redis.call('XDEL'" in broker
    assert broker.index("redis.call('XACK'") < broker.index("redis.call('XDEL'")

    for name in COMPOSE_FILES:
        compose = Path(name).read_text(encoding="utf-8")
        assert "--ack-type when_executed" in compose
        assert "--ack-type when_received" not in compose


def test_production_runtime_is_read_only_non_root_and_health_checked() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    user_entrypoint = Path("docker-user-entrypoint.sh").read_text(encoding="utf-8")

    assert 'ENTRYPOINT ["./docker-user-entrypoint.sh"]' in dockerfile
    assert "USER 10001:10001" in dockerfile
    assert "adduser -S -D -H -u 10001" in dockerfile
    assert 'exec su-exec remnashop "$@"' in user_entrypoint
    assert 'find "$writable_path"' in user_entrypoint

    for name in ("docker-compose.prod.internal.yml", "docker-compose.prod.external.yml"):
        compose = Path(name).read_text(encoding="utf-8")
        assert compose.count("*runtime") == 5
        assert 'user: "10001:10001"' in compose
        assert "read_only: true" in compose
        assert "no-new-privileges:true" in compose
        assert "cap_drop:\n    - ALL" in compose
        assert compose.count("src.infrastructure.healthcheck") == 3


def test_production_images_are_immutable() -> None:
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    assert all("@sha256:" in line for line in dockerfile.splitlines() if line.startswith("FROM "))

    for name in ("docker-compose.prod.internal.yml", "docker-compose.prod.external.yml"):
        compose = Path(name).read_text(encoding="utf-8")
        assert ":latest" not in compose
        assert "ghcr.io/${REMNASHOP_IMAGE_REPOSITORY:?" in compose
        assert "@${REMNASHOP_IMAGE_DIGEST:?" in compose
        assert "postgres:17@sha256:" in compose
        assert "valkey/valkey:9-alpine@sha256:" in compose


def test_production_data_services_are_private_and_receive_only_required_env() -> None:
    for name in ("docker-compose.prod.internal.yml", "docker-compose.prod.external.yml"):
        compose = Path(name).read_text(encoding="utf-8")
        assert "remnashop-data-network:" in compose
        assert "internal: true" in compose
        assert "REDIS_PASSWORD: ${REDIS_PASSWORD:?" in compose
        assert "--requirepass" in compose
        assert "--maxmemory-policy\n      - noeviction" in compose

        for service_name in ("remnashop-db", "remnashop-redis"):
            lines = _service_lines(name, service_name)
            assert "env_file: .env" not in lines
            assert "*env" not in "\n".join(lines)
            assert "- remnawave-network" not in lines
            assert "- remnashop-data-network" in lines


def test_large_temporary_work_uses_disk_backed_volume() -> None:
    for name in ("docker-compose.prod.internal.yml", "docker-compose.prod.external.yml"):
        compose = Path(name).read_text(encoding="utf-8")
        assert "TMPDIR: /opt/remnashop/tmp" in compose
        assert "remnashop-tmp:/opt/remnashop/tmp" in compose


def test_production_make_target_cannot_delete_volumes_or_build_stale_source() -> None:
    makefile = Path("Makefile").read_text(encoding="utf-8")
    production_recipe = makefile.split(".PHONY: _run_prod", maxsplit=1)[1].split(
        "# ── Migrations", maxsplit=1
    )[0]

    assert "down -v" not in production_recipe
    assert "up --build" not in production_recipe
    assert "Refusing production reset" in production_recipe
    assert "REMNASHOP_IMAGE_DIGEST=sha256:[0-9a-f]{64}" in production_recipe
    assert "TASKIQ_DEPLOYMENT_ID=change_me" in production_recipe
    assert "docker-compose.prod.external.yml pull" in production_recipe
    assert "--wait --wait-timeout 600" in production_recipe


def test_environment_template_and_setup_cover_production_runtime_contract() -> None:
    template = Path(".env.example").read_text(encoding="utf-8")
    makefile = Path("Makefile").read_text(encoding="utf-8")

    required_values = {
        'APP_ASSETS_DIR="/opt/remnashop/assets"',
        "APP_JWT_SECRET=change_me",
        "APP_API_KEY=change_me",
        "APP_AUTH_SERVICE_KEY=change_me",
        "REDIS_PASSWORD=change_me",
        "TASKIQ_DEPLOYMENT_ID=change_me",
        "REMNASHOP_IMAGE_REPOSITORY=flake92/remnashop",
    }
    assert required_values <= set(template.splitlines())
    for name in (
        "APP_CRYPT_KEY",
        "APP_JWT_SECRET",
        "APP_API_KEY",
        "APP_AUTH_SERVICE_KEY",
        "BOT_SECRET_TOKEN",
        "REMNAWAVE_WEBHOOK_SECRET",
        "DATABASE_PASSWORD",
        "REDIS_PASSWORD",
        "TASKIQ_DEPLOYMENT_ID",
    ):
        assert f"ensure_secret {name}" in makefile
    assert "*) return 0 ;;" in makefile


@pytest.mark.parametrize("name", COMPOSE_FILES)
def test_runtime_services_define_graceful_shutdown_contract(name: str) -> None:
    assert {
        "init: true",
        "stop_signal: SIGTERM",
        "stop_grace_period: 2m",
    } <= _service_lines(name, "remnashop")
    assert {
        "init: true",
        "stop_signal: SIGTERM",
        "stop_grace_period: 10m",
    } <= _service_lines(name, "remnashop-taskiq-worker")
    assert {
        "init: true",
        "stop_signal: SIGINT",
        "stop_grace_period: 30s",
    } <= _service_lines(name, "remnashop-taskiq-scheduler")
