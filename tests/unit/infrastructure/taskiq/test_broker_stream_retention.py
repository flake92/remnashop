import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from src.infrastructure.taskiq import broker as broker_module
from src.infrastructure.taskiq.health_state import LEGACY_TASKIQ_QUEUE_NAME


class _FakeRedis:
    def __init__(self, state: SimpleNamespace, **_: Any) -> None:
        self.state = state

    async def __aenter__(self) -> "_FakeRedis":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    async def eval(self, script: str, *args: Any) -> int | list[int]:
        self.state.calls.append(("eval", script, *args))
        if script == broker_module.REFRESH_PENDING_LEASE_SCRIPT:
            return self.state.refresh_result
        return self.state.ack_result


@pytest.mark.asyncio
async def test_running_entry_renews_lease_then_acknowledges_and_deletes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = SimpleNamespace(calls=[], refresh_result=1, ack_result=[1, 1])
    monkeypatch.setattr(
        broker_module,
        "Redis",
        lambda **kwargs: _FakeRedis(state, **kwargs),
    )
    broker = broker_module.AckDeletingRedisStreamBroker(
        "redis://localhost:6379/0",
        pending_lease_refresh_seconds=0.001,
    )

    acknowledge = broker._ack_generator("1-0", "taskiq")
    await asyncio.sleep(0.1)
    await acknowledge()

    refresh_calls = [
        call
        for call in state.calls
        if call[:2] == ("eval", broker_module.REFRESH_PENDING_LEASE_SCRIPT)
    ]
    assert refresh_calls
    ack_calls = [
        call
        for call in state.calls
        if call[:2] == ("eval", broker_module.ACK_AND_DELETE_OWNED_ENTRY_SCRIPT)
    ]
    assert ack_calls
    assert ack_calls[-1][2:] == (
        1,
        "taskiq",
        broker.consumer_group_name,
        broker.consumer_name,
        "1-0",
    )
    assert not broker._pending_lease_tasks


@pytest.mark.asyncio
async def test_ack_never_deletes_an_entry_reclaimed_by_another_consumer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = SimpleNamespace(calls=[], refresh_result=-1, ack_result=[-1, 0])
    monkeypatch.setattr(
        broker_module,
        "Redis",
        lambda **kwargs: _FakeRedis(state, **kwargs),
    )
    broker = broker_module.AckDeletingRedisStreamBroker(
        "redis://localhost:6379/0",
        pending_lease_refresh_seconds=60,
    )

    acknowledge = broker._ack_generator("2-0", "taskiq")
    await acknowledge()

    assert state.calls[-1][1] == broker_module.ACK_AND_DELETE_OWNED_ENTRY_SCRIPT
    script = broker_module.ACK_AND_DELETE_OWNED_ENTRY_SCRIPT
    assert script.index("pending[1][2] ~= ARGV[2]") < script.index("'XACK'")
    assert script.index("'XACK'") < script.index("'XDEL'")
    assert not broker._pending_lease_tasks


def test_broker_drains_legacy_stream_without_skipping_preexisting_entries() -> None:
    config = SimpleNamespace(redis=SimpleNamespace(dsn="redis://localhost:6379/0"))

    broker = broker_module.create_broker(config)

    assert broker.consumer_id == "0"
    assert broker.additional_streams == {LEGACY_TASKIQ_QUEUE_NAME: ">"}
    assert broker.queue_name != LEGACY_TASKIQ_QUEUE_NAME
