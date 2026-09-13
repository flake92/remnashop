from types import SimpleNamespace

import pytest

from src.infrastructure import healthcheck
from src.infrastructure.healthcheck import heartbeat_is_fresh
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
    monkeypatch.setattr(
        healthcheck.AppConfig,
        "get",
        lambda: SimpleNamespace(redis=SimpleNamespace(dsn="redis://unused")),
    )
    monkeypatch.setattr(
        healthcheck.Redis,
        "from_url",
        lambda *_args, **_kwargs: fake_redis,
    )

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
    monkeypatch.setattr(
        healthcheck.AppConfig,
        "get",
        lambda: SimpleNamespace(redis=SimpleNamespace(dsn="redis://unused")),
    )
    monkeypatch.setattr(
        healthcheck.Redis,
        "from_url",
        lambda *_args, **_kwargs: fake_redis,
    )

    assert not await healthcheck.check_taskiq()
