from pathlib import Path


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
        assert compose.count("condition: service_completed_successfully") == 3
