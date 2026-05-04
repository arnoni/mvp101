import os
import ssl
from typing import Any

import structlog
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

logger = structlog.get_logger(__name__)


def is_deployed_environment() -> bool:
    env = (os.getenv("ENV") or "").lower()
    vercel_env = (os.getenv("VERCEL_ENV") or "").lower()
    return env == "production" or vercel_env in {"production", "preview"}


def build_asyncpg_url_and_connect_args(database_url: str) -> tuple[str, dict[str, Any]]:
    url = make_url(database_url)
    async_url = url.set(drivername="postgresql+asyncpg")

    sslmode = async_url.query.get("sslmode")
    sslmode_require = isinstance(sslmode, str) and sslmode.lower() == "require"

    unsupported_libpq_params = {"sslmode", "channel_binding", "options"}
    clean_query = {
        key: value for key, value in async_url.query.items() if key.lower() not in unsupported_libpq_params
    }
    clean_async_url = async_url.set(query=clean_query)

    connect_args: dict[str, Any] = {}
    if is_deployed_environment() or sslmode_require:
        connect_args = {
            "ssl": ssl.create_default_context(),
            "statement_cache_size": 0,
            # Required for pgBouncer transaction pooling with asyncpg; disables
            # named prepared statements that can fail across pooled connections.
            "prepared_statement_cache_size": 0,
        }

    logger.debug(
        "database_connect_config",
        extra={
            "sanitized_db_url": clean_async_url.render_as_string(hide_password=True),
            "connect_args_keys": sorted(connect_args.keys()),
        },
    )

    return clean_async_url.render_as_string(hide_password=False), connect_args


def create_asyncpg_engine(database_url: str, **engine_kwargs: Any) -> AsyncEngine:
    """Create an AsyncEngine from DATABASE_URL using sanitized asyncpg URL + connect args."""
    clean_url, connect_args = build_asyncpg_url_and_connect_args(database_url)
    return create_async_engine(clean_url, connect_args=connect_args, **engine_kwargs)
