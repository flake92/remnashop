import hashlib
import os

from src.infrastructure.redis.key_builder import serialize_storage_key
from src.infrastructure.redis.keys import TaskiqPipelineHeartbeatKey

TASKIQ_HEARTBEAT_MAX_AGE_SECONDS = 150
TASKIQ_PIPELINE_HEARTBEAT_TTL_SECONDS = 180
TASKIQ_QUEUE_PREFIX = "taskiq"
LEGACY_TASKIQ_QUEUE_NAME = TASKIQ_QUEUE_PREFIX
TASKIQ_CONSUMER_GROUP_NAME = "taskiq"
DEFAULT_TASKIQ_STREAM_MAX_ENTRIES = 100_000


def taskiq_deployment_id() -> str:
    """Return a Redis-key-safe identity for exactly one deployed stack."""
    configured = os.environ.get("TASKIQ_DEPLOYMENT_ID", "local").strip()
    if not configured or configured.casefold() in {"change_me", "replace_me"}:
        raise ValueError("TASKIQ_DEPLOYMENT_ID must be a non-placeholder value")
    return hashlib.sha256(configured.encode("utf-8")).hexdigest()[:24]


def taskiq_heartbeat_key() -> str:
    return serialize_storage_key(TaskiqPipelineHeartbeatKey(deployment_id=taskiq_deployment_id()))


def taskiq_queue_name() -> str:
    return f"{TASKIQ_QUEUE_PREFIX}:{taskiq_deployment_id()}"


def taskiq_stream_max_entries() -> int:
    raw_value = os.environ.get(
        "TASKIQ_STREAM_MAX_ENTRIES",
        str(DEFAULT_TASKIQ_STREAM_MAX_ENTRIES),
    )
    value = int(raw_value)
    if value < 1:
        raise ValueError("TASKIQ_STREAM_MAX_ENTRIES must be greater than zero")
    return value
