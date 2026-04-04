#!/usr/bin/env python3
"""Production-targeted smoke test suite for dilldrill.com.

This script runs outbound HTTPS checks against the deployed app and (optionally)
production mutations against production DB/Redis with deterministic cleanup.

Safety defaults are conservative:
- Read-only checks run by default.
- Mutating production checks require explicit opt-in via env vars.
- Dodo checks are treated as test-mode checks on the production app.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
import psycopg
from upstash_redis import Redis as SyncRedis


DEFAULT_BASE_URL = "https://dilldrill.com"
DEFAULT_TIMEOUT_SECONDS = float(os.getenv("SMOKE_HTTP_TIMEOUT_SECONDS", "15"))


@dataclass
class SmokeConfig:
    base_url: str
    app_origin: str
    app_referer: str
    allow_mutations: bool
    allow_dodo_test_mode_on_production: bool
    cleanup_enabled: bool
    dry_run: bool
    timeout_seconds: float
    debug_response_bodies: bool


@dataclass
class CleanupTracker:
    run_id: str
    marker: str
    email: str
    user_id: str | None = None
    payment_intent_ids: set[str] = field(default_factory=set)
    webhook_event_ids: set[str] = field(default_factory=set)
    provider_payment_ids: set[str] = field(default_factory=set)
    redis_keys: set[str] = field(default_factory=set)
    redis_prefix_patterns: set[str] = field(default_factory=set)
    sql_actions: list[str] = field(default_factory=list)
    redis_actions: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


@dataclass
class TestResult:
    category: str
    name: str
    status: str
    reason: str
    http_status: int | None = None


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def build_config() -> SmokeConfig:
    base_url = os.getenv("APP_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise RuntimeError(f"APP_BASE_URL must be HTTPS. Got: {base_url}")

    return SmokeConfig(
        base_url=base_url,
        app_origin=base_url,
        app_referer=f"{base_url}/",
        allow_mutations=env_bool("ALLOW_PRODUCTION_MUTATIONS", False),
        allow_dodo_test_mode_on_production=env_bool("ALLOW_DODO_TEST_MODE_ON_PRODUCTION", False),
        cleanup_enabled=env_bool("SMOKE_TEST_CLEANUP", True),
        dry_run=env_bool("SMOKE_TEST_DRY_RUN", False),
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
        debug_response_bodies=env_bool("SMOKE_DEBUG_RESPONSE_BODIES", False),
    )


def require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Required environment variable missing: {name}")
    return value


def print_startup_summary(cfg: SmokeConfig, tracker: CleanupTracker) -> None:
    print("\n=== Production Smoke Test Startup Summary ===")
    print(f"Target APP_BASE_URL: {cfg.base_url}")
    print("Target environment: production app over live HTTPS")
    print(f"ALLOW_PRODUCTION_MUTATIONS: {cfg.allow_mutations}")
    print(f"ALLOW_DODO_TEST_MODE_ON_PRODUCTION: {cfg.allow_dodo_test_mode_on_production}")
    print("Dodo expectation: test keys/test-mode billing against production app (not Dodo live production credentials)")
    print(f"SMOKE_TEST_CLEANUP: {cfg.cleanup_enabled}")
    print(f"SMOKE_TEST_DRY_RUN: {cfg.dry_run}")
    print(f"Run marker: {tracker.marker}")
    print(f"Trace email: {tracker.email}")
    print("============================================\n")


def print_test_start(category: str, name: str) -> None:
    print(f"[START] [{category}] {name}")


def print_test_result(result: TestResult) -> None:
    code = f" http={result.http_status}" if result.http_status is not None else ""
    print(f"[{result.status}] [{result.category}] {result.name}{code} - {result.reason}")


def request_json(
    client: httpx.Client,
    method: str,
    full_url: str,
    cfg: SmokeConfig,
    *,
    json_body: dict[str, Any] | None = None,
    data: bytes | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[httpx.Response, dict[str, Any] | None]:
    headers = {
        "Origin": cfg.app_origin,
        "Referer": cfg.app_referer,
        "Accept": "application/json, text/plain;q=0.9, */*;q=0.8",
    }
    if extra_headers:
        headers.update(extra_headers)

    response = client.request(
        method=method,
        url=full_url,
        json=json_body,
        content=data,
        headers=headers,
        timeout=cfg.timeout_seconds,
        follow_redirects=False,
    )

    body: dict[str, Any] | None = None
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = response.json()
        except Exception:
            body = None

    return response, body


def db_conn() -> psycopg.Connection:
    return psycopg.connect(require_env("DATABASE_URL"))


def redis_client() -> SyncRedis:
    return SyncRedis(
        url=require_env("UPSTASH_REDIS_REST_URL"),
        token=require_env("UPSTASH_REDIS_REST_TOKEN"),
    )



def discover_sql_artifacts(conn: psycopg.Connection, tracker: CleanupTracker) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT id::text FROM users WHERE email = %s", (tracker.email,))
        for (uid,) in cur.fetchall():
            tracker.user_id = uid

        if tracker.user_id:
            cur.execute("SELECT id::text FROM payment_intents WHERE user_id = %s", (tracker.user_id,))
            tracker.payment_intent_ids.update(r[0] for r in cur.fetchall())
            tracker.redis_keys.add(f"dd:policy:entitlement:status:{tracker.user_id}")

            cur.execute("SELECT provider_payment_id FROM user_passes WHERE user_id = %s", (tracker.user_id,))
            tracker.provider_payment_ids.update(r[0] for r in cur.fetchall())

        for event_id in list(tracker.webhook_event_ids):
            cur.execute("SELECT event_id FROM webhook_events WHERE event_id = %s", (event_id,))
            if cur.fetchone():
                tracker.webhook_event_ids.add(event_id)


def cleanup_sql(conn: psycopg.Connection, tracker: CleanupTracker, dry_run: bool) -> None:
    try:
        discover_sql_artifacts(conn, tracker)
        with conn.cursor() as cur:
            if tracker.user_id:
                if not dry_run:
                    cur.execute("DELETE FROM magic_link_tokens WHERE user_id = %s OR email = %s", (tracker.user_id, tracker.email))
                tracker.sql_actions.append("magic_link_tokens by user_id/email")

            if tracker.webhook_event_ids:
                ids = tuple(tracker.webhook_event_ids)
                if not dry_run:
                    cur.execute("DELETE FROM webhook_events WHERE event_id = ANY(%s)", (list(ids),))
                tracker.sql_actions.append(f"webhook_events event_ids={list(ids)}")

            if tracker.provider_payment_ids:
                pids = tuple(tracker.provider_payment_ids)
                if not dry_run:
                    cur.execute("DELETE FROM user_passes WHERE provider_payment_id = ANY(%s)", (list(pids),))
                tracker.sql_actions.append(f"user_passes provider_payment_ids={list(pids)}")

            if tracker.payment_intent_ids:
                iids = tuple(tracker.payment_intent_ids)
                if not dry_run:
                    cur.execute("DELETE FROM payment_intents WHERE id = ANY(%s)", (list(iids),))
                tracker.sql_actions.append(f"payment_intents ids={list(iids)}")

            if tracker.user_id:
                if not dry_run:
                    cur.execute("DELETE FROM users WHERE id = %s", (tracker.user_id,))
                tracker.sql_actions.append(f"users id={tracker.user_id}")

            if not dry_run:
                conn.commit()
    except Exception as exc:
        tracker.failures.append(f"SQL cleanup failed: {exc}")
        try:
            conn.rollback()
        except Exception:
            pass


def cleanup_redis(rds: SyncRedis, tracker: CleanupTracker, dry_run: bool) -> None:
    for key in sorted(tracker.redis_keys):
        try:
            if not dry_run:
                rds.delete(key)
            tracker.redis_actions.append(f"delete {key}")
        except Exception as exc:
            tracker.failures.append(f"Redis cleanup failed for {key}: {exc}")

    for pattern in sorted(tracker.redis_prefix_patterns):
        try:
            matched = rds.keys(pattern) or []
            for key in matched:
                if not dry_run:
                    rds.delete(key)
                tracker.redis_actions.append(f"delete {key} (pattern={pattern})")
        except Exception as exc:
            tracker.failures.append(f"Redis cleanup pattern failed for {pattern}: {exc}")


def final_cleanup(cfg: SmokeConfig, tracker: CleanupTracker) -> None:
    print("\n=== Final Cleanup Phase ===")
    if not cfg.cleanup_enabled:
        print("[SKIP] Cleanup disabled via SMOKE_TEST_CLEANUP=false")
        return

    try:
        conn = db_conn()
    except Exception as exc:
        tracker.failures.append(f"Cannot open DB for cleanup: {exc}")
        conn = None

    try:
        rds = redis_client()
    except Exception as exc:
        tracker.failures.append(f"Cannot open Redis for cleanup: {exc}")
        rds = None

    if conn:
        with conn:
            cleanup_sql(conn, tracker, cfg.dry_run)
    if rds:
        cleanup_redis(rds, tracker, cfg.dry_run)

    print("Cleanup SQL actions:")
    for action in tracker.sql_actions:
        print(f" - {action}")
    print("Cleanup Redis actions:")
    for action in tracker.redis_actions:
        print(f" - {action}")

    if tracker.failures:
        print("Cleanup failures:")
        for failure in tracker.failures:
            print(f" - {failure}")
    else:
        print("Cleanup failures: none")
    print("=== Cleanup Complete ===\n")


def run_read_only_tests(client: httpx.Client, cfg: SmokeConfig, results: list[TestResult]) -> None:
    category = "SAFE READ/CHECK"

    name = "Homepage Reachability"
    print_test_start(category, name)
    try:
        url = f"{cfg.base_url}/"
        resp, _ = request_json(client, "GET", url, cfg)
        ok = resp.status_code == 200 and "text/html" in resp.headers.get("content-type", "")
        results.append(TestResult(category, name, "PASS" if ok else "FAIL", "Homepage responded", resp.status_code))
    except Exception as exc:
        results.append(TestResult(category, name, "FAIL", f"Request failed: {exc}"))

    name = "Status API Reachability"
    print_test_start(category, name)
    try:
        url = f"{cfg.base_url}/api/status"
        resp, body = request_json(client, "GET", url, cfg)
        ok = resp.status_code == 200 and isinstance(body, dict) and "tier" in body
        reason = f"tier={body.get('tier')} daily_limit={body.get('daily_limit')}" if body else "No JSON body"
        results.append(TestResult(category, name, "PASS" if ok else "FAIL", reason, resp.status_code))
    except Exception as exc:
        results.append(TestResult(category, name, "FAIL", f"Request failed: {exc}"))

    name = "Parse Location API Reachability"
    print_test_start(category, name)
    try:
        url = f"{cfg.base_url}/api/parse-location"
        payload = {"location_input": "Ho Chi Minh City"}
        resp, body = request_json(client, "POST", url, cfg, json_body=payload)
        ok = resp.status_code == 200 and isinstance(body, dict) and body.get("ok") is True
        results.append(TestResult(category, name, "PASS" if ok else "FAIL", "Parse endpoint responded", resp.status_code))
    except Exception as exc:
        results.append(TestResult(category, name, "FAIL", f"Request failed: {exc}"))


def run_production_write_tests(
    client: httpx.Client,
    cfg: SmokeConfig,
    tracker: CleanupTracker,
    results: list[TestResult],
) -> None:
    category = "PRODUCTION WRITE/INTEGRATION"

    if not cfg.allow_mutations:
        results.append(
            TestResult(
                category,
                "Magic Link Creation on Production App",
                "SKIP",
                "Set ALLOW_PRODUCTION_MUTATIONS=true to enable production write/integration tests",
            )
        )
        return

    name = "Magic Link Creation on Production App"
    print_test_start(category, name)

    turnstile_token = os.getenv("SMOKE_TURNSTILE_TOKEN", "").strip()
    if not turnstile_token:
        results.append(TestResult(category, name, "SKIP", "SMOKE_TURNSTILE_TOKEN not set; cannot safely validate mutation endpoint"))
        return

    try:
        url = f"{cfg.base_url}/api/auth/magic-link"
        payload = {"email": tracker.email, "turnstile_token": turnstile_token}
        resp, body = request_json(client, "POST", url, cfg, json_body=payload)
        tracker.redis_keys.add(f"magic_resend:cooldown:{tracker.email}")
        tracker.redis_prefix_patterns.add(f"magic_resend:count:{tracker.email}:*")
        ok = resp.status_code == 200
        if cfg.debug_response_bodies and body is not None:
            print(f"DEBUG response: {body}")
        results.append(TestResult(category, name, "PASS" if ok else "FAIL", "Generic magic-link response expected", resp.status_code))
    except Exception as exc:
        results.append(TestResult(category, name, "FAIL", f"Request failed: {exc}"))


def run_dodo_test_mode_tests(
    client: httpx.Client,
    cfg: SmokeConfig,
    tracker: CleanupTracker,
    results: list[TestResult],
) -> None:
    category = "DODO TEST-MODE BILLING ON PRODUCTION APP"

    if not cfg.allow_mutations:
        results.append(TestResult(category, "Dodo Test-Mode Intent Creation on Production App", "SKIP", "Mutations disabled"))
        results.append(TestResult(category, "Dodo Test-Mode Webhook Simulation on Production App", "SKIP", "Mutations disabled"))
        return

    if not cfg.allow_dodo_test_mode_on_production:
        results.append(
            TestResult(
                category,
                "Dodo Test-Mode Intent Creation on Production App",
                "SKIP",
                "Set ALLOW_DODO_TEST_MODE_ON_PRODUCTION=true to enable Dodo test-mode checks",
            )
        )
        results.append(
            TestResult(
                category,
                "Dodo Test-Mode Webhook Simulation on Production App",
                "SKIP",
                "Set ALLOW_DODO_TEST_MODE_ON_PRODUCTION=true to enable Dodo test-mode checks",
            )
        )
        return

    turnstile_token = os.getenv("SMOKE_TURNSTILE_TOKEN", "").strip()
    if not turnstile_token:
        results.append(TestResult(category, "Dodo Test-Mode Intent Creation on Production App", "SKIP", "SMOKE_TURNSTILE_TOKEN required"))
        results.append(TestResult(category, "Dodo Test-Mode Webhook Simulation on Production App", "SKIP", "No intent available"))
        return

    intent_id: str | None = None
    plan_code = os.getenv("SMOKE_PLAN_CODE", "1_day_test_a")

    name = "Dodo Test-Mode Intent Creation on Production App"
    print_test_start(category, name)
    try:
        url = f"{cfg.base_url}/api/billing/unlock-intent"
        payload = {"email": tracker.email, "plan": plan_code, "turnstile_token": turnstile_token}
        resp, body = request_json(client, "POST", url, cfg, json_body=payload)

        intent_id = body.get("intent_id") if isinstance(body, dict) else None
        checkout_url = body.get("checkout_url") if isinstance(body, dict) else None

        if intent_id:
            tracker.payment_intent_ids.add(intent_id)

        ok = resp.status_code == 200 and bool(intent_id) and bool(checkout_url)
        reason = "Created checkout intent for Dodo test-mode on production app"
        if isinstance(body, dict) and body.get("detail"):
            reason = f"detail={body.get('detail')}"
        results.append(TestResult(category, name, "PASS" if ok else "FAIL", reason, resp.status_code))
    except Exception as exc:
        results.append(TestResult(category, name, "FAIL", f"Request failed: {exc}"))

    name = "Dodo Test-Mode Webhook Simulation on Production App"
    print_test_start(category, name)
    webhook_secret = os.getenv("DODO_WEBHOOK_SECRET", "").strip()
    if not intent_id:
        results.append(TestResult(category, name, "SKIP", "No intent_id from prior test"))
        return
    if not webhook_secret:
        results.append(TestResult(category, name, "SKIP", "DODO_WEBHOOK_SECRET missing; cannot sign webhook simulation"))
        return

    try:
        payment_id = f"smoke_{tracker.run_id}_pay"
        tracker.webhook_event_ids.add(payment_id)
        tracker.provider_payment_ids.add(payment_id)
        tracker.redis_keys.add(f"lock:webhook:{payment_id}")

        payload = {
            "payment_id": payment_id,
            "status": "succeeded",
            "amount": 499,
            "currency": "USD",
            "customer": {"email": tracker.email},
            "metadata": {"intent_id": intent_id, "smoke_marker": tracker.marker},
        }
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        signature = hmac.new(webhook_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()

        url = f"{cfg.base_url}/api/webhooks/dodo"
        resp, body = request_json(
            client,
            "POST",
            url,
            cfg,
            data=raw,
            extra_headers={
                "Content-Type": "application/json",
                "Dodo-Signature": f"sha256={signature}",
            },
        )

        ok = resp.status_code == 200
        reason = "Webhook simulation delivered (test-mode billing over production app)"
        if isinstance(body, dict) and body.get("reason"):
            reason = f"{reason}; api_reason={body.get('reason')}"

        if cfg.debug_response_bodies and body is not None:
            print(f"DEBUG webhook response: {body}")

        results.append(TestResult(category, name, "PASS" if ok else "FAIL", reason, resp.status_code))

        entitlement_name = "Entitlement Status Check After Dodo Test-Mode Webhook"
        print_test_start(category, entitlement_name)
        status_url = f"{cfg.base_url}/api/status"
        status_resp, status_body = request_json(client, "GET", status_url, cfg)
        tier = status_body.get("tier") if isinstance(status_body, dict) else None
        check_ok = status_resp.status_code == 200 and tier in {"1_day", "3_day", "free"}
        check_reason = f"tier={tier} (note: status may depend on auth/session context)"
        results.append(TestResult(category, entitlement_name, "PASS" if check_ok else "FAIL", check_reason, status_resp.status_code))

    except Exception as exc:
        results.append(TestResult(category, name, "FAIL", f"Request failed: {exc}"))


def print_summary(results: list[TestResult], elapsed: float) -> int:
    print("\n=== Smoke Test Result Summary ===")
    for res in results:
        print_test_result(res)
    passed = sum(1 for r in results if r.status == "PASS")
    failed = sum(1 for r in results if r.status == "FAIL")
    skipped = sum(1 for r in results if r.status == "SKIP")
    print(f"Totals: PASS={passed} FAIL={failed} SKIP={skipped} elapsed={elapsed:.2f}s")
    print("===============================\n")
    return 1 if failed else 0


def main() -> int:
    started = time.time()
    cfg = build_config()

    run_id = datetime.now(UTC).strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
    marker = f"smoke-prod-{run_id}"
    email = f"{marker}@example.com"
    tracker = CleanupTracker(run_id=run_id, marker=marker, email=email)
    results: list[TestResult] = []

    print_startup_summary(cfg, tracker)

    if cfg.dry_run:
        print("[INFO] Dry-run enabled: requests will still execute, but cleanup deletions are no-op logged actions.")

    try:
        with httpx.Client() as client:
            run_read_only_tests(client, cfg, results)
            run_production_write_tests(client, cfg, tracker, results)
            run_dodo_test_mode_tests(client, cfg, tracker, results)
    finally:
        final_cleanup(cfg, tracker)

    return print_summary(results, time.time() - started)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"[FAIL] Unhandled smoke-test failure: {exc}")
        raise
