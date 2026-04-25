from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

_ALLOWED_TARGET_MODES = {"construction", "demand", "both"}
_ALLOWED_USER_STATES = {"anonymous", "registered", "simulated_paid", "paid", "unknown"}


def normalize_input_host(input_host: str | None) -> str:
    host = (input_host or "").strip().lower().strip(".")
    if not host:
        return "none"
    if host == "maps.app.goo.gl":
        return "maps.app.goo.gl"
    if host == "goo.gl":
        return "goo.gl"
    if host in {"google.com", "www.google.com", "maps.google.com"}:
        return "google.com"
    if host == "maps.apple.com":
        return "maps.apple.com"
    if host in {"openstreetmap.org", "www.openstreetmap.org"}:
        return "openstreetmap.org"
    if host in {"waze.com", "www.waze.com"}:
        return "waze.com"
    return "other"


def normalize_target_mode(target_mode: str) -> str:
    candidate = (target_mode or "").strip().lower()
    if candidate in _ALLOWED_TARGET_MODES:
        return candidate
    return "unknown"


def normalize_user_state(user_state: str) -> str:
    candidate = (user_state or "").strip().lower()
    if candidate in _ALLOWED_USER_STATES:
        return candidate
    return "unknown"


async def increment_input_format_stats(
    db: AsyncSession,
    *,
    target_mode: str,
    input_format: str,
    input_parse_status: str,
    input_host: str | None,
    user_state: str,
) -> None:
    normalized_host = normalize_input_host(input_host)
    normalized_target_mode = normalize_target_mode(target_mode)
    normalized_user_state = normalize_user_state(user_state)

    await db.execute(
        text(
            """
            INSERT INTO input_format_stats_daily (
                stat_date,
                target_mode,
                input_format,
                input_parse_status,
                input_host,
                user_state,
                count,
                first_seen_at,
                last_seen_at
            )
            VALUES (
                CURRENT_DATE,
                :target_mode,
                :input_format,
                :input_parse_status,
                :input_host,
                :user_state,
                1,
                now(),
                now()
            )
            ON CONFLICT (
                stat_date,
                target_mode,
                input_format,
                input_parse_status,
                input_host,
                user_state
            )
            DO UPDATE SET
                count = input_format_stats_daily.count + 1,
                last_seen_at = now()
            """
        ),
        {
            "target_mode": normalized_target_mode,
            "input_format": input_format,
            "input_parse_status": input_parse_status,
            "input_host": normalized_host,
            "user_state": normalized_user_state,
        },
    )
