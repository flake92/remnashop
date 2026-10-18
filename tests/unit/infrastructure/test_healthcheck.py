import subprocess
import sys

import pytest

from src.core.config.redis import RedisConfig
from src.infrastructure import healthcheck
from src.infrastructure.healthcheck import heartbeat_is_fresh
from src.infrastructure.taskiq import health_state
from src.infrastructure.taskiq.health_state import (
    taskiq_deployment_id,
    taskiq_heartbeat_key,
    taskiq_queue_name,
    taskiq_stream_max_entries,
)


def test_taskiq_heartbeat_requires_recent_valid_timestamp() -> None:
    assert heartbeat_is_fresh("100", now=101)
    assert heartbeat_is_fresh(b"100", now=250)
    assert not heartbeat_is_fresh("100", now=251)
    assert not heartbeat_is_fresh("future", now=101)
    assert not heartbeat_is_fresh("102", now=101)
    assert not heartbeat_is_fresh(None, now=101)


def test_taskiq_heartbeat_is_scoped_to_deployment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASKIQ_DEPLOYMENT_ID", "blue-release")
    blue_key = taskiq_heartbeat_key()
    blue_queue = taskiq_queue_name()

    monkeypatch.setenv("TASKIQ_DEPLOYMENT_ID", "green-release")
    green_key = taskiq_heartbeat_key()
    green_queue = taskiq_queue_name()

    assert blue_key.startswith("taskiq_pipeline_heartbeat:")
    assert green_key.startswith("taskiq_pipeline_heartbeat:")
    assert blue_key != green_key
    assert blue_queue.startswith("taskiq:")
    assert blue_queue != green_queue
    assert taskiq_deployment_id() not in {"blue-release", "green-release"}


def test_taskiq_stream_limit_must_be_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASKIQ_STREAM_MAX_ENTRIES", "250")
    assert taskiq_stream_max_entries() == 250

    monkeypatch.setenv("TASKIQ_STREAM_MAX_ENTRIES", "0")
    with pytest.raises(ValueError, match="greater than zero"):
        taskiq_stream_max_entries()


def test_taskiq_deployment_rejects_placeholder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TASKIQ_DEPLOYMENT_ID", "change_me")
    with pytest.raises(ValueError, match="non-placeholder"):
        taskiq_deployment_id()


class _FakeRedis:
    def __init__(
        self,
        *,
        heartbeat: str,
        stream_entries: int,
        legacy_pending: int = 0,
        legacy_lag: int = 0,
    ) -> None:
        self.heartbeat = heartbeat
        self.stream_entries = stream_entries
        self.legacy_pending = legacy_pending
        self.legacy_lag = legacy_lag

    async def get(self, _: str) -> str:
        return self.heartbeat

    async def xlen(self, _: str) -> int:
        return self.stream_entries

    async def xinfo_groups(self, _: str) -> list[dict[str, object]]:
        return [
            {
                "name": "taskiq",
                "pending": self.legacy_pending,
                "lag": self.legacy_lag,
            }
        ]

    async def aclose(self) -> None:
        return None


@pytest.mark.asyncio
async def test_taskiq_health_rejects_unbounded_stream_backlog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis(heartbeat="100", stream_entries=11)
    monkeypatch.setattr(healthcheck.time, "time", lambda: 101)
    monkeypatch.setenv("TASKIQ_DEPLOYMENT_ID", "blue")
    monkeypatch.setenv("TASKIQ_STREAM_MAX_ENTRIES", "10")
    monkeypatch.setattr(healthcheck, "redis_client", lambda: fake_redis)

    assert not await healthcheck.check_taskiq()


@pytest.mark.asyncio
async def test_taskiq_health_includes_legacy_pending_and_unread_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_redis = _FakeRedis(
        heartbeat="100",
        stream_entries=2,
        legacy_pending=4,
        legacy_lag=5,
    )
    monkeypatch.setattr(healthcheck.time, "time", lambda: 101)
    monkeypatch.setenv("TASKIQ_DEPLOYMENT_ID", "blue")
    monkeypatch.setenv("TASKIQ_STREAM_MAX_ENTRIES", "10")
    monkeypatch.setattr(healthcheck, "redis_client", lambda: fake_redis)

    assert not await healthcheck.check_taskiq()


def test_web_health_check_imports_only_the_standard_library() -> None:
    # Docker runs the check as a new process every interval; importing the app
    # config, Redis or taskiq here cost ~1 s of CPU per probe on small hosts.
    probe = (
        "import sys\n"
        "import src.infrastructure.healthcheck as healthcheck\n"
        "healthcheck.check_web\n"
        "heavy = sorted(name for name in sys.modules if name.split('.')[0] in "
        "{'redis', 'pydantic', 'pydantic_settings', 'taskiq', 'sqlalchemy', 'aiogram'} "
        "or name.startswith(('src.core', 'src.infrastructure.taskiq')))\n"
        "print(','.join(heavy))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""


@pytest.mark.parametrize("deployment", ["local", "blue-release", " spaced "])
def test_light_taskiq_names_match_the_application(
    monkeypatch: pytest.MonkeyPatch, deployment: str
) -> None:
    monkeypatch.setenv("TASKIQ_DEPLOYMENT_ID", deployment)
    monkeypatch.setenv("TASKIQ_STREAM_MAX_ENTRIES", "4321")
    assert healthcheck.taskiq_deployment_id() == health_state.taskiq_deployment_id()
    assert healthcheck.taskiq_heartbeat_key() == health_state.taskiq_heartbeat_key()
    assert healthcheck.taskiq_queue_name() == health_state.taskiq_queue_name()
    assert healthcheck.taskiq_stream_max_entries() == health_state.taskiq_stream_max_entries()
    assert (
        healthcheck.TASKIQ_HEARTBEAT_MAX_AGE_SECONDS
        == health_state.TASKIQ_HEARTBEAT_MAX_AGE_SECONDS
    )
    assert healthcheck.LEGACY_TASKIQ_QUEUE_NAME == health_state.LEGACY_TASKIQ_QUEUE_NAME
    assert healthcheck.TASKIQ_CONSUMER_GROUP_NAME == health_state.TASKIQ_CONSUMER_GROUP_NAME
    assert (
        healthcheck.DEFAULT_TASKIQ_STREAM_MAX_ENTRIES
        == health_state.DEFAULT_TASKIQ_STREAM_MAX_ENTRIES
    )


def test_light_taskiq_names_reject_what_the_application_rejects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TASKIQ_DEPLOYMENT_ID", "change_me")
    with pytest.raises(ValueError, match="non-placeholder"):
        healthcheck.taskiq_deployment_id()
    monkeypatch.setenv("TASKIQ_STREAM_MAX_ENTRIES", "0")
    with pytest.raises(ValueError, match="greater than zero"):
        healthcheck.taskiq_stream_max_entries()


@pytest.mark.parametrize(
    "env",
    [
        {},
        {
            "REDIS_HOST": "redis.internal",
            "REDIS_PORT": "6380",
            "REDIS_NAME": "3",
            "REDIS_PASSWORD": "f3a9c1d7e5b2a8c4d6e0f1b3a5c7d9e2",
        },
    ],
)
def test_light_redis_settings_match_redis_config(
    monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    for key in ("REDIS_HOST", "REDIS_PORT", "REDIS_NAME", "REDIS_PASSWORD"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    config = RedisConfig(_env_file=None)  # type: ignore[call-arg]
    settings = healthcheck.redis_connection_settings()
    assert settings["host"] == config.host
    assert settings["port"] == config.port
    assert str(settings["db"]) == config.name
    expected_password = config.password.get_secret_value() if config.password else None
    assert settings["password"] == expected_password


def test_taskiq_health_check_does_not_import_the_application() -> None:
    # Importing the app config or the taskiq package cost ~5 s of CPU per probe
    # on a 1-vCPU host: every probe timed out during startup and never passed.
    probe = (
        "import sys\n"
        "import src.infrastructure.healthcheck as healthcheck\n"
        "healthcheck.redis_client()\n"
        "healthcheck.taskiq_heartbeat_key()\n"
        "heavy = sorted(name for name in sys.modules if name.split('.')[0] in "
        "{'pydantic', 'pydantic_settings', 'taskiq', 'sqlalchemy', 'aiogram'} "
        "or name.startswith(('src.core', 'src.infrastructure.taskiq', "
        "'src.infrastructure.redis')))\n"
        "print(','.join(heavy))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == ""
