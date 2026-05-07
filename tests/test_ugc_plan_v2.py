import hashlib
import time
import asyncio
from fastapi import Request
from types import SimpleNamespace
from typing import Optional

from app.services.policy_engine import PolicyEngine, run_gate
from app.services.entitlement_service import TierStatus

class DummyQuotaRepo:
    async def get_usage(self, key: str, redis_op: Optional[str] = None) -> int:
        return 0

    async def check_available(self, key: str, max_limit: int, redis_op: Optional[str] = None) -> bool:
        return True

    async def check_and_consume(
        self,
        key: str,
        limit: int,
        ttl: int = 86400,
        idempotency_key: str | None = None,
        redis_op: Optional[str] = None,
    ):
        return True, limit - 1

class DummyRequest:
    def __init__(self):
        self.headers = {}
        self.state = SimpleNamespace(request_id="test")
        self.client = None
        self.app = SimpleNamespace(state=SimpleNamespace())

def norm_text(s: str) -> str:
    return " ".join((s or "").strip().lower().split())

def build_dedup_key(anon_id: str, title: str, desc: str, category: str, lat: float, lon: float) -> str:
    title_n = norm_text(title)
    desc_n = norm_text(desc)
    cat_n = norm_text(category or "")
    lat_q = round(float(lat), 3)
    lon_q = round(float(lon), 3)
    geo_cell = f"{lat_q}:{lon_q}"
    content_hash = hashlib.sha256(f"{title_n}|{desc_n}|{cat_n}".encode("utf-8")).hexdigest()
    day_bucket = time.strftime("%Y%m%d", time.gmtime())
    return hashlib.sha256(f"{anon_id}|{geo_cell}|{content_hash}|{day_bucket}".encode("utf-8")).hexdigest()

def test_dedup_key_stability():
    k1 = build_dedup_key("anon", "Title  A", "Desc\nhere", "Cat", 16.06999, 108.22301)
    k2 = build_dedup_key("anon", " title a ", "Desc here", "cat", 16.07, 108.223)
    assert k1 == k2

def test_run_gate_forces_turnstile():
    request = DummyRequest()
    quota_repo = DummyQuotaRepo()
    engine = PolicyEngine(quota_repo)
    async def run():
        try:
            await run_gate(
                request=request,
                data_turnstile_token=None,
                policy_engine=engine,
                quota_repo=quota_repo,
                anon_id="anon",
                user_id=None,
                tier=TierStatus.PAID,
                entitlement_stale=False,
                area_code="x",
                force_turnstile_required=True,
                disallow_admin_bypass=True,
            )
        except Exception as e:
            assert hasattr(e, "status_code") and e.status_code == 403
            return
        assert False
    asyncio.run(run())
