import hashlib
from typing import Any, Awaitable, Optional, cast

from redis.asyncio import Redis

from src.application.common.dao.auth import RefreshTokenRecord
from src.infrastructure.redis.key_builder import serialize_storage_key
from src.infrastructure.redis.keys import (
    EmailAuthAttemptsKey,
    EmailAuthChallengeKey,
    EmailAuthRequestKey,
    PasswordResetAttemptsKey,
    PasswordResetLockKey,
    PasswordResetRequestKey,
    RefreshTokenKey,
    UserTokensKey,
)

INCREMENT_WITH_TTL_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return current
"""

RELEASE_LOCK_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

CONSUME_VALUE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

STORE_REFRESH_TOKEN_SCRIPT = """
redis.call('SETEX', KEYS[1], ARGV[1], ARGV[2])
redis.call('SADD', KEYS[2], ARGV[3])
local current_ttl = redis.call('TTL', KEYS[2])
if current_ttl < tonumber(ARGV[1]) then
  redis.call('EXPIRE', KEYS[2], ARGV[1])
end
return 1
"""

CONSUME_REFRESH_TOKEN_SCRIPT = """
local value = redis.call('GET', KEYS[1])
if not value then
  return false
end
redis.call('DEL', KEYS[1])
local user_id = string.match(value, '^v1:(%d+):%d+$')
if not user_id then
  user_id = string.match(value, '^(%d+)$')
end
if user_id then
  redis.call('SREM', ARGV[1] .. user_id, ARGV[2])
end
return value
"""

REVOKE_ALL_REFRESH_TOKENS_SCRIPT = """
local members = redis.call('SMEMBERS', KEYS[1])
for _, member in ipairs(members) do
  redis.call('DEL', ARGV[1] .. member)
end
redis.call('DEL', KEYS[1])
return #members
"""

REFRESH_TOKEN_VALUE_VERSION = "v1"


def _refresh_token_digest(token: str) -> str:
    payload = f"remnashop-refresh-token:v1:{token}".encode()
    return hashlib.sha256(payload).hexdigest()


def _decode_redis_text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _serialize_refresh_token_record(user_id: int, token_version: int) -> str:
    return f"{REFRESH_TOKEN_VALUE_VERSION}:{user_id}:{token_version}"


def _parse_refresh_token_record(value: Any) -> Optional[RefreshTokenRecord]:
    parts = _decode_redis_text(value).split(":")
    if len(parts) != 3 or parts[0] != REFRESH_TOKEN_VALUE_VERSION:
        return None
    try:
        return RefreshTokenRecord(user_id=int(parts[1]), token_version=int(parts[2]))
    except ValueError:
        return None


class RedisAuthRepository:
    def __init__(self, redis: Redis) -> None:
        self.redis = redis

    async def store_refresh_token(
        self, token: str, user_id: int, token_version: int, ttl: int
    ) -> None:
        token_digest = _refresh_token_digest(token)
        token_key = serialize_storage_key(RefreshTokenKey(token=token_digest))
        user_set_key = serialize_storage_key(UserTokensKey(user_id=user_id))
        await cast(
            Awaitable[Any],
            self.redis.eval(
                STORE_REFRESH_TOKEN_SCRIPT,
                2,
                token_key,
                user_set_key,
                ttl,
                _serialize_refresh_token_record(user_id, token_version),
                token_digest,
            ),
        )

    async def get_refresh_token(self, token: str) -> Optional[RefreshTokenRecord]:
        key = serialize_storage_key(RefreshTokenKey(token=_refresh_token_digest(token)))
        value = await self.redis.get(key)
        if value is None:
            return None
        return _parse_refresh_token_record(value)

    async def revoke_refresh_token(self, token: str) -> None:
        token_digest = _refresh_token_digest(token)
        token_key = serialize_storage_key(RefreshTokenKey(token=token_digest))
        value = await cast(
            Awaitable[Any],
            self.redis.eval(
                CONSUME_REFRESH_TOKEN_SCRIPT,
                1,
                token_key,
                "user_tokens:",
                token_digest,
            ),
        )
        if value is None:
            # Tokens created before digest storage are invalidated during the
            # security upgrade and removed without accepting them for login.
            legacy_key = serialize_storage_key(RefreshTokenKey(token=token))
            await cast(
                Awaitable[Any],
                self.redis.eval(
                    CONSUME_REFRESH_TOKEN_SCRIPT,
                    1,
                    legacy_key,
                    "user_tokens:",
                    token,
                ),
            )

    async def get_and_revoke_refresh_token(self, token: str) -> Optional[RefreshTokenRecord]:
        token_digest = _refresh_token_digest(token)
        token_key = serialize_storage_key(RefreshTokenKey(token=token_digest))
        value = await cast(
            Awaitable[Any],
            self.redis.eval(
                CONSUME_REFRESH_TOKEN_SCRIPT,
                1,
                token_key,
                "user_tokens:",
                token_digest,
            ),
        )
        if value is None:
            # Fail closed for legacy records, which lack the token_version
            # required to prove that the session has not been invalidated.
            legacy_key = serialize_storage_key(RefreshTokenKey(token=token))
            await cast(
                Awaitable[Any],
                self.redis.eval(
                    CONSUME_REFRESH_TOKEN_SCRIPT,
                    1,
                    legacy_key,
                    "user_tokens:",
                    token,
                ),
            )
            return None
        record = _parse_refresh_token_record(value)
        if record is None:
            return None
        return record

    async def revoke_all_user_tokens(self, user_id: int) -> None:
        user_set_key = serialize_storage_key(UserTokensKey(user_id=user_id))
        await cast(
            Awaitable[Any],
            self.redis.eval(
                REVOKE_ALL_REFRESH_TOKENS_SCRIPT,
                1,
                user_set_key,
                "refresh:",
            ),
        )

    async def reserve_password_reset_request(self, identity_hash: str, ttl: int) -> bool:
        key = serialize_storage_key(PasswordResetRequestKey(identity_hash=identity_hash))
        return bool(await self.redis.set(key, "1", ex=ttl, nx=True))

    async def increment_password_reset_attempts(self, identity_hash: str, ttl: int) -> int:
        key = serialize_storage_key(PasswordResetAttemptsKey(identity_hash=identity_hash))
        value = await cast(Awaitable[Any], self.redis.eval(INCREMENT_WITH_TTL_SCRIPT, 1, key, ttl))
        return int(value)

    async def clear_password_reset_attempts(self, identity_hash: str) -> None:
        key = serialize_storage_key(PasswordResetAttemptsKey(identity_hash=identity_hash))
        await self.redis.delete(key)

    async def acquire_password_reset_lock(self, identity_hash: str, token: str, ttl: int) -> bool:
        key = serialize_storage_key(PasswordResetLockKey(identity_hash=identity_hash))
        return bool(await self.redis.set(key, token, ex=ttl, nx=True))

    async def release_password_reset_lock(self, identity_hash: str, token: str) -> None:
        key = serialize_storage_key(PasswordResetLockKey(identity_hash=identity_hash))
        await cast(Awaitable[Any], self.redis.eval(RELEASE_LOCK_SCRIPT, 1, key, token))

    async def reserve_email_auth_request(self, identity_hash: str, ttl: int) -> bool:
        key = serialize_storage_key(EmailAuthRequestKey(identity_hash=identity_hash))
        return bool(await self.redis.set(key, "1", ex=ttl, nx=True))

    async def store_email_auth_challenge(
        self, identity_hash: str, code_hash: str, ttl: int
    ) -> None:
        key = serialize_storage_key(EmailAuthChallengeKey(identity_hash=identity_hash))
        await self.redis.setex(key, ttl, code_hash)

    async def consume_email_auth_challenge(self, identity_hash: str, code_hash: str) -> bool:
        key = serialize_storage_key(EmailAuthChallengeKey(identity_hash=identity_hash))
        value = await cast(Awaitable[Any], self.redis.eval(CONSUME_VALUE_SCRIPT, 1, key, code_hash))
        return bool(value)

    async def increment_email_auth_attempts(self, identity_hash: str, ttl: int) -> int:
        key = serialize_storage_key(EmailAuthAttemptsKey(identity_hash=identity_hash))
        value = await cast(Awaitable[Any], self.redis.eval(INCREMENT_WITH_TTL_SCRIPT, 1, key, ttl))
        return int(value)

    async def clear_email_auth_attempts(self, identity_hash: str) -> None:
        key = serialize_storage_key(EmailAuthAttemptsKey(identity_hash=identity_hash))
        await self.redis.delete(key)
