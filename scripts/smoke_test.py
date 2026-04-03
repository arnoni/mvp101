#!/usr/bin/env python3
"""End-to-end smoke test for API + Database + Redis entitlement flow."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

import psycopg
from fastapi.testclient import TestClient

from app.core.config import settings


@dataclass
class SmokeContext:
    email: str
    user_id: str
    free_daily_limit: int
    intent_id: str | None = None
    provider_intent_id: str | None = None


def pass_fail(step: str, ok: bool, detail: str = "") -> bool:
    status = "PASS" if ok else "FAIL"
    suffix = f" - {detail}" if detail else ""
    print(f"[{status}] {step}{suffix}")
    return ok


def db_conn() -> psycopg.Connection:
    if not settings.DATABASE_URL:
        raise RuntimeError("DATABASE_URL is required for smoke test")
    return psycopg.connect(settings.DATABASE_URL)


def patch_billing_for_local_smoke() -> None:
    import app.api.billing as billing_api

    async def _fake_verify_turnstile(token: str, anon_id: str | None = None, client_ip: str | None = None) -> bool:
        return True

    async def _fake_checkout(**kwargs):
        intent_id = kwargs["intent_id"]
        return f"https://checkout.test.local/{intent_id}", f"dodo_checkout_{intent_id}"

    billing_api.verify_turnstile = _fake_verify_turnstile
    billing_api._create_dodo_checkout = _fake_checkout


def create_magic_session(client: TestClient, user_id: str) -> None:
    redis = client.app.state.redis
    if redis is None:
        raise RuntimeError("Redis is required for smoke test")

    raw_token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(raw_token.encode()).hexdigest()
    payload = json.dumps({"user_id": user_id})

    import anyio

    anyio.run(redis.set, f"magic:{token_hash}", payload, 1800)

    resp = client.get(f"/api/auth/magic?token={raw_token}", follow_redirects=False)
    if resp.status_code not in (302, 303):
        raise RuntimeError(f"Magic link consumption failed: {resp.status_code} {resp.text}")


def fetch_status(client: TestClient) -> dict:
    resp = client.get("/api/status")
    assert resp.status_code == 200, f"Unexpected /api/status: {resp.status_code} {resp.text}"
    return resp.json()


def main() -> int:
    patch_billing_for_local_smoke()

    from app.main import app

    all_ok = True
    with TestClient(app) as client:
        if not settings.DATABASE_URL:
            print("[FAIL] DATABASE_URL is missing")
            return 1
        if client.app.state.redis is None:
            print("[FAIL] Redis client is unavailable (check ENABLE_REDIS and Upstash env vars)")
            return 1

        email = f"smoke-{uuid.uuid4().hex[:12]}@example.com"
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO users (email, ab_cohort)
                    VALUES (%s, CASE WHEN random() < 0.5 THEN 'A' ELSE 'B' END)
                    RETURNING id
                    """,
                    (email,),
                )
                user_id = str(cur.fetchone()[0])
                conn.commit()

        create_magic_session(client, user_id)

        ctx = SmokeContext(email=email, user_id=user_id, free_daily_limit=0)

        # Test 1
        status1 = fetch_status(client)
        test1_ok = (
            status1.get("tier") == "free"
            and status1.get("daily_limit") in (3, 4)
            and not bool(status1.get("active_plan_code"))
        )
        all_ok &= pass_fail(
            "Test 1: Free User Creation & A/B Cohort Assignment",
            test1_ok,
            f"tier={status1.get('tier')} daily_limit={status1.get('daily_limit')} active_plan_code={status1.get('active_plan_code')}",
        )
        ctx.free_daily_limit = int(status1.get("daily_limit", 0))

        # Test 2
        checkout_resp = client.post(
            "/api/billing/unlock-intent",
            json={"email": ctx.email, "plan": "1_day_test_a", "turnstile_token": "smoke"},
            headers={"origin": "http://localhost:8000"},
        )
        body2 = checkout_resp.json()
        provider_id = body2.get("intent_id")
        test2_api_ok = checkout_resp.status_code == 200 and bool(body2.get("checkout_url")) and bool(provider_id)

        db_row_ok = False
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT user_id::text, amount_cents, plan_code, provider_intent_id
                    FROM payment_intents
                    WHERE id = %s
                    """,
                    (provider_id,),
                )
                row = cur.fetchone()
                if row:
                    db_row_ok = row[0] == ctx.user_id and int(row[1]) == 499 and row[2] == "1_day_test_a"
                    ctx.provider_intent_id = row[3]
            conn.commit()
        ctx.intent_id = provider_id

        all_ok &= pass_fail(
            "Test 2: Pre-Checkout (Payment Intent Creation)",
            test2_api_ok and db_row_ok,
            f"status={checkout_resp.status_code} intent_id={ctx.intent_id} provider_intent_id={ctx.provider_intent_id}",
        )

        # Test 3
        payment_id = f"pay_{uuid.uuid4().hex}"
        webhook_payload = {
            "payment_id": payment_id,
            "status": "succeeded",
            "amount": 499,
            "currency": "USD",
            "customer": {"email": ctx.email},
            "metadata": {"intent_id": ctx.intent_id},
        }
        raw = json.dumps(webhook_payload, separators=(",", ":")).encode()
        secret = settings.DODO_WEBHOOK_SECRET or ""
        signature = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()

        webhook_resp = client.post(
            "/api/webhooks/dodo",
            content=raw,
            headers={"content-type": "application/json", "Dodo-Signature": f"sha256={signature}"},
        )
        pass_inserted = False
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT expires_at
                    FROM user_passes
                    WHERE user_id = %s
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (ctx.user_id,),
                )
                row = cur.fetchone()
                if row:
                    pass_inserted = row[0] > datetime.now(UTC)
            conn.commit()

        all_ok &= pass_fail(
            "Test 3: Webhook Fulfillment (Simulating Dodo)",
            webhook_resp.status_code == 200 and pass_inserted,
            f"status={webhook_resp.status_code} body={webhook_resp.text}",
        )

        # Test 4
        status2 = fetch_status(client)
        test4_ok = status2.get("tier") == "1_day" and status2.get("daily_limit") == 10
        all_ok &= pass_fail(
            "Test 4: Post-Purchase Authorization Upgrade",
            test4_ok,
            f"tier={status2.get('tier')} daily_limit={status2.get('daily_limit')}",
        )

        # Test 5
        with db_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE user_passes
                    SET expires_at = NOW() - INTERVAL '1 minute'
                    WHERE user_id = %s
                    """,
                    (ctx.user_id,),
                )
            conn.commit()

        status3 = fetch_status(client)
        test5_ok = status3.get("tier") == "free" and status3.get("daily_limit") == ctx.free_daily_limit
        all_ok &= pass_fail(
            "Test 5: Time-Bomb Cache Validation (Edge Case)",
            test5_ok,
            f"tier={status3.get('tier')} daily_limit={status3.get('daily_limit')} expected={ctx.free_daily_limit}",
        )

    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
