import asyncio
from typing import Any, Optional

import structlog

logger = structlog.get_logger(__name__)

QUOTA_EXCEEDED_SENTINEL = -1
QUOTA_IDEMPOTENT_REPLAY_SENTINEL = 0
QUOTA_CONSUMED_SENTINEL = 1
DEFAULT_QUOTA_TTL_SECONDS = 60 * 60 * 24
DEFAULT_IDEMPOTENCY_TTL_SECONDS = 60 * 5
_quota_sha: str | None = None
_quota_script_lock = asyncio.Lock()

ATOMIC_QUOTA_LUA_SCRIPT = """
-- Atomically check and consume a quota unit.
-- KEYS[1] = quota counter key
-- KEYS[2] = optional idempotency key; empty string disables idempotency
-- ARGV[1] = quota limit
-- ARGV[2] = quota counter TTL in seconds
-- ARGV[3] = idempotency key TTL in seconds
--
-- Return tuple:
--   {-1, current_usage, 0}         quota exceeded; no increment performed
--   {0, current_usage, remaining}  idempotent replay; no increment performed
--   {1, new_usage, remaining}      quota consumed
local quota_key = KEYS[1]
local idempotency_key = KEYS[2]
local limit = tonumber(ARGV[1])
local quota_ttl = tonumber(ARGV[2])
local idempotency_ttl = tonumber(ARGV[3])

if not limit or limit < 1 then
  return redis.error_reply('quota limit must be a positive integer')
end
if not quota_ttl or quota_ttl < 1 then
  return redis.error_reply('quota ttl must be a positive integer')
end
if not idempotency_ttl or idempotency_ttl < 1 then
  return redis.error_reply('idempotency ttl must be a positive integer')
end

local current_raw = redis.call('GET', quota_key)
local current = 0
if current_raw then
  current = tonumber(current_raw) or 0
end

if idempotency_key and idempotency_key ~= '' then
  local replay = redis.call('GET', idempotency_key)
  if replay then
    local remaining = limit - current
    if remaining < 0 then remaining = 0 end
    return {0, current, remaining}
  end
end

if current >= limit then
  return {-1, current, 0}
end

local new_usage = redis.call('INCR', quota_key)
local existing_ttl = redis.call('TTL', quota_key)
-- Preserve rolling/custom expirations: only attach the default expiration if the
-- key has no expiration. TTL -1 means the key exists without expiration.
if existing_ttl == -1 then
  redis.call('EXPIRE', quota_key, quota_ttl)
end

if idempotency_key and idempotency_key ~= '' then
  redis.call('SET', idempotency_key, '1', 'EX', idempotency_ttl)
end

local remaining = limit - new_usage
if remaining < 0 then remaining = 0 end
return {1, new_usage, remaining}
"""


class QuotaRepository:
    """
    Redis-backed quota repository with fail-closed behavior.

    Quota consumption uses one Redis Lua script so the check, increment, TTL
    attachment, and optional idempotency marker are applied atomically on the
    Redis server. The repository prefers SCRIPT LOAD + EVALSHA after startup and
    falls back to EVAL when a client does not expose script-loading helpers.
    """

    def __init__(self, redis_client: Optional[Any] = None, *, quota_script_sha: Optional[str] = None):
        self.redis_client: Optional[Any] = redis_client
        if quota_script_sha:
            self.quota_script_sha = quota_script_sha

    @property
    def quota_script_sha(self) -> str | None:
        return _quota_sha

    @quota_script_sha.setter
    def quota_script_sha(self, value: str | None) -> None:
        global _quota_sha
        _quota_sha = str(value) if value else None

    async def load_lua_scripts(self) -> None:
        """Load and cache Lua script SHAs when explicitly requested."""
        if not self.redis_client:
            raise RuntimeError("QUOTA_REDIS_UNAVAILABLE")

        script_load = getattr(self.redis_client, "script_load", None)
        if not script_load:
            logger.info("quota_lua_script_load_skipped", reason="client_does_not_expose_script_load")
            return

        try:
            async with _quota_script_lock:
                sha = await script_load(ATOMIC_QUOTA_LUA_SCRIPT)
                self.quota_script_sha = str(sha)
            logger.info("quota_lua_script_loaded", script_sha=self.quota_script_sha)
        except Exception as exc:
            logger.error("quota_lua_script_load_failed", error=str(exc))
            raise RuntimeError("QUOTA_REDIS_UNAVAILABLE") from exc

    async def get_usage(self, key: str, redis_op: Optional[str] = None) -> int:
        if not self.redis_client:
            raise RuntimeError("QUOTA_REDIS_UNAVAILABLE")
        try:
            val = await self.redis_client.get(key)
            if val is None:
                return 0
            try:
                return int(val)
            except Exception:
                logger.error("QUOTA_PARSE_ERROR", key=key, raw_value=str(val), redis_op=redis_op)
                return 0
        except Exception as exc:
            logger.error("QUOTA_GET_USAGE_ERROR", error=str(exc), key=key, redis_op=redis_op)
            raise RuntimeError("QUOTA_REDIS_UNAVAILABLE") from exc

    async def increment(self, key: str, ttl: int = DEFAULT_QUOTA_TTL_SECONDS) -> int:
        if not self.redis_client:
            raise RuntimeError("QUOTA_REDIS_UNAVAILABLE")
        try:
            val = await self.redis_client.incr(key)
            ttl_value = await self._ttl(key)
            if ttl_value == -1:
                await self.redis_client.expire(key, ttl)
            usage = int(val) if val is not None else 1
            logger.info("quota_incremented", quota_key=key, quota_used_after=usage)
            return usage
        except Exception as exc:
            logger.error("QUOTA_INCREMENT_ERROR", error=str(exc), key=key)
            raise RuntimeError("QUOTA_REDIS_UNAVAILABLE") from exc

    async def check_available(self, key: str, max_limit: int, redis_op: Optional[str] = None) -> bool:
        usage = await self.get_usage(key, redis_op=redis_op)
        available = usage < max_limit
        if not available:
            logger.info("quota_exceeded", quota_key=key, quota_limit=max_limit, quota_used=usage, redis_op=redis_op)
        return available

    async def check_and_consume(
        self,
        key: str,
        daily_limit: int,
        ttl: int = DEFAULT_QUOTA_TTL_SECONDS,
        idempotency_key: Optional[str] = None,
        redis_op: Optional[str] = None,
    ) -> tuple[bool, int]:
        """
        Atomically check and consume one unit, returning (allowed, remaining).

        Existing callers can keep using the original three arguments. New callers
        may pass idempotency_key to make retries within five minutes no-op.
        """
        result_code, usage, remaining = await self.consume_quota(
            key=key,
            limit=daily_limit,
            ttl=ttl,
            idempotency_key=idempotency_key,
            redis_op=redis_op,
        )
        allowed = result_code in {QUOTA_CONSUMED_SENTINEL, QUOTA_IDEMPOTENT_REPLAY_SENTINEL}
        return allowed, remaining

    async def consume_quota(
        self,
        key: str,
        limit: int,
        ttl: int = DEFAULT_QUOTA_TTL_SECONDS,
        idempotency_key: Optional[str] = None,
        idempotency_ttl: int = DEFAULT_IDEMPOTENCY_TTL_SECONDS,
        redis_op: Optional[str] = None,
    ) -> tuple[int, int, int]:
        """
        Atomically consume one quota unit.

        Returns (result_code, usage, remaining), where result_code is -1 when
        quota is exceeded, 0 for an idempotent replay, and 1 for a fresh consume.
        """
        if not self.redis_client:
            raise RuntimeError("QUOTA_REDIS_UNAVAILABLE")

        safe_limit = max(1, int(limit))
        safe_ttl = max(1, int(ttl))
        safe_idempotency_ttl = max(1, int(idempotency_ttl))
        keys = [key, idempotency_key or ""]
        args = [safe_limit, safe_ttl, safe_idempotency_ttl]

        try:
            result = await self._eval_quota_script(keys=keys, args=args)
            result_code, usage, remaining = self._parse_lua_result(result)
            if result_code == QUOTA_EXCEEDED_SENTINEL:
                logger.info("quota_exceeded", quota_key=key, quota_limit=safe_limit, quota_used=usage, redis_op=redis_op)
            elif result_code == QUOTA_IDEMPOTENT_REPLAY_SENTINEL:
                logger.info(
                    "quota_idempotent_replay",
                    quota_key=key,
                    quota_limit=safe_limit,
                    quota_used=usage,
                    quota_remaining=remaining,
                    idempotency_key=bool(idempotency_key),
                    redis_op=redis_op,
                )
            else:
                logger.info(
                    "quota_consumed",
                    quota_key=key,
                    quota_limit=safe_limit,
                    quota_used=usage,
                    quota_remaining=remaining,
                    ttl=safe_ttl,
                    idempotency_key=bool(idempotency_key),
                    redis_op=redis_op,
                )
            return result_code, usage, remaining
        except RuntimeError:
            raise
        except Exception as exc:
            logger.error("quota_redis_failure", error=str(exc), quota_key=key, redis_op=redis_op)
            raise RuntimeError("QUOTA_REDIS_UNAVAILABLE") from exc

    async def _eval_quota_script(self, *, keys: list[str], args: list[int]) -> Any:
        if not self.redis_client:
            raise RuntimeError("QUOTA_REDIS_UNAVAILABLE")

        evalsha = getattr(self.redis_client, "evalsha", None)
        script_sha = self.quota_script_sha
        if script_sha and evalsha:
            try:
                return await self._call_evalsha(evalsha, script_sha, keys, args)
            except Exception as exc:
                if "NOSCRIPT" not in str(exc).upper():
                    raise
                logger.warning("quota_lua_evalsha_noscript")
                return await self._load_and_evalsha_quota_script(
                    keys=keys,
                    args=args,
                    stale_sha=script_sha,
                )

        script_load = getattr(self.redis_client, "script_load", None)
        if evalsha and script_load:
            return await self._load_and_evalsha_quota_script(keys=keys, args=args)

        eval_command = getattr(self.redis_client, "eval", None)
        if eval_command:
            return await self._call_eval(eval_command, ATOMIC_QUOTA_LUA_SCRIPT, keys, args)

        # Test-only compatibility for lightweight fakes that model Redis commands
        # but do not implement EVAL. Production Upstash/Redis clients support EVAL.
        return await self._command_fallback_for_non_eval_fake(keys=keys, args=args)

    async def _load_and_evalsha_quota_script(
        self,
        *,
        keys: list[str],
        args: list[int],
        stale_sha: str | None = None,
    ) -> Any:
        script_load = getattr(self.redis_client, "script_load", None)
        evalsha = getattr(self.redis_client, "evalsha", None)
        if not script_load or not evalsha:
            raise RuntimeError("QUOTA_REDIS_UNAVAILABLE")

        async with _quota_script_lock:
            script_sha = self.quota_script_sha
            if not script_sha or script_sha == stale_sha:
                try:
                    script_sha = str(await script_load(ATOMIC_QUOTA_LUA_SCRIPT))
                except Exception as exc:
                    logger.error("quota_lua_script_load_failed", error=str(exc))
                    raise
                self.quota_script_sha = script_sha
                logger.warning("quota_lua_script_loaded_lazy")

            return await self._call_evalsha(evalsha, script_sha, keys, args)

    @staticmethod
    async def _call_eval(eval_command: Any, script: str, keys: list[str], args: list[int]) -> Any:
        try:
            # Upstash REST client shape: eval(script, keys, args)
            return await eval_command(script, keys, args)
        except TypeError:
            # redis.asyncio shape: eval(script, numkeys, *keys_and_args)
            return await eval_command(script, len(keys), *(keys + [str(arg) for arg in args]))

    @staticmethod
    async def _call_evalsha(evalsha_command: Any, sha: str, keys: list[str], args: list[int]) -> Any:
        try:
            # Upstash REST client shape: evalsha(sha, keys, args)
            return await evalsha_command(sha, keys, args)
        except TypeError:
            # redis.asyncio shape: evalsha(sha, numkeys, *keys_and_args)
            return await evalsha_command(sha, len(keys), *(keys + [str(arg) for arg in args]))

    @staticmethod
    def _parse_lua_result(result: Any) -> tuple[int, int, int]:
        if not isinstance(result, (list, tuple)) or len(result) < 3:
            logger.error("QUOTA_LUA_INVALID_RESULT", result=result)
            raise RuntimeError("QUOTA_REDIS_UNAVAILABLE")
        return int(result[0]), int(result[1]), int(result[2])

    async def _ttl(self, key: str) -> int:
        ttl = getattr(self.redis_client, "ttl", None)
        if not ttl:
            return -1
        value = await ttl(key)
        return int(value)

    async def _command_fallback_for_non_eval_fake(self, *, keys: list[str], args: list[int]) -> list[int]:
        quota_key, idempotency_key = keys
        limit, quota_ttl, idempotency_ttl = args
        if idempotency_key and await self.redis_client.get(idempotency_key):
            current = int(await self.redis_client.get(quota_key) or 0)
            return [QUOTA_IDEMPOTENT_REPLAY_SENTINEL, current, max(0, limit - current)]
        current = int(await self.redis_client.get(quota_key) or 0)
        if current >= limit:
            return [QUOTA_EXCEEDED_SENTINEL, current, 0]
        new_usage = int(await self.redis_client.incr(quota_key))
        ttl_value = await self._ttl(quota_key)
        if ttl_value == -1:
            await self.redis_client.expire(quota_key, quota_ttl)
        if idempotency_key:
            await self.redis_client.set(idempotency_key, "1", ex=idempotency_ttl)
        return [QUOTA_CONSUMED_SENTINEL, new_usage, max(0, limit - new_usage)]
