"""Centralized Redis-backed abuse rate limiting."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import inspect
import time
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

import structlog
from fastapi import Request
from structlog.contextvars import bound_contextvars

from app.core.config import settings
from app.core.keys import KeyBuilder

logger = structlog.get_logger(__name__)

DEVELOPMENT_RATE_LIMIT_HMAC_SECRET = "dev_insecure_rate_limit_secret"

ATOMIC_FIXED_WINDOW_LUA_SCRIPT = """
local current = redis.call('INCR', KEYS[1])
if current == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[2])
  ttl = tonumber(ARGV[2])
end
local allowed = 0
if current <= tonumber(ARGV[1]) then
  allowed = 1
end
return {current, allowed, ttl}
"""


@dataclass(frozen=True)
class RateLimitIdentity:
    dimension: str
    identity_kind: str
    value: str


@dataclass(frozen=True)
class RateLimitResult:
    route: str
    dimension: str
    identity_kind: str
    identity_hash: str
    limit: int
    window_seconds: int
    current_count: int
    allowed: bool
    retry_after_seconds: int


class RateLimitBackendError(RuntimeError):
    def __init__(self, *, dimension: str, error: Exception):
        super().__init__(str(error))
        self.dimension = dimension
        self.error = error


def normalize_email(email: str) -> str:
    return email.strip().lower()


def _hmac_secret() -> str:
    configured = (settings.RATE_LIMIT_HMAC_SECRET or "").strip()
    if configured:
        return configured
    return DEVELOPMENT_RATE_LIMIT_HMAC_SECRET


def hash_rate_limit_identity(identity_kind: str, value: str) -> str:
    separated = f"rate-limit:{identity_kind}:{value}".encode()
    return hmac.new(_hmac_secret().encode(), separated, hashlib.sha256).hexdigest()[:32]


def _window_values(window_seconds: int, now: float | None) -> tuple[int, int]:
    current_time = int(time.time() if now is None else now)
    bucket = current_time // window_seconds
    ttl = window_seconds - (current_time % window_seconds)
    return bucket, max(1, ttl)


async def _call_eval(redis: Any, key: str, *, limit: int, ttl: int) -> Any:
    eval_command = redis.eval
    try:
        parameter_names = tuple(inspect.signature(eval_command).parameters)
    except (TypeError, ValueError):
        parameter_names = ()
    if len(parameter_names) >= 2 and parameter_names[1] in {"keys", "key"}:
        return await eval_command(ATOMIC_FIXED_WINDOW_LUA_SCRIPT, [key], [limit, ttl])
    try:
        return await eval_command(ATOMIC_FIXED_WINDOW_LUA_SCRIPT, [key], [limit, ttl])
    except TypeError:
        return await eval_command(ATOMIC_FIXED_WINDOW_LUA_SCRIPT, 1, key, limit, ttl)


async def _check_one(
    redis: Any,
    *,
    route: str,
    identity: RateLimitIdentity,
    limit: int,
    window_seconds: int,
    now: float | None,
) -> RateLimitResult:
    identity_hash = hash_rate_limit_identity(identity.identity_kind, identity.value)
    bucket, ttl = _window_values(window_seconds, now)
    key = KeyBuilder.abuse_rate_limit(
        route=route,
        dimension=identity.dimension,
        identity_hash=identity_hash,
        window_bucket=bucket,
    )
    raw = await _call_eval(redis, key, limit=limit, ttl=ttl)
    current_count, allowed, retry_after = (int(raw[0]), bool(int(raw[1])), int(raw[2]))
    return RateLimitResult(
        route=route,
        dimension=identity.dimension,
        identity_kind=identity.identity_kind,
        identity_hash=identity_hash,
        limit=limit,
        window_seconds=window_seconds,
        current_count=current_count,
        allowed=allowed,
        retry_after_seconds=max(1, retry_after),
    )


async def check_rate_limits(
    redis: Any,
    *,
    route: str,
    identities: Iterable[RateLimitIdentity],
    limit: int,
    window_seconds: int,
    now: float | None = None,
) -> list[RateLimitResult]:
    """Increment every applicable dimension before evaluating any denial."""
    identity_list = list(identities)
    if not identity_list:
        return []
    if redis is None:
        raise RateLimitBackendError(
            dimension="backend",
            error=RuntimeError("Redis rate-limit backend unavailable"),
        )

    outcomes = await asyncio.gather(
        *(
            _check_one(
                redis,
                route=route,
                identity=identity,
                limit=limit,
                window_seconds=window_seconds,
                now=now,
            )
            for identity in identity_list
        ),
        return_exceptions=True,
    )
    failures = [
        (identity_list[index], outcome)
        for index, outcome in enumerate(outcomes)
        if isinstance(outcome, Exception)
    ]
    if failures:
        identity, error = failures[0]
        raise RateLimitBackendError(dimension=identity.dimension, error=error)
    return [outcome for outcome in outcomes if isinstance(outcome, RateLimitResult)]


def denied_result(results: Iterable[RateLimitResult]) -> RateLimitResult | None:
    denied = [result for result in results if not result.allowed]
    if not denied:
        return None
    return max(denied, key=lambda result: result.retry_after_seconds)


def log_rate_limit_exceeded(request: Request, result: RateLimitResult) -> None:
    with bound_contextvars(client_ip=None):
        logger.warning(
            "rate_limit_exceeded",
            route=result.route,
            method=request.method,
            dimension=result.dimension,
            identity_kind=result.identity_kind,
            identity_hash=result.identity_hash,
            limit=result.limit,
            window_seconds=result.window_seconds,
            current_count=result.current_count,
            retry_after_seconds=result.retry_after_seconds,
            tier=str(getattr(request.state, "tier", "unknown")),
            client_ip_present=bool(
                getattr(request.state, "rate_limit_client_ip_present", False)
            ),
            request_id=getattr(request.state, "request_id", None),
            vercel_id=request.headers.get("x-vercel-id"),
        )


def log_rate_limit_backend_error(
    request: Request,
    *,
    route: str,
    dimension: str,
    failure_policy: str,
    error: Exception,
) -> None:
    with bound_contextvars(client_ip=None):
        logger.error(
            "rate_limit_backend_error",
            route=route,
            dimension=dimension,
            redis_op="eval_fixed_window",
            failure_policy=failure_policy,
            error_class=error.__class__.__name__,
            client_ip_present=bool(
                getattr(request.state, "rate_limit_client_ip_present", False)
            ),
            request_id=getattr(request.state, "request_id", None),
            vercel_id=request.headers.get("x-vercel-id"),
        )
