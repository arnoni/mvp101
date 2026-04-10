#!/usr/bin/env python3
"""Reset one test user across Neon Postgres + Upstash Redis with an auditable report.

Usage:
  python scripts/reset_test_user.py --email dilldrillteam@gmail.com --apply
  python scripts/reset_test_user.py --email dilldrillteam@gmail.com --dry-run

Environment variables:
  DATABASE_URL                 (required for Postgres cleanup)
  UPSTASH_REDIS_REST_URL       (optional but recommended)
  UPSTASH_REDIS_REST_TOKEN     (optional but recommended)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

try:
    import psycopg
except Exception:  # pragma: no cover - dependency availability is environment-specific
    psycopg = None


COOKIE_NAMES = ["dd_session", "dd_anon_id", "dd_lang"]


@dataclass
class SqlReport:
    user_ids: list[str] = field(default_factory=list)
    deleted_rows: dict[str, int] = field(default_factory=dict)
    matched_rows: dict[str, int] = field(default_factory=dict)


@dataclass
class RedisReport:
    matched_keys: list[str] = field(default_factory=list)
    deleted_keys: list[str] = field(default_factory=list)


class UpstashRestClient:
    def __init__(self, url: str, token: str):
        self.url = url.rstrip("/")
        self.token = token

    def cmd(self, *parts: str) -> Any:
        payload = json.dumps(list(parts)).encode("utf-8")
        req = urllib.request.Request(
            f"{self.url}/",
            data=payload,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                data = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:  # pragma: no cover
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Upstash HTTP {exc.code}: {body}") from exc

        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(f"Upstash error: {data['error']}")
        return data.get("result") if isinstance(data, dict) else data


SQL_COUNT_QUERIES = {
    "users": "SELECT count(*) FROM users WHERE lower(email) = lower(%s)",
    "magic_link_tokens": "SELECT count(*) FROM magic_link_tokens WHERE lower(email) = lower(%s) OR user_id::text = ANY(%s::text[])",
    "payment_intents": "SELECT count(*) FROM payment_intents WHERE user_id::text = ANY(%s::text[])",
    "user_passes": "SELECT count(*) FROM user_passes WHERE user_id::text = ANY(%s::text[])",
    "user_location_queries": "SELECT count(*) FROM user_location_queries WHERE user_id::text = ANY(%s::text[])",
    "webhook_events_email_hint": "SELECT count(*) FROM webhook_events WHERE payload::text ILIKE %s",
}


def fetch_user_ids(conn: Any, email: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT id::text FROM users WHERE lower(email) = lower(%s)", (email,))
        return [row[0] for row in cur.fetchall()]


def sql_count_snapshot(conn: Any, email: str, user_ids: list[str]) -> dict[str, int]:
    like_email = f"%{email}%"
    user_ids_param = user_ids if user_ids else []
    counts: dict[str, int] = {}
    with conn.cursor() as cur:
        for label, q in SQL_COUNT_QUERIES.items():
            if label == "users":
                params = (email,)
            elif label == "webhook_events_email_hint":
                params = (like_email,)
            elif label == "magic_link_tokens":
                params = (email, user_ids_param)
            else:
                params = (user_ids_param,)
            cur.execute(q, params)
            counts[label] = int(cur.fetchone()[0])
    return counts


def sql_delete(conn: Any, email: str, user_ids: list[str]) -> dict[str, int]:
    user_ids_param = user_ids if user_ids else []
    deleted: dict[str, int] = {}
    with conn.cursor() as cur:
        cur.execute("DELETE FROM magic_link_tokens WHERE lower(email)=lower(%s) OR user_id::text = ANY(%s::text[])", (email, user_ids_param))
        deleted["magic_link_tokens"] = cur.rowcount

        cur.execute("DELETE FROM user_location_queries WHERE user_id::text = ANY(%s::text[])", (user_ids_param,))
        deleted["user_location_queries"] = cur.rowcount

        cur.execute("DELETE FROM user_passes WHERE user_id::text = ANY(%s::text[])", (user_ids_param,))
        deleted["user_passes"] = cur.rowcount

        cur.execute("DELETE FROM payment_intents WHERE user_id::text = ANY(%s::text[])", (user_ids_param,))
        deleted["payment_intents"] = cur.rowcount

        cur.execute("DELETE FROM users WHERE lower(email)=lower(%s)", (email,))
        deleted["users"] = cur.rowcount

    conn.commit()
    return deleted


def redis_collect_keys(redis: UpstashRestClient, email: str, user_ids: list[str]) -> list[str]:
    keys: set[str] = set()

    explicit = [
        f"magic_resend:cooldown:{email}",
        *[f"dd:policy:entitlement:status:{uid}" for uid in user_ids],
    ]
    for k in explicit:
        if redis.cmd("EXISTS", k):
            keys.add(k)

    for pattern in ["dd:session:*", "magic:*", f"magic_resend:count:{email}:*", "session:*"]:
        found = redis.cmd("KEYS", pattern) or []
        for k in found:
            ks = str(k)
            if pattern in {f"magic_resend:count:{email}:*"}:
                keys.add(ks)
                continue
            value = redis.cmd("GET", ks)
            value_s = "" if value is None else str(value)
            if email.lower() in value_s.lower() or any(uid in value_s for uid in user_ids):
                keys.add(ks)

    return sorted(keys)


def redis_delete(redis: UpstashRestClient, keys: list[str]) -> list[str]:
    deleted: list[str] = []
    for key in keys:
        removed = redis.cmd("DEL", key)
        if int(removed or 0) > 0:
            deleted.append(key)
    return deleted


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Reset one test user in Postgres + Redis.")
    p.add_argument("--email", required=True)
    p.add_argument("--apply", action="store_true", help="actually delete data")
    p.add_argument("--dry-run", action="store_true", help="preview only")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    dry_run = args.dry_run or not args.apply

    report: dict[str, Any] = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "email": args.email.lower(),
        "dry_run": dry_run,
        "cookies_to_clear_in_browser": COOKIE_NAMES,
        "postgres": SqlReport().__dict__,
        "redis": RedisReport().__dict__,
        "notes": [
            "Cookies are client-side; script removes server-side session/token state so old cookies become invalid.",
            "Run with --apply to execute deletes.",
        ],
    }

    # Postgres
    db_url = os.getenv("DATABASE_URL", "").strip()
    if not db_url:
        report.setdefault("warnings", []).append("DATABASE_URL not set; Postgres cleanup skipped.")
    elif psycopg is None:
        report.setdefault("warnings", []).append("psycopg not installed; Postgres cleanup skipped.")
    else:
        conn = psycopg.connect(db_url)
        try:
            user_ids = fetch_user_ids(conn, args.email)
            report["postgres"]["user_ids"] = user_ids
            before = sql_count_snapshot(conn, args.email, user_ids)
            report["postgres"]["matched_rows"] = before
            if not dry_run:
                report["postgres"]["deleted_rows"] = sql_delete(conn, args.email, user_ids)
            else:
                report["postgres"]["deleted_rows"] = {k: 0 for k in before}
        finally:
            conn.close()

    # Redis (Upstash REST)
    rest_url = os.getenv("UPSTASH_REDIS_REST_URL", "").strip()
    rest_token = os.getenv("UPSTASH_REDIS_REST_TOKEN", "").strip()
    if not rest_url or not rest_token:
        report.setdefault("warnings", []).append("UPSTASH_REDIS_REST_URL/TOKEN not set; Redis cleanup skipped.")
    else:
        redis = UpstashRestClient(rest_url, rest_token)
        user_ids = report["postgres"].get("user_ids", [])
        keys = redis_collect_keys(redis, args.email.lower(), user_ids)
        report["redis"]["matched_keys"] = keys
        if not dry_run:
            report["redis"]["deleted_keys"] = redis_delete(redis, keys)

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
